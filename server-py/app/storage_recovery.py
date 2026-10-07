"""Verified PostgreSQL -> SQLite standby, with a closed SQLite write gate.

The original file is never a fallback merely because it still exists. Every
committed PostgreSQL mutation enters a transactional dirty-row journal. Mirror
transactions commit before conditional journal acknowledgements. Promotion
requires an external PostgreSQL writer barrier, an empty journal, complete
content certificates, a clean foreign-key check and restored original triggers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import closing
from collections import deque
from pathlib import Path

from .storage_migration import (CONTROL_TABLE, IDENTITY_COLUMN, INTERNAL_PREFIX,
    MIN_FREE_BYTES, OWNER_TABLE, RANGE_TABLE, _source_id, inspect_source,
    quote, sqlite_row, typed_row_bytes)
from .storage_runtime import advisory_lock_key


RECOVERY_OWNER = '_cliperx_recovery_owner'
RECOVERY_COUNTER = '_cliperx_recovery_counter'
RECOVERY_CHANGES = '_cliperx_recovery_changes'
RECOVERY_RANGES = '_cliperx_recovery_ranges'
GUARD_PREFIX = '_cliperx_recovery_guard_'
FORMAT = 'cliperx-storage-recovery-v1'
ABORT_FORMAT = 'cliperx-storage-recovery-abort-v1'


def _counter_sql(schema, tables):
    qualified = quote(schema)
    statements = [f'''CREATE OR REPLACE FUNCTION {qualified}._cliperx_recovery_capture()
      RETURNS trigger LANGUAGE plpgsql SET search_path TO {qualified},pg_catalog AS $$
      DECLARE version bigint;
      BEGIN
        IF (SELECT state FROM {RECOVERY_OWNER} WHERE singleton=1) IS DISTINCT FROM 'active' THEN
          RAISE EXCEPTION 'recovery-postgres-writer-gate-closed';
        END IF;
        UPDATE {RECOVERY_COUNTER} SET seq=seq+1 WHERE singleton=1 RETURNING seq INTO version;
        IF TG_OP IN ('DELETE','UPDATE') THEN
          INSERT INTO {RECOVERY_CHANGES}(table_name,row_id,seq)
            VALUES(TG_TABLE_NAME,OLD.{quote(IDENTITY_COLUMN)},version)
            ON CONFLICT(table_name,row_id) DO UPDATE SET seq=excluded.seq;
        END IF;
        IF TG_OP='INSERT' OR (TG_OP='UPDATE' AND NEW.{quote(IDENTITY_COLUMN)} IS DISTINCT FROM OLD.{quote(IDENTITY_COLUMN)}) THEN
          INSERT INTO {RECOVERY_CHANGES}(table_name,row_id,seq)
            VALUES(TG_TABLE_NAME,NEW.{quote(IDENTITY_COLUMN)},version)
            ON CONFLICT(table_name,row_id) DO UPDATE SET seq=excluded.seq;
        END IF;
        RETURN NULL;
      END;
      $$;''']
    for name in tables:
        statements.append(f'''CREATE TRIGGER _cliperx_recovery_capture AFTER INSERT OR UPDATE OR DELETE
          ON {qualified}.{quote(name)} FOR EACH ROW EXECUTE FUNCTION {qualified}._cliperx_recovery_capture();''')
    return '\n'.join(statements)


def _guards(tables):
    result = []
    for name in tables:
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            trigger = GUARD_PREFIX + name + '_' + operation.lower()
            result.append({'name': trigger, 'table': name, 'sql':
                f'''CREATE TRIGGER {quote(trigger)} BEFORE {operation} ON {quote(name)}
                    BEGIN SELECT CASE WHEN cliperx_recovery_authorized() != 1
                    THEN RAISE(ABORT,'standby-write-closed') END; END'''})
    return result


def _digest(rows):
    checksum = hashlib.sha256()
    for row in rows:
        checksum.update(typed_row_bytes(tuple(row)))
    return checksum.hexdigest()


class RecoveryMirror:
    """One owner per domain; business writers never wait on mirror reads.

    PostgreSQL remains authoritative during every mirror batch. The standby
    deliberately permits temporarily incomplete FK relationships between
    batches; its write guards prevent old applications from using that state.
    Foreign keys are checked, never ignored, before the file can be promoted.
    """

    def __init__(self, config, source_path, schema, *, domain=None, batch_rows=1000,
                 batch_bytes=32 * 1024**2, reserve_bytes=MIN_FREE_BYTES):
        if not re.fullmatch(r'cliperx_storage_[a-z0-9_]{1,40}', schema):
            raise ValueError('unowned-recovery-schema')
        import psycopg
        self.source = inspect_source(source_path)
        self.schema = schema
        self.tables = {table['name']: table for table in self.source['tables']}
        self.batch_rows = max(1, min(int(batch_rows), 5000))
        self.batch_bytes = max(1024**2, min(int(batch_bytes), 128 * 1024**2))
        self.reserve_bytes = max(MIN_FREE_BYTES, int(reserve_bytes))
        domains = (config.get('storage') or {}).get('domainSchemas') or {}
        domain = domain or next((name for name, value in domains.items() if value == schema), None)
        dsn = (config.get('storageDsns') or {}).get('market') if domain == 'market' else None
        self.pg = psycopg.connect(dsn or config['dsn'], autocommit=True, connect_timeout=3,
            options=f'-c search_path={schema},pg_catalog -c statement_timeout=10000 '
                    '-c lock_timeout=1000 -c work_mem=2048kB -c application_name=cliperx-recovery')
        lock = int.from_bytes(hashlib.sha256(('cliperx-recovery-owner:' + schema).encode()).digest()[:8],
                              'big', signed=True)
        if not self.pg.execute('SELECT pg_try_advisory_lock(%s)', (lock,)).fetchone()[0]:
            self.pg.close()
            raise RuntimeError('recovery-owner-already-running')
        self.manifest = None

    def close(self):
        self.pg.close()

    def _sqlite(self, *, writable=False):
        mode = 'rw' if writable else 'ro'
        connection = sqlite3.connect(Path(self.source['path']).as_uri() + '?mode=' + mode,
                                     uri=True, timeout=.25)
        connection.execute('PRAGMA cache_size=-2048')
        if writable:
            connection.create_function('cliperx_recovery_authorized', 0, lambda: 1)
            connection.execute('PRAGMA foreign_keys=OFF')
        else:
            connection.execute('PRAGMA query_only=ON')
        return connection

    def _load(self):
        if self.manifest is None:
            row = self.pg.execute(f'SELECT manifest FROM {RECOVERY_OWNER} WHERE singleton=1').fetchone()
            if row is None:
                raise RuntimeError('missing-recovery-manifest')
            self.manifest = row[0]
        return self.manifest

    def _validate_file(self, connection=None, *, active=True):
        manifest = self._load()
        stat = Path(self.source['path']).stat()
        if (stat.st_dev, stat.st_ino) != (manifest['device'], manifest['inode']):
            raise ValueError('standby-file-identity-changed')
        if self.source['schemaHash'] != manifest['schemaHash']:
            raise ValueError('standby-schema-changed')
        if active:
            if self.pg.execute(f'SELECT state FROM {RECOVERY_OWNER} WHERE singleton=1').fetchone()[0] != 'active':
                raise RuntimeError('standby-no-longer-active')
            if connection is None:
                with closing(self._sqlite()) as owned:
                    self._validate_file(owned, active=active)
            elif connection.execute('PRAGMA schema_version').fetchone()[0] != manifest['standbySchemaVersion']:
                raise ValueError('standby-schema-or-write-guards-changed')

    def _check_capacity(self):
        if shutil.disk_usage(self.source['path']).free < self.reserve_bytes:
            raise RuntimeError('recovery-disk-reserve-reached')

    def prepare(self, baseline_receipt, *, allowed_extra_tables=()):
        """Install capture and close the standby gate under the forward barrier.

        baseline_receipt is the real forward verify_barrier receipt. New,
        explicitly authorised tables must already exist, identically shaped
        and empty, in both PostgreSQL and SQLite.
        """
        from psycopg.types.json import Jsonb
        if self.pg.execute('SELECT to_regclass(%s)', (self.schema + '.' + RECOVERY_OWNER,)).fetchone()[0]:
            self._load()
            self._finish_guard_installation()
            self._validate_file()
            self._save_manifest()
            return self.manifest
        self.manifest = None
        self._check_capacity()
        owner = self.pg.execute(f'SELECT source_id,source_schema_hash FROM {OWNER_TABLE}').fetchall()
        if (len(owner) != 1 or baseline_receipt.get('schema') != self.schema
                or baseline_receipt.get('sourceId') != owner[0][0]
                or baseline_receipt.get('runtimeCutover') is not False):
            raise ValueError('unverified-forward-baseline')
        originals = {row[0] for row in self.pg.execute(f'SELECT table_name FROM {CONTROL_TABLE}')}
        receipts = baseline_receipt.get('tables') or []
        if {row['table'] for row in receipts} != originals or any(row.get('match') is not True for row in receipts):
            raise ValueError('incomplete-forward-baseline')
        original_tables = [table for table in self.source['tables'] if table['name'] in originals]
        old_hash = hashlib.sha256(json.dumps(original_tables, sort_keys=True).encode()).hexdigest()
        if old_hash != owner[0][1] or _source_id({**self.source, 'schemaHash': old_hash}) != owner[0][0]:
            raise ValueError('standby-original-schema-or-file-changed')
        extras = set(self.tables) - originals
        if extras != set(allowed_extra_tables):
            raise ValueError('unapproved-extra-standby-tables')
        # Column order matters for precise row decoding and hidden rowid writes.
        for name, table in self.tables.items():
            actual = [row[0] for row in self.pg.execute('''SELECT a.attname FROM pg_attribute a
                WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum''',
                (quote(self.schema) + '.' + quote(name),))]
            if actual != [column['name'] for column in table['columns']] + [IDENTITY_COLUMN]:
                raise ValueError('recovery-target-columns-mismatch:' + name)
        with closing(self._sqlite(writable=True)) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                sequence = connection.execute(f'SELECT value FROM {INTERNAL_PREFIX}counter WHERE id=1').fetchone()[0]
                if (sequence != baseline_receipt.get('sourceSequence')
                        or connection.execute(f'SELECT 1 FROM {INTERNAL_PREFIX}changes LIMIT 1').fetchone()):
                    raise ValueError('forward-baseline-has-new-source-writes')
                triggers = []
                for name, table, sql in connection.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"):
                    if table not in self.tables:
                        continue
                    if not name.startswith(('realtime_', INTERNAL_PREFIX)):
                        raise ValueError('unowned-standby-business-trigger:' + name)
                    triggers.append({'name': name, 'table': table, 'sql': sql})
                for name in extras:
                    if (connection.execute('SELECT 1 FROM ' + quote(name) + ' LIMIT 1').fetchone()
                            or self.pg.execute('SELECT 1 FROM ' + quote(name) + ' LIMIT 1').fetchone()):
                        raise ValueError('nonempty-extra-standby-table:' + name)
                guards = _guards(self.tables)
                manifest = {'format': FORMAT, 'schema': self.schema, 'path': self.source['path'],
                    'device': self.source['device'], 'inode': self.source['inode'],
                    'schemaHash': self.source['schemaHash'], 'tables': self.source['tables'],
                    'originalTriggers': triggers, 'guards': guards,
                    'baselineSourceSequence': sequence, 'preparedAtMs': int(time.time()*1000)}
                with self.pg.transaction():
                    self.pg.execute('SELECT pg_advisory_xact_lock(%s)', (advisory_lock_key(self.schema),))
                    self.pg.execute(f'''CREATE TABLE {RECOVERY_OWNER}(
                        singleton bigint PRIMARY KEY CHECK(singleton=1),manifest jsonb NOT NULL,
                        state text NOT NULL,last_verified_seq bigint,last_verified_receipt jsonb)''')
                    self.pg.execute(f'''CREATE TABLE {RECOVERY_COUNTER}(
                        singleton bigint PRIMARY KEY CHECK(singleton=1),seq bigint NOT NULL)''')
                    self.pg.execute(f'INSERT INTO {RECOVERY_COUNTER} VALUES(1,0)')
                    self.pg.execute(f'''CREATE TABLE {RECOVERY_CHANGES}(
                        table_name text NOT NULL,row_id bigint NOT NULL,seq bigint NOT NULL,
                        PRIMARY KEY(table_name,row_id))''')
                    self.pg.execute(f'CREATE INDEX ON {RECOVERY_CHANGES}(seq,table_name,row_id)')
                    self.pg.execute(f'''CREATE TABLE {RECOVERY_RANGES}(
                        table_name text NOT NULL,low_rowid bigint NOT NULL,high_rowid bigint NOT NULL,
                        row_count bigint NOT NULL,content_sha256 text NOT NULL,valid boolean NOT NULL,
                        verified_at_ms bigint NOT NULL,PRIMARY KEY(table_name,low_rowid),CHECK(low_rowid<=high_rowid))''')
                    self.pg.execute(f'''INSERT INTO {RECOVERY_RANGES}
                        SELECT * FROM {RANGE_TABLE} WHERE valid=true''')
                    covered = {row[0] for row in self.pg.execute(f'SELECT DISTINCT table_name FROM {RECOVERY_RANGES}')}
                    if covered != originals:
                        raise ValueError('forward-range-certificates-required')
                    for row in receipts:
                        ranges = self.pg.execute(f'''SELECT low_rowid,high_rowid,row_count,content_sha256
                            FROM {RECOVERY_RANGES} WHERE table_name=%s ORDER BY low_rowid''', (row['table'],)).fetchall()
                        if (row.get('rangeCount') != len(ranges)
                                or row.get('sourceRows') != sum(interval[2] for interval in ranges)
                                or row.get('rangeManifestSha256') != self._range_digest(ranges)):
                            raise ValueError('forward-range-certificate-receipt-mismatch')
                    for name in extras:
                        self.pg.execute(f'INSERT INTO {RECOVERY_RANGES} VALUES(%s,0,0,0,%s,true,%s)',
                                        (name, _digest(()), int(time.time()*1000)))
                    self.pg.execute(_counter_sql(self.schema, self.tables))
                    for trigger in triggers:
                        connection.execute('DROP TRIGGER ' + quote(trigger['name']))
                    for guard in guards:
                        connection.execute(guard['sql'])
                    manifest['standbySchemaVersion'] = connection.execute('PRAGMA schema_version').fetchone()[0]
                    self.pg.execute(f'INSERT INTO {RECOVERY_OWNER} VALUES(1,%s,\'active\',NULL,NULL)', (Jsonb(manifest),))
                    # Persist original trigger SQL in PostgreSQL before the
                    # SQLite DDL commits. If either commit fails, a retry can
                    # finish this guarded setup without losing the originals.
                    # Application writers remain stopped throughout prepare.
                self._after_prepare_pg_commit()
                connection.commit()
                self.manifest = manifest
            except BaseException:
                connection.rollback()
                raise
        self._save_manifest()
        return self.manifest

    def _after_prepare_pg_commit(self):
        """Fault-injection seam while the SQLite guard DDL can still roll back."""

    def _finish_guard_installation(self):
        """Resume a prepare interrupted after its PG metadata commit."""
        self._validate_file(active=False)
        manifest = self._load()
        if self.pg.execute(f'SELECT state FROM {RECOVERY_OWNER} WHERE singleton=1').fetchone()[0] != 'active':
            raise RuntimeError('standby-no-longer-active')
        with closing(self._sqlite(writable=True)) as connection:
            actual = {row[0]: row[1] for row in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
            if all(guard['name'] in actual for guard in manifest['guards']):
                return
            if (any(guard['name'] in actual for guard in manifest['guards'])
                    or any(actual.get(trigger['name']) != trigger['sql'] for trigger in manifest['originalTriggers'])):
                raise ValueError('standby-prepare-trigger-state-changed')
            connection.execute('BEGIN IMMEDIATE')
            try:
                counter = connection.execute(f'SELECT value FROM {INTERNAL_PREFIX}counter WHERE id=1').fetchone()[0]
                if counter != manifest['baselineSourceSequence']:
                    raise ValueError('standby-writes-before-guard-resume')
                for trigger in manifest['originalTriggers']:
                    connection.execute('DROP TRIGGER ' + quote(trigger['name']))
                for guard in manifest['guards']:
                    connection.execute(guard['sql'])
                if connection.execute('PRAGMA schema_version').fetchone()[0] != manifest['standbySchemaVersion']:
                    raise ValueError('standby-resumed-schema-version-changed')
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _save_json(path, value):
        path = Path(path)
        temporary = path.with_name(path.name + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as target:
                json.dump(value, target, ensure_ascii=False, indent=2)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _save_manifest(self):
        self._save_json(self.source['path'] + '.recovery-manifest.json', self._load())

    @staticmethod
    def _range_digest(ranges):
        return hashlib.sha256(json.dumps([list(row[:4]) for row in ranges],
                                        separators=(',', ':')).encode()).hexdigest()

    def _snapshot_changes(self):
        with self.pg.transaction():
            self.pg.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            candidates = self.pg.execute(f'''SELECT table_name,row_id,seq FROM {RECOVERY_CHANGES}
                ORDER BY seq,table_name,row_id LIMIT %s''', (self.batch_rows,)).fetchall()
            changes, observed, bytes_used = [], [], 0
            for change in candidates:
                name, rowid, _ = change
                if name not in self.tables:
                    raise ValueError('unowned-recovery-journal-table')
                table = self.tables[name]
                # Measure on PostgreSQL before transferring any row payload.
                # Both SELECTs use the same repeatable-read snapshot. A very
                # large body is therefore rejected without materialising it,
                # and the next row never exceeds the accumulated byte budget.
                size_row = self.pg.execute('SELECT ' + self._row_size_sql(table) +
                    ' FROM ' + quote(name) + ' WHERE ' + quote(IDENTITY_COLUMN) + '=%s', (rowid,)).fetchone()
                row_bytes = size_row[0] if size_row else 16
                if row_bytes > self.batch_bytes:
                    raise ValueError('recovery-row-bytes-exceeded')
                if bytes_used + row_bytes > self.batch_bytes:
                    break
                row = self._read_observed_row(table, rowid) if size_row else None
                if row is not None and self._row_bytes(row) != row_bytes:
                    raise ValueError('recovery-row-byte-estimate-mismatch')
                changes.append(change)
                observed.append((name, rowid, row))
                bytes_used += row_bytes
        return changes, observed

    @staticmethod
    def _row_size_sql(table):
        expressions = ['16']  # SQLite rowid, numerics and NULL each cost 16.
        for column in table['columns']:
            name = quote(column['name'])
            if table['name'] == 'dashboard_projection' and column['name'] == 'body':
                size = 'octet_length(' + name + ')-1'  # Remove the TEXT/BLOB tag.
            elif column['type'].upper() == 'TEXT':
                size = "octet_length(convert_to(" + name + ",'UTF8'))"
            elif column['type'].upper() == 'BLOB':
                size = 'octet_length(' + name + ')'
            else:
                expressions.append('16')
                continue
            expressions.append('CASE WHEN ' + name + ' IS NULL THEN 16 ELSE ' + size + ' END')
        return '+'.join(expressions)

    @staticmethod
    def _row_bytes(row):
        return sum(len(value.encode()) if isinstance(value, str) else len(value)
                   if isinstance(value, bytes) else 16 for value in row)

    def _read_observed_row(self, table, rowid):
        """Read one budget-approved row; never buffer a batch of bodies."""
        columns = ','.join(quote(column['name']) for column in table['columns'])
        row = self.pg.execute(f'SELECT {quote(IDENTITY_COLUMN)},{columns} FROM {quote(table["name"])} '
                              f'WHERE {quote(IDENTITY_COLUMN)}=%s', (rowid,)).fetchone()
        return sqlite_row(table, row) if row is not None else None

    def _apply(self, connection, observed):
        touched = {}
        for name, rowid, row in observed:
            touched.setdefault(name, set()).add(rowid)
            if row is None:
                connection.execute('DELETE FROM ' + quote(name) + ' WHERE rowid=?', (rowid,))
                continue
            table = self.tables[name]
            primary = [(index + 1, column) for index, column in enumerate(table['columns']) if column['pk']]
            matches = ' AND '.join(quote(column['name']) + ' IS ?' for _, column in primary)
            values = tuple(row[index] for index, _ in primary)
            old = connection.execute('SELECT rowid FROM ' + quote(name) + ' WHERE ' + matches, values).fetchone()
            if old:
                touched[name].add(old[0])
            connection.execute('DELETE FROM ' + quote(name) + ' WHERE rowid=? AND NOT (' + matches + ')',
                               (rowid, *values))
        # Clear obsolete identities for the entire batch before PK upserts.
        for name, _, row in observed:
            if row is None:
                continue
            table = self.tables[name]
            columns = [column['name'] for column in table['columns']]
            primary = [column['name'] for column in sorted(table['columns'], key=lambda value: value['pk']) if column['pk']]
            if not primary:
                raise ValueError('recovery-table-without-primary-key')
            names = 'rowid,' + ','.join(map(quote, columns))
            updates = 'rowid=excluded.rowid' + ''.join(',' + quote(column) + '=excluded.' + quote(column)
                                                      for column in columns if column not in primary)
            connection.execute('INSERT INTO ' + quote(name) + '(' + names + ') VALUES(' +
                ','.join('?' for _ in row) + ') ON CONFLICT(' + ','.join(map(quote, primary)) +
                ') DO UPDATE SET ' + updates, row)
        return touched

    def _after_sqlite_commit(self):
        """Fault-injection seam; no state or acknowledgement has advanced yet."""

    def replay(self):
        """One bounded snapshot -> standby commit -> conditional PostgreSQL ACK."""
        self._validate_file()
        self._check_capacity()
        changes, observed = self._snapshot_changes()
        if not changes:
            return 0
        with closing(self._sqlite(writable=True)) as connection:
            self._validate_file(connection)
            connection.execute('BEGIN IMMEDIATE')
            try:
                touched = self._apply(connection, observed)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        self._after_sqlite_commit()
        with self.pg.transaction():
            for name, rowids in touched.items():
                self.pg.execute(f'''UPDATE {RECOVERY_RANGES} AS ranges SET valid=false
                    FROM unnest(%s::bigint[]) AS changed(row_id) WHERE ranges.table_name=%s
                    AND changed.row_id BETWEEN ranges.low_rowid AND ranges.high_rowid''', (list(rowids), name))
            with self.pg.cursor() as cursor:
                cursor.executemany(f'DELETE FROM {RECOVERY_CHANGES} WHERE table_name=%s AND row_id=%s AND seq=%s', changes)
        return len(changes)

    def _bounds(self, name, connection):
        sqlite_bounds = tuple((connection.execute('SELECT rowid FROM ' + quote(name) +
            ' ORDER BY rowid ' + order + ' LIMIT 1').fetchone() or (None,))[0] for order in ('ASC', 'DESC'))
        pg_bounds = tuple((self.pg.execute(f'SELECT {quote(IDENTITY_COLUMN)} FROM {quote(name)} '
            f'ORDER BY {quote(IDENTITY_COLUMN)} {order} LIMIT 1').fetchone() or (None,))[0] for order in ('ASC', 'DESC'))
        values = [value for value in (*sqlite_bounds, *pg_bounds) if value is not None]
        return (min(values), max(values)) if values else (0, 0)

    def _range_jobs(self, name):
        with closing(self._sqlite()) as connection:
            minimum, maximum = self._bounds(name, connection)
        intervals = self.pg.execute(f'SELECT low_rowid,high_rowid,valid FROM {RECOVERY_RANGES} '
                                    'WHERE table_name=%s ORDER BY low_rowid', (name,)).fetchall()
        if any(left[1] >= right[0] for left, right in zip(intervals, intervals[1:])):
            raise ValueError('standby-overlapping-range-certificates:' + name)
        if intervals:
            minimum, maximum = min(minimum, intervals[0][0]), max(maximum, intervals[-1][1])
        jobs, position = [], minimum
        for low, high, valid in intervals:
            if position < low:
                jobs.append((position, low - 1, False))
            if not valid:
                jobs.append((low, high, True))
            position = high + 1
        if position <= maximum:
            jobs.append((position, maximum, False))
        return jobs

    def _range_chunks(self, name, table, cursor=None):
        jobs = self._range_jobs(name)
        if cursor is not None:
            after, before = [], []
            for low, high, existing in jobs:
                if low > cursor:
                    after.append((low, high, existing))
                elif not existing and high > cursor:
                    after.append((cursor+1, high, False))
                    before.append((low, cursor, False))
                else:
                    before.append((low, high, existing))
            jobs = after + before
        columns = ','.join(quote(column['name']) for column in table['columns'])
        for low, high, existing in jobs:
            while low <= high:
                self._check_capacity()
                with self.pg.transaction():
                    self.pg.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                    # Measure identities/UTF-8 bytes before transferring bodies.
                    candidates = self.pg.execute(f'SELECT {quote(IDENTITY_COLUMN)},'
                        f'{self._row_size_sql(table)} FROM {quote(name)} '
                        f'WHERE {quote(IDENTITY_COLUMN)} BETWEEN %s AND %s '
                        f'ORDER BY {quote(IDENTITY_COLUMN)} LIMIT %s',
                        (low, high, self.batch_rows+1)).fetchall()
                    selected, bytes_used = [], 0
                    for rowid, row_bytes in candidates[:self.batch_rows]:
                        if row_bytes > self.batch_bytes:
                            raise ValueError('recovery-range-bytes-exceeded')
                        if bytes_used + row_bytes > self.batch_bytes:
                            break
                        selected.append(rowid)
                        bytes_used += row_bytes
                    more = len(selected) < len(candidates)
                    interval_high = selected[-1] if more else high
                    pending = self.pg.execute(f'SELECT 1 FROM {RECOVERY_CHANGES} '
                        'WHERE table_name=%s AND row_id BETWEEN %s AND %s LIMIT 1',
                        (name, low, interval_high)).fetchone()
                    rows = [] if pending else [sqlite_row(table, row) for row in self.pg.execute(
                        f'SELECT {quote(IDENTITY_COLUMN)},{columns} FROM {quote(name)} '
                        f'WHERE {quote(IDENTITY_COLUMN)} BETWEEN %s AND %s '
                        f'ORDER BY {quote(IDENTITY_COLUMN)} LIMIT %s',
                        (low, interval_high, self.batch_rows+1)).fetchall()]
                    if not pending and (len(rows) != len(selected) or
                            sum(self._row_bytes(row) for row in rows) != bytes_used):
                        raise ValueError('recovery-range-byte-estimate-mismatch')
                status = {'table': name, 'lowRowid': low, 'highRowid': interval_high, 'pending': bool(pending)}
                if not pending:
                    with closing(self._sqlite()) as connection:
                        actual = connection.execute(f'SELECT rowid,{columns} FROM {quote(name)} '
                            'WHERE rowid BETWEEN ? AND ? ORDER BY rowid LIMIT ?',
                            (low, interval_high, self.batch_rows+1)).fetchall()
                    if len(actual) != len(rows) or _digest(actual) != _digest(rows):
                        raise ValueError('standby-range-content-mismatch:' + name)
                if not pending or (existing and interval_high < high):
                    with self.pg.transaction():
                        if existing and interval_high < high:
                            changed = self.pg.execute(f'''UPDATE {RECOVERY_RANGES}
                                SET high_rowid=%s,row_count=0,content_sha256='',valid=false
                                WHERE table_name=%s AND low_rowid=%s AND high_rowid=%s AND NOT valid''',
                                (interval_high, name, low, high))
                            if changed.rowcount != 1:
                                raise ValueError('standby-dirty-range-changed-during-split')
                            self.pg.execute(f'''INSERT INTO {RECOVERY_RANGES} VALUES(%s,%s,%s,0,'',false,%s)''',
                                (name, interval_high+1, high, int(time.time()*1000)))
                        if not pending:
                            self.pg.execute(f'''INSERT INTO {RECOVERY_RANGES} VALUES(%s,%s,%s,%s,%s,true,%s)
                                ON CONFLICT(table_name,low_rowid) DO UPDATE SET high_rowid=excluded.high_rowid,
                                row_count=excluded.row_count,content_sha256=excluded.content_sha256,
                                valid=true,verified_at_ms=excluded.verified_at_ms''',
                                (name, low, interval_high, len(rows), _digest(rows), int(time.time()*1000)))
                    if not pending:
                        status.update(rows=len(rows), match=True)
                self._range_cursors[name] = interval_high
                yield status
                low = interval_high + 1

    def reconcile_ranges(self, *, max_ranges=8):
        """Fair bounded verification; only exact typed hashes certify content."""
        self._validate_file()
        self._range_cursors = getattr(self, '_range_cursors', {})
        names = list(self.tables)
        previous = getattr(self, '_range_last_table', None)
        if max_ranges is not None and previous in names:
            start = names.index(previous) + 1
            names = names[start:] + names[:start]
        queue = deque((name, self._range_chunks(name, self.tables[name],
            self._range_cursors.get(name) if max_ranges is not None else None)) for name in names)
        processed = 0
        while queue and (max_ranges is None or processed < max_ranges):
            name, chunks = queue.popleft()
            try:
                status = next(chunks)
            except StopIteration:
                continue
            queue.append((name, chunks))
            processed += 1
            self._range_last_table = name
            yield status

    def verify_barrier(self):
        """Verify pre-warmed content under an externally closed PG writer gate."""
        with self.pg.transaction():
            self.pg.execute('SELECT pg_advisory_xact_lock(%s)', (advisory_lock_key(self.schema),))
            return self._verify_barrier_locked()

    def _verify_barrier_locked(self):
        from psycopg.types.json import Jsonb
        self._validate_file()
        start = self.pg.execute(f'SELECT seq FROM {RECOVERY_COUNTER} WHERE singleton=1').fetchone()[0]
        if self.pg.execute(f'SELECT 1 FROM {RECOVERY_CHANGES} LIMIT 1').fetchone():
            raise ValueError('standby-pending-cdc')
        results = []
        with closing(self._sqlite()) as connection:
            self._validate_file(connection)
            connection.execute('BEGIN')
            for name in self.tables:
                intervals = self.pg.execute(f'SELECT low_rowid,high_rowid,row_count,content_sha256,valid '
                    f'FROM {RECOVERY_RANGES} WHERE table_name=%s ORDER BY low_rowid', (name,)).fetchall()
                if not intervals or any(not row[4] for row in intervals):
                    raise ValueError('standby-missing-or-dirty-range:' + name)
                if any(left[1] + 1 != right[0] for left, right in zip(intervals, intervals[1:])):
                    raise ValueError('standby-noncontiguous-range:' + name)
                minimum, maximum = self._bounds(name, connection)
                if minimum < intervals[0][0] or maximum > intervals[-1][1]:
                    raise ValueError('standby-uncovered-range:' + name)
                results.append({'table': name, 'rows': sum(row[2] for row in intervals),
                    'rangeCount': len(intervals), 'rangeManifestSha256': self._range_digest(intervals),
                    'match': True, 'proof': 'ordered-typed-content-per-range'})
            if connection.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('standby-foreign-key-check-failed')
        final = self.pg.execute(f'SELECT seq FROM {RECOVERY_COUNTER} WHERE singleton=1').fetchone()[0]
        if start != final or self.pg.execute(f'SELECT 1 FROM {RECOVERY_CHANGES} LIMIT 1').fetchone():
            raise ValueError('postgres-writes-during-standby-verification')
        receipt = {'format': FORMAT, 'schema': self.schema, 'sourceSequence': final,
                   'sourceId': _source_id(self.source), 'sourceSchemaHash': self.source['schemaHash'],
                   'standbySourceSequence': self._load()['baselineSourceSequence'],
                   'verifiedAtMs': int(time.time()*1000), 'tables': results,
                   'foreignKeysValid': True, 'standbyReady': True, 'runtimeCutover': False}
        self.pg.execute(f'UPDATE {RECOVERY_OWNER} SET last_verified_seq=%s,last_verified_receipt=%s WHERE singleton=1',
                        (final, Jsonb(receipt)))
        return receipt

    def restore_sqlite(self):
        """Restore the original SQLite writer protocol after successful barrier verification.

        This never resumes application writers or changes runtime settings.
        The caller must keep PostgreSQL writers stopped until code/config switch.
        """
        with self.pg.transaction():
            self.pg.execute('SELECT pg_advisory_xact_lock(%s)', (advisory_lock_key(self.schema),))
            return self._restore_sqlite_locked()

    def _restore_sqlite_locked(self):
        from psycopg.types.json import Jsonb
        self._validate_file(active=False)
        state, verified, receipt = self.pg.execute(f'SELECT state,last_verified_seq,last_verified_receipt '
                                                  f'FROM {RECOVERY_OWNER} WHERE singleton=1').fetchone()
        current = self.pg.execute(f'SELECT seq FROM {RECOVERY_COUNTER} WHERE singleton=1').fetchone()[0]
        if (verified is None or verified != current or not receipt or not receipt.get('standbyReady')
                or self.pg.execute(f'SELECT 1 FROM {RECOVERY_CHANGES} LIMIT 1').fetchone()):
            raise ValueError('standby-promotion-requires-current-verification')
        if state == 'restored':
            with closing(self._sqlite()) as connection:
                self._validate_restored_sqlite(connection, receipt)
            return receipt
        if state != 'active':
            raise ValueError('unowned-recovery-restoration-state')
        sequences = []
        for name, table in self.tables.items():
            if 'AUTOINCREMENT' not in table['sql'].upper():
                continue
            primary = [column for column in table['columns'] if column['pk']]
            if len(primary) != 1 or primary[0]['type'].upper() != 'INTEGER':
                raise ValueError('unsupported-standby-autoincrement')
            column = primary[0]['name']
            sequence = self.pg.execute('SELECT pg_get_serial_sequence(%s,%s)',
                                      (quote(self.schema) + '.' + quote(name), column)).fetchone()[0]
            if sequence is None:
                raise ValueError('missing-recovery-identity-sequence')
            namespace, seqname = self.pg.execute('''SELECT n.nspname,c.relname FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=%s::regclass''', (sequence,)).fetchone()
            frontier, called = self.pg.execute('SELECT last_value,is_called FROM ' +
                                                quote(namespace) + '.' + quote(seqname)).fetchone()
            maximum = self.pg.execute('SELECT COALESCE(MAX(' + quote(column) + '),0) FROM ' + quote(name)).fetchone()[0]
            sequences.append((name, max(maximum, frontier if called else frontier - 1)))
        with closing(self._sqlite(writable=True)) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                present = {row[0]: row[1] for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
                guards_present = all(guard['name'] in present for guard in self._load()['guards'])
                originals_present = all(present.get(trigger['name']) == trigger['sql']
                                        for trigger in self._load()['originalTriggers'])
                if guards_present:
                    self._validate_file(connection)
                    for guard in self._load()['guards']:
                        connection.execute('DROP TRIGGER ' + quote(guard['name']))
                    for trigger in self._load()['originalTriggers']:
                        connection.execute(trigger['sql'])
                elif not originals_present or any(name.startswith(GUARD_PREFIX) for name in present):
                    raise ValueError('standby-trigger-restoration-incomplete')
                elif connection.execute(f'SELECT value FROM {INTERNAL_PREFIX}counter WHERE id=1').fetchone()[0] != self._load()['baselineSourceSequence']:
                    raise ValueError('standby-writes-during-interrupted-restoration')
                for name, frontier in sequences:
                    old = connection.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (name,)).fetchone()
                    if old is None:
                        connection.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', (name, frontier))
                    elif old[0] < frontier:
                        connection.execute('UPDATE sqlite_sequence SET seq=? WHERE name=?', (frontier, name))
                if connection.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('standby-foreign-key-check-failed')
                if self.pg.execute(f'SELECT seq FROM {RECOVERY_COUNTER} WHERE singleton=1').fetchone()[0] != current:
                    raise ValueError('postgres-writes-during-standby-restoration')
                restored_version = connection.execute('PRAGMA schema_version').fetchone()[0]
                restored_receipt = {**receipt, 'triggersRestored': True,
                    'restoredSchemaVersion': restored_version,
                    'restoredSequences': [dict(table=name, frontier=value) for name, value in sequences]}
                self._validate_restored_sqlite(connection, restored_receipt)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        self._after_restore_sqlite_commit()
        self.pg.execute(f"UPDATE {RECOVERY_OWNER} SET state='restored',last_verified_receipt=%s WHERE singleton=1",
                        (Jsonb(restored_receipt),))
        return restored_receipt

    def _after_restore_sqlite_commit(self):
        """Fault-injection seam before PostgreSQL records the restored state."""

    def _validate_restored_sqlite(self, connection, receipt):
        """Reject changed source/trigger state even after an interrupted restore."""
        self._validate_file(active=False)
        manifest = self._load()
        actual_source = inspect_source(self.source['path'])
        if (actual_source['schemaHash'] != manifest['schemaHash']
                or _source_id(actual_source) != receipt.get('sourceId')):
            raise ValueError('restored-standby-schema-or-file-changed')
        if (not receipt.get('triggersRestored')
                or connection.execute('PRAGMA schema_version').fetchone()[0] != receipt.get('restoredSchemaVersion')):
            raise ValueError('restored-standby-schema-version-changed')
        expected = {trigger['name']: trigger['sql'] for trigger in manifest['originalTriggers']}
        actual = {name: sql for name, table, sql in connection.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'")
            if table in self.tables or name.startswith(GUARD_PREFIX)}
        if actual != expected:
            raise ValueError('restored-standby-trigger-protocol-changed')
        counter = connection.execute(f'SELECT value FROM {INTERNAL_PREFIX}counter WHERE id=1').fetchone()[0]
        if (counter != manifest['baselineSourceSequence']
                or connection.execute(f'SELECT 1 FROM {INTERNAL_PREFIX}changes LIMIT 1').fetchone()):
            raise ValueError('restored-standby-has-source-writes')
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('standby-foreign-key-check-failed')

    def _certificate_bundle(self, receipt):
        """Export every exact typed interval, bound to the verified table receipt."""
        result = []
        expected = {row['table']: row for row in receipt.get('tables') or []}
        if set(expected) != set(self.tables) or any(row.get('match') is not True for row in expected.values()):
            raise ValueError('incomplete-recovery-restoration-receipt')
        for name in self.tables:
            rows = self.pg.execute(f'''SELECT low_rowid,high_rowid,row_count,content_sha256,valid,verified_at_ms
                FROM {RECOVERY_RANGES} WHERE table_name=%s ORDER BY low_rowid''', (name,)).fetchall()
            table_receipt = expected[name]
            if (not rows or any(row[4] is not True for row in rows)
                    or any(left[1] + 1 != right[0] for left, right in zip(rows, rows[1:]))
                    or table_receipt.get('rangeCount') != len(rows)
                    or table_receipt.get('rows') != sum(row[2] for row in rows)
                    or table_receipt.get('rangeManifestSha256') != self._range_digest(rows)):
                raise ValueError('recovery-restoration-certificate-mismatch:' + name)
            result.extend(dict(zip(('table', 'lowRowid', 'highRowid', 'rowCount', 'contentSha256',
                                    'valid', 'verifiedAtMs'), (name, *row))) for row in rows)
        return result

    def abort(self):
        """Remove this recovery generation after a verified SQLite restoration.

        Both application writer groups must remain externally stopped. The
        complete manifest, restored receipt and typed certificates are saved
        before transactional PG cleanup. No business table/data is removed,
        and no runtime config or writer process is changed. The caller can
        rebase its forward migration from the returned certificates.
        """
        exists = self.pg.execute('SELECT to_regclass(%s)', (self.schema + '.' + RECOVERY_OWNER,)).fetchone()[0]
        if not exists and self.manifest is None:
            path = Path(self.source['path'] + '.recovery-manifest.json')
            self.manifest = json.loads(path.read_text())
        manifest = self._load()
        generation = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        archive_path = self.source['path'] + '.recovery-abort-' + generation + '.json'
        with closing(self._sqlite(writable=True)) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                with self.pg.transaction():
                    self.pg.execute('SELECT pg_advisory_xact_lock(%s)', (advisory_lock_key(self.schema),))
                    if not exists:
                        archive = json.loads(Path(archive_path).read_text())
                        if (archive.get('format') != ABORT_FORMAT or archive.get('manifestSha256') != generation
                                or archive.get('schema') != self.schema or archive.get('manifest') != manifest):
                            raise ValueError('unowned-recovery-abort-archive')
                        self._validate_restored_sqlite(connection, archive['restoredReceipt'])
                        self._assert_recovery_removed()
                    else:
                        state, verified, receipt = self.pg.execute(f'''SELECT state,last_verified_seq,last_verified_receipt
                            FROM {RECOVERY_OWNER} WHERE singleton=1''').fetchone()
                        current = self.pg.execute(f'SELECT seq FROM {RECOVERY_COUNTER} WHERE singleton=1').fetchone()[0]
                        if (state != 'restored' or verified != current or not receipt
                                or receipt.get('sourceSequence') != current or not receipt.get('standbyReady')
                                or self.pg.execute(f'SELECT 1 FROM {RECOVERY_CHANGES} LIMIT 1').fetchone()):
                            raise ValueError('recovery-abort-requires-verified-restoration')
                        self._validate_restored_sqlite(connection, receipt)
                        certificates = self._certificate_bundle(receipt)
                        archive = {**receipt, 'format': ABORT_FORMAT, 'manifest': manifest,
                            'manifestSha256': generation, 'restoredReceipt': receipt,
                            'rangeCertificates': certificates, 'archivePath': archive_path,
                            'abortedAtMs': int(time.time()*1000), 'cleanupPrepared': True,
                            'recoveryRemoved': False, 'postgresWriterBarrierRequired': True}
                        self._save_json(archive_path, archive)
                        qualified = quote(self.schema) + '.'
                        for name in self.tables:
                            self.pg.execute('DROP TRIGGER _cliperx_recovery_capture ON ' + qualified + quote(name))
                        self.pg.execute('DROP FUNCTION ' + qualified + '_cliperx_recovery_capture()')
                        for name in (RECOVERY_CHANGES, RECOVERY_RANGES, RECOVERY_COUNTER, RECOVERY_OWNER):
                            self.pg.execute('DROP TABLE ' + qualified + quote(name))
                # PostgreSQL cleanup is committed before the SQLite barrier
                # releases. A retry after a later failure uses the durable
                # cleanupPrepared archive and confirms all owned objects gone.
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        self._after_abort_pg_commit()
        archive['recoveryRemoved'] = True
        self._save_json(archive_path, archive)
        return archive

    def _assert_recovery_removed(self):
        for name in (RECOVERY_CHANGES, RECOVERY_RANGES, RECOVERY_COUNTER, RECOVERY_OWNER):
            if self.pg.execute('SELECT to_regclass(%s)', (self.schema + '.' + name,)).fetchone()[0]:
                raise ValueError('recovery-abort-partial-cleanup')
        if self.pg.execute('SELECT to_regprocedure(%s)',
                           (quote(self.schema) + '._cliperx_recovery_capture()',)).fetchone()[0]:
            raise ValueError('recovery-abort-capture-function-remains')
        if self.pg.execute('''SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s
            AND t.tgname='_cliperx_recovery_capture' LIMIT 1''', (self.schema,)).fetchone():
            raise ValueError('recovery-abort-capture-trigger-remains')

    def _after_abort_pg_commit(self):
        """Fault-injection seam after PG cleanup and before final archive marking."""

    def status(self):
        state = self.pg.execute(f'SELECT state,last_verified_seq FROM {RECOVERY_OWNER} WHERE singleton=1').fetchone()
        pending, low, high = self.pg.execute(f'SELECT count(*),min(seq),max(seq) FROM {RECOVERY_CHANGES}').fetchone()
        counter = self.pg.execute(f'SELECT seq FROM {RECOVERY_COUNTER} WHERE singleton=1').fetchone()[0]
        dirty = self.pg.execute(f'SELECT count(*) FROM {RECOVERY_RANGES} WHERE NOT valid').fetchone()[0]
        return {'schema': self.schema, 'state': state[0], 'pendingRows': pending,
                'firstPendingSequence': low, 'lastPendingSequence': high, 'sourceSequence': counter,
                'dirtyRanges': dirty, 'lastVerifiedSequence': state[1],
                'standbyReady': state[0] == 'active' and state[1] == counter and pending == 0 and dirty == 0,
                'manifestPath': self.source['path'] + '.recovery-manifest.json'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'replay', 'reconcile-ranges',
                                             'verify-barrier', 'restore-sqlite', 'abort', 'status', 'run'))
    parser.add_argument('--source', required=True)
    parser.add_argument('--schema', required=True)
    parser.add_argument('--domain')
    parser.add_argument('--settings', default='data/architecture-runtime.json')
    parser.add_argument('--baseline-receipt')
    parser.add_argument('--allowed-extra-table', action='append', default=[])
    parser.add_argument('--batch-rows', type=int, default=1000)
    parser.add_argument('--interval', type=float, default=1)
    args = parser.parse_args()
    config = json.loads(Path(args.settings).read_text())
    if config.get('managedBy') != 'cliperx-architecture-v1':
        raise ValueError('unmanaged-recovery-settings')
    mirror = RecoveryMirror(config, args.source, args.schema, domain=args.domain, batch_rows=args.batch_rows)
    try:
        if args.operation == 'prepare':
            if not args.baseline_receipt:
                parser.error('prepare requires --baseline-receipt')
            manifest = mirror.prepare(json.loads(Path(args.baseline_receipt).read_text()),
                                      allowed_extra_tables=args.allowed_extra_table)
            result = {'schema': args.schema, 'prepared': True, 'tables': len(manifest['tables']),
                      'manifestPath': args.source + '.recovery-manifest.json', 'runtimeCutover': False}
        elif args.operation == 'replay':
            result = {'replayedRows': mirror.replay(), **mirror.status()}
        elif args.operation == 'reconcile-ranges':
            result = {'checkedRanges': sum(1 for _ in mirror.reconcile_ranges(max_ranges=None)), **mirror.status()}
        elif args.operation == 'verify-barrier':
            result = mirror.verify_barrier()
        elif args.operation == 'restore-sqlite':
            result = mirror.restore_sqlite()
        elif args.operation == 'abort':
            result = mirror.abort()
        elif args.operation == 'status':
            result = mirror.status()
        else:
            last_report = 0
            while True:
                try:
                    replayed = mirror.replay()
                    if replayed < mirror.batch_rows:
                        list(mirror.reconcile_ranges(max_ranges=8))
                    if time.monotonic() - last_report > 30:
                        print(json.dumps(mirror.status()), flush=True)
                        last_report = time.monotonic()
                    time.sleep(max(.1, min(10, args.interval)))
                except (sqlite3.OperationalError, ValueError) as exc:
                    print(json.dumps({'schema': args.schema, 'mirrorError': type(exc).__name__,
                                      'standbyReady': False}), flush=True)
                    time.sleep(max(1, min(10, args.interval)))
        print(json.dumps(result), flush=True)
    finally:
        mirror.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Driver messages may embed account values or private failed rows.
        raise SystemExit('storage-recovery-failed:' + type(exc).__name__)
