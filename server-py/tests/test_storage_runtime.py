import asyncio
import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from app import storage_runtime as runtime
from app.postgres_sql import Table, compile_sql


TABLES = {
    'facts': Table('facts', ('kind', 'id', 'body'), ('text', 'text', 'text'), ('kind', 'id')),
    'trades': Table('trades', ('asset', 'id', 't', 'body'), ('text', 'text', 'bigint', 'text'), ('asset', 'id')),
    'candles': Table('candles', ('asset', 'bar', 'openTime', 'close', 'confirmed'), ('text', 'text', 'bigint', 'double precision', 'bigint'), ('asset', 'bar', 'openTime')),
    'realtime_events': Table('realtime_events', ('id', 'event', 'body', 'at'), ('bigint', 'text', 'text', 'bigint'), ('id',), ('id',)),
    'projection_dirty': Table('projection_dirty', ('chain', 'token', 'generation'), ('text', 'text', 'bigint'), ('chain', 'token')),
}


def test_sql_parameters_literals_and_percent_are_not_rewritten():
    compiled = compile_sql("SELECT '? rowid INDEXED BY x',id FROM facts WHERE id LIKE '%s%' AND body=?", ('x',), TABLES)
    assert "'? rowid INDEXED BY x'" in compiled.sql
    assert "'%%s%%'" in compiled.sql
    assert '%(__cliperx_arg_0)s' in compiled.sql
    assert compiled.parameters == {'__cliperx_arg_0': 'x'}


def test_hidden_rowids_and_implicit_insert_columns():
    sql = compile_sql('SELECT rowid,* FROM candles INDEXED BY candles_open_time ORDER BY openTime,rowid', (), TABLES).sql
    assert 'INDEXED' not in sql
    assert '"openTime"' in sql
    assert sql.count('"_cliperx_source_rowid"') == 2
    sql = compile_sql('INSERT OR IGNORE INTO facts VALUES (?,?,?)', ('a', 'b', 'c'), TABLES).sql
    assert '("kind", "id", "body")' in sql
    assert 'ON CONFLICT DO NOTHING' in sql
    assert 'RETURNING "_cliperx_source_rowid"' in sql
    assert 'RETURNING "id"' in compile_sql('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)', ('x', '{}', 0), TABLES).sql


def test_json_context_numeric_cast_null_safe_and_scalar_max():
    sql = compile_sql("SELECT SUM(json_extract(body,'$.type')='buy'),SUM(CASE WHEN json_type(body,'$.volume') IN ('integer','real') THEN json_extract(body,'$.volume') END),CAST(json_extract(body,'$.long') AS INTEGER) FROM trades", (), TABLES).sql
    assert 'SUM(CAST(' in sql
    assert '_CLIPERX_SQLITE_NUM(' in sql
    assert '_CLIPERX_SQLITE_INT(JSONB_EXTRACT_PATH_TEXT' in sql
    assert '_CLIPERX_SQLITE_JSON_TYPE(' in sql
    sql = compile_sql('UPDATE projection_dirty SET generation=max(generation,?) WHERE generation IS NOT ?', (1, None), TABLES).sql
    assert 'GREATEST(' in sql and 'IS DISTINCT FROM' in sql
    sql = compile_sql('INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body WHERE facts.body IS NOT excluded.body', ('k', 'i', '{}'), TABLES).sql
    assert 'WHERE "facts"."body" IS DISTINCT FROM "excluded"."body"' in sql
    sql = compile_sql('INSERT INTO projection_dirty VALUES (?,?,?) ON CONFLICT(chain,token) DO UPDATE SET generation=MAX(generation,excluded.generation)', ('c', 't', 2), TABLES).sql
    assert 'GREATEST("projection_dirty"."generation", "excluded"."generation")' in sql
    assert 'DO UPDATE SET "generation" =' in sql
    assert 'DO UPDATE SET "projection_dirty"."generation"' not in sql


@pytest.mark.parametrize('statement', ['SELECT * FROM public.facts', 'SELECT * FROM unknown', 'VACUUM', 'ATTACH DATABASE ? AS x', 'SELECT json_extract(body,?) FROM facts'])
def test_unknown_commands_and_domains_fail_closed(statement):
    arguments = ('x',) if '?' in statement else ()
    with pytest.raises((sqlite3.NotSupportedError, sqlite3.OperationalError)):
        compile_sql(statement, arguments, TABLES)


def test_default_sqlite_and_row_protocol(tmp_path, monkeypatch):
    monkeypatch.setenv('ARCHITECTURE_SETTINGS', str(tmp_path/'missing.json'))
    connection = runtime.sync_connect(':memory:')
    assert isinstance(connection, sqlite3.Connection)
    connection.close()
    row = runtime.Row(('CamelCase', 'id'), (42, 'u'))
    assert row[0] == row['camelcase'] == 42
    assert list(row) == [42, 'u']
    assert dict(row) == {'CamelCase': 42, 'id': 'u'}


def test_enable_switch_rejects_unknown_path_and_readonly_uri(tmp_path, monkeypatch):
    config = {'managedBy': 'cliperx-architecture-v1', 'dsn': 'not-used', 'storage': {'enabled': True, 'backend': 'postgres', 'domainSchemas': runtime._SCHEMAS}}
    path = tmp_path/'runtime.json'; path.write_text(json.dumps(config))
    monkeypatch.setenv('ARCHITECTURE_SETTINGS', str(path))
    monkeypatch.setenv('RESEARCH_DB', str(tmp_path/'research.sqlite'))
    assert runtime.is_postgres_path(tmp_path/'research.sqlite')
    with pytest.raises(sqlite3.OperationalError):
        runtime.sync_connect(tmp_path/'unknown.sqlite')
    assert runtime._ordinary_path((tmp_path/'research.sqlite').as_uri()+'?mode=ro', uri=True)[1]
    monkeypatch.setenv('LIVE_MARKET_DB', str(tmp_path/'research.sqlite'))
    with pytest.raises(sqlite3.OperationalError):
        runtime.is_postgres_path(tmp_path/'research.sqlite')


def test_only_market_has_optional_separate_dsn_route():
    config = {'dsn': 'private-main', 'storageDsns': {'market': 'private-market'}}
    assert runtime.dsn_for_domain(config, 'market') == 'private-market'
    assert runtime.dsn_for_domain(config, 'research') == 'private-main'
    assert runtime.dsn_for_domain({'dsn': 'private-main'}, 'market') == 'private-main'
    with pytest.raises(sqlite3.OperationalError):
        runtime.dsn_for_domain({'dsn': 'private-main', 'storageDsns': {'accounts': 'other'}}, 'accounts')


def test_connection_diagnostics_identify_capacity_without_leaking_private_details():
    class DriverError(Exception):
        sqlstate = '53300'
        errno = None
    error = DriverError('postgresql://private-user:private-password@private-host/private-db too many clients already')
    result = runtime._connection_failure(error)
    assert isinstance(result, sqlite3.OperationalError)
    assert result.postgres_diagnostic['sqlstate'] == '53300'
    assert result.postgres_diagnostic['category'] == 'connection_capacity'
    assert 'private-' not in str(result)
    assert 'private-' not in json.dumps(result.postgres_diagnostic)
    # libpq often drops FATAL SQLSTATE at the initial connect boundary.
    result = runtime._connection_failure(Exception('connection to private-host failed: FATAL: remaining connection slots are reserved'))
    assert result.postgres_diagnostic['category'] == 'connection_capacity'
    assert result.postgres_diagnostic['sqlstate'] is None
    assert 'private-' not in str(result)


@pytest.mark.parametrize('state,context,busy,category', [
    ('55P03', 'operation', True, 'lock_contention'),
    ('40001', 'operation', True, 'serialization_retry'),
    ('40P01', 'commit', True, 'deadlock_retry'),
    ('57014', 'writer_lock', True, 'statement_timeout'),
    ('57014', 'operation', False, 'statement_timeout'),
    ('57014', 'commit', False, 'statement_timeout'),
])
def test_write_diagnostics_retry_only_contention_without_private_details(state, context, busy, category):
    from app.db import _is_transient_write_busy
    class DriverError(Exception):
        sqlstate = state
    result = runtime._operation_failure(
        DriverError('private-password private-host private-query private-bind'), context=context)
    assert result.postgres_diagnostic['category'] == category
    assert result.postgres_diagnostic['sqlstate'] == state
    assert _is_transient_write_busy(result) == busy
    assert getattr(result, 'sqlite_errorcode', None) == (sqlite3.SQLITE_BUSY if busy else None)
    assert 'private-' not in str(result) + json.dumps(result.postgres_diagnostic)


@pytest.fixture
def postgres_runtime(tmp_path, monkeypatch):
    settings = os.environ.get('MIGRATION_TEST_SETTINGS')
    if not settings:
        pytest.skip('Real PostgreSQL fixture needs MIGRATION_TEST_SETTINGS')
    from app.storage_migration import ShadowMigration
    from app.storage_schema import postgres_runtime_schema_sql
    config = json.loads(Path(settings).read_text())
    source = tmp_path/'research.sqlite'
    with sqlite3.connect(source) as connection:
        connection.executescript('''
          CREATE TABLE facts(kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(kind,id));
          CREATE TABLE trades(asset TEXT NOT NULL,id TEXT NOT NULL,t INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(asset,id));
          CREATE TABLE candles(asset TEXT NOT NULL,bar TEXT NOT NULL,openTime INTEGER NOT NULL,close REAL,confirmed INTEGER,PRIMARY KEY(asset,bar,openTime));
          CREATE TABLE realtime_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event TEXT,body TEXT,at INTEGER);
          CREATE TABLE dashboard_projection(name TEXT PRIMARY KEY,body TEXT NOT NULL);
          CREATE TABLE projection_dirty(chain TEXT NOT NULL,token TEXT NOT NULL,generation INTEGER NOT NULL,PRIMARY KEY(chain,token));
        ''')
        connection.execute("INSERT INTO realtime_events(event,body,at) VALUES ('seed','{}',0)")
        connection.execute("INSERT INTO dashboard_projection VALUES ('text','中文🙂')")
        connection.execute('INSERT INTO dashboard_projection VALUES (?,?)', ('blob', b'\x00\xff'))
    schema = 'cliperx_storage_runtime_' + uuid.uuid4().hex[:20]
    schemas = {**runtime._SCHEMAS, 'research': schema}
    monkeypatch.setattr(runtime, '_SCHEMAS', schemas)
    monkeypatch.setenv('RESEARCH_DB', str(source))
    instance = ShadowMigration(config, source, schema, batch_rows=2)
    try:
        instance.prepare()
        for name in instance.tables:
            list(instance.backfill(name))
        instance.verify()
        with instance.pg.transaction():
            instance.pg.execute(runtime.runtime_support_sql(schema), prepare=False)
            instance.pg.execute(postgres_runtime_schema_sql(schema, list(instance.tables), realtime_outbox=False, identity_tables=('realtime_events',)), prepare=False)
        private = tmp_path/'private-runtime.json'
        private.write_text(json.dumps({**config, 'managedBy': 'cliperx-architecture-v1', 'storage': {'enabled': True, 'backend': 'postgres', 'domainSchemas': schemas}}))
        private.chmod(0o600)
        monkeypatch.setenv('ARCHITECTURE_SETTINGS', str(private))
        yield source, instance, schema
    finally:
        instance.pg.execute('DROP SCHEMA "' + schema + '" CASCADE')
        instance.close()
        private_path = tmp_path/'private-runtime.json'
        private_path.unlink(missing_ok=True)


def test_real_transaction_recovery_row_factory_and_isolation(postgres_runtime):
    source, _, _ = postgres_runtime
    connection = runtime.sync_connect(source)
    reader = runtime.sync_connect(source, readonly=True)
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('INSERT INTO facts VALUES (?,?,?)', ('kind', 'id', '{}'))
        assert reader.execute('SELECT count(*) FROM facts').fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('INSERT INTO facts VALUES (?,?,?)', ('kind', 'id', '{}'))
        assert connection.in_transaction
        assert connection.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
        connection.commit()
        reader.row_factory = sqlite3.Row
        row = reader.execute('SELECT * FROM facts').fetchone()
        assert dict(row) == {'kind': 'kind', 'id': 'id', 'body': '{}'}
        assert len(row) == 3
        with pytest.raises(sqlite3.OperationalError):
            reader.execute('DELETE FROM facts')
        reader.execute('BEGIN')
        assert reader.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
        connection.execute('INSERT INTO facts VALUES (?,?,?)', ('kind', 'next', '{}'))
        connection.commit()
        assert reader.execute('SELECT count(*) FROM facts').fetchone()[0] == 1
        reader.rollback()
        assert reader.execute('SELECT count(*) FROM facts').fetchone()[0] == 2
    finally:
        connection.close(); reader.close()


@pytest.mark.parametrize('statement_deadline', [False, True])
def test_real_writer_lock_deadline_is_busy_and_retries_with_fresh_transaction(postgres_runtime, statement_deadline):
    source, _, _ = postgres_runtime
    owner = runtime.sync_connect(source)
    contender = runtime.sync_connect(source, timeout=.15)
    try:
        owner.execute('BEGIN IMMEDIATE')
        if statement_deadline:
            contender._pg.execute('SET lock_timeout=0')
            contender._pg.execute('SET statement_timeout=150')
        with pytest.raises(sqlite3.OperationalError) as failure:
            contender.execute('BEGIN IMMEDIATE')
        assert failure.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert failure.value.postgres_diagnostic['sqlstate'] == ('57014' if statement_deadline else '55P03')
        assert not contender.in_transaction
        owner.rollback()
        contender.execute('BEGIN IMMEDIATE')
        contender.execute('INSERT INTO facts VALUES (?,?,?)', ('test', 'retry-success', '{}'))
        contender.commit()
        assert owner.execute("SELECT count(*) FROM facts WHERE id='retry-success'").fetchone()[0] == 1
    finally:
        contender.close()
        owner.close()


def test_real_actual_json_activity_queries_and_large_integer(postgres_runtime):
    source, _, _ = postgres_runtime
    connection = runtime.sync_connect(source)
    try:
        rows = [('a', 'one', 1, json.dumps({'type': 'buy', 'volume': 1.25, 'user': 'a', 'long': 2**62})),
                ('a', 'two', 2, json.dumps({'type': 'sell', 'volume': 2, 'user': 0})),
                ('a', 'three', 3, json.dumps({'type': 'buy', 'volume': None, 'user': ''}))]
        connection.executemany('INSERT INTO trades VALUES (?,?,?,?)', rows)
        query = '''SELECT SUM(json_extract(body,'$.type')='buy'),SUM(json_extract(body,'$.type')='sell'),
          SUM(CASE WHEN json_type(body,'$.volume') IN ('integer','real') THEN json_extract(body,'$.volume') END),
          COUNT(DISTINCT CASE WHEN json_extract(body,'$.user') NOT IN ('',0) THEN json_extract(body,'$.user') END) FROM trades'''
        assert connection.execute(query).fetchone() == (2, 1, 3.25, 1)
        assert connection.execute("SELECT CAST(json_extract(body,'$.long') AS INTEGER) FROM trades WHERE id='one'").fetchone()[0] == 2**62
        connection.execute('INSERT INTO facts VALUES (?,?,?)', ('k', 'i', '{}'))
        connection.execute("UPDATE facts SET body=json_set(body,'$.name',?,'$.n',?)", ('中文', 3))
        assert json.loads(connection.execute('SELECT body FROM facts').fetchone()[0]) == {'name': '中文', 'n': 3}
        connection.execute('INSERT INTO projection_dirty VALUES (?,?,?)', ('c', 't', 5))
        connection.execute('INSERT INTO projection_dirty VALUES (?,?,?) ON CONFLICT(chain,token) DO UPDATE SET generation=MAX(generation,excluded.generation)', ('c', 't', 2))
        assert connection.execute('SELECT generation FROM projection_dirty').fetchone()[0] == 5
    finally:
        connection.close()


def test_real_projection_mixed_body_and_event_lastrowid(postgres_runtime):
    source, _, _ = postgres_runtime
    connection = runtime.sync_connect(source)
    try:
        assert connection.execute("SELECT body FROM dashboard_projection WHERE name='text'").fetchone()[0] == '中文🙂'
        assert connection.execute("SELECT body FROM dashboard_projection WHERE name='blob'").fetchone()[0] == b'\x00\xff'
        connection.execute('INSERT INTO dashboard_projection VALUES (?,?)', ('new', b'compressed'))
        assert connection.execute("SELECT body FROM dashboard_projection WHERE name='new'").fetchone()[0] == b'compressed'
        rowid = connection.execute('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)', ('new', '{}', 1)).lastrowid
        assert rowid == 2
        connection.execute('INSERT INTO realtime_events VALUES (NULL,?,?,?)', ('new', '{}', 2))
        assert connection.execute('SELECT max(id) FROM realtime_events').fetchone()[0] == 3
        assert connection.execute('SELECT rowid,id FROM realtime_events WHERE id=?', (rowid,)).fetchone() == (rowid, rowid)
        connection.commit()
    finally:
        connection.close()


def test_real_async_await_context_cursor_and_rollback(postgres_runtime):
    source, _, _ = postgres_runtime

    async def exercise():
        async with runtime.async_connect(source) as connection:
            connection.row_factory = sqlite3.Row
            await connection.execute('BEGIN IMMEDIATE')
            await connection.executemany('INSERT INTO facts VALUES (?,?,?)', [('k', '1', '{}'), ('k', '2', '{}')])
            async with connection.execute('SELECT * FROM facts ORDER BY id') as cursor:
                rows = [dict(row) async for row in cursor]
                assert [row['id'] for row in rows] == ['1', '2']
            assert connection.total_changes == 2
            await connection.rollback()
            assert (await connection.execute_fetchall('SELECT count(*) FROM facts'))[0][0] == 0
        connection = await runtime.async_connect(source, readonly=True)
        await connection.close()

    asyncio.run(exercise())
