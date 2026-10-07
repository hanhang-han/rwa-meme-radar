"""Real PostgreSQL checks for the research/native authority cutover.

Only disposable owned schemas are changed. The fixture is enabled on the
production test host with its existing private settings path, never a DSN.
"""
import asyncio
import json
import os
import sqlite3
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.db import ResearchStore, SCHEMA, WriterLock
from app.live_market_store import CatalogueReadStore, LiveMarketStore, RAW_LOG_SCHEMA
from app.realtime_schema import REALTIME_SCHEMA, enqueue_event, trigger_schema
from app.storage_migration import ShadowMigration
from app import storage_runtime
from app import db as research_db
from app.storage_runtime import async_connect, runtime_support_sql
from app.storage_schema import postgres_runtime_schema_sql


@pytest.fixture
def core_pg(tmp_path, monkeypatch):
    settings = os.environ.get('MIGRATION_TEST_SETTINGS')
    if not settings:
        pytest.skip('Real PostgreSQL core fixture needs MIGRATION_TEST_SETTINGS')
    config = json.loads(Path(settings).read_text())
    sources = {'research': tmp_path / 'research.sqlite', 'market': tmp_path / 'live-market.sqlite'}
    schemas = {domain: 'cliperx_storage_test_' + uuid.uuid4().hex for domain in sources}
    monkeypatch.setattr(storage_runtime, '_SCHEMAS', {**storage_runtime._SCHEMAS, **schemas})
    migrations = []
    try:
        for domain, path in sources.items():
            with sqlite3.connect(path) as connection:
                connection.executescript(SCHEMA + REALTIME_SCHEMA + RAW_LOG_SCHEMA)
                if domain == 'research':
                    connection.executescript(trigger_schema())
                else:
                    connection.execute('CREATE TABLE live_market_identity(name TEXT PRIMARY KEY,value TEXT NOT NULL)')
            migration = ShadowMigration(config, path, schemas[domain], batch_rows=10)
            migrations.append(migration)
            migration.prepare()
            for table in migration.tables:
                list(migration.backfill(table))
            assert all(row['match'] for row in migration.verify()['tables'])
            migration.pg.execute(runtime_support_sql(schemas[domain]))
            migration.pg.execute(postgres_runtime_schema_sql(schemas[domain], migration.tables,
                                                             realtime_outbox=domain == 'research'))
        runtime = {**config, 'storage': {'enabled': True, 'backend': 'postgres', 'domainSchemas': dict(storage_runtime._SCHEMAS)}}
        runtime_path = tmp_path / 'runtime.json'
        runtime_path.write_text(json.dumps(runtime))
        runtime_path.chmod(0o600)
        monkeypatch.setenv('ARCHITECTURE_SETTINGS', str(runtime_path))
        monkeypatch.setenv('RESEARCH_DB', str(sources['research']))
        monkeypatch.setenv('LIVE_MARKET_DB', str(sources['market']))
        monkeypatch.setattr(research_db, 'DB_PATH', str(sources['research']))
        yield sources, schemas
    finally:
        for migration in migrations:
            migration.pg.execute('DROP SCHEMA "' + migration.schema + '" CASCADE')
            migration.close()


def test_postgres_research_facts_outbox_activity_and_candle_corrections(core_pg):
    sources, _ = core_pg
    async def check():
        store = await ResearchStore(str(sources['research']), '196', write_lock=WriterLock()).connect()
        try:
            assert store.db.backend == 'postgres'
            value = {'symbol': '中文🙂', 'price': .000000000000012345, 'missing': None, 'zero': 0}
            await store.put('asset', 'token', value)
            assert await store.get('asset', 'token') == value
            first = (await store.fetchone('SELECT COUNT(*) FROM change_outbox'))[0]
            await store.put('asset', 'token', value)
            await store.put('collector-job', 'ignored', {'done': True})
            assert (await store.fetchone('SELECT COUNT(*) FROM change_outbox'))[0] == first
            await store.put_trades('token', [
                {'id': 'buy', 't': 1_790_000_000_000, 'type': 'buy', 'volume': 2.5, 'user': 'u'},
                {'id': 'sell', 't': 1_790_000_000_001, 'type': 'sell', 'volume': 3, 'user': 'u'},
            ])
            assert await store.activity('token', 0) == {'buys': 1, 'sells': 1, 'volume': 5.5, 'count': 2, 'traders': 1}
            candle = {'t': 1_790_000_000_000, 'o': .000001, 'h': .000002, 'l': .0000001,
                      'c': .0000015, 'v': None, 'vu': 0, 'confirmed': True}
            await store.put_candles('token', '1m', [candle])
            count = (await store.fetchone('SELECT COUNT(*) FROM change_outbox'))[0]
            await store.put_candles('token', '1m', [candle])
            await store.put_candles('token', '1m', [{**candle, 'c': .9, 'confirmed': False}])
            assert (await store.fetchone('SELECT COUNT(*) FROM change_outbox'))[0] == count
            assert (await store.candle_range('token', '1m'))[-1]['c'] == candle['c']
            await store.put_candles('token', '1m', [{**candle, 'c': .0000016}])
            assert (await store.fetchone('SELECT COUNT(*) FROM change_outbox'))[0] == count + 1
        finally:
            await store.close()
    asyncio.run(check())


def test_postgres_fact_event_and_outbox_rollback_together(core_pg):
    sources, _ = core_pg
    async def check():
        store = await ResearchStore(str(sources['research']), '196', write_lock=WriterLock()).connect()
        try:
            await store.db.execute('BEGIN IMMEDIATE')
            await store.db.execute('INSERT INTO facts VALUES (?,?,?)', ('196:asset', 'rollback', '{}'))
            await enqueue_event(store.db, 'price', {'price': 12})
            await store.db.rollback()
            assert await store.get('asset', 'rollback') is None
            assert (await store.fetchone('SELECT COUNT(*) FROM change_outbox'))[0] == 0
            assert (await store.fetchone('SELECT COUNT(*) FROM realtime_events'))[0] == 0
        finally:
            await store.close()
    asyncio.run(check())


def test_postgres_outbox_maintenance_is_bounded_and_does_not_block_publication(core_pg, monkeypatch):
    from app import realtime_projection as projection
    sources, _ = core_pg
    path = str(sources['research'])
    monkeypatch.setattr(projection, 'bridge_shared_sources', AsyncMock())
    monkeypatch.setattr(research_db, '_stores', {})
    monkeypatch.setattr(projection, '_tick_lock', asyncio.Lock())
    for name in ('_committed_projection', '_committed_facts', '_committed_page_views'):
        monkeypatch.setattr(projection, name, None)
    monkeypatch.setattr(projection, 'SHARED_FILES', ())
    monkeypatch.setattr('app.state._source_statuses', lambda *args: [])

    async def check():
        scoped = await research_db.store('196')
        try:
            await scoped.db.executemany(
                'INSERT INTO change_outbox(kind,entity,operation,at) VALUES (?,?,?,?)',
                [('candle', '196:test', 'update', 1)] * 1250)
            await scoped.db.commit()
            # Only the disposable schema receives an intentionally slow row
            # trigger. The former publication DELETE times out on this data.
            owner = storage_runtime.sync_connect(path)
            try:
                owner._pg.execute('''CREATE FUNCTION _test_slow_prune() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN PERFORM pg_sleep(.003); RETURN OLD; END $$''')
                owner._pg.execute('''CREATE TRIGGER _test_slow_prune BEFORE DELETE ON change_outbox
                    FOR EACH ROW EXECUTE FUNCTION _test_slow_prune()''')
            finally:
                owner.close()
            async with async_connect(path, timeout=.25) as old_path:
                with pytest.raises(sqlite3.OperationalError) as failure:
                    await old_path.execute('DELETE FROM change_outbox WHERE id<=?', (1250,))
                assert failure.value.postgres_diagnostic['sqlstate'] == '57014'
                assert getattr(failure.value, 'sqlite_errorcode', None) is None
                await old_path.rollback()
            full = await projection.projection_tick(force=True)
            assert full['changed']
            assert full['inputCursor'] == 1250
            assert (await scoped.fetchone('SELECT COUNT(*) FROM change_outbox'))[0] == 1250
            # Append changes after publication; pruning must leave them all.
            await scoped.db.executemany(
                'INSERT INTO change_outbox(kind,entity,operation,at) VALUES (?,?,?,?)',
                [('candle', '196:new', 'update', 1)] * 10)
            await scoped.db.commit()
            outcome = await projection.prune_consumed_outbox()
            assert outcome['updated'] == 64
            assert outcome['inputCursor'] == 1250
            assert (await scoped.fetchone('SELECT MIN(id) FROM change_outbox'))[0] == 65
            assert (await scoped.fetchone('SELECT COUNT(*) FROM change_outbox WHERE id>1250'))[0] == 10
            tape = await projection.projection_tick()
            assert not tape['changed']
            assert tape['inputCursor'] == 1260
            assert tape['revision'] == full['revision']
        finally:
            await research_db.close_all()
    asyncio.run(check())


def test_postgres_read_snapshot_is_repeatable_and_catalogue_is_readonly(core_pg):
    sources, _ = core_pg
    async def check():
        path = str(sources['research'])
        writer = await ResearchStore(path, '196', write_lock=WriterLock()).connect()
        catalogue = await CatalogueReadStore(path, '196', write_lock=WriterLock()).connect()
        try:
            await writer.put('asset', 'token', {'n': 1})
            async with async_connect(path, readonly=True) as reader:
                await reader.execute('BEGIN')
                before = await reader.execute_fetchall("SELECT body FROM facts WHERE id='token'")
                await writer.put('asset', 'token', {'n': 2})
                assert [tuple(row) for row in await reader.execute_fetchall("SELECT body FROM facts WHERE id='token'")] == [tuple(row) for row in before]
                await reader.rollback()
                assert json.loads((await reader.execute_fetchall("SELECT body FROM facts WHERE id='token'"))[0][0]) == {'n': 2}
            assert await catalogue.get('asset', 'token') == {'n': 2}
            with pytest.raises(PermissionError):
                await catalogue.put('asset', 'token', {'n': 3})
        finally:
            await catalogue.close()
            await writer.close()
    asyncio.run(check())


def test_postgres_native_writes_are_isolated_and_do_not_create_research_outbox(core_pg):
    sources, _ = core_pg
    async def check():
        research = await ResearchStore(str(sources['research']), '196', write_lock=WriterLock()).connect()
        native = await LiveMarketStore(str(sources['market']), '196', write_lock=WriterLock()).connect()
        try:
            await native.put('asset', 'native-only', {'price': 1})
            assert await research.get('asset', 'native-only') is None
            assert (await native.fetchone('SELECT COUNT(*) FROM change_outbox'))[0] == 0
            await research.db.execute('BEGIN IMMEDIATE')
            await research.db.execute('INSERT INTO facts VALUES (?,?,?)', ('196:asset', 'locked', '{}'))
            # Different domain locks must keep native writes available while
            # a research write transaction is pending.
            await asyncio.wait_for(native.put('asset', 'independent', {'price': 2}), 2)
            await research.db.rollback()
        finally:
            await native.close()
            await research.close()
    asyncio.run(check())


def test_postgres_event_ids_follow_commit_order_and_rowid_alias(core_pg):
    sources, _ = core_pg
    async def check():
        path = str(sources['research'])
        async with async_connect(path) as first, async_connect(path) as second, async_connect(path, readonly=True) as reader:
            await first.execute('BEGIN IMMEDIATE')
            seq1 = await enqueue_event(first, 'price', {'n': 1})
            waiting = asyncio.create_task(enqueue_event(second, 'price', {'n': 2}))
            try:
                await asyncio.sleep(.1)
                assert not waiting.done()
                assert (await reader.execute_fetchall('SELECT COALESCE(MAX(id),0) FROM realtime_events'))[0][0] == 0
                await first.commit()
                seq2 = await asyncio.wait_for(waiting, 2)
                await second.commit()
                assert seq2 > seq1
                assert [tuple(row) for row in await reader.execute_fetchall('SELECT id,rowid FROM realtime_events ORDER BY id')] == [(seq1, seq1), (seq2, seq2)]
            finally:
                if not waiting.done():
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
                await first.rollback()
                await second.rollback()
    asyncio.run(check())


def test_postgres_read_model_bridge_tracks_authoritative_publication(core_pg):
    sources, schemas = core_pg
    from app.read_model_worker import capture_publication
    from app.realtime_projection import _encode_snapshot
    async def publish(revision):
        async with async_connect(str(sources['research'])) as connection:
            await connection.execute('BEGIN IMMEDIATE')
            await connection.executemany('''INSERT INTO dashboard_projection VALUES (?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,cursor=excluded.cursor,
                input_cursor=excluded.input_cursor,body=excluded.body,built_at=excluded.built_at''',
                [(name, revision, 0, 0, _encode_snapshot(json.dumps({'revision': revision})), revision)
                 for name in ('full', 'overview', 'market', 'feed')])
            await connection.commit()
    asyncio.run(publish(1))
    signature, publication = capture_publication(sources['research'])
    assert publication[0]['sourceEpoch'] == 'postgres:' + schemas['research']
    assert all(meta['revision'] == 1 for meta in publication[0]['views'].values())
    assert capture_publication(sources['research'], signature) == (signature, None)
    asyncio.run(publish(2))
    newer, publication = capture_publication(sources['research'], signature)
    assert newer != signature
    assert all(meta['revision'] == 2 for meta in publication[0]['views'].values())


def test_postgres_sse_replays_without_synchronous_database_calls_on_event_loop(core_pg, monkeypatch):
    sources, _ = core_pg
    from app import stream_hub as hub
    from app.api import stream as stream_api
    import threading
    token = '0x' + '1' * 40
    monkeypatch.setenv('STREAM_LEDGER_PATH', str(sources['research']))
    monkeypatch.setattr(hub, '_db', None)
    monkeypatch.setattr(hub, '_clients', set())
    original_cursor = hub.cursor
    def threaded_cursor():
        assert threading.current_thread() is not threading.main_thread()
        return original_cursor()
    monkeypatch.setattr(hub, 'cursor', threaded_cursor)
    def prohibited(*args, **kwargs):
        raise AssertionError('synchronous journal read on SSE event loop')
    monkeypatch.setattr(stream_api, 'cursor', prohibited)
    monkeypatch.setattr(stream_api, 'replay_page', prohibited)
    monkeypatch.setattr(stream_api, 'replay_batch', prohibited)
    async def check():
        async with async_connect(str(sources['research'])) as writer:
            await enqueue_event(writer, 'price', {'chainId': '196', 'token': token, 'price': 2})
            await writer.commit()
        response = await stream_api.get_stream(protocol=2, scope='quotes', tokens='196:' + token,
                                               trades='none', candles='none', snapshot=False, after='0')
        received = []
        try:
            while True:
                frame = await asyncio.wait_for(anext(response.body_iterator), 2)
                received.append(frame)
                if b'event: hello\n' in frame:
                    break
        finally:
            await response.body_iterator.aclose()
        assert any(b'event: price\n' in frame and b'"price":2' in frame for frame in received)
        assert not hub.clients()
    try:
        asyncio.run(check())
    finally:
        if hub._db is not None:
            hub._db.close()
            hub._db = None
