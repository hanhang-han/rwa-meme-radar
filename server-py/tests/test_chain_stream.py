import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.db import ResearchStore
from app.collectors.chain_stream import ChainPoolStream, SWAP_V2, SWAP_V3, decode_swap
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


class DurablePoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = await ResearchStore(os.path.join(self.tmp.name, "research.sqlite"), "196").connect()
        self.collector = ChainPoolStream("196")
        self.collector.s = self.store
        self.market = Market("196", TOKEN0, "dex", POOL, "USDG", "MEME", "pool", POOL)
        self.at = 1_790_317_800_000

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    def trade(self, id, size, offset=0):
        return {"id": id, "t": self.at + offset, "price": 3, "quantity": size,
                "quoteQuantity": 3 * size, "type": "buy", "blockHash": "0xabc",
                "blockNumber": 200, "logIndex": offset}

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

    async def test_full_queue_drains_before_bounded_scan_then_resubscribes(self):
        self.collector.queue = asyncio.Queue(maxsize=2)
        self.collector.queue.put_nowait({'id': 'oldest'})
        self.collector.queue.put_nowait({'id': 'second'})
        self.collector.recovery_needed = True
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
        self.assertEqual(order, [('log', 'oldest'), ('log', 'second'),
                                 ('head', 'eth_getBlockByNumber'),
                                 ('scan', 'pools-live', head)])
        self.assertFalse(self.collector.recovery_needed)
        self.assertTrue(self.collector.queue.empty())

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
        self.collector.catch_up.assert_awaited_once()

    async def test_long_recovery_publishes_queue_progress_before_replay_finishes(self):
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
        original = self.collector._write_candle
        writes = 0

        async def fail_second_bar(*args):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise RuntimeError('failed-candle')
            await original(*args)

        self.collector._write_candle = fail_second_bar
        with self.assertRaisesRegex(RuntimeError, 'failed-candle'):
            await self.collector.commit_trade(self.market, self.trade('rollback', 2))
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM trades'))[0], 0)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM candles'))[0], 0)
        self.assertEqual((await self.store.fetchone('SELECT COUNT(*) FROM realtime_events'))[0], 0)
        self.assertIsNone(await self.store.get('market-registry', self.market.storage))

        self.collector._write_candle = original
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
