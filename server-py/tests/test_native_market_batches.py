"""Native-only batches, including real PostgreSQL transaction guarantees."""
import asyncio
import json
import sqlite3
import time
import statistics

import pytest

from app.collectors.chain_stream import CANDLE_UPSERT, FACT_UPSERT, SWAP_V2
from app.collectors.live_market import HotMarketStream
from app.db import WriterLock
from app.live_market_store import (LiveMarketStore, NativeMarketBatchConnection,
                                   NATIVE_BATCH_BYTES, NATIVE_BATCH_ROWS)
from app.realtime_schema import enqueue_events
from test_postgres_core_storage import core_pg


class RecordingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def execute(self, statement, parameters=()):
        self.calls.append(('execute', statement, tuple(parameters)))
        return await self.connection.execute(statement, parameters)

    async def executemany(self, statement, parameters):
        rows = list(parameters)
        self.calls.append(('executemany', statement, rows))
        return await self.connection.executemany(statement, rows)


def test_sqlite_batch_wrapper_preserves_original_sequential_path(tmp_path):
    import aiosqlite
    async def check():
        async with aiosqlite.connect(tmp_path / 'original.sqlite') as connection:
            await connection.execute('CREATE TABLE facts(kind TEXT,id TEXT,body TEXT,PRIMARY KEY(kind,id))')
            await connection.execute('BEGIN IMMEDIATE')
            recorder = RecordingConnection(connection)
            batched = NativeMarketBatchConnection(recorder)
            await batched.executemany(FACT_UPSERT, [('56:batch', 'same', '{"v":1}'), ('56:batch', 'same', '{"v":2}')])
            assert [call[0] for call in recorder.calls] == ['executemany']
            assert (await (await connection.execute('SELECT body FROM facts')).fetchone())[0] == '{"v":2}'
            await connection.rollback()
            assert (await (await connection.execute('SELECT COUNT(*) FROM facts')).fetchone())[0] == 0
    asyncio.run(check())


def test_postgres_native_batch_row_and_utf8_byte_bounds(core_pg):
    sources, _ = core_pg

    async def check():
        store = await LiveMarketStore(str(sources['market']), '56', write_lock=WriterLock()).connect()
        recorder = RecordingConnection(store.db)
        batched = NativeMarketBatchConnection(recorder)
        try:
            await store.db.execute('BEGIN IMMEDIATE')
            rows = [('56:batch', str(index), json.dumps({'i': index, 'text': '中文🙂', 'v': None}))
                    for index in range(NATIVE_BATCH_ROWS * 2 + 1)]
            cursor = await batched.executemany(FACT_UPSERT, rows)
            assert cursor.rowcount == len(rows)
            assert [len(call[2]) // 3 for call in recorder.calls] == [32, 32, 1]
            await store.db.commit()
            assert await store.get('batch', '0') == {'i': 0, 'text': '中文🙂', 'v': None}
            recorder.calls.clear()
            # Two rows fit exactly; a third row must start a new statement.
            kind = '56:byte'
            body = '"' + 'x' * (NATIVE_BATCH_BYTES // 2 - len(kind) - 1 - 12 - 2) + '"'
            rows = [(kind, str(index), body) for index in range(3)]
            assert all(batched.payload_bytes(row) == NATIVE_BATCH_BYTES // 2 for row in rows)
            await store.db.execute('BEGIN IMMEDIATE')
            await batched.executemany(FACT_UPSERT, rows)
            assert [len(call[2]) // 3 for call in recorder.calls] == [2, 1]
            assert all(sum(batched.payload_bytes(call[2][i:i+3]) for i in range(0, len(call[2]), 3))
                       <= NATIVE_BATCH_BYTES for call in recorder.calls)
            await store.db.rollback()
            recorder.calls.clear()
            # UTF-8 bytes and oversized individual rows cannot be merged.
            rows = [('56:unicode', 'a', '"' + '界' * 50000 + '"'),
                    ('56:unicode', 'b', '"' + '界' * 50000 + '"'),
                    ('56:unicode', 'c', '"' + '界' * 100000 + '"')]
            await store.db.execute('BEGIN IMMEDIATE')
            await batched.executemany(FACT_UPSERT, rows)
            assert [call[0] for call in recorder.calls] == ['execute', 'execute', 'executemany']
            await store.db.rollback()
        finally:
            await store.close()
    asyncio.run(check())


def test_postgres_native_repeated_keys_preserve_sequential_updates_and_event_ids(core_pg):
    sources, _ = core_pg

    async def check():
        store = await LiveMarketStore(str(sources['market']), '56', write_lock=WriterLock()).connect()
        recorder = RecordingConnection(store.db)
        batched = NativeMarketBatchConnection(recorder)
        try:
            await store.db.execute('BEGIN IMMEDIATE')
            await batched.executemany(FACT_UPSERT, [('56:batch', 'same', '{"v":1}'),
                                                   ('56:batch', 'same', '{"v":2}')])
            assert recorder.calls[-1][0] == 'executemany'
            candle = ('56:pool', '5m', 1000, 1, 2, 1, 2, 3, None, 0)
            await batched.executemany(CANDLE_UPSERT, [candle, (*candle[:7], 6, None, 1)])
            assert recorder.calls[-1][0] == 'executemany'
            events = [('candle', {'i': index}) for index in range(35)] + [('trade', {'i': 35})]
            await enqueue_events(batched, events)
            await store.db.commit()
            assert await store.get('batch', 'same') == {'v': 2}
            assert (await store.candle_range('pool', '5m'))[0]['v'] == 6
            result = await store.fetchall('SELECT id,event,body FROM realtime_events ORDER BY id')
            assert [(row[1], json.loads(row[2])['i']) for row in result] == [(name, body['i']) for name, body in events]
            assert [row[0] for row in result] == sorted({row[0] for row in result})
            await store.db.execute('BEGIN IMMEDIATE')
            cursor = await batched.executemany('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
                                             [('trade', '{}', 1), ('trade', '{}', 2)])
            assert cursor.lastrowid == result[-1][0] + 2
            assert cursor.rowcount == 2
            await store.db.rollback()
        finally:
            await store.close()
    asyncio.run(check())


def test_postgres_native_batch_failure_rolls_back_facts_candles_and_events(core_pg):
    sources, _ = core_pg

    async def check():
        store = await LiveMarketStore(str(sources['market']), '56', write_lock=WriterLock()).connect()
        batched = NativeMarketBatchConnection(store.db)
        try:
            await batched.execute('BEGIN IMMEDIATE')
            await batched.executemany(FACT_UPSERT, [('56:batch', str(index), '{}') for index in range(40)])
            await batched.executemany(CANDLE_UPSERT, [('56:pool', '5m', 1000, 1, 1, 1, 1, 1, None, 0)])
            await enqueue_events(batched, [('candle', {'i': 1}), ('trade', {'i': 1})])
            with pytest.raises(sqlite3.IntegrityError):
                await batched.executemany('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
                                         [('trade', '{}', 1), (None, '{}', 2)])
            await batched.rollback()
            for table in ('facts', 'candles', 'realtime_events'):
                assert (await store.fetchone('SELECT COUNT(*) FROM ' + table))[0] == 0
            # A rolled-back failed batch cannot poison the next transaction.
            await batched.execute('BEGIN IMMEDIATE')
            await enqueue_events(batched, [('trade', {'next': True})])
            await batched.commit()
            assert (await store.fetchone('SELECT COUNT(*) FROM realtime_events'))[0] == 1
        finally:
            await store.close()
    asyncio.run(check())


def test_postgres_native_real_swaps_dedupe_accumulate_and_retract(core_pg):
    sources, _ = core_pg
    token, quote, pool = ('0x' + digit * 40 for digit in ('1', '2', '3'))
    block_hash, tx_hash = '0x' + '4' * 64, '0x' + '5' * 64

    async def check():
        store = await LiveMarketStore(str(sources['market']), '56', write_lock=WriterLock()).connect()
        stream = HotMarketStream('56')
        stream.s = store
        stream.pools = {pool: {'pool': pool, 'token0': token, 'token1': quote}}
        stream.selected_bases = {pool: {token}}
        stream.assets = {token: {'symbol': 'MEME'}, quote: {'symbol': 'QUOTE'}}
        stream.decimals = {token: 18, quote: 6}
        at = int(time.time()) * 1000
        stream.remember_header({'number': '0xc8', 'hash': block_hash, 'timestamp': hex(at // 1000)})
        await stream._open_trade_db()
        try:
            data = '0x' + ''.join(value.to_bytes(32, 'big').hex() for value in (0, 6000000, 2 * 10**18, 0))
            logs = [{'address': pool, 'blockHash': block_hash, 'blockNumber': '0xc8',
                     'transactionHash': tx_hash, 'transactionIndex': '0x0', 'logIndex': hex(index),
                     'topics': [SWAP_V2], 'data': data} for index in (1, 2)]
            for log in [*logs, logs[0]]:
                await stream.process_log(log)
            market = stream.market(stream.pools[pool], token)
            assert (await store.candle_range(market.storage, '1m'))[0]['v'] == 4
            assert (await store.fetchone('SELECT COUNT(*) FROM trades'))[0] == 2
            events = await store.fetchall('SELECT id,event,body FROM realtime_events ORDER BY id')
            trade_indexes = [index for index, row in enumerate(events) if row[1] == 'trade']
            assert len(trade_indexes) == 2
            for index in trade_indexes:
                assert events[index - 1][1] == 'candle'
                assert json.loads(events[index - 1][2])['sourceEventAt'] == at
            async def rpc(method, params):
                if method == 'eth_getLogs':
                    return logs
                height = int(params[0], 16)
                return {'number': hex(height), 'timestamp': hex(at // 1000 - max(0, 200 - height) * 2),
                        'hash': block_hash if height == 200 else '0x' + f'{height:064x}'}
            stream.rpc = rpc
            await stream.scan_recent_pools({'number': '0xc8', 'hash': block_hash, 'timestamp': hex(at // 1000)})
            assert (await store.get('pool-live-cursor', pool))['block'] == 200
            meta = await store.get('candle-meta', market.candle_key('5m'))
            assert meta['lastSourceEventAt'] == at
            assert meta['poolScan']['canonical'] is True
            # Native historical coverage uses the same migrated tables and
            # must verify all decoded swaps through an adapter-compatible read.
            accepted = await stream.record_time_coverage(199, 200,
                {'number': '0xc8', 'hash': block_hash, 'timestamp': hex(at // 1000)}, [pool], logs)
            assert accepted == 1
            coverage = await store.all('pool-time-coverage')
            assert len(coverage) == 1
            assert coverage[0]['canonical'] is coverage[0]['decoded'] is True
            assert coverage[0]['swapLogCount'] == 2
            await stream.process_log({**logs[0], 'removed': True})
            assert (await store.fetchone('SELECT COUNT(*) FROM trades'))[0] == 0
            assert await store.candle_range(market.storage, '1m') == []
            assert (await store.get('candle-meta', market.candle_key('5m')))['poolScan']['canonical'] is False
            assert 'trade-remove' in [row[0] for row in await store.fetchall('SELECT event FROM realtime_events')]
        finally:
            await stream.close()
            await store.close()
    asyncio.run(check())


def test_postgres_native_same_swap_load_reports_plain_and_batched_timings(core_pg):
    """Actual adapter/transactions, equivalent load, no timing pass threshold."""
    sources, _ = core_pg

    async def measure(batched, digit):
        token, quote, pool = ('0x' + digit * 40, '0x' + 'b' * 40, '0x' + ('c' if digit == '6' else 'd') * 40)
        block_hash, tx_hash = '0x' + digit * 64, '0x' + ('e' if batched else 'f') * 64
        store = await LiveMarketStore(str(sources['market']), '56', write_lock=WriterLock()).connect()
        stream = HotMarketStream('56')
        stream.s = store
        stream.pools = {pool: {'pool': pool, 'token0': token, 'token1': quote}}
        stream.selected_bases = {pool: {token}}
        stream.assets = {token: {'symbol': 'MEME'}, quote: {'symbol': 'QUOTE'}}
        stream.decimals = {token: 18, quote: 6}
        at = int(time.time()) * 1000
        stream.remember_header({'number': '0xc8', 'hash': block_hash, 'timestamp': hex(at // 1000)})
        await stream._open_trade_db()
        if not batched:
            stream.trade_db = stream.trade_db.connection
            stream.trade_store.db = stream.trade_db
        data = '0x' + ''.join(value.to_bytes(32, 'big').hex() for value in (0, 6000000, 2 * 10**18, 0))
        times = []
        try:
            for index in range(128):
                began = time.perf_counter()
                await stream.process_log({'address': pool, 'blockHash': block_hash, 'blockNumber': '0xc8',
                    'transactionHash': tx_hash, 'transactionIndex': '0x0', 'logIndex': hex(index),
                    'topics': [SWAP_V2], 'data': data})
                times.append((time.perf_counter() - began) * 1000)
            market = stream.market(stream.pools[pool], token)
            assert len(await store.recent_trades(market.storage, 500)) == 128
            for bar in ('1m', '5m', '1H'):
                assert (await store.candle_range(market.storage, bar))[0]['v'] == 256
            ordered = sorted(times)
            return {'events': len(times), 'elapsedMs': round(sum(times), 1),
                    'eventsPerSecond': round(1000 * len(times) / sum(times), 2),
                    'eventP50Ms': round(statistics.median(times), 1),
                    'eventP95Ms': round(ordered[int(.95 * len(ordered)) - 1], 1)}
        finally:
            await stream.close()
            await store.close()

    async def check():
        result = {'plain': await measure(False, '6'), 'batched': await measure(True, '7')}
        print('native-batch-real-pg-benchmark=' + json.dumps(result, sort_keys=True))
    asyncio.run(check())
