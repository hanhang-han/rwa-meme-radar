import asyncio
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.db import ResearchStore
from app.collectors.chain_stream import ChainPoolStream, SWAP_V2, SWAP_V3, SYNC_V2, WATCHED_POOL_BURST, decode_swap, event_key
from app.collectors.market_streams import Market

TOKEN0 = "0x" + "1" * 40
TOKEN1 = "0x" + "2" * 40
POOL = "0x" + "3" * 40


def encoded(*values):
    return "0x" + "".join((v % 2 ** 256).to_bytes(32, "big").hex() for v in values)


class DecodeTests(unittest.TestCase):
    def test_v2_direction_quantity_and_decimals(self):
        log = {"topics": [SWAP_V2], "data": encoded(0, 6_000_000, 2 * 10 ** 18, 0)}
        buy = decode_swap(log, TOKEN0, TOKEN0, 18, 6)
        self.assertEqual((buy["price"], buy["size"], buy["quoteVolume"], buy["type"]), (3, 2, 6, "buy"))
        reverse = decode_swap(log, TOKEN0, TOKEN1, 18, 6)
        self.assertAlmostEqual(reverse["price"], 1 / 3)
        self.assertEqual(reverse["type"], "sell")

    def test_v3_signed_words_and_distinct_execution_price(self):
        log = {"topics": [SWAP_V3], "data": encoded(-2 * 10 ** 18, 6_000_000, 2 ** 96, 100, -23)}
        row = decode_swap(log, TOKEN0, TOKEN0, 18, 6)
        self.assertEqual(row["price"], 3)
        self.assertEqual(row["sqrtPriceX96"], str(2 ** 96))
        self.assertIsNone(decode_swap({"topics": [SWAP_V3], "data": encoded(1, 2, 3, 4, 5)}, TOKEN0, TOKEN0, 18, 6))
        self.assertIsNone(decode_swap({"topics": [SWAP_V3], "data": "0x"}, TOKEN0, TOKEN0, 18, 6))


class RpcTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_rpc_uses_verified_http_while_chain_identity_stays_on_wss(self):
        for chain in ('56', '4663'):
            with self.subTest(chain=chain):
                methods = []

                def respond(request):
                    body = json.loads(request.content)
                    methods.append(body['method'])
                    results = {'eth_chainId': hex(int(chain)), 'eth_getLogs': [],
                               'eth_getBlockByNumber': {'number': '0x1'}, 'eth_call': '0x1'}
                    return httpx.Response(200, json={'result': results[body['method']]})

                collector = ChainPoolStream(chain)
                # Robinhood needs an explicitly configured HTTP provider that
                # accepts the full replay page; its default PublicNode does not.
                collector.http_reads_enabled = True
                collector.rpc_interval = 0
                collector.ws = object()
                collector.ws_call = AsyncMock(return_value=hex(int(chain)))
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
                    self.assertEqual(await collector.rpc('eth_getLogs', [{}]), [])
                    self.assertEqual(await collector.rpc('eth_getBlockByNumber', ['0x1', False]),
                                     {'number': '0x1'})
                    self.assertEqual(await collector.rpc('eth_call', [{'to': TOKEN0}, 'latest']), '0x1')
                    self.assertEqual(await collector.rpc('eth_chainId', []), hex(int(chain)))
                self.assertEqual(methods, ['eth_chainId', 'eth_getLogs',
                                           'eth_getBlockByNumber', 'eth_call'])
                collector.ws_call.assert_awaited_once_with(collector.ws, 'eth_chainId', [])

    async def test_default_robinhood_replay_avoids_unsupported_http_page(self):
        with patch.dict(os.environ, {'ROBINHOOD_STREAM_HTTP_READS': 'false'}):
            collector = ChainPoolStream('4663')
        collector.rpc_interval = 0
        collector.ws = object()
        collector.ws_call = AsyncMock(return_value=[])

        def fail_if_http_used(request):
            self.fail('default Robinhood HTTP rejects 100 and 300-block / 8-pool log pages')

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail_if_http_used)) as collector.http:
            self.assertEqual(await collector.rpc('eth_getLogs', [{}]), [])
        collector.ws_call.assert_awaited_once_with(collector.ws, 'eth_getLogs', [{}])

    async def test_wrong_http_chain_never_supplies_replay_logs(self):
        methods = []

        def respond(request):
            method = json.loads(request.content)['method']
            methods.append(method)
            return httpx.Response(200, json={'result': '0x1'})

        collector = ChainPoolStream('56')
        collector.rpc_interval = 0
        collector.ws = object()
        collector.ws_call = AsyncMock(return_value=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
            self.assertEqual(await collector.rpc('eth_getLogs', [{}]), [])
            self.assertEqual(await collector.rpc('eth_getLogs', [{}]), [])
        self.assertEqual(methods, ['eth_chainId'])
        self.assertEqual(collector.ws_call.await_count, 2)
        self.assertFalse(collector.http_chain_verified)

    async def test_wrong_http_chain_without_wss_fails_closed(self):
        methods = []

        def respond(request):
            methods.append(json.loads(request.content)['method'])
            return httpx.Response(200, json={'result': None})

        collector = ChainPoolStream('56')
        collector.rpc_interval = 0
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
            with self.assertRaisesRegex(ValueError, 'wrong-http-chain-id'):
                await collector.rpc('eth_getLogs', [{}])
        self.assertEqual(methods, ['eth_chainId'])

    async def test_wrong_http_chain_cannot_supply_block_height(self):
        methods = []

        def respond(request):
            methods.append(json.loads(request.content)['method'])
            return httpx.Response(200, json={'result': '0x1'})

        collector = ChainPoolStream('56')
        collector.rpc_interval = 0
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
            with self.assertRaisesRegex(ValueError, 'wrong-http-chain-id'):
                await collector.rpc('eth_blockNumber', [])
        self.assertEqual(methods, ['eth_chainId'])

    async def test_http_transport_failure_falls_back_then_revalidates(self):
        methods = []

        def respond(request):
            method = json.loads(request.content)['method']
            methods.append(method)
            if method == 'eth_getLogs' and methods.count(method) == 1:
                return httpx.Response(403)
            return httpx.Response(200, json={'result': '0x1237' if method == 'eth_chainId' else []})

        collector = ChainPoolStream('4663')
        collector.http_reads_enabled = True
        collector.rpc_interval = 0
        collector.ws = object()
        collector.ws_call = AsyncMock(return_value=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
            self.assertEqual(await collector.rpc('eth_getLogs', [{}]), [])
            self.assertEqual(await collector.rpc('eth_getLogs', [{}]), [])
            collector.http_fallback_until = 0
            self.assertEqual(await collector.rpc('eth_getLogs', [{}]), [])
        self.assertEqual(methods, ['eth_chainId', 'eth_getLogs', 'eth_chainId', 'eth_getLogs'])
        self.assertEqual(collector.ws_call.await_count, 2)
        self.assertTrue(collector.http_chain_verified)
        self.assertEqual(collector.rpc_rate_failures, 0)


class DurablePoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = await ResearchStore(os.path.join(self.tmp.name, "research.sqlite"), "196").connect()
        self.collector = ChainPoolStream("196")
        self.collector.s = self.store
        self.market = Market("196", TOKEN0, "dex", POOL, "USDG", "MEME", "pool", POOL)
        self.at = 1_790_317_800_000

    async def asyncTearDown(self):
        await self.collector.close()
        await self.store.close()
        self.tmp.cleanup()

    def trade(self, id, size, offset=0):
        return {"id": id, "t": self.at + offset, "price": 3, "quantity": size,
                "quoteQuantity": 3 * size, "type": "buy", "blockHash": "0xabc",
                "blockNumber": 200, "logIndex": offset}

    async def busy_shared_reader(self):
        started, release = threading.Event(), threading.Event()

        def hold():
            started.set()
            if not release.wait(5):
                raise TimeoutError('test shared reader was not released')
            return 1

        await self.store.db.create_function('hold_shared_reader', 0, hold)
        blocked = asyncio.create_task(self.store.db.execute_fetchall('SELECT hold_shared_reader()'))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        return release, blocked

    async def prepare_logs(self):
        await self.store.db.execute('''CREATE TABLE chain_stream_logs (
            chain TEXT NOT NULL, id TEXT NOT NULL, pool TEXT NOT NULL,
            block INTEGER NOT NULL, hash TEXT NOT NULL, at INTEGER NOT NULL,
            body TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chain,id))''')
        await self.store.db.commit()
        self.collector.pools = {POOL: {'pool': POOL, 'token0': TOKEN0, 'token1': TOKEN1,
                                       'queryToken': TOKEN0}}
        self.collector.decimals = {TOKEN0: 18, TOKEN1: 6}
        header = {'number': '0xc8', 'hash': '0xabc', 'timestamp': hex(self.at // 1000)}
        self.collector.remember_header(header)
        return {'address': POOL, 'blockHash': '0xabc', 'blockNumber': '0xc8',
                'transactionHash': '0xdef', 'transactionIndex': '0x0', 'logIndex': '0x1',
                'topics': [SWAP_V2], 'data': encoded(0, 6_000_000, 2 * 10 ** 18, 0)}

    async def test_irrelevant_sync_does_not_enter_live_queue_or_persistence(self):
        swap = await self.prepare_logs()
        sync = {**swap, 'topics': [SYNC_V2], 'data': encoded(2 * 10 ** 18, 6_000_000)}

        class FakeSocket:
            def __init__(self, events):
                self.events = iter(events)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return json.dumps({'params': {'result': next(self.events)}})
                except StopIteration:
                    raise StopAsyncIteration

        await self.collector.reader(FakeSocket([sync, swap, {**sync, 'removed': True}]))
        self.assertEqual(self.collector.queue.qsize(), 2)
        self.assertEqual([self.collector.queue.get_nowait()['topics'][0] for _ in range(2)],
                         [SWAP_V2, SYNC_V2])
        self.collector.log_header = AsyncMock()
        await self.collector.process_log(sync)
        self.collector.log_header.assert_not_awaited()
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM chain_stream_logs'))[0], 0)

    async def test_replay_skips_irrelevant_sync_and_advances_covered_range(self):
        swap = await self.prepare_logs()
        sync = {**swap, 'topics': [SYNC_V2], 'data': encoded(2 * 10 ** 18, 6_000_000)}
        await self.store.put('chain-stream-cursor', 'pools', {'block': 199, 'hash': hex(199)})
        self.collector.last_prune = self.at

        async def rpc(method, params):
            if method == 'eth_getLogs':
                return [sync]
            block = 200 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': '0xabc' if block == 200 else hex(block),
                    'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            progress = await self.collector.catch_up(max_ranges=1)
        self.assertTrue(progress['caughtUp'])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 200)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM chain_stream_logs'))[0], 0)

    async def test_relation_sync_keeps_pool_ratio_and_removed_sync_retracts_block(self):
        swap = await self.prepare_logs()
        sync = {**swap, 'transactionHash': '0xfee', 'logIndex': '0x2',
                'topics': [SYNC_V2], 'data': encoded(2 * 10 ** 18, 6_000_000)}
        self.collector.relations[POOL] = [{'id': 'relation', 'pool': POOL, 'token': TOKEN0,
                                            'stockSide': TOKEN1}]
        await self.collector.process_log(sync)
        quote = await self.store.get('pool-quote', POOL)
        self.assertAlmostEqual(quote['memePerStock'], 1 / 3)
        self.assertEqual(quote['method'], 'v2-sync-stream')
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM chain_stream_logs'))[0], 1)

        await self.collector.process_log(swap)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 1)
        # A removed Sync is also a block reorg signal for the prior Swap.
        self.collector.relations.clear()
        await self.collector.process_log({**sync, 'removed': True})
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 0)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM chain_stream_logs'))[0], 0)

    async def test_usd_anchor_merge_bypasses_busy_shared_connection(self):
        log = await self.prepare_logs()
        await self.collector._open_trade_db()
        await self.store.put('asset', TOKEN0, {
            'price': 1, 'priceCurrency': 'USD', 'marketCap': 100,
            'fieldTimes': {'price': self.at - 2000},
        })
        await self.store.put('asset', TOKEN1, {
            'price': 2, 'priceCurrency': 'USD',
            'fieldTimes': {'price': self.at - 1000},
            'fieldSources': {'price': 'independent-provider'},
        })
        real_get = self.store.get
        release = blocked = None

        async def intercept_get(kind, ident):
            nonlocal release, blocked
            result = await real_get(kind, ident)
            if kind == 'asset' and ident == TOKEN1:
                release, blocked = await self.busy_shared_reader()
            return result

        decoded = decode_swap(log, TOKEN0, TOKEN0, 18, 6)
        with patch.object(self.store, 'get', new=intercept_get):
            task = asyncio.create_task(self.collector.pool_asset_quote(
                self.collector.pools[POOL], TOKEN0, decoded, self.at, 200, event_key(log)))
            completed_while_blocked = False
            quote = None
            try:
                done, _ = await asyncio.wait({task}, timeout=1.5)
                completed_while_blocked = task in done and release is not None
                if completed_while_blocked:
                    await task
                    quote = await self.collector.trade_store.get('asset', TOKEN0)
            finally:
                if release is not None:
                    release.set()
                    await blocked
                await task
        self.assertTrue(completed_while_blocked)
        self.assertEqual(quote['price'], 6)
        self.assertEqual(quote['fieldSources']['price'], 'Chain RPC')
        self.assertEqual(quote['priceProvenance']['quoteToken'], TOKEN1)

    async def test_relation_pool_quote_bypasses_busy_shared_connection(self):
        swap = await self.prepare_logs()
        await self.collector._open_trade_db()
        sync = {**swap, 'transactionHash': '0xfee', 'logIndex': '0x2',
                'topics': [SYNC_V2], 'data': encoded(2 * 10 ** 18, 6_000_000)}
        self.collector.relations[POOL] = [{'id': 'relation', 'pool': POOL,
                                            'token': TOKEN0, 'stockSide': TOKEN1}]
        real_get = self.store.get
        release = blocked = None

        async def intercept_get(kind, ident):
            nonlocal release, blocked
            result = await real_get(kind, ident)
            if kind == 'pool-quote':
                release, blocked = await self.busy_shared_reader()
            return result

        with patch.object(self.store, 'get', new=intercept_get), \
             patch.object(self.collector, 'reserve_valuation', new=AsyncMock()):
            task = asyncio.create_task(self.collector.process_log(sync))
            completed_while_blocked = False
            quote = None
            try:
                done, _ = await asyncio.wait({task}, timeout=1.5)
                completed_while_blocked = task in done and release is not None
                if completed_while_blocked:
                    await task
                    quote = await self.collector.trade_store.get('pool-quote', POOL)
            finally:
                if release is not None:
                    release.set()
                    await blocked
                await task
        self.assertTrue(completed_while_blocked)
        self.assertAlmostEqual(quote['memePerStock'], 1 / 3)
        self.assertEqual(quote['method'], 'v2-sync-stream')

    async def test_reserve_valuation_bypasses_busy_shared_connection(self):
        await self.prepare_logs()
        await self.collector._open_trade_db()
        self.collector.relations[POOL] = [{'id': 'relation', 'pool': POOL,
                                            'token': TOKEN0, 'stockSide': TOKEN1}]
        await self.store.put('asset', TOKEN0, {'price': 2, 'priceCurrency': 'USD',
                             'fieldTimes': {'price': self.at - 1000}})
        await self.store.put('asset', TOKEN1, {'price': 3, 'priceCurrency': 'USD',
                             'fieldTimes': {'price': self.at - 1000}})
        await self.store.put('relation', 'relation', {'id': 'relation', 'name': 'existing'})
        real_get = self.store.get
        release = blocked = None

        async def intercept_get(kind, ident):
            nonlocal release, blocked
            result = await real_get(kind, ident)
            if kind == 'relation':
                release, blocked = await self.busy_shared_reader()
            return result

        with patch.object(self.store, 'get', new=intercept_get):
            task = asyncio.create_task(self.collector.reserve_valuation(
                self.collector.pools[POOL], (2 * 10 ** 18, 6_000_000), 18, 6,
                self.at, 200, '0xabc', 2))
            completed_while_blocked = False
            relation = None
            try:
                done, _ = await asyncio.wait({task}, timeout=1.5)
                completed_while_blocked = task in done and release is not None
                if completed_while_blocked:
                    await task
                    relation = await self.collector.trade_store.get('relation', 'relation')
            finally:
                if release is not None:
                    release.set()
                    await blocked
                await task
        self.assertTrue(completed_while_blocked)
        self.assertEqual(relation['liquidityUsd'], 22)
        self.assertEqual(relation['name'], 'existing')
        self.assertEqual(relation['valuationOrder'], [200, 2])

    async def test_metadata_wait_does_not_block_removal_or_restore_orphan(self):
        log = await self.prepare_logs()
        started, release = asyncio.Event(), asyncio.Event()
        header = self.collector.headers['0xabc']

        async def delayed_block(block_hash):
            self.assertFalse(self.collector.lock.locked())
            started.set()
            await release.wait()
            return header

        self.collector.block = delayed_block
        ingest = asyncio.create_task(self.collector.process_log(log))
        try:
            await asyncio.wait_for(started.wait(), .5)
            await asyncio.wait_for(self.collector.process_log({**log, 'removed': True}), .5)
            release.set()
            await asyncio.wait_for(ingest, .5)
        finally:
            release.set()
            await asyncio.gather(ingest, return_exceptions=True)
        self.assertEqual((await self.store.fetchone('SELECT count(*) FROM chain_stream_logs'))[0], 0)
        self.assertEqual(self.collector.processed, 0)

    async def test_concurrent_live_and_replay_deduplicate_after_metadata(self):
        log = await self.prepare_logs()
        ready, release = asyncio.Event(), asyncio.Event()
        header = self.collector.headers['0xabc']
        count = 0

        async def delayed_block(block_hash):
            nonlocal count
            count += 1
            if count == 2:
                ready.set()
            await release.wait()
            return header

        self.collector.block = delayed_block
        ingests = [asyncio.create_task(self.collector.process_log(dict(log))) for _ in range(2)]
        try:
            await asyncio.wait_for(ready.wait(), .5)
            release.set()
            await asyncio.wait_for(asyncio.gather(*ingests), 1)
        finally:
            release.set()
            await asyncio.gather(*ingests, return_exceptions=True)
        self.assertEqual((await self.store.fetchone('SELECT count(*) FROM trades'))[0], 1)
        self.assertEqual(self.collector.processed, 1)
        candle = await self.store.get('market-candle', self.collector.market(self.collector.pools[POOL], TOKEN0).storage + ':1m')
        self.assertEqual(candle['row']['v'], 2)

    async def test_live_queue_pauses_replay_without_advancing_watermark(self):
        log = await self.prepare_logs()
        await self.store.put('chain-stream-cursor', 'pools', {'block': 50, 'hash': '0x50'})
        self.collector.last_prune = self.at

        async def rpc(method, params):
            if method == 'eth_getLogs':
                return [log]
            n = 51 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(n), 'hash': f'0x{n}', 'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        self.collector.process_log = AsyncMock()
        self.collector.retract_orphans = AsyncMock()
        self.collector.queue.put_nowait(log)
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            replay = asyncio.create_task(self.collector.catch_up())
            try:
                await asyncio.sleep(.06)
                self.assertFalse(replay.done())
                self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 50)
                self.collector.queue.get_nowait()
                self.collector.live_processing = True
                await asyncio.sleep(.06)
                self.collector.process_log.assert_not_awaited()
                self.collector.live_processing = False
                await asyncio.wait_for(replay, .5)
            finally:
                replay.cancel()
                await asyncio.gather(replay, return_exceptions=True)
        self.collector.process_log.assert_awaited_once_with({**log, '_receivedAt': self.at})
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 51)

    async def test_high_live_queue_pauses_both_replay_lanes_and_preserves_gap(self):
        await self.store.put('chain-stream-cursor', 'pools', {
            'block': 50, 'hash': '0x32', 'coverageFrom': 40})
        await self.store.put('chain-stream-cursor', 'pools-live', {
            'block': 900, 'hash': '0x384', 'coverageFrom': 800})
        self.collector.pools = {POOL: {}}
        self.collector.latest_head = 1000
        self.collector.queue = asyncio.Queue(maxsize=8)
        for index in range(4):
            self.collector.queue.put_nowait(index)
        self.collector.rpc = AsyncMock()
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            paused = await self.collector.reconcile_step()
            await self.collector.status_fact(force=True)
        self.assertFalse(paused['caughtUp'])
        self.collector.rpc.assert_not_awaited()
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 50)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools-live'))['block'], 900)
        status = await self.store.get('chain-stream', 'pools')
        self.assertTrue(status['replayPausedForLive'])
        self.assertEqual(status['historicalGapBlocks'], 749)

        self.collector.queue.get_nowait()  # Three pending: remain paused.
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            await self.collector.reconcile_step()
        self.collector.rpc.assert_not_awaited()
        self.collector.queue.get_nowait()  # Drain below the resume threshold.
        self.collector.queue.get_nowait()
        self.collector.queue.get_nowait()

        ranges = []
        async def rpc(method, params):
            if method == 'eth_getLogs':
                ranges.append((int(params[0]['fromBlock'], 16), int(params[0]['toBlock'], 16)))
                return []
            block = 1000 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': hex(block), 'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        self.collector.retract_orphans = AsyncMock()
        self.collector.last_prune = self.at
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            await self.collector.reconcile_step()
            await self.collector.status_fact(force=True)
        self.assertEqual(ranges, [(901, 1000), (51, 150)])
        self.assertFalse((await self.store.get('chain-stream', 'pools'))['replayPausedForLive'])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools-live'))['block'], 1000)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 150)

    async def test_production_sized_queue_yields_before_half_full(self):
        await self.store.put('chain-stream-cursor', 'pools', {
            'block': 50, 'hash': '0x32', 'coverageFrom': 40})
        self.collector.latest_head = 1000
        self.collector.rpc = AsyncMock()
        for index in range(31):
            self.collector.queue.put_nowait(index)
        self.assertFalse(self.collector.replay_under_pressure())
        self.collector.queue.put_nowait(31)
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            paused = await self.collector.reconcile_step()
        self.assertFalse(paused['caughtUp'])
        self.collector.rpc.assert_not_awaited()
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 50)
        for _ in range(23):
            self.collector.queue.get_nowait()
        self.assertTrue(self.collector.replay_under_pressure())  # Nine pending.
        self.collector.queue.get_nowait()
        self.assertFalse(self.collector.replay_under_pressure())

    async def test_queue_filling_during_replay_discards_partial_range(self):
        await self.store.put('chain-stream-cursor', 'pools', {'block': 50, 'hash': '0x32'})
        self.collector.pools = {f'0x{i:040x}': {} for i in range(65)}
        self.collector.queue = asyncio.Queue(maxsize=8)
        self.collector.last_prune = self.at
        pages = []

        async def rpc(method, params):
            if method == 'eth_getLogs':
                pages.append(params[0]['address'])
                for index in range(4):
                    self.collector.queue.put_nowait(index)
                return []
            block = 51 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': hex(block), 'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            result = await self.collector.catch_up()
        self.assertFalse(result['caughtUp'])
        self.assertEqual(len(pages), 1)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 50)
        self.assertEqual((await self.store.get('chain-stream-scan', 'pools'))['pausedForLiveAt'], self.at)

    async def test_partial_replay_resumes_without_duplicate_trade_or_candle(self):
        first = await self.prepare_logs()
        second = {**first, 'transactionHash': '0xdef2', 'logIndex': '0x2'}
        await self.store.put('chain-stream-cursor', 'pools', {
            'block': 199, 'hash': hex(199), 'coverageFrom': 190})
        self.collector.queue = asyncio.Queue(maxsize=8)
        self.collector.last_prune = self.at

        async def rpc(method, params):
            if method == 'eth_getLogs':
                return [first, second]
            block = 200 if params[0] == 'latest' else int(params[0], 16)
            block_hash = '0xabc' if block == 200 else hex(block)
            return {'number': hex(block), 'hash': block_hash,
                    'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        original = self.collector.process_log
        calls = 0

        async def fill_after_first(log):
            nonlocal calls
            await original(log)
            calls += 1
            if calls == 1:
                for index in range(4):
                    self.collector.queue.put_nowait(index)

        self.collector.process_log = fill_after_first
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            paused = await self.collector.catch_up()
        self.assertFalse(paused['caughtUp'])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 199)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 1)
        while not self.collector.queue.empty():
            self.collector.queue.get_nowait()

        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            finished = await self.collector.catch_up()
        self.assertTrue(finished['caughtUp'])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 200)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 2)
        market = self.collector.market(self.collector.pools[POOL], TOKEN0)
        candle = await self.store.get('market-candle', market.storage + ':1m')
        self.assertEqual(candle['row']['v'], 4)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM realtime_events'))[0],
                         2 * (len(self.collector.bars(market)) + 1))

    async def test_permanently_nonempty_live_queue_allows_bounded_recent_replay(self):
        await self.store.put('chain-stream-cursor', 'pools', {'block': 50, 'hash': '0x32'})
        self.collector.pools = {POOL: {}}
        self.collector.last_prune = self.at
        self.collector.retract_orphans = AsyncMock()
        self.collector.process_log = AsyncMock()
        self.collector.queue.put_nowait('live')
        self.collector.queue.put_nowait('live')
        ranges = []
        live_processed = 0

        async def live_consumer():
            nonlocal live_processed
            while True:
                self.collector.queue.get_nowait()
                self.collector.live_processing = True
                await asyncio.sleep(.01)
                live_processed += 1
                self.collector.queue.put_nowait('live')
                self.collector.live_processing = False
                await asyncio.sleep(0)

        async def rpc(method, params):
            if method == 'eth_getLogs':
                start, end = int(params[0]['fromBlock'], 16), int(params[0]['toBlock'], 16)
                ranges.append((start, end))
                return [{'blockNumber': hex(701), 'blockHash': hex(701),
                         'transactionIndex': '0x0', 'logIndex': hex(i)}
                        for i in range(16)] if start == 701 else []
            block = 1000 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': hex(block), 'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        consumer = asyncio.create_task(live_consumer())
        try:
            with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
                await asyncio.wait_for(self.collector.reconcile_step(), 3)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        self.assertGreater(live_processed, 20)
        self.assertFalse(self.collector.queue.empty())
        self.assertEqual(self.collector.process_log.await_count, 16)
        self.assertEqual(ranges, [(701, 800), (801, 900), (901, 1000), (51, 150)])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools-live'))['block'], 1000)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 150)

    async def test_full_queue_drains_before_resubscription_and_later_reconciles(self):
        self.collector.queue = asyncio.Queue(maxsize=2)
        self.collector.queue.put_nowait({'id': 'oldest'})
        self.collector.queue.put_nowait({'id': 'second'})
        self.collector.recovery_needed = True
        await self.store.put('chain-stream-cursor', 'pools', {'block': 50, 'hash': '0x50'})
        await self.store.put('chain-stream-cursor', 'pools-live', {'block': 55, 'hash': '0x55'})
        order = []
        head = {'number': '0x64', 'hash': '0x100', 'timestamp': hex(self.at // 1000)}

        async def process(event):
            order.append(('log', event['id']))

        async def rpc(method, params):
            order.append(('head', method))
            return head

        async def catch_up(lane, **kwargs):
            order.append(('scan', lane, kwargs['head']))
            return {'caughtUp': False}

        self.collector.process_log = process
        self.collector.rpc = rpc
        self.collector.catch_up = catch_up
        self.collector.status_fact = AsyncMock()
        reader = asyncio.get_running_loop().create_future()
        await self.collector.recover_live_queue(reader)
        self.assertEqual(order, [('log', 'oldest'), ('log', 'second')])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools-live'))['block'], 55)
        self.assertFalse(self.collector.recovery_needed)
        self.assertTrue(self.collector.queue.empty())
        await self.collector.reconcile_step()
        self.assertEqual(order[-2:], [('head', 'eth_getBlockByNumber'),
                                      ('scan', 'pools-live', head)])

    async def test_watched_pool_priority_preserves_each_pool_order_and_cold_progress(self):
        cold_pool = '0x' + '4' * 40
        await self.store.put('candle-watch', 'active', {
            'pool': POOL, 'address': TOKEN0, 'bar': '1m', 'expiresAt': self.at + 60_000})
        await self.store.put('candle-watch', 'expired', {
            'pool': cold_pool, 'address': TOKEN0, 'bar': '1m', 'expiresAt': self.at - 1})
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            await self.collector.refresh_watches()
        self.assertEqual(self.collector.watched_pools, {POOL})
        self.collector.queue.put_nowait({'address': cold_pool, 'id': 'cold-0'})
        for index in range(WATCHED_POOL_BURST + 1):
            self.collector.queue.put_nowait({'address': POOL, 'id': f'hot-{index}'})
        self.collector.queue.put_nowait({'address': cold_pool, 'id': 'cold-1'})
        picked = [self.collector.take_live_event()['id'] for _ in range(WATCHED_POOL_BURST + 3)]
        self.assertEqual(picked, [*(f'hot-{index}' for index in range(WATCHED_POOL_BURST)),
                                  'cold-0', f'hot-{WATCHED_POOL_BURST}', 'cold-1'])
        self.assertTrue(self.collector.queue.empty())

    async def test_removed_log_gets_priority_only_at_its_pool_head(self):
        cold_pool = '0x' + '4' * 40
        self.collector.watched_pools = {POOL}
        self.collector.queue.put_nowait({'address': POOL, 'id': 'hot'})
        self.collector.queue.put_nowait({'address': cold_pool, 'id': 'reorg', 'removed': True})
        self.assertEqual(self.collector.take_live_event()['id'], 'reorg')
        self.assertEqual(self.collector.take_live_event()['id'], 'hot')
        self.collector.queue.put_nowait({'address': POOL, 'id': 'before-removal'})
        self.collector.queue.put_nowait({'address': POOL, 'id': 'removal', 'removed': True})
        self.assertEqual(self.collector.take_live_event()['id'], 'before-removal')
        self.assertEqual(self.collector.take_live_event()['id'], 'removal')

    async def test_failed_prioritized_log_retries_before_later_same_pool_log(self):
        cold_pool = '0x' + '4' * 40
        await self.store.put('candle-watch', 'active', {
            'pool': POOL, 'address': TOKEN0, 'bar': '1m', 'expiresAt': self.at + 60_000})
        self.collector.queue.put_nowait({'address': cold_pool, 'id': 'cold'})
        self.collector.queue.put_nowait({'address': POOL, 'id': 'hot-first'})
        self.collector.queue.put_nowait({'address': POOL, 'id': 'hot-later'})
        seen = []

        async def process(event):
            seen.append(event['id'])
            if event['id'] == 'hot-first' and seen.count('hot-first') == 1:
                raise TimeoutError('metadata unavailable')

        self.collector.process_log = process
        self.collector.status_fact = AsyncMock()
        self.collector.rpc = AsyncMock(return_value={'number': '0x64'})
        self.collector.catch_up = AsyncMock(return_value={'caughtUp': True})
        reader = asyncio.get_running_loop().create_future()
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            with self.assertRaisesRegex(TimeoutError, 'metadata unavailable'):
                await self.collector.recover_live_queue(reader)
            self.assertEqual(self.collector.retry_event['id'], 'hot-first')
            await self.collector.recover_live_queue(reader)
        self.assertEqual(seen, ['hot-first', 'hot-first', 'hot-later', 'cold'])
        self.assertTrue(self.collector.queue.empty())
        self.assertIsNone(self.collector.retry_event)

    async def test_failed_queue_head_survives_reconnect_before_later_logs(self):
        self.collector.queue = asyncio.Queue(maxsize=2)
        self.collector.queue.put_nowait({'id': 'first'})
        self.collector.queue.put_nowait({'id': 'later'})
        order = []

        async def fail_first(event):
            order.append(event['id'])
            raise TimeoutError('metadata unavailable')

        self.collector.process_log = fail_first
        self.collector.status_fact = AsyncMock()
        reader = asyncio.get_running_loop().create_future()
        with self.assertRaisesRegex(TimeoutError, 'metadata unavailable'):
            await self.collector.recover_live_queue(reader)
        self.assertEqual(self.collector.retry_event, {'id': 'first'})
        self.assertEqual(self.collector.queue.qsize(), 1)
        self.assertTrue(self.collector.recovery_needed)

        async def success(event):
            order.append(event['id'])

        self.collector.process_log = success
        self.collector.rpc = AsyncMock(return_value={'number': '0x64'})
        self.collector.catch_up = AsyncMock(return_value={'caughtUp': True})
        await self.collector.recover_live_queue(reader)
        self.assertEqual(order, ['first', 'first', 'later'])
        self.assertIsNone(self.collector.retry_event)
        self.assertTrue(self.collector.queue.empty())
        self.collector.catch_up.assert_not_awaited()

    async def test_long_recovery_publishes_queue_progress_before_resubscription(self):
        self.collector.queue = asyncio.Queue(maxsize=65)
        for index in range(65):
            self.collector.queue.put_nowait({'id': index})
        self.collector.recovery_needed = True
        sizes = []

        async def status_fact(**_):
            sizes.append(self.collector.queue.qsize())

        self.collector.process_log = AsyncMock()
        self.collector.status_fact = status_fact
        self.collector.rpc = AsyncMock(return_value={'number': '0x64'})
        self.collector.catch_up = AsyncMock(return_value={'caughtUp': False})
        reader = asyncio.get_running_loop().create_future()
        await self.collector.recover_live_queue(reader)
        self.assertEqual(sizes, [65, 1, 0])
        self.assertFalse(self.collector.recovery_needed)
        self.collector.catch_up.assert_not_awaited()

    async def test_reader_overflow_retains_old_logs_for_recovery(self):
        self.collector.queue = asyncio.Queue(maxsize=1)
        self.collector.queue.put_nowait({'id': 'queued'})

        class FakeSocket:
            closed = False

            def __aiter__(self):
                self.yielded = False
                return self

            async def __anext__(self):
                if self.yielded:
                    raise StopAsyncIteration
                self.yielded = True
                return json.dumps({'params': {'result': {'id': 'new-log'}}})

            async def close(self):
                self.closed = True

        socket = FakeSocket()
        with self.assertRaisesRegex(RuntimeError, 'chain-stream-backpressure'):
            await self.collector.reader(socket)
        self.assertTrue(socket.closed)
        self.assertTrue(self.collector.recovery_needed)
        self.assertEqual(self.collector.queue.get_nowait(), {'id': 'queued'})

    async def test_health_distinguishes_source_age_from_fresh_processing(self):
        log = await self.prepare_logs()
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at + 50_000):
            await self.collector.process_log(log)
        self.collector.last_received = self.at + 55_000
        self.collector.queue.put_nowait(log)
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at + 60_000):
            await self.collector.status_fact(force=True)
        health = await self.store.get('chain-stream', 'pools')
        self.assertEqual(health['lastSourceEventAt'], self.at)
        self.assertEqual(health['lastProcessedAt'], self.at + 50_000)
        self.assertEqual(health['lastReceivedAt'], self.at + 55_000)
        self.assertEqual(health['sourceLagMs'], 60_000)
        self.assertEqual(health['queueDepth'], 1)

    async def test_same_price_trades_accumulate_atomically_and_duplicates_do_not(self):
        self.assertTrue(await self.collector.commit_trade(self.market, self.trade("first", 2)))
        self.assertTrue(await self.collector.commit_trade(self.market, self.trade("second", 5, 1)))
        self.assertFalse(await self.collector.commit_trade(self.market, self.trade("first", 2)))
        rows = await self.store.recent_trades(self.market.storage, 20)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["volume"] is None for row in rows))
        candle = await self.store.get("market-candle", self.market.storage + ":1m")
        self.assertEqual(candle["row"]["v"], 7)
        self.assertEqual(candle["row"]["vu"], 21)
        self.assertEqual(candle["priceCurrency"], "USDG")
        cur = await self.store.db.execute("SELECT event,body FROM realtime_events")
        frames = await cur.fetchall()
        self.assertEqual(sum(row[0] == "trade" for row in frames), 2)
        self.assertEqual(sum(row[0] == "candle" for row in frames), 6)

    async def test_trade_commit_uses_dedicated_connection_while_shared_read_is_busy(self):
        with patch('app.collectors.chain_stream.store', new=AsyncMock(return_value=self.store)):
            await self.collector.init()
        self.assertIsNot(self.collector.trade_db, self.store.db)
        started, release = threading.Event(), threading.Event()

        def hold_shared_reader():
            started.set()
            if not release.wait(5):
                raise TimeoutError('test shared reader was not released')
            return 1

        await self.store.db.create_function('hold_shared_reader', 0, hold_shared_reader)
        blocked = asyncio.create_task(self.store.db.execute_fetchall('SELECT hold_shared_reader()'))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        try:
            self.assertTrue(await asyncio.wait_for(
                self.collector.commit_trade(self.market, self.trade('dedicated', 2)), 2))
        finally:
            release.set()
            await blocked
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 1)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM realtime_events'))[0],
                         len(self.collector.bars(self.market)) + 1)
        await self.collector.close()
        self.assertIsNone(self.collector.trade_db)
        self.assertFalse(self.collector.initialized)

    async def test_failed_dedicated_trade_does_not_rollback_shared_read_transaction(self):
        await self.collector._open_trade_db()
        await self.store.db.execute("""CREATE TRIGGER fail_dedicated_candle BEFORE INSERT ON candles
            WHEN NEW.bar='1m' BEGIN SELECT RAISE(ABORT, 'failed-dedicated-candle'); END""")
        await self.store.db.commit()
        await self.store.db.execute('BEGIN')
        await self.store.fetchone('SELECT COUNT(*) FROM facts')
        self.assertTrue(self.store.db.in_transaction)
        try:
            with self.assertRaisesRegex(Exception, 'failed-dedicated-candle'):
                await self.collector.commit_trade(self.market, self.trade('isolated-failure', 2))
            self.assertTrue(self.store.db.in_transaction)
        finally:
            await self.store.db.rollback()
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 0)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM realtime_events'))[0], 0)

    async def test_failed_dedicated_fact_write_does_not_rollback_shared_read_transaction(self):
        await self.collector._open_trade_db()
        self.assertIs(self.collector.trade_store.db, self.collector.trade_db)
        self.assertIs(self.collector.trade_store._write_lock, self.store._write_lock)
        await self.store.db.execute('BEGIN')
        await self.store.fetchone('SELECT COUNT(*) FROM facts')
        self.assertTrue(self.store.db.in_transaction)
        try:
            with self.assertRaisesRegex(Exception, 'NOT NULL constraint failed'):
                await self.collector.trade_store.put('pool-quote', None, {'price': 1})
            self.assertTrue(self.store.db.in_transaction)
            self.assertFalse(self.collector.trade_db.in_transaction)
        finally:
            await self.store.db.rollback()

    async def test_cancelled_dedicated_fact_write_rolls_back_before_releasing_write_lock(self):
        await self.collector._open_trade_db()
        started, release = threading.Event(), threading.Event()

        def hold_fact_insert():
            started.set()
            if not release.wait(5):
                raise TimeoutError('test fact insert was not released')
            return 1

        await self.collector.trade_db.create_function('hold_fact_insert', 0, hold_fact_insert)
        await self.collector.trade_db.execute('''CREATE TEMP TRIGGER hold_fact_insert_trigger
            BEFORE INSERT ON facts BEGIN SELECT hold_fact_insert(); END''')
        task = asyncio.create_task(self.collector.trade_store.put(
            'pool-quote', POOL, {'memePerStock': 3}))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        try:
            task.cancel()
            await asyncio.sleep(.02)
            self.assertTrue(self.store._write_lock.locked())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertFalse(self.store._write_lock.locked())
        self.assertFalse(self.collector.trade_db.in_transaction)
        self.assertIsNone(await self.store.get('pool-quote', POOL))

    async def test_raw_log_and_processed_flag_bypass_busy_shared_connection(self):
        log = await self.prepare_logs()
        await self.collector._open_trade_db()
        started, release = threading.Event(), threading.Event()
        blocked = None
        lookups = 0
        real_fetchone = self.store.fetchone

        def hold_shared_reader():
            started.set()
            if not release.wait(5):
                raise TimeoutError('test shared reader was not released')
            return 1

        async def intercept_fetchone(query, parameters=()):
            nonlocal blocked, lookups
            result = await real_fetchone(query, parameters)
            if query.startswith('SELECT processed FROM chain_stream_logs'):
                lookups += 1
                if lookups == 2:
                    blocked = asyncio.create_task(self.store.db.execute_fetchall('SELECT hold_shared_reader()'))
                    self.assertTrue(await asyncio.to_thread(started.wait, 1))
            return result

        async def observe_raw_before_trade(market, trade):
            rows = await self.collector.trade_db.execute_fetchall(
                'SELECT processed FROM chain_stream_logs WHERE chain=? AND id=?',
                (self.collector.chain, event_key(log)))
            self.assertEqual(rows[0][0], 0)
            return True

        await self.store.db.create_function('hold_shared_reader', 0, hold_shared_reader)
        with patch.object(self.store, 'fetchone', new=intercept_fetchone), \
             patch.object(self.collector, 'commit_trade', new=observe_raw_before_trade), \
             patch.object(self.collector, 'pool_asset_quote', new=AsyncMock()):
            task = asyncio.create_task(self.collector.process_log(log))
            completed_while_blocked = False
            try:
                done, _ = await asyncio.wait({task}, timeout=1.5)
                completed_while_blocked = task in done
                if completed_while_blocked:
                    rows = await self.collector.trade_db.execute_fetchall(
                        'SELECT processed FROM chain_stream_logs WHERE chain=? AND id=?',
                        (self.collector.chain, event_key(log)))
                    self.assertEqual(rows[0][0], 1)
            finally:
                release.set()
                if blocked is not None:
                    await blocked
                await task
        self.assertTrue(completed_while_blocked)

    async def test_cancelled_dedicated_trade_rolls_back_before_releasing_write_lock(self):
        await self.collector._open_trade_db()
        started, release = threading.Event(), threading.Event()

        def hold_trade_insert():
            started.set()
            if not release.wait(5):
                raise TimeoutError('test trade insert was not released')
            return 1

        await self.collector.trade_db.create_function('hold_trade_insert', 0, hold_trade_insert)
        await self.collector.trade_db.execute('''CREATE TEMP TRIGGER hold_trade_insert_trigger
            BEFORE INSERT ON trades BEGIN SELECT hold_trade_insert(); END''')
        task = asyncio.create_task(self.collector.commit_trade(
            self.market, self.trade('cancelled-dedicated', 2)))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        try:
            task.cancel()
            await asyncio.sleep(.02)
            self.assertTrue(self.store._write_lock.locked())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertFalse(self.store._write_lock.locked())
        self.assertFalse(self.collector.trade_db.in_transaction)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 0)

    async def test_chain_trade_preserves_candle_then_trade_event_order_and_observation(self):
        await self.collector.commit_trade(self.market, self.trade('ordered', 2))
        frames = await self.store.fetchall('SELECT event,body FROM realtime_events ORDER BY id')
        self.assertEqual([row[0] for row in frames],
                         ['candle'] * len(self.collector.bars(self.market)) + ['trade'])
        self.assertEqual([json.loads(row[1])['bar'] for row in frames[:-1]],
                         self.collector.bars(self.market))
        for bar in self.collector.bars(self.market):
            meta = await self.store.get('candle-meta', self.market.candle_key(bar))
            opened = json.loads(next(row[1] for row in frames[:-1]
                                     if json.loads(row[1])['bar'] == bar))['row']['t']
            self.assertEqual(meta['rowObservedAt'][str(opened)], meta['lastObservationAt'])
            self.assertEqual(meta['lastSourceEventAt'], self.at)

    async def test_failed_chain_candle_rolls_back_trade_and_all_events(self):
        # Fail after the first bar in the candle batch has been written.
        await self.store.db.execute("""CREATE TRIGGER fail_chain_candle BEFORE INSERT ON candles
            WHEN NEW.bar='1m' BEGIN SELECT RAISE(ABORT, 'failed-candle'); END""")
        await self.store.db.commit()
        with self.assertRaisesRegex(Exception, 'failed-candle'):
            await self.collector.commit_trade(self.market, self.trade('rollback', 2))
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 0)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM candles'))[0], 0)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM realtime_events'))[0], 0)
        self.assertIsNone(await self.store.get('market-registry', self.market.storage))
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM facts'))[0], 0)

        await self.store.db.execute('DROP TRIGGER fail_chain_candle')
        await self.store.db.commit()
        self.assertTrue(await self.collector.commit_trade(self.market, self.trade('rollback', 2)))
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM realtime_events'))[0],
                         len(self.collector.bars(self.market)) + 1)

    async def test_reorg_rebuilds_volume_and_can_remove_last_candle(self):
        await self.collector.commit_trade(self.market, self.trade("first", 2))
        await self.collector.commit_trade(self.market, self.trade("second", 5, 1))
        await self.collector.retract_trade(self.market, "second")
        candle = await self.store.get("market-candle", self.market.storage + ":1m")
        self.assertEqual(candle["row"]["v"], 2)
        await self.collector.retract_trade(self.market, "first")
        cur = await self.store.db.execute("SELECT COUNT(*) FROM candles WHERE asset=?", (self.store.key(self.market.storage),))
        self.assertEqual((await cur.fetchone())[0], 0)
        cur = await self.store.db.execute("SELECT body FROM realtime_events WHERE event='trade-remove'")
        removed = [json.loads(r[0])["ids"][0] for r in await cur.fetchall()]
        self.assertEqual(removed, ["second", "first"])

    async def test_history_correction_does_not_roll_current_bucket_back(self):
        await self.collector.commit_trade(self.market, self.trade("new", 2, 120_000))
        await self.collector.commit_trade(self.market, self.trade("old", 1, 0))
        current = await self.store.get("market-candle", self.market.storage + ":1m")
        self.assertEqual(current["row"]["t"], self.at + 120_000)

    async def test_rebuilt_historical_and_current_candles_ignore_duplicate_replay(self):
        def priced(ident, size, offset, price):
            return {**self.trade(ident, size, offset),
                    'price': price, 'quoteQuantity': price * size}

        for trade in (priced('late', 2, 120_040, 5),
                      priced('early', 1, 120_010, 2),
                      priced('history', 3, 0, 3)):
            self.assertTrue(await self.collector.commit_trade(self.market, trade))

        # Missing accumulators force an ordered rebuild from persisted trades.
        for bar, opened in (('1m', self.at + 120_000),
                            ('1H', self.at - self.at % 3_600_000)):
            await self.store.db.execute('DELETE FROM facts WHERE kind=? AND id=?',
                (self.store.key('pool-candle-acc'),
                 f'{self.market.storage}:{bar}:{opened}'))
        await self.store.db.commit()
        middle = priced('middle', 4, 120_020, 4)
        self.assertTrue(await self.collector.commit_trade(self.market, middle))

        rows = await self.store.fetchall(
            'SELECT bar,openTime,open,high,low,close,volume,volumeUsd FROM candles WHERE asset=? ORDER BY bar,openTime',
            (self.store.key(self.market.storage),))
        candles = {(bar, opened): (o, h, l, c, v, vu)
                   for bar, opened, o, h, l, c, v, vu in rows}
        self.assertEqual(candles[('1m', self.at)], (3, 3, 3, 3, 3, 9))
        self.assertEqual(candles[('1m', self.at + 120_000)], (2, 5, 2, 5, 7, 28))
        self.assertEqual(candles[('1H', self.at - self.at % 3_600_000)],
                         (3, 5, 2, 5, 10, 37))
        current = await self.store.get('market-candle', self.market.storage + ':1m')
        self.assertEqual((current['row']['t'], current['row']['o'], current['row']['v']),
                         (self.at + 120_000, 2, 7))

        events_before = (await self.store.fetchone('SELECT COUNT(*) FROM realtime_events'))[0]
        self.assertFalse(await self.collector.commit_trade(self.market, middle))
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 4)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM realtime_events'))[0],
                         events_before)
        self.assertEqual(await self.store.fetchall(
            'SELECT bar,openTime,open,high,low,close,volume,volumeUsd FROM candles WHERE asset=? ORDER BY bar,openTime',
            (self.store.key(self.market.storage),)), rows)

    async def test_failed_range_does_not_advance_checkpoint(self):
        self.collector.pools = {f"0x{i:040x}": {} for i in range(65)}
        await self.store.put("chain-stream-cursor", "pools", {"block": 50, "hash": "0x50"})
        calls = 0

        async def rpc(method, params):
            nonlocal calls
            if method == "eth_getBlockByNumber":
                n = 51 if params[0] == "latest" else int(params[0], 16)
                return {"number": hex(n), "hash": f"0x{n}", "timestamp": hex(self.at // 1000)}
            calls += 1
            if calls == 2:
                raise RuntimeError("provider interrupted second filter page")
            return []

        self.collector.rpc = rpc
        with self.assertRaises(RuntimeError):
            await self.collector.catch_up()
        self.assertEqual((await self.store.get("chain-stream-cursor", "pools"))["block"], 50)

    async def test_recent_lane_catches_tip_without_claiming_historical_gap(self):
        await self.store.put('chain-stream-cursor', 'pools', {
            'block': 50, 'hash': '0x32', 'coverageFrom': 40})
        self.collector.pools = {POOL: {}}
        self.collector.last_prune = self.at
        head = 1000
        ranges = []

        async def rpc(method, params):
            if method == 'eth_getLogs':
                ranges.append((int(params[0]['fromBlock'], 16), int(params[0]['toBlock'], 16)))
                return []
            block = head if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': hex(block), 'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        self.collector.retract_orphans = AsyncMock()
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            progress = await self.collector.reconcile_step()
            await self.collector.status_fact(force=True)
        historical = await self.store.get('chain-stream-cursor', 'pools')
        recent = await self.store.get('chain-stream-cursor', 'pools-live')
        status = await self.store.get('chain-stream', 'pools')
        self.assertFalse(progress['caughtUp'])
        self.assertEqual(ranges, [(701, 800), (801, 900), (901, 1000), (51, 150)])
        self.assertEqual((historical['block'], historical['coverageFrom']), (150, 40))
        self.assertEqual((recent['block'], recent['coverageFrom']), (1000, 701))
        self.assertEqual(status['lastProcessedBlock'], 150)
        self.assertEqual(status['lastNearTipBlock'], 1000)
        self.assertEqual(status['historicalGapBlocks'], 550)
        self.assertEqual(status['nearTipLagBlocks'], 0)
        self.assertEqual(status['coverageFrom'], 40)
        self.assertEqual(status['historicalScan']['lastSuccessfulBlock'], 150)
        self.assertEqual(status['nearTipScan']['lastSuccessfulBlock'], 1000)
        self.assertEqual(status['nearTipScan']['lastRangePages'], 1)
        self.assertEqual(status['nearTipScan']['lastRangeLogs'], 0)
        self.assertGreaterEqual(status['nearTipScan']['lastPageRpcMs'], 0)

        # A new process resumes the saved recent watermark, including the
        # interval missed while it was offline, without changing old history.
        restarted = ChainPoolStream('196')
        restarted.s = self.store
        restarted.pools = {POOL: {}}
        restarted.last_prune = self.at
        restarted.rpc = rpc
        restarted.retract_orphans = AsyncMock()
        head = 1050
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            await restarted.reconcile_step()
        self.assertEqual(ranges[-2:], [(1001, 1050), (151, 250)])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools-live'))['block'], 1050)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 250)

    async def test_recent_lane_failure_never_advances_either_watermark(self):
        await self.store.put('chain-stream-cursor', 'pools', {'block': 50, 'hash': '0x32'})
        self.collector.pools = {POOL: {}}

        async def rpc(method, params):
            if method == 'eth_getLogs':
                raise RuntimeError('provider-unavailable')
            block = 1000 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': hex(block), 'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        with self.assertRaisesRegex(RuntimeError, 'provider-unavailable'):
            await self.collector.reconcile_step()
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 50)
        self.assertIsNone(await self.store.get('chain-stream-cursor', 'pools-live'))
        self.assertIsNone(await self.store.get('chain-stream-scan', 'pools'))
        recent_scan = await self.store.get('chain-stream-scan', 'pools-live')
        self.assertIn('provider-unavailable', recent_scan['lastError'])
        self.assertIsNotNone(recent_scan['lastAttemptAt'])
        self.assertIsNotNone(recent_scan['lastErrorAt'])
        self.assertIsNone(recent_scan['lastPageLogs'])
        self.assertGreaterEqual(recent_scan['lastPageRpcMs'], 0)
        await self.collector.status_fact(error='RuntimeError:provider-unavailable', force=True)
        await self.collector.status_fact(force=True)
        status = await self.store.get('chain-stream', 'pools')
        self.assertIsNone(status['error'])
        self.assertEqual(status['lastError'], 'RuntimeError:provider-unavailable')
        self.assertEqual(status['nearTipScan']['lastError'], recent_scan['lastError'])

        async def recovered_rpc(method, params):
            if method == 'eth_getLogs':
                return []
            block = 1000 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': hex(block), 'timestamp': hex(self.at // 1000)}

        self.collector.rpc = recovered_rpc
        self.collector.retract_orphans = AsyncMock()
        self.collector.last_prune = self.at
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            await self.collector.reconcile_step()
            await self.collector.status_fact(force=True)
        recovered = await self.store.get('chain-stream', 'pools')
        self.assertEqual(recovered['nearTipScan']['lastSuccessfulBlock'], 1000)
        self.assertEqual(recovered['nearTipScan']['lastPageLogs'], 0)
        self.assertGreaterEqual(recovered['nearTipScan']['lastRangeDurationMs'], 0)
        self.assertIn('provider-unavailable', recovered['nearTipScan']['lastError'])
        self.assertEqual(recovered['lastError'], 'RuntimeError:provider-unavailable')

    async def test_gap_excludes_recent_observation_range_after_restart(self):
        await self.store.put('chain-stream-cursor', 'pools', {
            'block': 100, 'coverageFrom': 50})
        await self.store.put('chain-stream-cursor', 'pools-live', {
            'block': 900, 'coverageFrom': 800})
        await self.store.put('chain-stream-scan', 'pools', {
            'lastAttemptAt': self.at - 2000, 'lastErrorAt': self.at - 1000,
            'lastError': 'TimeoutError:historical page timed out'})
        restarted = ChainPoolStream('196')
        restarted.s = self.store
        restarted.latest_head = 950
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            await restarted.status_fact(force=True)
        status = await self.store.get('chain-stream', 'pools')
        self.assertEqual(status['historicalGapBlocks'], 699)
        self.assertEqual(status['nearTipLagBlocks'], 50)
        self.assertEqual(status['coverageFrom'], 50)
        self.assertEqual(status['historicalScan']['lastError'], 'TimeoutError:historical page timed out')

    async def test_long_recent_outage_gets_priority_until_its_gap_is_scanned(self):
        await self.store.put('chain-stream-cursor', 'pools', {'block': 50, 'hash': '0x32'})
        await self.store.put('chain-stream-cursor', 'pools-live', {
            'block': 100, 'hash': '0x64', 'coverageFrom': 70})
        self.collector.pools = {POOL: {}}
        ranges = []

        async def rpc(method, params):
            if method == 'eth_getLogs':
                ranges.append((int(params[0]['fromBlock'], 16), int(params[0]['toBlock'], 16)))
                return []
            block = 1000 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': hex(block), 'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        self.collector.retract_orphans = AsyncMock()
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            progress = await self.collector.reconcile_step()
        self.assertFalse(progress['caughtUp'])
        self.assertEqual(ranges, [(101, 200), (201, 300), (301, 400)])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 50)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools-live'))['block'], 400)

    async def test_distant_recent_lane_restarts_at_tip_and_preserves_historical_gap(self):
        await self.store.put('chain-stream-cursor', 'pools', {
            'block': 50, 'hash': '0x32', 'coverageFrom': 40})
        await self.store.put('chain-stream-cursor', 'pools-live', {
            'block': 100, 'hash': '0x64', 'coverageFrom': 70})
        self.collector.pools = {POOL: {}}
        self.collector.last_prune = self.at
        ranges = []

        async def rpc(method, params):
            if method == 'eth_getLogs':
                ranges.append((int(params[0]['fromBlock'], 16), int(params[0]['toBlock'], 16)))
                return []
            block = 10_000 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': hex(block), 'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        self.collector.retract_orphans = AsyncMock()
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            await self.collector.reconcile_step()
            await self.collector.status_fact(force=True)
        recent = await self.store.get('chain-stream-cursor', 'pools-live')
        historical = await self.store.get('chain-stream-cursor', 'pools')
        status = await self.store.get('chain-stream', 'pools')
        self.assertEqual(ranges, [(9701, 9800), (9801, 9900), (9901, 10_000), (51, 150)])
        self.assertEqual((recent['block'], recent['coverageFrom']), (10_000, 9701))
        self.assertEqual(recent['rebasedFromBlock'], 100)
        self.assertEqual(historical['block'], 150)
        self.assertEqual(status['nearTipLagBlocks'], 0)
        self.assertEqual(status['historicalGapBlocks'], 9550)
        self.assertEqual(status['nearTipRebasedFromBlock'], 100)

    async def test_recent_rebase_failure_does_not_certify_skipped_blocks(self):
        await self.store.put('chain-stream-cursor', 'pools', {'block': 50, 'hash': '0x32'})
        await self.store.put('chain-stream-cursor', 'pools-live', {
            'block': 100, 'hash': '0x64', 'coverageFrom': 70})
        self.collector.pools = {POOL: {}}

        async def rpc(method, params):
            if method == 'eth_getLogs':
                raise RuntimeError('recent-provider-unavailable')
            block = 10_000 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': hex(block), 'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        with self.assertRaisesRegex(RuntimeError, 'recent-provider-unavailable'):
            await self.collector.reconcile_step()
        recent = await self.store.get('chain-stream-cursor', 'pools-live')
        historical = await self.store.get('chain-stream-cursor', 'pools')
        self.assertEqual((recent['block'], recent['coverageFrom']), (9700, 9701))
        self.assertEqual(historical['block'], 50)
        self.assertEqual((await self.store.get('chain-stream-scan', 'pools-live'))['lastError'],
                         'RuntimeError:recent-provider-unavailable')

    async def test_quiet_elapsed_candle_closes_without_invented_trades(self):
        with patch('app.collectors.chain_stream.now_ms',return_value=self.at+1000):
            await self.collector.commit_trade(self.market,self.trade('only',2))
        current=await self.store.get('market-candle',self.market.storage+':1m')
        self.assertFalse(current['row']['confirmed'])
        with patch('app.collectors.chain_stream.now_ms',return_value=self.at+61000):
            await self.collector.close_elapsed_bars()
        current=await self.store.get('market-candle',self.market.storage+':1m')
        self.assertTrue(current['row']['confirmed'])
        self.assertEqual(current['row']['v'],2)
        self.assertEqual(len(await self.store.recent_trades(self.market.storage,10)),1)

    async def test_elapsed_candle_batches_commit_independently_and_retry_without_duplicate_events(self):
        await self.store.put('market-registry', self.market.storage, self.market.record())
        candles = []
        accumulators = []
        for index in range(26):
            opened = self.at + index * 60_000
            candles.append((self.store.key(self.market.storage), '1m', opened,
                            1, 1, 1, 1, 1, 1, 0))
            body = {'t': opened, 'o': 1, 'h': 1, 'l': 1, 'c': 1,
                    'v': 1, 'vu': 1, 'confirmed': False}
            accumulators.append((self.store.key('pool-candle-acc'),
                                 self.market.storage + ':1m:' + str(opened), json.dumps(body)))
        await self.store.db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)', candles)
        await self.store.db.executemany('INSERT INTO facts VALUES (?,?,?)', accumulators)
        await self.store.db.commit()

        original = self.collector._write_candle
        writes = 0

        async def fail_after_first_batch(*args):
            nonlocal writes
            writes += 1
            if writes == 26:
                raise RuntimeError('second batch failed')
            await original(*args)

        self.collector._write_candle = fail_after_first_batch
        stamp = self.at + 27 * 60_000
        with patch('app.collectors.chain_stream.now_ms', return_value=stamp):
            with self.assertRaisesRegex(RuntimeError, 'second batch failed'):
                await self.collector.close_elapsed_bars()
        self.assertEqual((await self.store.fetchone('SELECT count(*) FROM candles WHERE confirmed=1'))[0], 25)
        self.assertEqual((await self.store.fetchone("SELECT count(*) FROM realtime_events WHERE event='candle'"))[0], 25)

        self.collector._write_candle = original
        with patch('app.collectors.chain_stream.now_ms', return_value=stamp):
            await self.collector.close_elapsed_bars()
        self.assertEqual((await self.store.fetchone('SELECT count(*) FROM candles WHERE confirmed=1'))[0], 26)
        self.assertEqual((await self.store.fetchone("SELECT count(*) FROM realtime_events WHERE event='candle'"))[0], 26)

    async def test_empty_candle_closure_never_waits_for_a_writer(self):
        await self.store._write_lock.acquire()
        try:
            await asyncio.wait_for(self.collector.close_elapsed_bars(), .3)
        finally:
            self.store._write_lock.release()

    async def test_chain_trade_keeps_wire_receipt_distinct_from_storage_time(self):
        trade = {**self.trade('wire-time', 2), 'receivedAt': self.at + 50}
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at + 5000):
            await self.collector.commit_trade(self.market, trade)
        stored = (await self.store.recent_trades(self.market.storage, 1))[0]
        self.assertEqual(stored['receivedAt'], self.at + 50)
        self.assertEqual(stored['persistedAt'], self.at + 5000)
        frame = await self.store.get('market-candle', self.market.storage + ':1m')
        self.assertEqual(frame['receivedAt'], self.at + 50)

    async def test_range_reorg_does_not_certify_old_fork(self):
        self.collector.pools={POOL:{}}
        await self.store.put('chain-stream-cursor','pools',{'block':50,'hash':'old50'})
        end_reads=0
        checked=[]
        async def rpc(method,params):
            nonlocal end_reads
            if method=='eth_getLogs':return []
            n=51 if params[0]=='latest' else int(params[0],16)
            if params[0]=='0x33':end_reads+=1
            h=('old' if end_reads<2 else 'new')+str(n)
            return {'number':hex(n),'hash':h,'timestamp':hex(self.at//1000)}
        async def retract(start,end,canonical_hashes=None):checked.append((start,end))
        self.collector.rpc=rpc
        self.collector.retract_orphans=retract
        with self.assertRaisesRegex(RuntimeError,'chain-reorg-during-range'):
            await self.collector.catch_up()
        self.assertEqual(checked,[])  # Do not retract using a changed range's hashes.
        self.assertEqual((await self.store.get('chain-stream-cursor','pools'))['block'],50)

    async def test_anchored_log_hashes_avoid_duplicate_block_rpc_but_check_orphan_only_blocks(self):
        await self.prepare_logs()
        for height, block_hash in [(200, '0xabc'), (201, 'orphan')]:
            await self.store.db.execute('INSERT INTO chain_stream_logs VALUES (?,?,?,?,?,?,?,1)',
                ('196', str(height), POOL, height, block_hash, self.at, '{}'))
        await self.store.db.commit()
        self.collector.rpc = AsyncMock(return_value={'hash': 'canonical201'})
        self.collector.retract_block = AsyncMock()
        await self.collector.retract_orphans(200, 201, {200: '0xabc'})
        self.collector.rpc.assert_awaited_once_with('eth_getBlockByNumber', ['0xc9', False])
        self.collector.retract_block.assert_awaited_once_with('orphan')

    async def test_log_header_recovers_indexing_lag_without_restarting_subscription(self):
        log = await self.prepare_logs()
        self.collector.headers.clear()
        header = {'number': log['blockNumber'], 'hash': log['blockHash'], 'timestamp': hex(self.at//1000)}
        self.collector.rpc = AsyncMock(side_effect=[None, header])
        with patch('app.collectors.chain_stream.asyncio.sleep', new=AsyncMock()):
            await self.collector.process_log(log)
        self.assertEqual(self.collector.processed, 1)
        self.assertEqual(self.collector.reconnects, 0)
        self.assertEqual(self.collector.rpc.await_args_list[1].args,
                         ('eth_getBlockByNumber', [log['blockNumber'], False]))

    async def test_missing_hash_of_reverted_queued_log_is_retracted_not_reconnected(self):
        log = await self.prepare_logs()
        self.collector.headers.clear()
        self.collector.rpc = AsyncMock(side_effect=[None, {'hash': 'replacement'}])
        self.collector.retract_block = AsyncMock()
        with patch('app.collectors.chain_stream.asyncio.sleep', new=AsyncMock()):
            await self.collector.process_log(log)
        self.collector.retract_block.assert_awaited_once_with('0xabc')
        self.assertEqual(self.collector.processed, 0)


if __name__ == "__main__":
    unittest.main()
