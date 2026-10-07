"""Lossless, resumable SQLite -> PostgreSQL shadow import.

This is a migration tool, not a runtime switch. All tables, source rowids,
binary values and original times are retained. A compact, transaction-bound
dirty-row journal covers INSERT/UPDATE/DELETE on every source table, including
tables intentionally absent from the dashboard outbox. A final writer barrier
and complete verification are required before a separate runtime cutover.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import struct
import time
from contextlib import closing
from collections import deque
from pathlib import Path

INTERNAL_PREFIX = '_cliperx_migration_'
IDENTITY_COLUMN = '_cliperx_source_rowid'
CONTROL_TABLE = '_cliperx_migration_state'
OWNER_TABLE = '_cliperx_migration_owner'
RANGE_TABLE = '_cliperx_migration_ranges'
MIN_FREE_BYTES = 5 * 1024**3
PRESENT_COLUMN = INTERNAL_PREFIX + 'present'
INDEX_SUPPORT_SQL = """
CREATE OR REPLACE FUNCTION _cliperx_migration_json(value text) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
BEGIN
  RETURN value::jsonb;
EXCEPTION WHEN invalid_text_representation OR untranslatable_character OR numeric_value_out_of_range THEN
  RETURN NULL;
END;
$$;
"""


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def source_connection(path, *, writable=False):
    connection = sqlite3.connect(Path(path).resolve().as_uri() +
                                 ('?mode=rw' if writable else '?mode=ro'),
                                 uri=True, timeout=3)
    connection.execute('PRAGMA cache_size=-2048')
    connection.execute('PRAGMA busy_timeout=3000')
    if not writable:
        connection.execute('PRAGMA query_only=ON')
    return connection


def sqlite_busy(exc):
    """Retry only SQLite BUSY, never LOCKED, schema or integrity failures."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, 'sqlite_errorcode', None)
    if code is not None:
        return code & 0xff == sqlite3.SQLITE_BUSY
    # Older Python SQLite bindings do not expose an error code.
    return str(exc) in ('database is locked', 'database is busy')


def retry_sqlite_busy(operation, *, attempts=4):
    for attempt in range(attempts):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not sqlite_busy(exc) or attempt + 1 == attempts:
                raise
            time.sleep(.1 * 2**attempt)


def inspect_source(path):
    path = Path(path).resolve()
    stat = path.stat()
    with closing(source_connection(path)) as connection:
        tables = []
        for name, sql in connection.execute("""SELECT name,sql FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"""):
            if name.startswith(INTERNAL_PREFIX):
                continue
            if not sql or 'WITHOUT ROWID' in sql.upper() or 'VIRTUAL TABLE' in sql.upper():
                raise ValueError('unsupported-source-table:' + name)
            columns = [dict(zip(('cid', 'name', 'type', 'notnull', 'default', 'pk'), row))
                       for row in connection.execute('PRAGMA table_info(' + quote(name) + ')')]
            if any(c['name'].startswith(INTERNAL_PREFIX) or
                   c['name'] == IDENTITY_COLUMN for c in columns):
                raise ValueError('reserved-column-name:' + name)
            for c in columns:
                if c['type'].upper() not in ('TEXT', 'INTEGER', 'REAL', 'BLOB'):
                    raise ValueError('unsupported-column-type:' + name)
            indexes = [row[0] for row in connection.execute("""SELECT sql FROM sqlite_master
                WHERE type='index' AND tbl_name=? AND sql IS NOT NULL ORDER BY name""", (name,))]
            tables.append({'name': name, 'sql': sql, 'columns': columns, 'indexes': indexes})
        schema_hash = hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest()
        size = connection.execute('PRAGMA page_count').fetchone()[0]
        free = connection.execute('PRAGMA freelist_count').fetchone()[0]
        page_size = connection.execute('PRAGMA page_size').fetchone()[0]
    return {'path': str(path), 'device': stat.st_dev, 'inode': stat.st_ino,
            'schemaHash': schema_hash, 'bytes': stat.st_size,
            'occupiedBytes': (size - free) * page_size, 'freePages': free,
            'tables': tables}


def _source_id(source):
    return hashlib.sha256(json.dumps({k: source[k] for k in
        ('path', 'device', 'inode', 'schemaHash')}, sort_keys=True).encode()).hexdigest()


def typed_row_bytes(row):
    """Unambiguous ordered encoding, including exact binary64 floats/types."""
    result = bytearray()
    for value in row:
        if value is None:
            tag, data = b'n', b''
        elif isinstance(value, bytes):
            tag, data = b'b', value
        elif isinstance(value, str):
            tag, data = b's', value.encode('utf-8')
        elif isinstance(value, int):
            tag, data = b'i', str(value).encode('ascii')
        elif isinstance(value, float):
            tag, data = b'f', struct.pack('!d', value)
        else:
            raise ValueError('unsupported-source-value-type')
        result.extend(tag + struct.pack('!Q', len(data)) + data)
    return bytes(result)


def pg_row(table, row):
    values = list(row)
    # SQLite historically stores either TEXT or compressed BLOB in this
    # column. The tag preserves both types, even an empty string/empty blob.
    if table['name'] == 'dashboard_projection':
        offset = 1 + next(i for i, c in enumerate(table['columns']) if c['name'] == 'body')
        value = values[offset]
        values[offset] = b's' + value.encode() if isinstance(value, str) else b'b' + value
    return tuple(values)


def sqlite_row(table, row):
    values = list(row)
    if table['name'] == 'dashboard_projection':
        offset = 1 + next(i for i, c in enumerate(table['columns']) if c['name'] == 'body')
        value = bytes(values[offset])
        if value[:1] == b's':
            values[offset] = value[1:].decode()
        elif value[:1] == b'b':
            values[offset] = value[1:]
        else:
            raise ValueError('invalid-binary-type-tag')
    return tuple(values)


def target_ddl(table):
    """AST-based schema conversion; never replace inside SQL string literals."""
    from sqlglot import exp, parse_one
    tree = parse_one(table['sql'], read='sqlite')
    if not isinstance(tree, exp.Create) or tree.args.get('kind') != 'TABLE':
        raise ValueError('unsupported-schema-statement')
    for data_type in tree.find_all(exp.DataType):
        if data_type.this == exp.DataType.Type.INT:
            data_type.set('this', exp.DataType.Type.BIGINT)
        elif data_type.this in (exp.DataType.Type.FLOAT, exp.DataType.Type.DOUBLE):
            data_type.set('this', exp.DataType.Type.DOUBLE)
    for column in tree.find_all(exp.ColumnDef):
        if table['name'] == 'dashboard_projection' and column.name == 'body':
            column.set('kind', exp.DataType.build('BYTEA', dialect='postgres'))
    # Replay may contain a child delete and a parent update in the same
    # transaction. Validate references at commit, without removing integrity.
    for reference in tree.find_all(exp.Reference):
        actions = [option for option in (reference.args.get('options') or [])
                   if option not in ('DEFERRABLE', 'NOT DEFERRABLE',
                                     'INITIALLY DEFERRED', 'INITIALLY IMMEDIATE')]
        reference.set('options', [*actions, 'DEFERRABLE', 'INITIALLY DEFERRED'])
    # SQLite's parser decorates composite PK columns with implicit NULLS
    # FIRST ordering. PostgreSQL permits that on indexes, not PK constraints.
    for key in tree.find_all(exp.PrimaryKey):
        key.set('expressions', [item.this if isinstance(item, exp.Ordered) else item
                                for item in key.expressions])
    for identifier in tree.find_all(exp.Identifier):
        identifier.set('quoted', True)
    # A separate identity retains tie order and enables exact deletion replay.
    hidden = parse_one(f'CREATE TABLE t ({IDENTITY_COLUMN} BIGINT GENERATED BY DEFAULT AS IDENTITY UNIQUE)',
                       read='postgres').this.expressions[0]
    tree.this.append('expressions', hidden)
    return tree.sql(dialect='postgres')


def target_index_ddl(statement):
    from sqlglot import exp, parse_one
    tree = parse_one(statement, read='sqlite')
    for function in list(tree.find_all(exp.Anonymous)):
        if function.name.lower() == 'json_valid':
            decoded = exp.Anonymous(this='_cliperx_migration_json', expressions=[function.expressions[0].copy()])
            function.replace(exp.Not(this=exp.Is(this=decoded, expression=exp.Null())))
    for function in list(tree.find_all(exp.JSONExtract)):
        decoded = exp.Anonymous(this='_cliperx_migration_json', expressions=[function.this.copy()])
        elements = function.expression.expressions
        if any(not isinstance(part, (exp.JSONPathRoot, exp.JSONPathKey)) for part in elements):
            raise ValueError('unsupported-index-json-path')
        keys = [exp.Literal.string(part.this) for part in elements if isinstance(part, exp.JSONPathKey)]
        name = 'jsonb_extract_path' if tree.this.name == 'live_acc_time' else 'jsonb_extract_path_text'
        function.replace(exp.Anonymous(this=name, expressions=[decoded, *keys]))
    for glob in list(tree.find_all(exp.Glob)):
        patterns = {'*[^0-9]*': '[^0-9]', '*[^0-9a-f]*': '[^0-9a-f]'}
        if not isinstance(glob.expression, exp.Literal) or glob.expression.this not in patterns:
            raise ValueError('unsupported-index-glob')
        glob.replace(exp.RegexpLike(this=glob.this.copy(), expression=exp.Literal.string(patterns[glob.expression.this])))
    for ordered in tree.this.args['params'].args['columns']:
        if isinstance(ordered, exp.Ordered) and not isinstance(ordered.this, (exp.Column, exp.Paren)):
            ordered.set('this', exp.Paren(this=ordered.this))
    for identifier in tree.find_all(exp.Identifier):
        identifier.set('quoted', True)
    return tree.sql(dialect='postgres')


def install_journal(source):
    """All mutations and their journal entries commit in the same SQLite TX."""
    prefix = INTERNAL_PREFIX
    connection = source_connection(source['path'], writable=True)
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(f'CREATE TABLE IF NOT EXISTS {prefix}counter '
                           '(id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL)')
        connection.execute(f'INSERT OR IGNORE INTO {prefix}counter VALUES (1,0)')
        connection.execute(f'CREATE TABLE IF NOT EXISTS {prefix}changes '
                           '(table_name TEXT NOT NULL,row_id INTEGER NOT NULL,seq INTEGER NOT NULL,'
                           'PRIMARY KEY(table_name,row_id))')
        connection.execute(f'CREATE INDEX IF NOT EXISTS {prefix}changes_seq ON {prefix}changes(seq)')
        for table in source['tables']:
            name = table['name']
            literal = "'" + name.replace("'", "''") + "'"
            for operation in ('INSERT', 'UPDATE', 'DELETE'):
                ref = 'OLD' if operation == 'DELETE' else 'NEW'
                trigger = quote(prefix + name + '_' + operation.lower())
                statements = [f'UPDATE {prefix}counter SET value=value+1 WHERE id=1;']
                refs = ('OLD', 'NEW') if operation == 'UPDATE' else (ref,)
                for r in refs:
                    statements.append(f'INSERT INTO {prefix}changes VALUES '
                        f'({literal},{r}.rowid,(SELECT value FROM {prefix}counter WHERE id=1)) '
                        'ON CONFLICT(table_name,row_id) DO UPDATE SET seq=excluded.seq;')
                connection.execute(f'CREATE TRIGGER IF NOT EXISTS {trigger} AFTER {operation} '
                                   f'ON {quote(name)} BEGIN ' + ' '.join(statements) + ' END')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


class ShadowMigration:
    def __init__(self, config, source_path, schema, *, batch_rows=2000,
                 reserve_bytes=MIN_FREE_BYTES):
        if not re.fullmatch(r'cliperx_storage_[a-z0-9_]{1,40}', schema):
            raise ValueError('invalid-owned-target-schema')
        import psycopg
        self.source = inspect_source(source_path)
        self.schema = schema
        self.source_id = _source_id(self.source)
        self.reserve_bytes = max(MIN_FREE_BYTES, reserve_bytes)
        self.batch_rows = max(1, min(batch_rows, 5000))
        self.pg = psycopg.connect(config['dsn'], autocommit=True, connect_timeout=3,
            options='-c statement_timeout=30000 -c lock_timeout=3000')
        self.lock = int.from_bytes(hashlib.sha256(schema.encode()).digest()[:8], 'big', signed=True)
        if not self.pg.execute('SELECT pg_try_advisory_lock(%s)', (self.lock,)).fetchone()[0]:
            self.pg.close()
            raise RuntimeError('migration-already-running')
        self.tables = {t['name']: t for t in self.source['tables']}
        with closing(source_connection(self.source['path'])) as connection:
            self._referenced_tables = {reference[2].casefold()
                for name in self.tables
                for reference in connection.execute('PRAGMA foreign_key_list(' + quote(name) + ')')}

    def close(self):
        if self.pg.closed:
            return
        try:
            # Socket close alone releases the session lock asynchronously on
            # the server. Confirm release before an immediate resume opens a
            # new connection for this same schema.
            self.pg.rollback()
            released = self.pg.execute('SELECT pg_advisory_unlock(%s)', (self.lock,)).fetchone()[0]
            if not released:
                raise RuntimeError('migration-advisory-lock-not-owned')
        finally:
            self.pg.close()

    def check_capacity(self):
        if shutil.disk_usage(self.source['path']).free < self.reserve_bytes:
            raise RuntimeError('migration-disk-reserve-reached')

    def prepare(self):
        self.check_capacity()
        schema = quote(self.schema)
        exists = self.pg.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (self.schema,)).fetchone()
        with self.pg.transaction():
            if not exists:
                self.pg.execute('CREATE SCHEMA ' + schema)
                self.pg.execute('SET search_path TO ' + schema)
                self.pg.execute(f'CREATE TABLE {OWNER_TABLE}(source_id text PRIMARY KEY,source_schema_hash text NOT NULL)')
                self.pg.execute(f'INSERT INTO {OWNER_TABLE} VALUES (%s,%s)',
                                (self.source_id, self.source['schemaHash']))
            else:
                self.pg.execute('SET search_path TO ' + schema)
                owner = self.pg.execute('SELECT to_regclass(%s)', (OWNER_TABLE,)).fetchone()[0]
                if owner is None:
                    raise ValueError('refusing-unowned-target-schema')
                row = self.pg.execute(f'SELECT source_id,source_schema_hash FROM {OWNER_TABLE}').fetchall()
                if row != [(self.source_id, self.source['schemaHash'])]:
                    raise ValueError('migration-source-or-schema-changed')
            self.pg.execute(f'CREATE TABLE IF NOT EXISTS {CONTROL_TABLE} '
                '(table_name text PRIMARY KEY,high_rowid bigint NOT NULL,last_rowid bigint NOT NULL DEFAULT 0,'
                'copied_rows bigint NOT NULL DEFAULT 0,backfill_done boolean NOT NULL DEFAULT false,'
                'indexes_done boolean NOT NULL DEFAULT false,verified_at_ms bigint)')
            self.pg.execute(f'CREATE TABLE IF NOT EXISTS {RANGE_TABLE} '
                '(table_name text NOT NULL,low_rowid bigint NOT NULL,high_rowid bigint NOT NULL,'
                'row_count bigint NOT NULL,content_sha256 text NOT NULL,valid boolean NOT NULL,'
                'verified_at_ms bigint NOT NULL,PRIMARY KEY(table_name,low_rowid),'
                'CHECK(low_rowid<=high_rowid))')
            self.pg.execute(INDEX_SUPPORT_SQL)
        # Install CDC before defining any backfill upper bound. Each batch
        # uses a short source snapshot, avoiding a multi-hour growing WAL.
        install_journal(self.source)
        with closing(source_connection(self.source['path'])) as connection:
            for table in self.source['tables']:
                pk = [c['name'] for c in table['columns'] if c['pk']]
                if not pk:
                    raise ValueError('source-table-needs-primary-key:' + table['name'])
                predicate = ' OR '.join(quote(c) + ' IS NULL' for c in pk)
                if connection.execute(f'SELECT 1 FROM {quote(table["name"])} WHERE {predicate} LIMIT 1').fetchone():
                    raise ValueError('null-source-primary-key:' + table['name'])
                existing = self.pg.execute(f'SELECT 1 FROM {CONTROL_TABLE} WHERE table_name=%s',
                                           (table['name'],)).fetchone()
                if existing:
                    continue
                high = connection.execute('SELECT COALESCE(MAX(rowid),0) FROM ' + quote(table['name'])).fetchone()[0]
                with self.pg.transaction():
                    self.pg.execute(target_ddl(table))
                    # SQLite ordinary rowids can be negative or zero too.
                    low = connection.execute('SELECT COALESCE(MIN(rowid),1) FROM ' + quote(table['name'])).fetchone()[0] - 1
                    if low < -(2**63):
                        raise ValueError('unsupported-minimum-rowid')
                    self.pg.execute(f'INSERT INTO {CONTROL_TABLE}(table_name,high_rowid,last_rowid) VALUES (%s,%s,%s)',
                                    (table['name'], high, low))

    def _stage_rows(self, table, rows, deleted_rowids=()):
        """One COPY per table/batch, including tombstones, reused for all SQL."""
        columns = [IDENTITY_COLUMN] + [c['name'] for c in table['columns']]
        names = ','.join(map(quote, columns))
        stage = '_cliperx_copy_v2_' + hashlib.sha256(table['name'].encode()).hexdigest()[:16]
        self.pg.execute(f'CREATE TEMP TABLE IF NOT EXISTS {quote(stage)} ON COMMIT DELETE ROWS '
                        f'AS SELECT {names},true AS {quote(PRESENT_COLUMN)} '
                        f'FROM {quote(table["name"])} WITH NO DATA')
        self.pg.execute(f'TRUNCATE pg_temp.{quote(stage)}')
        pk = [c['name'] for c in table['columns'] if c['pk']]
        for suffix, keys in (('rowid', [IDENTITY_COLUMN]), ('pk', pk)):
            self.pg.execute(f'CREATE INDEX IF NOT EXISTS {quote(stage+"_"+suffix)} '
                            f'ON pg_temp.{quote(stage)} ({",".join(map(quote, keys))})')
        with self.pg.cursor() as cursor:
            with cursor.copy(f'COPY pg_temp.{quote(stage)} ({names},{quote(PRESENT_COLUMN)}) FROM STDIN') as copier:
                for row in rows:
                    copier.write_row((*pg_row(table, row), True))
                for rowid in deleted_rowids:
                    copier.write_row((rowid, *(None for _ in table['columns']), False))
        # Small staging statistics prevent a scan of a multi-million-row
        # destination when a few thousand indexed identities have changed.
        self.pg.execute(f'ANALYZE pg_temp.{quote(stage)}')
        return stage

    def _upsert(self, table, rows):
        if not rows:
            return
        columns = [IDENTITY_COLUMN] + [c['name'] for c in table['columns']]
        names = ','.join(map(quote, columns))
        pk = [c['name'] for c in sorted(table['columns'], key=lambda c: c['pk']) if c['pk']]
        if not pk:
            raise ValueError('source-table-needs-primary-key:' + table['name'])
        # Deduplicate by actual source PK, not by a hidden rowid. REPLACE in
        # SQLite can change rowid without changing the business identity.
        conflict = ','.join(map(quote, pk))
        updates = ','.join(f'{quote(c)}=excluded.{quote(c)}' for c in columns if c not in pk)
        stage = getattr(self, '_replay_stages', {}).get(table['name'])
        if stage is None:
            stage = self._stage_rows(table, rows)
        self.pg.execute(f'INSERT INTO {quote(table["name"])} ({names}) '
                        f'SELECT {names} FROM pg_temp.{quote(stage)} WHERE {quote(PRESENT_COLUMN)} '
                        f'ON CONFLICT ({conflict}) DO UPDATE SET {updates}')

    def _reconcile_range(self, table, low, high, source_rows=None):
        """Verify the stored destination bytes for a short source rowid interval.

        The importer owns target writes. Source writes remain recorded in its
        transactional journal; replay invalidates every affected certificate.
        No source read snapshot is held during the PostgreSQL write/read.
        """
        columns = ','.join(quote(c['name']) for c in table['columns'])
        if source_rows is None:
            with closing(source_connection(self.source['path'])) as connection:
                source_rows = connection.execute(f'SELECT rowid,{columns} FROM {quote(table["name"])} '
                    'WHERE rowid BETWEEN ? AND ? ORDER BY rowid', (low, high)).fetchall()
        target_rows = self.pg.execute(f'SELECT {quote(IDENTITY_COLUMN)},{columns} '
            f'FROM {quote(table["name"])} WHERE {quote(IDENTITY_COLUMN)} BETWEEN %s AND %s '
            f'ORDER BY {quote(IDENTITY_COLUMN)}', (low, high)).fetchall()
        hashes = [hashlib.sha256(), hashlib.sha256()]
        for row in source_rows:
            hashes[0].update(typed_row_bytes(tuple(row)))
        for row in target_rows:
            hashes[1].update(typed_row_bytes(sqlite_row(table, row)))
        if len(source_rows) != len(target_rows) or hashes[0].digest() != hashes[1].digest():
            raise ValueError('range-content-reconciliation-failed:' + table['name'])
        self.pg.execute(f'INSERT INTO {RANGE_TABLE} VALUES (%s,%s,%s,%s,%s,true,%s) '
            'ON CONFLICT(table_name,low_rowid) DO UPDATE SET high_rowid=excluded.high_rowid,'
            'row_count=excluded.row_count,content_sha256=excluded.content_sha256,valid=true,'
            'verified_at_ms=excluded.verified_at_ms',
            (table['name'], low, high, len(source_rows), hashes[0].hexdigest(), int(time.time()*1000)))
        return {'table': table['name'], 'lowRowid': low, 'highRowid': high,
                'rows': len(source_rows), 'sha256': hashes[0].hexdigest()}

    def backfill(self, table_name):
        table = self.tables[table_name]
        while True:
            self.check_capacity()
            high, last, done = self.pg.execute(f'SELECT high_rowid,last_rowid,backfill_done FROM {CONTROL_TABLE} '
                                             'WHERE table_name=%s', (table_name,)).fetchone()
            if done:
                break
            columns = ','.join(quote(c['name']) for c in table['columns'])
            with closing(source_connection(self.source['path'])) as connection:
                rows = connection.execute(f'SELECT rowid,{columns} FROM {quote(table_name)} '
                    'WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?', (last, high, self.batch_rows)).fetchall()
            with self.pg.transaction():
                self._upsert(table, rows)
                interval_high = rows[-1][0] if rows else high
                if interval_high >= last + 1:
                    self._reconcile_range(table, last + 1, interval_high, rows)
                self.pg.execute(f'UPDATE {CONTROL_TABLE} SET last_rowid=%s,copied_rows=copied_rows+%s,'
                    'backfill_done=%s,verified_at_ms=NULL WHERE table_name=%s',
                    (rows[-1][0] if rows else high, len(rows), not rows, table_name))
            if rows:
                yield {'phase': 'backfill', 'table': table_name, 'lastRowid': rows[-1][0], 'rows': len(rows)}
        indexed = self.pg.execute(f'SELECT indexes_done FROM {CONTROL_TABLE} WHERE table_name=%s',
                                 (table_name,)).fetchone()[0]
        if not indexed:
            self.check_capacity()
            with self.pg.transaction():
                # Large historical indexes can legitimately take minutes.
                # This override is scoped to this shadow-only build TX.
                self.pg.execute("SET LOCAL statement_timeout='30min'")
                for statement in table['indexes']:
                    self.pg.execute(target_index_ddl(statement))
                self.pg.execute(f'UPDATE {CONTROL_TABLE} SET indexes_done=true WHERE table_name=%s', (table_name,))

    def replay(self, limit=None):
        """One source snapshot, PG commit, then conditional source journal ACK.

        The monotonic counter prevents deleting a newer entry even if a row
        is deleted/reinserted while PG writes are in flight. Retry is idempotent.
        Replay starts only after all tables finish backfill, so new versions
        cannot be overwritten by an older chunk read from the source.
        """
        if self.pg.execute(f'SELECT 1 FROM {CONTROL_TABLE} WHERE NOT backfill_done LIMIT 1').fetchone():
            raise ValueError('cdc-replay-before-backfill-complete')
        self.check_capacity()
        limit = max(1, min(limit or self.batch_rows, 5000))

        def read_batch():
            with closing(source_connection(self.source['path'])) as connection:
                connection.execute('BEGIN')
                changes = connection.execute(f'SELECT table_name,row_id,seq FROM {INTERNAL_PREFIX}changes '
                    'ORDER BY seq,table_name,row_id LIMIT ?', (limit,)).fetchall()
                if changes:
                    last_seq = changes[-1][2]
                    tail = connection.execute(f'SELECT table_name,row_id FROM {INTERNAL_PREFIX}changes '
                                              'WHERE seq=?', (last_seq,)).fetchall()
                    included = {(name, rowid) for name, rowid, seq in changes if seq == last_seq}
                    if any(tuple(row) not in included for row in tail):
                        # UPDATE rowid records OLD and NEW at one seq. Never
                        # split their parent identity across PG transactions.
                        changes = [change for change in changes if change[2] != last_seq]
                        if not changes:
                            raise ValueError('cdc-batch-too-small-for-atomic-rowid-change')
                ids_by_table = {}
                for name, rowid, _ in changes:
                    ids_by_table.setdefault(name, []).append(rowid)
                observed = {}
                for name, rowids in ids_by_table.items():
                    columns = ','.join(quote(c['name']) for c in self.tables[name]['columns'])
                    rows = []
                    # Remain compatible with SQLite's older 999-parameter cap.
                    for offset in range(0, len(rowids), 900):
                        chunk = rowids[offset:offset+900]
                        rows.extend(connection.execute(f'SELECT rowid,{columns} FROM {quote(name)} '
                            f'WHERE rowid IN ({",".join("?" for _ in chunk)})', chunk).fetchall())
                    found = {row[0] for row in rows}
                    if name.casefold() in self._referenced_tables:
                        pk = [(index+1, column['name']) for index, column in
                              enumerate(self.tables[name]['columns']) if column['pk']]
                        live_keys = {tuple(row[index] for index, _ in pk) for row in rows}
                        previous = self.pg.execute(f'SELECT {",".join(quote(key) for _, key in pk)} '
                            f'FROM {quote(name)} WHERE {quote(IDENTITY_COLUMN)}=ANY(%s::bigint[])',
                            (rowids,)).fetchall()
                        for old_key in previous:
                            if tuple(old_key) in live_keys:
                                continue
                            current = connection.execute(f'SELECT rowid FROM {quote(name)} WHERE ' +
                                ' AND '.join(f'{quote(key)}=?' for _, key in pk) + ' LIMIT 1', old_key).fetchone()
                            if current:
                                # Coalesced multiple rowid moves can put the
                                # same live parent beyond this batch. Refuse
                                # its old tombstone before CASCADE can fire.
                                raise ValueError('cdc-parent-move-crosses-batch-boundary')
                    observed[name] = (rows, [rowid for rowid in rowids if rowid not in found])
                return changes, observed

        changes, observed = retry_sqlite_busy(read_batch)
        if not changes:
            return 0
        try:
            with self.pg.transaction():
                self._replay_stages = {}
                for name, (rows, deleted) in observed.items():
                    self._replay_stages[name] = self._stage_rows(self.tables[name], rows, deleted)
                for name, stage in self._replay_stages.items():
                    keys = [c['name'] for c in self.tables[name]['columns'] if c['pk']]
                    matches = ' AND '.join(f'destination.{quote(key)}=changed.{quote(key)}' for key in keys)
                    live_match = ' AND '.join(f'destination.{quote(key)}=live.{quote(key)}' for key in keys)
                    # A same-PK replacement can move to another rowid. Invalidate
                    # both the new and previous location before updating in place.
                    self.pg.execute(f'UPDATE {RANGE_TABLE} AS r SET valid=false FROM ('
                        f'SELECT {quote(IDENTITY_COLUMN)} AS row_id FROM pg_temp.{quote(stage)} UNION '
                        f'SELECT destination.{quote(IDENTITY_COLUMN)} FROM {quote(name)} AS destination '
                        f'JOIN pg_temp.{quote(stage)} AS changed ON ({matches}) '
                        f'WHERE changed.{quote(PRESENT_COLUMN)}) AS affected '
                        'WHERE r.valid AND r.table_name=%s '
                        'AND affected.row_id BETWEEN r.low_rowid AND r.high_rowid', (name,))
                    # One indexed set deletion, protecting any same-PK live
                    # parent in this snapshot from delete/cascade/recreation.
                    self.pg.execute(f'DELETE FROM {quote(name)} AS destination '
                        f'USING pg_temp.{quote(stage)} AS changed '
                        f'WHERE destination.{quote(IDENTITY_COLUMN)}=changed.{quote(IDENTITY_COLUMN)} '
                        f'AND (NOT changed.{quote(PRESENT_COLUMN)} OR NOT ({matches})) '
                        f'AND NOT EXISTS (SELECT 1 FROM pg_temp.{quote(stage)} AS live '
                        f'WHERE live.{quote(PRESENT_COLUMN)} AND {live_match})')
                for name, (rows, _) in observed.items():
                    self._upsert(self.tables[name], rows)
                    self.pg.execute(f'UPDATE {CONTROL_TABLE} SET verified_at_ms=NULL WHERE table_name=%s', (name,))
        finally:
            self._replay_stages = {}
        # Only retry the ACK after PG committed. Never prepare/import again or
        # advance past an unacknowledged seq; a later replay is idempotent.
        retry_sqlite_busy(lambda: self._ack_changes(changes))
        return len(changes)

    def _ack_changes(self, changes):
        with closing(source_connection(self.source['path'], writable=True)) as connection:
            try:
                connection.execute('BEGIN IMMEDIATE')
                connection.executemany(f'DELETE FROM {INTERNAL_PREFIX}changes '
                    'WHERE table_name=? AND row_id=? AND seq=?', changes)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def has_pending(self):
        def pending():
            with closing(source_connection(self.source['path'])) as connection:
                return bool(connection.execute(f'SELECT 1 FROM {INTERNAL_PREFIX}changes LIMIT 1').fetchone())
        return retry_sqlite_busy(pending)

    def _range_plan(self, table):
        name = table['name']
        bounds = self.pg.execute(f'SELECT min({quote(IDENTITY_COLUMN)}),max({quote(IDENTITY_COLUMN)}) '
                                 f'FROM {quote(name)}').fetchone()
        with closing(source_connection(self.source['path'])) as connection:
            edge_rows = [connection.execute(f'SELECT rowid FROM {quote(name)} ORDER BY rowid {order} LIMIT 1').fetchone()
                         for order in ('ASC', 'DESC')]
            source_bounds = tuple(row[0] if row else None for row in edge_rows)
        intervals = self.pg.execute(f'SELECT low_rowid,high_rowid,valid FROM {RANGE_TABLE} '
                                   'WHERE table_name=%s ORDER BY low_rowid', (name,)).fetchall()
        if any(left[1] >= right[0] for left, right in zip(intervals, intervals[1:])):
            raise ValueError('overlapping-range-certificates:' + name)
        # Include historical certificates after deletes, not just live edges.
        values = [v for v in (*bounds, *source_bounds) if v is not None]
        if intervals:
            values.extend((intervals[0][0], intervals[-1][1]))
        minimum, maximum = (min(values), max(values)) if values else (0, 0)
        jobs, position = [], minimum
        for low, high, valid in intervals:
            if position < low:
                jobs.append((position, low - 1, False))
            if not valid:
                jobs.append((low, high, True))
            position = high + 1
        if position <= maximum:
            jobs.append((position, maximum, False))
        return intervals, jobs

    def range_coverage(self):
        """Cheap structural readiness from identities/metadata; never read bodies.

        Zero dirty rows does not prove coverage. Every source/target edge and
        historical certificate extent must have contiguous certificates.
        This is a readiness hint; verify_barrier remains the final proof.
        """
        uncovered, dirty, ranges = [], 0, 0
        for table in self.source['tables']:
            intervals, jobs = self._range_plan(table)
            missing = sum(not existing for _, _, existing in jobs)
            if missing:
                uncovered.append({'table': table['name'], 'ranges': missing})
            dirty += sum(not valid for _, _, valid in intervals)
            ranges += len(intervals)
        return {'coverageComplete': not uncovered, 'uncoveredTables': len(uncovered),
                'uncoveredRanges': sum(row['ranges'] for row in uncovered),
                'uncoveredByTable': uncovered, 'dirtyRanges': dirty, 'certificateRanges': ranges}

    def _range_chunks(self, table, cursor=None):
        _, jobs = self._range_plan(table)
        if cursor is not None:
            after, before = [], []
            for low, high, existing in jobs:
                if low > cursor:
                    after.append((low, high, existing))
                elif not existing and high > cursor:
                    # A pending chunk of an uncovered gap must not block
                    # its cold suffix forever on subsequent bounded passes.
                    after.append((cursor + 1, high, False))
                    before.append((low, cursor, False))
                else:
                    before.append((low, high, existing))
            jobs = after + before
        name = table['name']
        columns = ','.join(quote(c['name']) for c in table['columns'])
        for low, high, existing in jobs:
            while low <= high:
                self.check_capacity()
                with closing(source_connection(self.source['path'])) as connection:
                    connection.execute('BEGIN')
                    # Find a bounded interval using identities before touching
                    # bodies. A single hot row in a million-row uncovered tail
                    # now blocks its own chunk, not the entire tail.
                    ids = connection.execute(f'SELECT rowid FROM {quote(name)} '
                        'WHERE rowid BETWEEN ? AND ? ORDER BY rowid LIMIT ?',
                        (low, high, self.batch_rows)).fetchall()
                    interval_high = ids[-1][0] if len(ids) == self.batch_rows else high
                    pending = connection.execute(f'SELECT 1 FROM {INTERNAL_PREFIX}changes '
                        'WHERE table_name=? AND row_id BETWEEN ? AND ? LIMIT 1',
                        (name, low, interval_high)).fetchone()
                    rows = [] if pending else connection.execute(f'SELECT rowid,{columns} FROM {quote(name)} '
                        'WHERE rowid BETWEEN ? AND ? ORDER BY rowid', (low, interval_high)).fetchall()
                status = {'phase': 'reconcile-range', 'table': name,
                          'lowRowid': low, 'highRowid': interval_high, 'pending': bool(pending)}
                if not pending or (existing and interval_high < high):
                    with self.pg.transaction():
                        if existing and interval_high < high:
                            # Split only an already-invalid certificate. Both
                            # halves remain invalid until actual content hashes
                            # prove them; no old proof is borrowed for a subset.
                            self.pg.execute(f'UPDATE {RANGE_TABLE} SET high_rowid=%s,row_count=0,'
                                'content_sha256=%s,valid=false WHERE table_name=%s AND low_rowid=%s '
                                'AND high_rowid=%s AND NOT valid',
                                (interval_high, '0'*64, name, low, high))
                            self.pg.execute(f'INSERT INTO {RANGE_TABLE} VALUES(%s,%s,%s,0,%s,false,%s)',
                                (name, interval_high+1, high, '0'*64, int(time.time()*1000)))
                        if not pending:
                            status.update(self._reconcile_range(table, low, interval_high, rows))
                self._range_cursors[name] = interval_high
                yield status
                low = interval_high + 1

    def reconcile_ranges(self, *, max_ranges=None):
        """Round-robin bounded content verification, preserving all proof rules."""
        if self.pg.execute(f'SELECT 1 FROM {CONTROL_TABLE} WHERE NOT backfill_done OR NOT indexes_done LIMIT 1').fetchone():
            raise ValueError('verification-before-import-complete')
        self._range_cursors = getattr(self, '_range_cursors', {})
        tables = list(self.source['tables'])
        names = [table['name'] for table in tables]
        previous = getattr(self, '_range_last_table', None)
        if max_ranges is not None and previous in names:
            start = names.index(previous) + 1
            tables = tables[start:] + tables[:start]
        queue = deque((table['name'], self._range_chunks(table,
            self._range_cursors.get(table['name']) if max_ranges is not None else None)) for table in tables)
        processed = 0
        while queue and (max_ranges is None or processed < max_ranges):
            name, chunks = queue.popleft()
            try:
                progress = next(chunks)
            except StopIteration:
                continue
            queue.append((name, chunks))
            processed += 1
            self._range_last_table = name
            yield progress

    def verify_barrier(self):
        """Finalize prechecked intervals under a brief externally held writer barrier.

        Does not rescan all historical rows. Rejects uncovered ranges, dirty
        intervals, pending CDC, or any source write during this final check.
        Sequence setup is safe only while the target remains shadow-only.
        """
        if _source_id(inspect_source(self.source['path'])) != self.source_id:
            raise ValueError('migration-source-or-schema-changed')
        if self.pg.execute(f'SELECT 1 FROM {CONTROL_TABLE} WHERE NOT backfill_done OR NOT indexes_done LIMIT 1').fetchone():
            raise ValueError('verification-before-import-complete')
        results = []
        with closing(source_connection(self.source['path'])) as connection:
            connection.execute('BEGIN')
            start = connection.execute(f'SELECT value FROM {INTERNAL_PREFIX}counter WHERE id=1').fetchone()[0]
            if connection.execute(f'SELECT 1 FROM {INTERNAL_PREFIX}changes LIMIT 1').fetchone():
                raise ValueError('verification-pending-cdc')
            for table in self.source['tables']:
                name = table['name']
                intervals = self.pg.execute(f'SELECT low_rowid,high_rowid,row_count,content_sha256,valid '
                    f'FROM {RANGE_TABLE} WHERE table_name=%s ORDER BY low_rowid', (name,)).fetchall()
                if not intervals or any(not row[4] for row in intervals):
                    raise ValueError('verification-missing-or-dirty-range:' + name)
                if any(left[1] + 1 != right[0] for left, right in zip(intervals, intervals[1:])):
                    raise ValueError('verification-noncontiguous-range:' + name)
                edge_rows = [connection.execute(f'SELECT rowid FROM {quote(name)} ORDER BY rowid {order} LIMIT 1').fetchone()
                             for order in ('ASC', 'DESC')]
                src_bounds = tuple(row[0] if row else None for row in edge_rows)
                dst_bounds = self.pg.execute(f'SELECT min({quote(IDENTITY_COLUMN)}),max({quote(IDENTITY_COLUMN)}) '
                                            f'FROM {quote(name)}').fetchone()
                values = [v for v in (*src_bounds, *dst_bounds) if v is not None]
                if values and (min(values) < intervals[0][0] or max(values) > intervals[-1][1]):
                    raise ValueError('verification-uncovered-range:' + name)
                count = sum(row[2] for row in intervals)
                manifest = hashlib.sha256(json.dumps([list(row[:4]) for row in intervals],
                    separators=(',', ':')).encode()).hexdigest()
                results.append({'table': name, 'sourceRows': count, 'targetRows': count,
                    'rangeCount': len(intervals), 'rangeManifestSha256': manifest, 'match': True,
                    'proof': 'ordered-typed-content-per-range'})
            sequences = self._initialize_sequences(connection)
        with closing(source_connection(self.source['path'])) as connection:
            final = connection.execute(f'SELECT value FROM {INTERNAL_PREFIX}counter WHERE id=1').fetchone()[0]
            pending = connection.execute(f'SELECT 1 FROM {INTERNAL_PREFIX}changes LIMIT 1').fetchone()
        if start != final or pending:
            raise ValueError('source-writes-during-verification')
        if _source_id(inspect_source(self.source['path'])) != self.source_id:
            raise ValueError('migration-source-or-schema-changed')
        return {'schema': self.schema, 'sourceId': self.source_id, 'verifiedAtMs': int(time.time()*1000),
            'sourceSequence': final, 'tables': results, 'initializedSequences': sequences, 'runtimeCutover': False}

    def _initialize_sequences(self, source_connection):
        """Preserve AUTOINCREMENT frontiers even after highest rows were pruned.

        Target schemas are shadow-only and exclusively owned by this importer.
        A later runtime cutover still needs a final writer barrier. nextval()
        allocation alone is never treated as a committed replay watermark.
        """
        has_sequences = source_connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'").fetchone()
        source_sequences = dict(source_connection.execute('SELECT name,seq FROM sqlite_sequence')) if has_sequences else {}
        initialized = []
        for table in self.source['tables']:
            name = table['name']
            maximum_rowid = source_connection.execute('SELECT max(rowid) FROM ' + quote(name)).fetchone()[0]
            for column in [IDENTITY_COLUMN] + [c['name'] for c in table['columns']]:
                sequence = self.pg.execute('SELECT pg_get_serial_sequence(%s,%s)', (quote(name), column)).fetchone()[0]
                if sequence is None:
                    continue
                if column == IDENTITY_COLUMN:
                    frontier = maximum_rowid
                else:
                    maximum = source_connection.execute('SELECT max(' + quote(column) + ') FROM ' + quote(name)).fetchone()[0]
                    frontier = max(maximum or 0, source_sequences.get(name, 0))
                self.pg.execute('SELECT setval(%s::regclass,%s,%s)',
                                (sequence, max(1, frontier or 0), frontier is not None and frontier >= 1))
                initialized.append({'table': name, 'column': column, 'sourceFrontier': frontier})
        return initialized

    def verify(self):
        """Full, ordered, typed content hash; run under an external writer stop.

        Holds a source read snapshot and a PostgreSQL REPEATABLE READ snapshot.
        Any source writes during verification invalidate the result via CDC.
        This never authorizes a cutover by itself or reports live data complete.
        """
        if _source_id(inspect_source(self.source['path'])) != self.source_id:
            raise ValueError('migration-source-or-schema-changed')
        if self.pg.execute(f'SELECT 1 FROM {CONTROL_TABLE} WHERE NOT backfill_done OR NOT indexes_done LIMIT 1').fetchone():
            raise ValueError('verification-before-import-complete')
        result = []
        sequences = []
        with closing(source_connection(self.source['path'])) as connection, self.pg.transaction():
            connection.execute('BEGIN')
            self.pg.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            start = connection.execute(f'SELECT value FROM {INTERNAL_PREFIX}counter WHERE id=1').fetchone()[0]
            pending = connection.execute(f'SELECT 1 FROM {INTERNAL_PREFIX}changes LIMIT 1').fetchone()
            if pending:
                raise ValueError('verification-pending-cdc')
            for table in self.source['tables']:
                columns = ','.join(quote(c['name']) for c in table['columns'])
                src = connection.execute(f'SELECT rowid,{columns} FROM {quote(table["name"])} ORDER BY rowid')
                with self.pg.cursor(name='verify_' + hashlib.sha256(table['name'].encode()).hexdigest()[:12]) as dst:
                    dst.execute(f'SELECT {quote(IDENTITY_COLUMN)},{columns} FROM {quote(table["name"])} ORDER BY {quote(IDENTITY_COLUMN)}')
                    hashes = [hashlib.sha256(), hashlib.sha256()]
                    counts = [0, 0]
                    for i, iterator in enumerate((src, dst)):
                        for row in iterator:
                            counts[i] += 1
                            normalized = tuple(row) if i == 0 else sqlite_row(table, row)
                            hashes[i].update(typed_row_bytes(normalized))
                    match = counts[0] == counts[1] and hashes[0].digest() == hashes[1].digest()
                    result.append({'table': table['name'], 'sourceRows': counts[0], 'targetRows': counts[1],
                        'sourceSha256': hashes[0].hexdigest(), 'targetSha256': hashes[1].hexdigest(), 'match': match})
                    if not match:
                        raise ValueError('full-content-reconciliation-failed:' + table['name'])
            sequences = self._initialize_sequences(connection)
        with closing(source_connection(self.source['path'])) as connection:
            final = connection.execute(f'SELECT value FROM {INTERNAL_PREFIX}counter WHERE id=1').fetchone()[0]
            if final != start:
                raise ValueError('source-writes-during-verification')
        if _source_id(inspect_source(self.source['path'])) != self.source_id:
            raise ValueError('migration-source-or-schema-changed')
        return {'schema': self.schema, 'sourceId': self.source_id, 'verifiedAtMs': int(time.time()*1000),
                'sourceSequence': final, 'tables': result, 'initializedSequences': sequences,
                'runtimeCutover': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('inventory', 'prepare', 'backfill', 'replay', 'verify',
                                              'reconcile-ranges', 'verify-barrier'))
    parser.add_argument('--source', required=True)
    parser.add_argument('--schema')
    parser.add_argument('--settings', default='data/architecture-runtime.json')
    parser.add_argument('--output')
    parser.add_argument('--batch-rows', type=int, default=2000)
    args = parser.parse_args()
    if args.operation == 'inventory':
        result = inspect_source(args.source)
    else:
        if not args.schema:
            parser.error('--schema is required')
        config = json.loads(Path(args.settings).read_text())
        if config.get('managedBy') != 'cliperx-architecture-v1':
            raise ValueError('unmanaged-postgres-configuration')
        migration = ShadowMigration(config, args.source, args.schema, batch_rows=args.batch_rows)
        try:
            migration.prepare()
            if args.operation == 'backfill':
                for table in migration.tables:
                    for status in migration.backfill(table):
                        print(json.dumps(status), flush=True)
            if args.operation == 'replay':
                result = {'replayedRows': migration.replay(), 'runtimeCutover': False}
            elif args.operation == 'reconcile-ranges':
                processed = 0
                for status in migration.reconcile_ranges():
                    print(json.dumps(status), flush=True)
                    processed += 1
                result = {'checkedRanges': processed, 'runtimeCutover': False}
            elif args.operation == 'verify-barrier':
                result = migration.verify_barrier()
            elif args.operation == 'verify':
                result = migration.verify()
            else:
                result = {'schema': args.schema, 'sourceId': migration.source_id, 'runtimeCutover': False}
        finally:
            migration.close()
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        destination.chmod(0o600)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # PostgreSQL error details may include an entire failed private row.
        # Keep driver tracebacks and source/account bodies out of CLI receipts.
        import sys
        print(json.dumps({'phase': 'failed', 'errorClass': type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
