import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.db import ResearchStore
from app.collectors.chain_stream import ChainPoolStream, LIVE_STAGE_NAMES, SWAP_V2, SWAP_V3, SYNC_V2, WATCHED_POOL_BURST, decode_swap, event_key
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

    async def _wait_until(self, predicate):
        while not predicate():
            await asyncio.sleep(.001)

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

    async def test_status_and_scan_diagnostics_bypass_busy_shared_connection(self):
        await self.collector._open_status_db()
        await self.store.put('chain-stream-cursor', 'pools', {'block': 50})
        await self.store.put('chain-stream-cursor', 'pools-live', {'block': 75})
        self.collector.latest_head = 75
        self.collector._catch_up = AsyncMock(return_value={'caughtUp': True})
        release, blocked = await self.busy_shared_reader()
        task = asyncio.create_task(self.collector.status_fact(force=True))
        completed_while_blocked = False
        try:
            done, _ = await asyncio.wait({task}, timeout=1.5)
            completed_while_blocked = task in done
            if completed_while_blocked:
                await task
                await self.collector.catch_up('pools-live')
                status = await self.collector.status_store.get('chain-stream', 'pools')
                diagnostic = await self.collector.status_store.get('chain-stream-scan', 'pools-live')
                self.assertEqual((status['lastProcessedBlock'], status['lastNearTipBlock']), (50, 75))
                self.assertIn('lastCompletedAt', diagnostic)
        finally:
            release.set()
            await blocked
            await task
        self.assertTrue(completed_while_blocked)

    async def test_scheduled_status_does_not_hold_live_processing(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_status(**_):
            started.set()
            await release.wait()

        self.collector.status_fact = slow_status
        self.collector.process_log = AsyncMock()
        self.collector.schedule_status_fact(force=True)
        await asyncio.wait_for(started.wait(), .5)
        try:
            await asyncio.wait_for(self.collector.process_queued({'id': 'live'}), .5)
            self.collector.process_log.assert_awaited_once_with({'id': 'live'})
        finally:
            release.set()
            await self.collector.status_task

    async def test_scheduled_status_preserves_queued_failure(self):
        errors = []

        async def record_status(*, error=None, force=False):
            errors.append(error)

        self.collector.status_fact = record_status
        self.collector.schedule_status_fact('provider-unavailable', force=True)
        self.collector.schedule_status_fact(force=True)
        await self.collector.status_task
        self.assertEqual(errors, ['provider-unavailable'])

    async def test_scheduled_bar_close_does_not_hold_live_processing(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_close():
            started.set()
            await release.wait()

        self.collector.close_elapsed_bars = slow_close
        self.collector.process_log = AsyncMock()
        self.collector.last_watches = self.at
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            self.collector.schedule_housekeeping()
        await asyncio.wait_for(started.wait(), .5)
        try:
            await asyncio.wait_for(self.collector.process_queued({'id': 'live'}), .5)
            self.collector.process_log.assert_awaited_once_with({'id': 'live'})
        finally:
            release.set()
            await self.collector.bar_close_task

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

    async def test_swap_log_keeps_native_trade_unit_without_inventing_tx_wallet(self):
        swap = await self.prepare_logs()
        # A V2 Swap's indexed sender is the pool caller (often a router),
        # while wallet means transaction.from, which is absent from this log.
        swap['topics'] = [SWAP_V2, '0x' + '0' * 24 + TOKEN1[2:],
                          '0x' + '0' * 24 + TOKEN0[2:]]
        await self.collector.process_log(swap)
        market = self.collector.market(self.collector.pools[POOL], TOKEN0)
        body = (await self.store.recent_trades(market.storage, 1))[0]
        self.assertEqual((body['price'], body['size'], body['quoteQuantity']), (3, 2, 6))
        self.assertEqual(body['priceCurrency'], TOKEN1)
        self.assertEqual(body['quoteToken'], TOKEN1)
        self.assertIsNone(body['volume'])
        self.assertNotIn('wallet', body)

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
        release, blocked = await self.busy_shared_reader()
        decoded = decode_swap(log, TOKEN0, TOKEN0, 18, 6)
        task = asyncio.create_task(self.collector.pool_asset_quote(
            self.collector.pools[POOL], TOKEN0, decoded, self.at, 200, event_key(log)))
        completed_while_blocked = False
        quote = None
        try:
            done, _ = await asyncio.wait({task}, timeout=1.5)
            completed_while_blocked = task in done
            if completed_while_blocked:
                await task
                quote = await self.collector.trade_store.get('asset', TOKEN0)
        finally:
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
        release, blocked = await self.busy_shared_reader()
        with patch.object(self.collector, 'reserve_valuation', new=AsyncMock()):
            task = asyncio.create_task(self.collector.process_log(sync))
            completed_while_blocked = False
            quote = None
            try:
                done, _ = await asyncio.wait({task}, timeout=1.5)
                completed_while_blocked = task in done
                if completed_while_blocked:
                    await task
                    quote = await self.collector.trade_store.get('pool-quote', POOL)
            finally:
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
        release, blocked = await self.busy_shared_reader()
        task = asyncio.create_task(self.collector.reserve_valuation(
            self.collector.pools[POOL], (2 * 10 ** 18, 6_000_000), 18, 6,
            self.at, 200, '0xabc', 2))
        completed_while_blocked = False
        relation = None
        try:
            done, _ = await asyncio.wait({task}, timeout=1.5)
            completed_while_blocked = task in done
            if completed_while_blocked:
                await task
                relation = await self.collector.trade_store.get('relation', 'relation')
        finally:
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
        for _ in range(32):
            self.collector.queue.put_nowait(log)
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            paused = await self.collector.catch_up()
            self.assertFalse(paused['caughtUp'])
            self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 50)
            self.collector.process_log.assert_not_awaited()
            for _ in range(32):
                self.collector.queue.get_nowait()
            finished = await asyncio.wait_for(self.collector.catch_up(), .5)
            self.assertTrue(finished['caughtUp'])
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

    async def test_live_wait_budget_favors_shallow_queue_without_starving_replay(self):
        async def waited_with_pending(count):
            self.collector.queue = asyncio.Queue(maxsize=4096)
            for index in range(count):
                self.collector.queue.put_nowait(index)
            elapsed = 0.0
            sleeps = []

            async def advance(seconds):
                nonlocal elapsed
                sleeps.append(seconds)
                elapsed += seconds

            with patch('app.collectors.chain_stream.time.monotonic', side_effect=lambda: elapsed), \
                    patch('app.collectors.chain_stream.asyncio.sleep', side_effect=advance):
                self.assertFalse(await self.collector.wait_for_live())
            self.assertEqual(self.collector.queue.qsize(), count)
            return sum(sleeps)

        self.assertAlmostEqual(await waited_with_pending(2), .02, places=3)
        self.assertAlmostEqual(await waited_with_pending(20), .25, places=3)
        self.collector.queue = asyncio.Queue(maxsize=4096)
        for index in range(32):
            self.collector.queue.put_nowait(index)
        self.assertTrue(self.collector.replay_under_pressure())
        for _ in range(24):
            self.collector.queue.get_nowait()
        self.assertFalse(self.collector.replay_under_pressure())

    async def test_repeated_queue_spikes_resume_same_bnb_range_without_refetch(self):
        await self.store.put('chain-stream-cursor', 'pools', {'block': 199, 'hash': hex(199)})
        collector = ChainPoolStream('56')
        collector.s = self.store
        collector.pools = {f'0x{i:040x}': {} for i in range(1, 18)}
        collector.last_prune = self.at
        collector.retract_orphans = AsyncMock()
        collector.process_log = AsyncMock()
        pages = []

        async def rpc(method, params):
            if method == 'eth_getLogs':
                addresses = params[0]['address']
                pages.append(addresses)
                for index in range(32):
                    collector.queue.put_nowait((len(pages), index))
                return [{'blockNumber': hex(200), 'blockHash': hex(200),
                         'transactionIndex': '0x0', 'logIndex': hex(len(pages))}]
            block = 499 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': hex(block), 'timestamp': hex(self.at // 1000)}

        async def drain_live():
            for expected_page in range(1, 4):
                while len(pages) < expected_page or not collector.replay_paused:
                    await asyncio.sleep(.001)
                self.assertEqual(collector.queue.qsize(), 32)
                for _ in range(24):
                    collector.queue.get_nowait()
                self.assertFalse(collector.replay_under_pressure())
                for _ in range(8):
                    collector.queue.get_nowait()

        collector.rpc = rpc
        consumer = asyncio.create_task(drain_live())
        try:
            with patch.object(self.store, 'put', wraps=self.store.put) as put:
                with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
                    result = await asyncio.wait_for(collector.catch_up(), 2)
                cursor_writes = [call.args[2]['block'] for call in put.call_args_list
                                 if call.args[:2] == ('chain-stream-cursor', 'pools')]
            await consumer
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await collector.close()
        self.assertTrue(result['caughtUp'])
        self.assertEqual([len(page) for page in pages], [8, 8, 1])
        self.assertEqual(cursor_writes, [499])
        self.assertEqual(collector.process_log.await_count, 3)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 499)
        self.assertEqual((await self.store.get('chain-stream-scan', 'pools'))['pausedForLiveAt'], self.at)

    async def test_cancelled_pressure_wait_leaves_cursor_and_replays_all_pages(self):
        await self.store.put('chain-stream-cursor', 'pools', {'block': 199, 'hash': hex(199)})
        self.collector.pools = {f'0x{i:040x}': {} for i in range(65)}
        self.collector.last_prune = int(time.time() * 1000)
        self.collector.retract_orphans = AsyncMock()
        self.collector.process_log = AsyncMock()
        pages = []

        async def rpc(method, params):
            if method == 'eth_getLogs':
                pages.append(params[0]['address'])
                if len(pages) == 1:
                    for index in range(4):
                        self.collector.queue.put_nowait(index)
                return [{'blockNumber': hex(200), 'blockHash': hex(200),
                         'transactionIndex': '0x0', 'logIndex': hex(len(pages))}]
            block = 200 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': hex(block), 'timestamp': hex(self.at // 1000)}

        self.collector.queue = asyncio.Queue(maxsize=8)
        self.collector.rpc = rpc
        replay = asyncio.create_task(self.collector.catch_up())
        try:
            await asyncio.wait_for(self._wait_until(lambda: self.collector.replay_paused), 1)
            await asyncio.sleep(.12)
            self.assertEqual(len(pages), 1)
            self.assertFalse(replay.done())  # Waiting yields; it cannot spin through pages.
            replay.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(replay, .5)
        finally:
            replay.cancel()
            await asyncio.gather(replay, return_exceptions=True)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 199)
        self.collector.process_log.assert_not_awaited()
        while not self.collector.queue.empty():
            self.collector.queue.get_nowait()
        result = await asyncio.wait_for(self.collector.catch_up(), 1)
        self.assertTrue(result['caughtUp'])
        self.assertEqual(len(pages), 3)  # First page is fetched again after cancellation.
        self.assertEqual(self.collector.process_log.await_count, 2)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 200)

    async def test_reorg_while_waiting_retries_range_before_certifying_coverage(self):
        await self.store.put('chain-stream-cursor', 'pools', {'block': 199, 'hash': hex(199)})
        self.collector.pools = {f'0x{i:040x}': {} for i in range(65)}
        self.collector.queue = asyncio.Queue(maxsize=8)
        self.collector.last_prune = int(time.time() * 1000)
        self.collector.process_log = AsyncMock()
        self.collector.retract_orphans = AsyncMock()
        chain_hash = 'old200'
        pages = []

        async def rpc(method, params):
            if method == 'eth_getLogs':
                pages.append((chain_hash, params[0]['address']))
                if len(pages) == 1:
                    for index in range(4):
                        self.collector.queue.put_nowait(index)
                return [{'blockNumber': hex(200), 'blockHash': chain_hash,
                         'transactionIndex': '0x0', 'logIndex': '0x1'}] if len(params[0]['address']) == 64 else []
            block = 200 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'hash': chain_hash if block == 200 else hex(block),
                    'timestamp': hex(self.at // 1000)}

        self.collector.rpc = rpc
        replay = asyncio.create_task(self.collector.catch_up())
        try:
            await asyncio.wait_for(self._wait_until(lambda: self.collector.replay_paused), 1)
            chain_hash = 'new200'
            while not self.collector.queue.empty():
                self.collector.queue.get_nowait()
            with self.assertRaisesRegex(RuntimeError, 'chain-reorg-during-range'):
                await asyncio.wait_for(replay, 1)
        finally:
            replay.cancel()
            await asyncio.gather(replay, return_exceptions=True)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 199)
        self.collector.retract_orphans.assert_not_awaited()
        finished = await asyncio.wait_for(self.collector.catch_up(), 1)
        self.assertTrue(finished['caughtUp'])
        self.assertEqual([hash for hash, _ in pages],
                         ['old200', 'new200', 'new200', 'new200'])
        self.assertEqual(self.collector.process_log.await_args_list[-1].args[0]['blockHash'], 'new200')
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 200)

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
        async def drain_live():
            await self._wait_until(lambda: self.collector.replay_paused)
            self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 199)
            self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 1)
            while not self.collector.queue.empty():
                self.collector.queue.get_nowait()

        consumer = asyncio.create_task(drain_live())
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            finished = await asyncio.wait_for(self.collector.catch_up(), 2)
        await consumer
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
                for _ in range(3):
                    await asyncio.wait_for(self.collector.reconcile_step(), 3)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        self.assertGreater(live_processed, 0)
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

    async def test_transient_busy_retries_live_log_in_place_without_reconnect(self):
        first = {'id': 'first'}
        later = {'id': 'later'}
        self.collector.queue.put_nowait(later)
        busy = sqlite3.OperationalError('database is locked')
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        self.collector.process_log = AsyncMock(side_effect=[busy, busy, None])

        with patch('app.db.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await self.collector.process_queued(first)

        self.assertEqual(self.collector.process_log.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertTrue(all(call.args == (first,) for call in self.collector.process_log.await_args_list))
        self.assertIsNone(self.collector.retry_event)
        self.assertFalse(self.collector.recovery_needed)
        self.assertEqual(self.collector.reconnects, 0)
        self.assertIs(self.collector.queue.get_nowait(), later)
        self.assertEqual(len(self.collector.live_log_durations_ms), 1)

    async def test_persistent_busy_retains_queue_head_after_bounded_attempts(self):
        first = {'id': 'first'}
        later = {'id': 'later'}
        self.collector.queue.put_nowait(later)
        busy = sqlite3.OperationalError('database is locked')
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        self.collector.process_log = AsyncMock(side_effect=busy)

        with patch('app.db.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(sqlite3.OperationalError):
                await self.collector.process_queued(first)

        self.assertEqual(self.collector.process_log.await_count, 4)
        self.assertEqual(sleep.await_count, 3)
        self.assertIs(self.collector.retry_event, first)
        self.assertTrue(self.collector.recovery_needed)
        self.assertIs(self.collector.queue.get_nowait(), later)

    async def test_nonbusy_sqlite_error_and_cancellation_are_not_retried(self):
        for error in (sqlite3.OperationalError('database is locked'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                event = {'id': 'first'}
                if isinstance(error, sqlite3.OperationalError):
                    error.sqlite_errorcode = getattr(sqlite3, 'SQLITE_BUSY_SNAPSHOT', 517)
                self.collector.process_log = AsyncMock(side_effect=error)
                self.collector.retry_event = None
                self.collector.recovery_needed = False
                with patch('app.db.asyncio.sleep', new_callable=AsyncMock) as sleep:
                    with self.assertRaises(type(error)):
                        await self.collector.process_queued(event)
                self.collector.process_log.assert_awaited_once_with(event)
                sleep.assert_not_awaited()
                self.assertIs(self.collector.retry_event, event)
                self.assertTrue(self.collector.recovery_needed)

    async def test_healthy_socket_resets_prior_reconnect_backoff(self):
        class SocketContext:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *_):
                return False

        real_sleep = asyncio.sleep
        delays = []

        async def stop_after_third_retry(seconds):
            delays.append(seconds)
            if len(delays) == 3:
                raise asyncio.CancelledError()

        async def closed_reader(_):
            raise RuntimeError('socket closed')

        async def ws_call(_, method, __):
            await real_sleep(0)  # Let the reader close before the live loop.
            return hex(196) if method == 'eth_chainId' else 'subscription'

        self.collector.init = AsyncMock()
        self.collector.catalogue = AsyncMock(return_value=False)
        self.collector.reader = closed_reader
        self.collector.ws_call = ws_call
        self.collector.recover_live_queue = AsyncMock()
        self.collector.reconcile = AsyncMock()
        self.collector.schedule_status_fact = lambda *_, **__: None
        with patch('app.collectors.chain_stream.websockets.connect', side_effect=[
                RuntimeError('unavailable'), RuntimeError('unavailable'), SocketContext()]), \
             patch('app.collectors.chain_stream.time',
                   SimpleNamespace(monotonic=Mock(side_effect=[100, 161]))), \
             patch('app.collectors.chain_stream.asyncio.sleep', new=stop_after_third_retry):
            with self.assertRaises(asyncio.CancelledError):
                await self.collector.run()

        self.assertEqual(delays, [2, 4, 2])
        self.assertEqual(self.collector.reconnects, 3)

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
        await self.collector.status_task
        self.assertEqual(sizes[-1], 0)
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

    async def put_quote_price(self, price, at, *, kind='market', currency='USD',
                              provider='OKX', chain='196', independent=True):
        await self.store.merge_asset_observation(TOKEN1, {
            'token': TOKEN1, 'chainId': chain,
            'fieldSources': {'price': provider},
            'fieldObservations': {'price': {
                'provider': provider, 'scope': 'token', 'venue': 'dex',
                'currency': currency, 'timeKind': kind,
                'marketAt': at if kind == 'market' else None, 'receivedAt': at,
                'independent': independent,
            }},
            'fieldTimeKinds': {'price': kind},
        }, {'price': (price, at)}, sample=(price, None, at))

    async def test_chain_trade_uses_dated_same_chain_usd_quote_without_repricing_candle(self):
        market = replace(self.market, quote_token=TOKEN1)
        await self.put_quote_price(1.02, self.at - 30_000)
        trade = self.trade('dated-usd', 2)
        self.assertTrue(await self.collector.commit_trade(market, trade))
        body = (await self.store.recent_trades(market.storage, 1))[0]
        self.assertEqual((body['price'], body['quoteQuantity']), (3, 6))
        self.assertAlmostEqual(body['volume'], 6.12)
        self.assertEqual(body['volumeCurrency'], 'USD')
        self.assertEqual(body['quoteAt'], self.at - 30_000)
        self.assertEqual(body['volumeProvenance'], {
            'method': 'native-quote-quantity-times-dated-usd-price',
            'chainId': '196', 'quoteToken': TOKEN1, 'quotePriceUsd': 1.02,
            'quoteAt': self.at - 30_000, 'timeKind': 'market',
            'provider': 'OKX', 'evidence': 'asset-fact',
        })
        candle = await self.store.get('market-candle', market.storage + ':1m')
        self.assertEqual((candle['row']['v'], candle['row']['vu'], candle['priceCurrency']),
                         (2, 6, 'USDG'))
        self.assertFalse(await self.collector.commit_trade(market, trade))
        self.assertEqual((await self.store.recent_trades(market.storage, 1))[0]['volume'], 6.12)

    async def test_historical_replay_uses_prior_sample_instead_of_current_quote(self):
        market = replace(self.market, quote_token=TOKEN1)
        await self.put_quote_price(1.01, self.at - 120_000)
        await self.put_quote_price(9, self.at + 60_000)
        await self.collector.commit_trade(market, self.trade('replayed', 2))
        body = (await self.store.recent_trades(market.storage, 1))[0]
        self.assertAlmostEqual(body['volume'], 6.06)
        self.assertEqual(body['quoteAt'], self.at - 120_000)
        self.assertEqual(body['volumeProvenance']['evidence'], 'asset-sample')

    async def test_usd_volume_requires_independent_market_time_and_same_chain(self):
        market = replace(self.market, quote_token=TOKEN1)
        cases = (
            ('received-time-only', self.at - 30_000, 'received', 'USD', 'OKX', '196', True),
            ('future-quote', self.at + 1, 'market', 'USD', 'OKX', '196', True),
            ('stale-quote', self.at - 900_001, 'market', 'USD', 'OKX', '196', True),
            ('native-currency', self.at - 30_000, 'market', 'USDT', 'OKX', '196', True),
            ('derived-quote', self.at - 30_000, 'market', 'USD', 'Chain RPC', '196', False),
            ('not-independent', self.at - 30_000, 'market', 'USD', 'OKX', '196', False),
            ('unverified-quote', self.at - 30_000, 'market', 'USD', 'OKX', '196', None),
            ('wrong-chain', self.at - 30_000, 'market', 'USD', 'OKX', '56', True),
        )
        for name, quote_at, kind, currency, provider, chain, independent in cases:
            with self.subTest(name=name):
                await self.store.db.execute('DELETE FROM samples WHERE asset=?', (self.store.key(TOKEN1),))
                await self.store.db.execute('DELETE FROM sample_evidence WHERE asset=?', (self.store.key(TOKEN1),))
                await self.store.db.execute('DELETE FROM facts WHERE kind=? AND id=?',
                                            (self.store.key('asset'), TOKEN1))
                await self.store.db.commit()
                await self.put_quote_price(1, quote_at, kind=kind, currency=currency,
                                           provider=provider, chain=chain, independent=independent)
                await self.collector.commit_trade(market, self.trade(name, 2))
                body = (await self.store.recent_trades(market.storage, 1))[0]
                self.assertIsNone(body['volume'])
                self.assertEqual(body['volumeCurrency'], 'USDG')
                self.assertNotIn('quoteAt', body)
                self.assertNotIn('volumeProvenance', body)

    async def test_usd_volume_does_not_infer_peg_or_accept_invalid_quantity(self):
        market = replace(self.market, quote_token=TOKEN1)
        await self.collector.commit_trade(market, self.trade('symbol-is-not-peg', 2))
        self.assertIsNone((await self.store.recent_trades(market.storage, 1))[0]['volume'])
        await self.put_quote_price(1, self.at - 1000)
        await self.collector.commit_trade(market, {**self.trade('bad-quantity', 2, 1000),
                                                    'quoteQuantity': 0})
        self.assertIsNone((await self.store.recent_trades(market.storage, 1))[0]['volume'])

    async def test_market_registry_is_written_only_when_definition_changes(self):
        self.assertTrue(await self.collector.commit_trade(self.market, self.trade('registry-first', 2)))
        self.assertEqual(self.collector.market_registry[self.market.storage], self.market.record())
        with patch.object(self.collector, '_fact_params', wraps=self.collector._fact_params) as encode:
            self.assertTrue(await self.collector.commit_trade(
                self.market, self.trade('registry-second', 3, 1)))
        self.assertFalse(any(call.args[0] == 'market-registry' for call in encode.call_args_list))
        changed = replace(self.market, base_symbol='UPDATED')
        with patch.object(self.collector, '_fact_params', wraps=self.collector._fact_params) as encode:
            self.assertTrue(await self.collector.commit_trade(changed, self.trade('registry-third', 4, 2)))
        self.assertEqual(sum(call.args[0] == 'market-registry' for call in encode.call_args_list), 1)
        self.assertEqual(await self.store.get('market-registry', changed.storage), changed.record())

    async def test_trade_prepares_market_identity_before_sqlite_write_transaction(self):
        original_frame = Market.frame
        original_record = Market.record
        stages = []

        def frame(market):
            stages.append(('frame', self.store.db.in_transaction))
            return original_frame(market)

        def record(market):
            stages.append(('record', self.store.db.in_transaction))
            return original_record(market)

        def bars(market):
            stages.append(('bars', self.store.db.in_transaction))
            return ['1m', '5m', '1H']

        with patch.object(Market, 'frame', frame), patch.object(Market, 'record', record), \
             patch.object(self.collector, 'bars', bars):
            self.assertTrue(await self.collector.commit_trade(self.market, self.trade('prepared', 2)))
        self.assertTrue(stages)
        self.assertFalse(any(in_transaction for _, in_transaction in stages), stages)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM candles'))[0], 3)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM realtime_events'))[0], 4)

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

    async def test_external_writer_contention_releases_local_lock_before_retry(self):
        await self.collector._open_trade_db()
        external = sqlite3.connect(self.store.path, timeout=.1)
        external.execute('BEGIN IMMEDIATE')
        task = asyncio.create_task(self.collector.commit_trade(
            self.market, self.trade('externally-contended', 2)))
        try:
            # The first BEGIN waits briefly for the external process. The
            # retry delay must release our process-wide lock so other chains
            # can commit while this one is contended.
            for _ in range(50):
                if self.store._write_lock.locked():
                    break
                await asyncio.sleep(.005)
            else:
                self.fail('trade never began its first write attempt')
            acquired = asyncio.Event()

            async def take_writer_turn():
                async with self.store._write_lock:
                    acquired.set()

            local_writer = asyncio.create_task(take_writer_turn())
            await asyncio.wait_for(acquired.wait(), .6)
            await local_writer
        finally:
            external.rollback()
            external.close()
        self.assertTrue(await asyncio.wait_for(task, 2))
        self.assertFalse(await self.collector.commit_trade(
            self.market, self.trade('externally-contended', 2)))
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 1)

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
        self.assertNotIn(self.market.storage, self.collector.market_registry)
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
            for _ in range(3):
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
            for _ in range(3):
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

    async def test_distant_recent_replay_gives_pending_pool_a_turn_each_step(self):
        await self.store.put('chain-stream-cursor', 'pools', {'block': 50})
        await self.store.put('chain-stream-cursor', 'pools-live', {'block': 100})
        head = {'number': hex(1000), 'hash': hex(1000), 'timestamp': hex(self.at // 1000)}
        self.collector.rpc = AsyncMock(return_value=head)
        order = []

        async def recent(lane, **kwargs):
            order.append(('recent', lane, kwargs['max_ranges']))
            return {'caughtUp': False}

        async def backfill(observed_head, *, limit):
            order.append(('pool', observed_head['number'], limit))
            return {'requested': 1, 'updated': 1, 'failed': 0}

        self.collector.catch_up = recent
        self.collector.backfill_pending_pools = backfill
        for _ in range(2):
            progress = await self.collector.reconcile_step()
            self.assertFalse(progress['caughtUp'])
        self.assertEqual(order, [
            ('recent', 'pools-live', 1), ('pool', head['number'], 1),
            ('recent', 'pools-live', 1), ('pool', head['number'], 1),
        ])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 50)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools-live'))['block'], 100)

    async def test_failed_pool_backfill_does_not_block_next_recent_attempt(self):
        await self.store.put('chain-stream-cursor', 'pools', {'block': 50})
        await self.store.put('chain-stream-cursor', 'pools-live', {'block': 100})
        self.collector.pools = {POOL: {
            'streamBackfillFromBlock': 900,
            'verificationBlock': 900,
            'verificationBlockHash': '0x' + '9' * 64,
        }}
        self.collector.pending_pools.add(POOL)
        observed = []

        async def rpc(method, params):
            if params[0] == 'latest':
                return {'number': hex(1000), 'hash': hex(1000),
                        'timestamp': hex(self.at // 1000)}
            self.assertEqual(params[0], hex(900))
            return None  # Pool verification anchor unavailable.

        async def recent(lane, **kwargs):
            observed.append(lane)
            return {'caughtUp': False}

        self.collector.rpc = rpc
        self.collector.catch_up = recent
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            await self.collector.reconcile_step()
            await self.collector.reconcile_step()
        self.assertEqual(observed, ['pools-live', 'pools-live'])
        failed = await self.store.get('pool-stream-backfill', POOL)
        self.assertEqual(failed['failureCount'], 1)
        self.assertEqual(failed['lastError'], 'ValueError')
        self.assertGreater(failed['nextRetryAt'], self.at)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools-live'))['block'], 100)

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
        self.assertEqual(ranges, [(101, 200)])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 50)
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools-live'))['block'], 200)
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            await self.collector.reconcile_step()
        self.assertEqual(ranges, [(101, 200), (201, 300)])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools'))['block'], 50)

    async def test_distant_recent_lane_replays_from_saved_cursor_without_skipping_gap(self):
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
        self.assertEqual(ranges, [(101, 200)])
        self.assertEqual((recent['block'], recent['coverageFrom']), (200, 70))
        self.assertNotIn('rebasedFromBlock', recent)
        self.assertEqual(historical['block'], 50)
        self.assertEqual(status['nearTipLagBlocks'], 9800)
        self.assertEqual(status['historicalGapBlocks'], 19)
        self.assertIsNone(status['nearTipRebasedFromBlock'])
        with patch('app.collectors.chain_stream.now_ms', return_value=self.at):
            await self.collector.reconcile_step()
        self.assertEqual(ranges, [(101, 200), (201, 300)])
        self.assertEqual((await self.store.get('chain-stream-cursor', 'pools-live'))['block'], 300)

    async def test_distant_recent_lane_failure_keeps_saved_cursor(self):
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
        self.assertEqual((recent['block'], recent['coverageFrom']), (100, 70))
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

    async def test_fresh_log_reuses_cached_new_head_without_rpc(self):
        log = await self.prepare_logs()
        self.collector.headers.clear()
        self.collector.ws = object()
        self.collector.latest_head = 199
        self.collector.rpc = AsyncMock(side_effect=AssertionError('unneeded header RPC'))
        header = {'number': log['blockNumber'], 'hash': log['blockHash'],
                  'timestamp': hex(self.at // 1000)}
        self.collector.remember_header(header)
        self.assertEqual(await self.collector.log_header(log), header)
        self.collector.rpc.assert_not_awaited()

    async def test_fresh_log_without_new_head_uses_rpc_immediately(self):
        log = await self.prepare_logs()
        self.collector.headers.clear()
        self.collector.ws = object()
        self.collector.latest_head = 200
        header = {'number': log['blockNumber'], 'hash': log['blockHash'],
                  'timestamp': hex(self.at // 1000)}
        self.collector.rpc = AsyncMock(return_value=header)
        self.assertEqual(await asyncio.wait_for(self.collector.log_header(log), 1), header)
        self.collector.rpc.assert_awaited_once_with('eth_getBlockByHash', ['0xabc', False])

    async def test_live_status_counts_header_cache_and_rpc_with_processing_duration(self):
        cached = await self.prepare_logs()
        uncached = {**cached, 'blockHash': '0xabe', 'blockNumber': '0xc9',
                    'transactionHash': '0xeee'}
        header = {'number': '0xc9', 'hash': '0xabe', 'timestamp': hex(self.at // 1000)}
        self.collector.rpc = AsyncMock(return_value=header)

        await self.collector.process_queued(cached)
        await self.collector.process_queued(uncached)
        await self.collector.status_fact(force=True)

        status = await self.store.get('chain-stream', 'pools')
        self.assertEqual((status['liveHeaderCacheHitsRecent'],
                          status['liveHeaderRpcMissesRecent'],
                          status['liveLogProcessingSamplesRecent']), (1, 1, 2))
        self.assertGreaterEqual(status['liveLogProcessingLastMs'], 0)
        self.assertGreaterEqual(status['liveLogProcessingP95Ms'],
                                status['liveLogProcessingLastMs'])
        stages = status['liveStageTimingsRecentMs']
        self.assertTrue({'rawLookup', 'header', 'decimals', 'mutationLockWait',
                         'rawInsert', 'usdEvidence', 'tradeCommit', 'assetQuote',
                         'rawMarkProcessed'} <= stages.keys())
        for stage, summary in stages.items():
            with self.subTest(stage=stage):
                self.assertIn(stage, LIVE_STAGE_NAMES)
                self.assertEqual(set(summary), {'count', 'p50', 'p95', 'max'})
                self.assertTrue(1 <= summary['count'] <= 256)
                self.assertTrue(0 <= summary['p50'] <= summary['p95'] <= summary['max'])
        self.collector.rpc.assert_awaited_once_with('eth_getBlockByHash', ['0xabe', False])

    async def test_live_stage_status_keeps_only_bounded_recent_samples(self):
        await self.collector.process_log(await self.prepare_logs())  # Replay is unsampled.
        self.assertTrue(all(not samples for samples in self.collector.live_stage_durations_ms.values()))
        for samples in self.collector.live_stage_durations_ms.values():
            samples.extend(float(value) for value in range(300))
        await self.collector.status_fact(force=True)
        stages = (await self.store.get('chain-stream', 'pools'))['liveStageTimingsRecentMs']
        self.assertEqual(set(stages), set(LIVE_STAGE_NAMES))
        self.assertEqual(stages['rawInsert'], {
            'count': 256, 'p50': 171.0, 'p95': 287.0, 'max': 299.0})
        self.assertTrue(all(summary['count'] == 256 for summary in stages.values()))
        self.assertLess(len(json.dumps(stages)), 1024)

    async def test_failed_live_raw_insert_is_timed_and_retained_for_retry(self):
        log = await self.prepare_logs()
        self.collector._write_stream_log = AsyncMock(
            side_effect=sqlite3.OperationalError('database is locked'))
        with self.assertRaisesRegex(sqlite3.OperationalError, 'database is locked'):
            await self.collector.process_queued(log)
        await self.collector.status_fact(force=True)
        status = await self.store.get('chain-stream', 'pools')
        self.assertEqual(self.collector._write_stream_log.await_count, 4)
        self.assertEqual(status['liveStageTimingsRecentMs']['rawInsert']['count'], 4)
        self.assertNotIn('rawMarkProcessed', status['liveStageTimingsRecentMs'])
        self.assertIs(self.collector.retry_event, log)
        self.assertEqual(status['liveLogProcessingSamplesRecent'], 1)

    async def test_live_diagnostic_window_is_bounded_and_excludes_replay_lookups(self):
        cached = {'number': '0xc8', 'hash': '0xabc', 'timestamp': hex(self.at // 1000)}
        uncached = {'number': '0xc9', 'hash': '0xabe', 'timestamp': hex(self.at // 1000)}
        self.collector.remember_header(cached)
        self.collector.rpc = AsyncMock(return_value=uncached)

        async def process_header(event):
            await self.collector.log_header(event)

        self.collector.process_log = process_header
        await self.collector.log_header({'blockHash': '0xabc'})  # Replay is outside the live queue.
        for _ in range(256):
            await self.collector.process_queued({'blockHash': '0xabc'})
        await self.collector.process_queued({'blockHash': '0xabe'})
        await self.collector.status_fact(force=True)

        status = await self.store.get('chain-stream', 'pools')
        self.assertEqual((status['liveHeaderCacheHitsRecent'],
                          status['liveHeaderRpcMissesRecent'],
                          status['liveLogProcessingSamplesRecent']), (255, 1, 256))
        self.collector.rpc.assert_awaited_once_with('eth_getBlockByHash', ['0xabe', False])

    async def test_header_cache_spans_more_than_a_thousand_blocks(self):
        for height in range(8192):
            self.collector.remember_header({'number': hex(height), 'hash': hex(height)})
        self.assertEqual(len(self.collector.headers), 8192)
        self.assertIn('0x0', self.collector.headers)
        self.collector.remember_header({'number': '0x2000', 'hash': '0x2000'})
        self.assertEqual(len(self.collector.headers), 8192)
        self.assertNotIn('0x0', self.collector.headers)

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
