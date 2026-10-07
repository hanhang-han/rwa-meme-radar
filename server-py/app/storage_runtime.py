"""Explicit, fail-closed runtime switch for verified SQLite storage domains.

The default remains SQLite. PostgreSQL outages never fall back to an obsolete
SQLite authority. All writes acquire a schema-specific transaction lock before
identity allocation, so committed event cursors cannot skip a late transaction.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .postgres_sql import ROWID, Table, compile_sql

_SCHEMAS = {name: 'cliperx_storage_' + name for name in
            ('research', 'market', 'accounts', 'budget', 'leases', 'stream_archive')}
_metadata_cache = {}
_metadata_lock = threading.Lock()


def _safe_postgres_diagnostic(error):
    """Describe operational failures without retaining driver messages/DSNs.

    libpq connection failures can omit SQLSTATE, including a server's FATAL
    capacity rejection. Classify those messages internally; never expose their
    host, role, database, query, bind values or original message text.
    """
    state = getattr(error, 'sqlstate', None) or getattr(getattr(error, 'diag', None), 'sqlstate', None)
    if not isinstance(state, str) or not re.fullmatch(r'[0-9A-Z]{5}', state):
        state = None
    message = str(error).lower()
    categories = (
        ('lock_contention', state == '55P03'),
        ('serialization_retry', state == '40001'),
        ('deadlock_retry', state == '40P01'),
        ('statement_timeout', state == '57014'),
        ('connection_capacity', state == '53300' or any(text in message for text in
          ('too many clients', 'too many connections', 'remaining connection slots', 'connection limit exceeded'))),
        ('memory_capacity', state == '53200' or 'out of memory' in message or 'cannot allocate memory' in message),
        ('authentication', state in ('28000', '28P01') or 'password authentication failed' in message),
        ('connect_timeout', 'timeout expired' in message or 'connection timeout' in message or 'timed out' in message),
        ('connection_refused', 'connection refused' in message),
        ('connection_unavailable', bool(state and state.startswith('08')) or state == '57P03'),
    )
    category = next((name for name, matches in categories if matches), 'unknown')
    kind = type(error).__name__
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', kind):
        kind = 'PostgresError'
    errno = getattr(error, 'errno', None)
    if not isinstance(errno, int) or isinstance(errno, bool) or not 0 < errno < 4096:
        errno = None
    return {'category': category, 'sqlstate': state, 'errorClass': kind, 'errno': errno}


def _connection_failure(error):
    diagnostic = _safe_postgres_diagnostic(error)
    # Include only fixed vocabulary and the validated five-character SQLSTATE
    # in the message. Existing sqlite3.OperationalError handlers still apply.
    failure = sqlite3.OperationalError('postgres-storage-connection-failed:' +
        diagnostic['category'] + ':sqlstate=' + (diagnostic['sqlstate'] or 'unavailable'))
    failure.postgres_diagnostic = diagnostic
    return failure


def _operation_failure(error, *, context='operation'):
    """Preserve safe SQLSTATE diagnostics and existing whole-write retries."""
    diagnostic = _safe_postgres_diagnostic(error)
    # A statement cancellation is retryable only while acquiring our writer
    # gate. Retrying an expensive query would repeat the same slow operation.
    busy = diagnostic['sqlstate'] in ('55P03', '40001', '40P01') or (
        diagnostic['sqlstate'] == '57014' and context == 'writer_lock')
    context = context if context in ('operation', 'commit', 'writer_lock') else 'operation'
    message = ('database is locked:' if busy else 'postgres-storage-') + context + '-failed:'
    failure = sqlite3.OperationalError(message + diagnostic['category'] +
        ':sqlstate=' + (diagnostic['sqlstate'] or 'unavailable'))
    failure.postgres_diagnostic = diagnostic
    if busy:
        failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
        failure.sqlite_errorname = 'SQLITE_BUSY'
    return failure


def advisory_lock_key(schema):
    if schema not in _SCHEMAS.values():
        raise ValueError('unowned-storage-schema')
    return int.from_bytes(hashlib.sha256(('cliperx-storage-writer:' + schema).encode()).digest()[:8], 'big', signed=True)


def _settings():
    path = Path(os.environ.get('ARCHITECTURE_SETTINGS', 'data/architecture-runtime.json'))
    try:
        config = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise sqlite3.OperationalError('invalid-storage-runtime-settings') from exc
    storage = config.get('storage') or {}
    if not storage.get('enabled'):
        return {}
    if (config.get('managedBy') != 'cliperx-architecture-v1' or storage.get('enabled') is not True
            or storage.get('backend') != 'postgres' or not isinstance(config.get('dsn'), str)
            or storage.get('domainSchemas') != _SCHEMAS):
        raise sqlite3.OperationalError('invalid-postgres-storage-switch')
    overrides = config.get('storageDsns', {})
    if (not isinstance(overrides, dict) or set(overrides) - {'market'}
            or any(not isinstance(value, str) or not value.strip() for value in overrides.values())):
        raise sqlite3.OperationalError('invalid-storage-domain-dsn-route')
    return config


def dsn_for_domain(config, domain):
    if domain not in _SCHEMAS:
        raise sqlite3.OperationalError('unknown-storage-domain')
    overrides = config.get('storageDsns', {})
    if set(overrides) - {'market'}:
        raise sqlite3.OperationalError('unowned-storage-domain-dsn-route')
    return overrides.get(domain, config['dsn'])


def _ordinary_path(path, *, uri=False):
    value = os.fspath(path)
    readonly = False
    if value == ':memory:':
        return value, readonly
    if value.startswith('file:'):
        if not uri:
            raise ValueError('storage-uri-requires-uri-option')
        parsed = urlsplit(value)
        if parsed.netloc not in ('', 'localhost'):
            raise ValueError('remote-storage-uri')
        query = parse_qs(parsed.query)
        if set(query) - {'mode', 'immutable', 'cache'}:
            raise ValueError('unsupported-storage-uri-option')
        if query.get('mode', ['rw'])[0] not in ('ro', 'rw', 'rwc'):
            raise ValueError('unsupported-storage-uri-mode')
        readonly = query.get('mode') == ['ro'] or query.get('immutable') == ['1']
        value = unquote(parsed.path)
    return str(Path(value).expanduser().resolve()), readonly


def domain_for_path(path, *, uri=False):
    physical, _ = _ordinary_path(path, uri=uri)
    research = os.environ.get('RESEARCH_DB', 'data/research.sqlite')
    paths = {
        'research': research,
        'market': os.environ.get('LIVE_MARKET_DB') or 'data/live-market.sqlite',
        'accounts': os.environ.get('DEVELOPER_DB_PATH', 'data/developer-access.sqlite'),
        'budget': os.environ.get('OKX_LEDGER_PATH', 'data/okx-budget.sqlite'),
        'leases': os.environ.get('DEMAND_LEASE_DB') or research + '.leases.sqlite',
        'stream_archive': 'data/stream-events.sqlite',
    }
    matches = [domain for domain, value in paths.items() if _ordinary_path(value)[0] == physical]
    if len(matches) != 1:
        raise sqlite3.OperationalError('unknown-or-aliased-storage-domain')
    # An env path or symlink must not collapse independent runtime domains.
    occupied = [_ordinary_path(value)[0] for value in paths.values()]
    if len(set(occupied)) != len(occupied):
        raise sqlite3.OperationalError('storage-domain-paths-are-not-independent')
    existing = [value for value in occupied if Path(value).exists()]
    identities = [(Path(value).stat().st_dev, Path(value).stat().st_ino) for value in existing]
    if len(set(identities)) != len(identities):
        raise sqlite3.OperationalError('storage-domains-share-a-physical-file')
    return matches[0]


def is_postgres_path(path, *, uri=False):
    config = _settings()
    if not config:
        return False
    domain_for_path(path, uri=uri)
    return True


storage_domain_enabled = is_postgres_path


def runtime_support_sql(schema):
    """Installer SQL, explicitly applied by the deployment before activation."""
    advisory_lock_key(schema)
    return f'''
SET LOCAL search_path TO "{schema}", pg_catalog;
CREATE OR REPLACE FUNCTION _cliperx_sqlite_num(value text) RETURNS double precision
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE part text;
BEGIN
  part := substring(ltrim(value) from '^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?');
  IF part IS NULL THEN RETURN 0; END IF;
  RETURN part::double precision;
EXCEPTION WHEN numeric_value_out_of_range THEN
  RETURN CASE WHEN left(part,1)='-' THEN '-Infinity'::double precision ELSE 'Infinity'::double precision END;
END; $$;
CREATE OR REPLACE FUNCTION _cliperx_sqlite_int(value text) RETURNS bigint
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE part text; number numeric;
BEGIN
  part := substring(ltrim(value) from '^[+-]?[0-9]+');
  IF part IS NULL THEN RETURN 0; END IF;
  number := part::numeric;
  RETURN greatest(-9223372036854775808::numeric,least(9223372036854775807::numeric,number))::bigint;
END; $$;
CREATE OR REPLACE FUNCTION _cliperx_sqlite_json_type(value jsonb) RETURNS text
LANGUAGE SQL IMMUTABLE STRICT AS $$
  SELECT CASE jsonb_typeof(value)
    WHEN 'number' THEN CASE WHEN value::text ~ '^[+-]?[0-9]+$' THEN 'integer' ELSE 'real' END
    WHEN 'string' THEN 'text'
    WHEN 'boolean' THEN CASE WHEN value='true'::jsonb THEN 'true' ELSE 'false' END
    ELSE jsonb_typeof(value) END;
$$;
CREATE OR REPLACE FUNCTION _cliperx_sqlite_substr(value text,start_at bigint,take bigint)
RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE pos bigint; n bigint := length(value);
BEGIN
  pos := CASE WHEN start_at<0 THEN greatest(0,n+start_at) WHEN start_at=0 THEN 0 ELSE start_at-1 END;
  IF take<0 THEN RETURN substring(value from greatest(0,pos+take)::integer+1 for (pos-greatest(0,pos+take))::integer); END IF;
  IF start_at=0 THEN take:=greatest(0,take-1); END IF;
  IF start_at<0 AND n+start_at<0 THEN take:=greatest(0,take+n+start_at); END IF;
  RETURN substring(value from least(2147483646,pos)::integer+1 for least(2147483647,take)::integer);
END; $$;
CREATE OR REPLACE FUNCTION _cliperx_sqlite_substr(value text,start_at bigint)
RETURNS text LANGUAGE SQL IMMUTABLE STRICT AS $$
  SELECT _cliperx_sqlite_substr(value,start_at,2147483647::bigint);
$$;
'''


class Row:
    """sqlite3.Row's sequence and case-insensitive mapping access."""
    __slots__ = ('_values', '_names', '_lookup')

    def __init__(self, names, values):
        self._values = tuple(values)
        self._names = tuple(names)
        self._lookup = {name.lower(): index for index, name in reversed(list(enumerate(names)))}

    def keys(self):
        return list(self._names)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._lookup[key.lower()]]
            except KeyError as exc:
                raise IndexError('No item with that key') from exc
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class Cursor:
    def __init__(self, connection, rows=(), names=(), *, rowcount=-1, lastrowid=None):
        self.connection = connection
        self._rows = iter(rows)
        self.description = tuple((name, None, None, None, None, None, None) for name in names) if names else None
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.arraysize = 1
        self._closed = False

    def fetchone(self):
        if self._closed:
            raise sqlite3.ProgrammingError('cursor-is-closed')
        value = next(self._rows, None)
        if value is None:
            return None
        if self.connection.row_factory is None:
            return tuple(value)
        names = [entry[0] for entry in self.description]
        if self.connection.row_factory in (sqlite3.Row, Row):
            return Row(names, value)
        return self.connection.row_factory(self, tuple(value))

    def fetchall(self):
        result = []
        while (value := self.fetchone()) is not None:
            result.append(value)
        return result

    def fetchmany(self, size=None):
        result = []
        for _ in range(self.arraysize if size is None else size):
            value = self.fetchone()
            if value is None:
                break
            result.append(value)
        return result

    def close(self):
        self._closed = True

    def __iter__(self):
        return self

    def __next__(self):
        value = self.fetchone()
        if value is None:
            raise StopIteration
        return value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _read_metadata(pg, schema):
    rows = pg.execute('''SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),
      a.attidentity, EXISTS(SELECT 1 FROM pg_index i WHERE i.indrelid=c.oid AND i.indisprimary AND a.attnum=ANY(i.indkey))
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
      WHERE n.nspname=%s AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
      ORDER BY c.relname,a.attnum''', (schema,)).fetchall()
    specs = {}
    for name, column, kind, identity, primary in rows:
        if name.startswith('_cliperx_') or column == ROWID:
            continue
        specs.setdefault(name, {'columns': [], 'types': [], 'primary': [], 'identities': []})
        specs[name]['columns'].append(column)
        specs[name]['types'].append(kind)
        if primary:
            specs[name]['primary'].append(column)
        if identity:
            specs[name]['identities'].append(column)
    if not specs:
        raise sqlite3.OperationalError('missing-migrated-storage-schema')
    return {name.lower(): Table(name, tuple(spec['columns']), tuple(spec['types']), tuple(spec['primary']), tuple(spec['identities'])) for name, spec in specs.items()}


class Connection:
    backend = 'postgres'

    def __init__(self, config, domain, *, readonly=False, timeout=15, isolation_level=''):
        import psycopg
        self.domain = domain
        self.schema = config['storage']['domainSchemas'][domain]
        self.readonly = bool(readonly)
        self.isolation_level = isolation_level
        self.row_factory = None
        self.total_changes = 0
        self._last_changes = 0
        self._lastrowid = None
        self._write_locked = False
        self._closed = False
        self._progress_handler = None
        self._timeout_ms = max(1, int(timeout * 1000))
        options = (f'-c search_path={self.schema},pg_catalog -c statement_timeout={max(self._timeout_ms, 1000)} '
                   f'-c lock_timeout={self._timeout_ms} -c idle_in_transaction_session_timeout=60000 '
                   f'-c application_name=cliperx-storage-{domain} -c work_mem=2048kB')
        if readonly:
            options += ' -c default_transaction_read_only=on'
        dsn = dsn_for_domain(config, domain)
        try:
            self._pg = psycopg.connect(dsn, autocommit=True, connect_timeout=max(1, min(15, int(timeout))), options=options)
        except psycopg.Error as error:
            raise _connection_failure(error) from None
        self._mutex = threading.RLock()
        identity = (hashlib.sha256(dsn.encode()).digest(), self.schema)
        try:
            with _metadata_lock:
                self.tables = _metadata_cache.get(identity)
                if self.tables is None:
                    self.tables = _read_metadata(self._pg, self.schema)
                    _metadata_cache[identity] = self.tables
            owner = self._pg.execute('SELECT source_id,source_schema_hash FROM _cliperx_migration_owner').fetchall()
            if len(owner) != 1 or any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in owner[0]):
                raise sqlite3.OperationalError('invalid-migrated-storage-owner')
            if self._pg.execute('SELECT 1 FROM _cliperx_migration_state WHERE NOT backfill_done OR NOT indexes_done LIMIT 1').fetchone():
                raise sqlite3.OperationalError('storage-import-not-complete')
            if self._pg.execute('SELECT to_regprocedure(%s)', (self.schema + '._cliperx_sqlite_num(text)',)).fetchone()[0] is None:
                raise sqlite3.OperationalError('storage-runtime-support-not-installed')
        except BaseException:
            self._pg.close()
            raise

    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus
        return self._pg.info.transaction_status in (TransactionStatus.INTRANS, TransactionStatus.INERROR)

    def _begin(self, immediate=False):
        if self.in_transaction:
            raise sqlite3.OperationalError('cannot-start-transaction-within-transaction')
        sql = 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY' if self.readonly else 'BEGIN'
        self._pg.execute(sql)
        if immediate:
            self._lock_writer()

    def _lock_writer(self):
        import psycopg
        if self.readonly:
            raise sqlite3.OperationalError('attempt-to-write-readonly-storage')
        if not self._write_locked:
            try:
                self._pg.execute('SELECT pg_advisory_xact_lock(%s)', (advisory_lock_key(self.schema),))
            except psycopg.Error as error:
                # BEGIN IMMEDIATE also enters this path outside _execute's
                # statement savepoint. Never leave its aborted transaction
                # behind when the existing write retry starts a fresh turn.
                self.rollback()
                raise _operation_failure(error, context='writer_lock') from None
            self._write_locked = True

    def _empty(self, rows=(), names=()):
        return Cursor(self, rows, names)

    def _pragma(self, statement):
        match = re.fullmatch(r'PRAGMA\s+(\w+)(?:\s*=\s*([\w+-]+)|\s*\(\s*([\w\"]+)\s*\))?\s*;?', statement, re.I)
        if not match:
            raise sqlite3.NotSupportedError('unsupported-storage-pragma')
        name, value, table = match.groups()
        name = name.lower()
        if name == 'query_only':
            if value is not None:
                if value.upper() not in ('ON', 'OFF', '0', '1'):
                    raise sqlite3.NotSupportedError('invalid-query-only-value')
                enabled = value.upper() in ('ON', '1')
                if self.readonly and not enabled:
                    raise sqlite3.OperationalError('cannot-disable-readonly-storage')
                if enabled:
                    if self.in_transaction:
                        raise sqlite3.OperationalError('query-only-must-precede-transaction')
                    self.readonly = True
                    self._pg.execute('SET default_transaction_read_only=on')
            return self._empty([(int(self.readonly),)], ['query_only'])
        if name == 'busy_timeout':
            if value is not None:
                if not value.isdigit():
                    raise sqlite3.NotSupportedError('invalid-busy-timeout')
                self._timeout_ms = max(1, int(value))
                self._pg.execute("SELECT set_config('lock_timeout',%s,false)", (str(self._timeout_ms),))
            return self._empty([(self._timeout_ms,)], [name])
        if name in ('journal_mode', 'journal_size_limit', 'foreign_keys', 'wal_checkpoint'):
            allowed = {'journal_mode': {'WAL'}, 'foreign_keys': {'ON', '1'}, 'wal_checkpoint': set(), 'journal_size_limit': set()}
            if value is not None and name in ('journal_mode', 'foreign_keys') and value.upper() not in allowed[name]:
                raise sqlite3.NotSupportedError('unsupported-storage-pragma-value')
            if name == 'journal_mode':
                # Existing modules use this only for their SQLite setup branch;
                # the adapter reports its actual engine rather than fake WAL.
                return self._empty([('postgres',)], [name])
            if name == 'foreign_keys':
                return self._empty([(1,)], [name])
            return self._empty([(0, 0, 0)] if name == 'wal_checkpoint' else [(0,)], [name])
        if name == 'table_info' and table:
            spec = self.tables.get(table.strip('"').lower())
            if not spec:
                return self._empty()
            rows = [(index, column, kind.upper(), int(column in spec.primary_key), None,
                     spec.primary_key.index(column) + 1 if column in spec.primary_key else 0)
                    for index, (column, kind) in enumerate(zip(spec.columns, spec.types))]
            return self._empty(rows, ['cid', 'name', 'type', 'notnull', 'dflt_value', 'pk'])
        raise sqlite3.NotSupportedError('unsupported-storage-pragma')

    def _existing_ddl(self, statement):
        from sqlglot import exp, parse_one
        tree = parse_one(statement, read='sqlite')
        if not isinstance(tree, exp.Create) or not tree.args.get('exists') or tree.args.get('kind') not in ('TABLE', 'INDEX'):
            raise sqlite3.NotSupportedError('runtime-schema-changes-require-migration')
        if tree.args['kind'] == 'TABLE':
            target = tree.this.this
            spec = self.tables.get(target.name.lower())
            if spec is None:
                raise sqlite3.OperationalError('missing-migrated-storage-table')
            declared = [node.name for node in tree.this.expressions if isinstance(node, exp.ColumnDef)]
            if any(column.lower() not in {name.lower() for name in spec.columns} for column in declared):
                raise sqlite3.OperationalError('migrated-storage-schema-mismatch')
        else:
            index_name = tree.this.name
            row = self._pg.execute('SELECT 1 FROM pg_indexes WHERE schemaname=%s AND indexname=%s', (self.schema, index_name)).fetchone()
            if row is None:
                raise sqlite3.OperationalError('missing-migrated-storage-index')
        return self._empty()

    def execute(self, statement, parameters=()):
        import psycopg
        try:
            return self._execute(statement, parameters)
        except psycopg.Error as exc:
            # Control statements/PRAGMAs also fail transactionally; do not leave
            # the connection aborted or leak the driver's SQL/value diagnostics.
            if self.in_transaction:
                self.rollback()
            if isinstance(exc, psycopg.IntegrityError):
                raise sqlite3.IntegrityError('storage-integrity-constraint') from None
            raise _operation_failure(exc) from None

    def _execute(self, statement, parameters=()):
        import psycopg
        from psycopg.pq import TransactionStatus
        with self._mutex:
            if self._closed:
                raise sqlite3.ProgrammingError('connection-is-closed')
            clean = statement.strip()
            if re.match(r'^PRAGMA\b', clean, re.I):
                return self._pragma(clean)
            if re.fullmatch(r'BEGIN(?:\s+(?:IMMEDIATE|DEFERRED|EXCLUSIVE))?\s*;?', clean, re.I):
                self._begin(bool(re.search(r'IMMEDIATE|EXCLUSIVE', clean, re.I)))
                return self._empty()
            if re.fullmatch(r'COMMIT\s*;?', clean, re.I):
                self.commit()
                return self._empty()
            if re.fullmatch(r'ROLLBACK\s*;?', clean, re.I):
                self.rollback()
                return self._empty()
            if re.match(r'^CREATE\b', clean, re.I):
                return self._existing_ddl(clean)
            if re.fullmatch(r'SELECT\s+changes\(\)\s*;?', clean, re.I):
                return self._empty([(self._last_changes,)], ['changes()'])
            if re.fullmatch(r'SELECT\s+last_insert_rowid\(\)\s*;?', clean, re.I):
                return self._empty([(self._lastrowid or 0,)], ['last_insert_rowid()'])
            compiled = compile_sql(clean, parameters, self.tables)
            auto_commit = False
            savepoint = False
            try:
                if compiled.writes:
                    if self.readonly:
                        raise sqlite3.OperationalError('attempt-to-write-readonly-storage')
                    if not self.in_transaction:
                        self._begin()
                        auto_commit = self.isolation_level is None
                    self._lock_writer()
                if self.in_transaction:
                    self._pg.execute('SAVEPOINT _cliperx_statement')
                    savepoint = True
                cursor = self._pg.execute(compiled.sql, compiled.parameters or None)
                names = [column.name for column in cursor.description] if cursor.description else []
                rows = cursor.fetchall() if cursor.description else []
                rowcount = cursor.rowcount if compiled.writes else -1
                lastrowid = self._lastrowid
                if compiled.returning_rowid:
                    if rows:
                        lastrowid = rows[-1][0]
                        self._lastrowid = lastrowid
                    rows, names = [], []
                else:
                    for index, name in enumerate(names):
                        if name == 'body':
                            converted = []
                            for row in rows:
                                row = list(row)
                                value = row[index]
                                if isinstance(value, (bytes, bytearray, memoryview)):
                                    value = bytes(value)
                                    if value[:1] == b's':
                                        row[index] = value[1:].decode()
                                    elif value[:1] == b'b':
                                        row[index] = value[1:]
                                    else:
                                        raise sqlite3.DataError('invalid-projection-body-tag')
                                converted.append(tuple(row))
                            rows = converted
                if savepoint:
                    self._pg.execute('RELEASE SAVEPOINT _cliperx_statement')
                if compiled.writes:
                    self._last_changes = rowcount
                    self.total_changes += max(0, rowcount)
                if auto_commit:
                    self.commit()
                return Cursor(self, rows, names, rowcount=rowcount, lastrowid=lastrowid)
            except BaseException as exc:
                if isinstance(exc, psycopg.Error) and getattr(exc, 'sqlstate', None) in ('40001', '40P01'):
                    # These failures require a fresh transaction, including
                    # all writes preceding the failed statement.
                    self.rollback()
                elif auto_commit:
                    self.rollback()
                elif savepoint and self._pg.info.transaction_status != TransactionStatus.IDLE:
                    self._pg.execute('ROLLBACK TO SAVEPOINT _cliperx_statement')
                    self._pg.execute('RELEASE SAVEPOINT _cliperx_statement')
                elif self._pg.info.transaction_status == TransactionStatus.INERROR:
                    self.rollback()
                if isinstance(exc, psycopg.IntegrityError):
                    raise sqlite3.IntegrityError('storage-integrity-constraint') from None
                if isinstance(exc, psycopg.DataError):
                    raise sqlite3.DataError('storage-value-error') from None
                if isinstance(exc, psycopg.ProgrammingError):
                    raise sqlite3.OperationalError('unsupported-or-invalid-storage-query') from None
                if isinstance(exc, psycopg.Error):
                    raise _operation_failure(exc) from None
                raise

    def executemany(self, statement, parameters):
        count = 0
        last = None
        for values in parameters:
            cursor = self.execute(statement, values)
            count += max(0, cursor.rowcount)
            last = cursor.lastrowid
        return Cursor(self, rowcount=count, lastrowid=last)

    def executescript(self, statements):
        from sqlglot import parse
        # sqlite3.executescript commits its pending transaction before a script.
        self.commit()
        for tree in parse(statements, read='sqlite'):
            if tree is not None:
                self.execute(tree.sql(dialect='sqlite'))
        return self._empty()

    def execute_fetchall(self, statement, parameters=()):
        return self.execute(statement, parameters).fetchall()

    def commit(self):
        import psycopg
        with self._mutex:
            try:
                self._pg.commit()
            except psycopg.IntegrityError:
                self._pg.rollback()
                raise sqlite3.IntegrityError('storage-integrity-constraint-at-commit') from None
            except psycopg.Error as error:
                self._pg.rollback()
                raise _operation_failure(error, context='commit') from None
            finally:
                self._write_locked = False

    def rollback(self):
        with self._mutex:
            try:
                self._pg.rollback()
            finally:
                self._write_locked = False

    def close(self):
        with self._mutex:
            if not self._closed:
                self._pg.close()
                self._closed = True

    def set_progress_handler(self, callback, instructions):
        # PostgreSQL has server deadlines, no SQLite VM instruction counter.
        self._progress_handler = callback
        if callback is not None:
            self._pg.execute("SELECT set_config('statement_timeout',%s,false)", (str(self._timeout_ms),))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *args):
        self.rollback() if exc_type else self.commit()


def sync_connect(path, *, readonly=False, timeout=15, uri=False, isolation_level='', check_same_thread=True, **kwargs):
    config = _settings()
    if not config:
        if readonly:
            ordinary, uri_readonly = _ordinary_path(path, uri=uri)
            if not uri_readonly:
                path = Path(ordinary).as_uri() + '?mode=ro'
                uri = True
        return sqlite3.connect(path, timeout=timeout, uri=uri, isolation_level=isolation_level, check_same_thread=check_same_thread, **kwargs)
    if kwargs:
        raise sqlite3.NotSupportedError('unsupported-postgres-connect-option')
    _, uri_readonly = _ordinary_path(path, uri=uri)
    return Connection(config, domain_for_path(path, uri=uri), readonly=readonly or uri_readonly,
                      timeout=timeout, isolation_level=isolation_level)


connect = sync_connect


class _Operation:
    def __init__(self, coroutine):
        self._coroutine = coroutine
        self._result = None

    def __await__(self):
        return self._coroutine.__await__()

    async def __aenter__(self):
        self._result = await self._coroutine
        return self._result

    async def __aexit__(self, *args):
        if self._result is not None:
            await self._result.close()


class AsyncCursor:
    def __init__(self, connection, cursor):
        self.connection = connection
        self._cursor = cursor

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def description(self):
        return self._cursor.description

    @property
    def arraysize(self):
        return self._cursor.arraysize

    @arraysize.setter
    def arraysize(self, value):
        self._cursor.arraysize = value

    async def fetchone(self):
        return await self.connection._run(self._cursor.fetchone)

    async def fetchall(self):
        return await self.connection._run(self._cursor.fetchall)

    async def fetchmany(self, size=None):
        return await self.connection._run(self._cursor.fetchmany, size)

    async def close(self):
        await self.connection._run(self._cursor.close)

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.fetchone()
        if value is None:
            raise StopAsyncIteration
        return value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


class AsyncConnection:
    backend = 'postgres'

    def __init__(self, path, kwargs):
        self._path, self._kwargs = path, kwargs
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='cliperx-storage')
        self._connection = None
        self._opening = None
        self._row_factory = None
        self._closed = False

    async def _open(self):
        if self._connection is None:
            if self._opening is None:
                loop = asyncio.get_running_loop()
                self._opening = loop.run_in_executor(self._executor, lambda: sync_connect(self._path, **self._kwargs))
            self._connection = await asyncio.shield(self._opening)
            self._connection.row_factory = self._row_factory
        return self

    def __await__(self):
        return self._open().__await__()

    async def _run(self, function, *args):
        if self._closed:
            raise sqlite3.ProgrammingError('connection-is-closed')
        loop = asyncio.get_running_loop()
        # Cancellation must not abandon a still-running write whose caller will
        # then release its application writer lock. Drain the database operation.
        future = loop.run_in_executor(self._executor, lambda: function(*args))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(future)
            finally:
                raise

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._row_factory = value
        if self._connection is not None:
            self._connection.row_factory = value

    @property
    def in_transaction(self):
        return bool(self._connection and self._connection.in_transaction)

    @property
    def total_changes(self):
        return self._connection.total_changes if self._connection else 0

    @property
    def schema(self):
        return self._connection.schema if self._connection else None

    def execute(self, statement, parameters=()):
        async def operation():
            await self._open()
            return AsyncCursor(self, await self._run(self._connection.execute, statement, parameters))
        return _Operation(operation())

    def executemany(self, statement, parameters):
        async def operation():
            await self._open()
            return AsyncCursor(self, await self._run(self._connection.executemany, statement, parameters))
        return _Operation(operation())

    def executescript(self, statements):
        async def operation():
            await self._open()
            return AsyncCursor(self, await self._run(self._connection.executescript, statements))
        return _Operation(operation())

    async def execute_fetchall(self, statement, parameters=()):
        cursor = await self.execute(statement, parameters)
        return await cursor.fetchall()

    async def commit(self):
        await self._open()
        await self._run(self._connection.commit)

    async def rollback(self):
        await self._open()
        await self._run(self._connection.rollback)

    async def set_progress_handler(self, callback, instructions):
        await self._open()
        await self._run(self._connection.set_progress_handler, callback, instructions)

    async def close(self):
        if self._closed:
            return
        try:
            if self._connection is None and self._opening is not None:
                self._connection = await asyncio.shield(self._opening)
            if self._connection is not None:
                await self._run(self._connection.close)
        finally:
            self._closed = True
            self._executor.shutdown(wait=False)

    async def __aenter__(self):
        return await self._open()

    async def __aexit__(self, *args):
        await self.close()


def async_connect(path, *, readonly=False, timeout=15, uri=False, isolation_level='', **kwargs):
    if not _settings():
        import aiosqlite
        if readonly:
            ordinary, uri_readonly = _ordinary_path(path, uri=uri)
            if not uri_readonly:
                path, uri = Path(ordinary).as_uri() + '?mode=ro', True
        return aiosqlite.connect(path, timeout=timeout, uri=uri, isolation_level=isolation_level, **kwargs)
    domain_for_path(path, uri=uri)
    return AsyncConnection(path, dict(readonly=readonly, timeout=timeout, uri=uri, isolation_level=isolation_level, **kwargs))
