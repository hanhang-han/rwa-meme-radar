"""End-to-end storage boundaries for bounded native market collectors."""
import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, patch

from app import db as research_db
from app import live_market_store as live
from app import demand_leases as leases
from app.collectors.chain_stream import SWAP_V2
from app.collectors.live_market import HotMarketStream, choose_markets
from app.demand_leases import flush_lease_writer, publish_lease, stop_lease_writer


TOKEN = '0x' + '1' * 40
QUOTE = '0x' + '2' * 40
POOL = '0x' + '3' * 40
OTHER = '0x' + '7' * 40
OTHER_POOL = '0x' + '8' * 40
BLOCK_HASH = '0x' + '4' * 64
TX_HASH = '0x' + '5' * 64
DAY = 86_400_000


def encoded(*values):
    return '0x' + ''.join((value % 2 ** 256).to_bytes(32, 'big').hex() for value in values)


class HotMarketIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original_path = self.temp.name + '/research.sqlite'
        self.market_path = self.temp.name + '/live.sqlite'
        self.path_setting = patch.object(research_db, 'DB_PATH', self.original_path)
        self.path_setting.start()
        self.addCleanup(self.path_setting.stop)
        self.environment = patch.dict(os.environ, {
            'LIVE_MARKET_DB': self.market_path, 'LIVE_MARKET_POOL_LIMIT': '4',
            'LIVE_MARKET_MEME_LIMIT': '2', 'LIVE_MARKET_STOCK_LIMIT': '1',
            'NODE_ENV': 'test', 'PROJECTION_COOLDOWN_SECONDS': '10'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        # Each IsolatedAsyncioTestCase owns its lease task. A cancelled task
        # from a different case's closed loop must never enter this teardown.
        self.lease_globals = patch.multiple(leases, _task=None, _pending={},
                                           _last_pruned={}, _initialized={})
        self.lease_globals.start()
        self.addCleanup(self.lease_globals.stop)
        self.streams = []
        self.fixture_resources = AsyncExitStack()
        self.addAsyncCleanup(self.fixture_resources.aclose)
        self.fixture_resources.push_async_callback(live.close_all)
        os.environ.pop('DEMAND_LEASE_DB', None)
        await live.close_all()
        live.configure_demand_leases()
        self.original = research_db.ResearchStore(self.original_path, '196', write_lock=research_db.WriterLock())
        self.fixture_resources.push_async_callback(self.original.close)
        self.fixture_resources.push_async_callback(stop_lease_writer)
        self.fixture_resources.push_async_callback(self.close_streams)
        await self.original.connect()
        self.at = int(time.time() * 1000) - 1000
        self.pool = {'pool': POOL, 'token0': TOKEN, 'token1': QUOTE,
                     'verificationStatus': 'verified', 'liquidityUsd': 10000}
        await self.original.put('pool', POOL, self.pool)
        await self.original.put('asset', TOKEN, {'token': TOKEN, 'kind': 'candidate',
                                                'symbol': 'MEME', 'volume24h': 1000})
        await self.original.put('asset', QUOTE, {'token': QUOTE, 'kind': 'base',
            'symbol': 'USDG', 'price': 4, 'priceCurrency': 'USD',
            'fieldTimes': {'price': self.at}, 'fieldObservations': {'price': {
                'marketAt': self.at, 'timeKind': 'market', 'currency': 'USD',
                'provider': 'OKX', 'scope': 'token', 'independent': True}}})
        await self.original.put('pool-quote', POOL, {'pool': POOL, 'decimals0': 18, 'decimals1': 6})
        self.hot = HotMarketStream('196')
        self.streams.append(self.hot)

    async def close_streams(self):
        results = await asyncio.gather(*(stream.close() for stream in self.streams), return_exceptions=True)
        failure = next((value for value in results if isinstance(value, BaseException)), None)
        if failure is not None:
            raise failure

    async def start_hot(self):
        await self.hot.init()
        self.hot.latest_head = 200
        self.hot.remember_header({'number': '0xc8', 'hash': BLOCK_HASH,
                                  'timestamp': hex(self.at // 1000)})
        # EVM trade time is whole seconds; bind the anchor to this timestamp.
        self.at = self.at // 1000 * 1000
        quote = await self.original.get('asset', QUOTE)
        quote['fieldTimes']['price'] = self.at
        quote['fieldObservations']['price']['marketAt'] = self.at
        await self.original.put('asset', QUOTE, quote)
        await self.hot.refresh_catalogue()
        self.market = self.hot.market(self.hot.pools[POOL], TOKEN)

    def log(self, *, index=1, quantity=2):
        return {'address': POOL, 'blockHash': BLOCK_HASH, 'blockNumber': '0xc8',
                'transactionHash': TX_HASH, 'transactionIndex': '0x0', 'logIndex': hex(index),
                'topics': [SWAP_V2],
                'data': encoded(0, quantity * 3_000_000, quantity * 10 ** 18, 0)}

    async def test_catalogue_writer_cannot_block_real_swap_commit(self):
        # Prepare decimals/time before reserving the catalogue's actual writer.
        await self.start_hot()
        original_quote = await self.original.get('asset', QUOTE)
        external = sqlite3.connect(self.original_path)
        external.execute('BEGIN IMMEDIATE')
        external.execute('UPDATE facts SET body=? WHERE kind=? AND id=?',
                         ('{"price":999}', '196:asset', TOKEN))
        self.hot.pool_asset_quote = AsyncMock(side_effect=AssertionError('research derivation on hot path'))
        self.hot.reserve_valuation = AsyncMock(side_effect=AssertionError('valuation on hot path'))
        try:
            async with self.original._write_lock:
                await asyncio.wait_for(self.hot.process_queued(self.log()), timeout=1.5)
            self.assertTrue(external.in_transaction)
            self.assertEqual((await self.hot.s.fetchone('SELECT COUNT(*) FROM trades'))[0], 1)
            self.assertEqual((await self.original.fetchone('SELECT COUNT(*) FROM trades'))[0], 0)
            self.assertEqual((await self.original.get('asset', QUOTE)), original_quote)
            self.hot.pool_asset_quote.assert_not_awaited()
            self.hot.reserve_valuation.assert_not_awaited()
        finally:
            external.rollback()
            external.close()

    async def test_native_candle_units_remain_separate_from_dated_usd_volume(self):
        await self.start_hot()
        await self.hot.process_log(self.log())
        trades = await self.hot.s.recent_trades(self.market.storage, 10)
        candle = (await self.hot.s.candle_range(self.market.storage, '1m'))[0]
        meta = await self.hot.s.get('candle-meta', self.market.candle_key('1m'))
        self.assertEqual((candle['c'], candle['v'], candle['vu']), (3, 2, 6))
        self.assertEqual((meta['priceCurrency'], meta['volumeCurrency']), ('USDG', 'USDG'))
        self.assertEqual((trades[0]['priceCurrency'], trades[0]['volumeCurrency']), ('USDG', 'USD'))
        self.assertEqual(trades[0]['volume'], 24)

    async def test_future_anchor_does_not_invent_dollar_volume(self):
        await self.start_hot()
        quote = await self.original.get('asset', QUOTE)
        quote['fieldTimes']['price'] = self.at + 60000
        quote['fieldObservations']['price']['marketAt'] = self.at + 60000
        await self.original.put('asset', QUOTE, quote)
        await self.hot.refresh_catalogue()
        await self.hot.process_log(self.log())
        trade = (await self.hot.s.recent_trades(self.market.storage))[0]
        self.assertIsNone(trade['volume'])
        self.assertEqual(trade['price'], 3)
        self.assertEqual(trade['quoteQuantity'], 6)

    async def test_duplicate_restart_and_reorg_do_not_double_count(self):
        await self.start_hot()
        await self.hot.process_log(self.log())
        await self.hot.process_log(self.log())
        self.assertEqual((await self.hot.s.candle_range(self.market.storage, '1m'))[0]['v'], 2)
        await self.hot.close()
        await live.close_all()
        self.hot = HotMarketStream('196')
        self.streams.append(self.hot)
        await self.start_hot()
        await self.hot.process_log(self.log())
        self.assertEqual((await self.hot.s.candle_range(self.market.storage, '1m'))[0]['v'], 2)
        await self.hot.process_log({**self.log(), 'removed': True})
        self.assertEqual(await self.hot.s.candle_range(self.market.storage, '1m'), [])
        self.assertEqual((await self.hot.s.fetchone('SELECT COUNT(*) FROM trades'))[0], 0)
        events = await self.hot.s.fetchall('SELECT event FROM realtime_events')
        self.assertIn('candle-reset', [row[0] for row in events])
        self.assertIn('trade-remove', [row[0] for row in events])

    async def test_reconnect_subscribes_without_discarding_retry_head_or_queue(self):
        await self.start_hot()
        self.hot.retry_event = self.log(index=1)
        self.hot.queue.put_nowait(self.log(index=2))
        self.hot.recovery_needed = True
        reader = asyncio.get_running_loop().create_future()
        try:
            await asyncio.wait_for(self.hot.recover_live_queue(reader), timeout=.1)
            self.assertEqual(self.hot.retry_event['logIndex'], '0x1')
            self.assertEqual(self.hot.queue.qsize(), 1)
            self.assertFalse(self.hot.recovery_needed)
            event, self.hot.retry_event = self.hot.retry_event, None
            await self.hot.process_queued(event)
            await self.hot.process_queued(self.hot.take_live_event())
            self.assertEqual((await self.hot.s.candle_range(self.market.storage, '1m'))[0]['v'], 4)
        finally:
            reader.cancel()

    async def add_other_pool(self):
        await self.original.put('asset', OTHER, {'token': OTHER, 'symbol': 'OTHER',
                                                'kind': 'candidate', 'volume24h': 2000})
        await self.original.put('pool', OTHER_POOL, {'pool': OTHER_POOL, 'token0': OTHER,
            'token1': QUOTE, 'verificationStatus': 'verified', 'liquidityUsd': 20000})

    async def test_reorg_of_previously_selected_pool_still_retracts_history(self):
        await self.start_hot()
        await self.hot.process_log(self.log())
        await self.add_other_pool()
        self.hot.capacity = 1
        watch = {'pool': OTHER_POOL, 'address': OTHER, 'expiresAt': int(time.time()*1000) + 60000}
        with patch('app.collectors.live_market.all_leases', AsyncMock(return_value=[watch])):
            await self.hot.refresh_catalogue()
        self.assertNotIn(POOL, self.hot.pools)
        await self.hot.process_log({**self.log(), 'removed': True})
        self.assertEqual(await self.hot.s.candle_range(self.market.storage, '1m'), [])
        self.assertEqual((await self.hot.s.fetchone('SELECT COUNT(*) FROM trades'))[0], 0)

    async def test_new_selection_invalidates_old_scan_generation(self):
        await self.start_hot()
        before = self.hot.selection_generation
        await self.add_other_pool()
        await self.hot.refresh_catalogue()
        self.assertGreater(self.hot.selection_generation, before)
        self.assertIn(OTHER_POOL, self.hot.pending_pools)
        state = await self.hot.s.get('live-selection', 'pools')
        self.assertEqual(state['basesByPool'][OTHER_POOL], [OTHER])
        self.assertTrue(await self.hot.catalogue())

    def recent_rpc(self, logs=None, *, height=200):
        async def rpc(method, params):
            if method == 'eth_getLogs':
                query = params[0]
                return [log for log in logs or [] if int(query['fromBlock'], 16)
                        <= int(log['blockNumber'], 16) <= int(query['toBlock'], 16)]
            block = height if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(block), 'timestamp': hex(self.at // 1000),
                    'hash': BLOCK_HASH if block == 200 else '0x' + f'{block:064x}'}
        self.hot.rpc = rpc
        return {'number': hex(height), 'timestamp': hex(self.at // 1000),
                'hash': BLOCK_HASH if height == 200 else '0x' + f'{height:064x}'}

    async def test_recent_empty_scan_works_under_pressure_without_advancing_history_or_source_clock(self):
        await self.start_hot()
        historical = {'block': 100, 'hash': '0x' + f'{100:064x}', 'updatedAt': self.at - 100000}
        await self.hot.s.put('chain-stream-cursor', 'pools', historical)
        old = {'lastSourceEventAt': self.at - 120000, 'lastSuccessfulAt': self.at - 100000}
        await self.hot.s.put('candle-meta', self.market.candle_key('5m'), old)
        for index in range(40):
            self.hot.queue.put_nowait(self.log(index=index))
        self.assertTrue(self.hot.replay_under_pressure())
        result = await self.hot.scan_recent_pools(self.recent_rpc())
        self.assertEqual(result['verifiedPools'], 1)
        self.assertEqual(await self.hot.s.get('chain-stream-cursor', 'pools'), historical)
        self.assertEqual((await self.hot.s.get('pool-live-cursor', POOL))['block'], 200)
        meta = await self.hot.s.get('candle-meta', self.market.candle_key('5m'))
        self.assertEqual((meta['lastSourceEventAt'], meta['lastSuccessfulAt']),
                         (old['lastSourceEventAt'], old['lastSuccessfulAt']))
        self.assertEqual((meta['poolScan']['pool'], meta['poolScan']['throughBlock']), (POOL, 200))
        self.assertEqual(await self.hot.s.candle_range(self.market.storage, '5m'), [])
        self.assertEqual(self.hot.queue.qsize(), 40)

    async def test_recent_real_swap_advances_source_clock_and_dedupes_later_queue_replay(self):
        await self.start_hot()
        head = self.recent_rpc([self.log()])
        result = await self.hot.scan_recent_pools(head)
        self.assertEqual((result['processedLogs'], result['verifiedPools']), (1, 1))
        meta = await self.hot.s.get('candle-meta', self.market.candle_key('5m'))
        self.assertEqual(meta['lastSourceEventAt'], self.at)
        await self.hot.process_queued(self.log())
        self.assertEqual((await self.hot.s.candle_range(self.market.storage, '5m'))[0]['v'], 2)
        self.assertEqual((await self.hot.s.fetchone('SELECT COUNT(*) FROM trades'))[0], 1)

    async def test_two_older_trades_in_one_bar_cannot_regress_latest_source_clock(self):
        await self.start_hot()
        latest_at = self.at // 3600000 * 3600000 + 10000
        for index, (at, price) in enumerate(((latest_at, 3), (latest_at - 1000, 4), (latest_at - 2000, 5))):
            await self.hot.commit_trade(self.market, {'id': str(index), 't': at, 'price': price,
                'quantity': 2, 'quoteQuantity': price * 2, 'type': 'buy', 'pool': POOL})
        for bar in ('1m', '5m', '1H'):
            meta = await self.hot.s.get('candle-meta', self.market.candle_key(bar))
            self.assertEqual(meta['lastSourceEventAt'], latest_at)
            row = (await self.hot.s.candle_range(self.market.storage, bar))[0]
            self.assertEqual((row['c'], row['v']), (3, 6))
        events = await self.hot.s.fetchall("SELECT body FROM realtime_events WHERE event='trade' ORDER BY id")
        self.assertEqual([json.loads(row[0])['sourceEventAt'] for row in events],
                         [latest_at, latest_at - 1000, latest_at - 2000])

    async def test_recent_dense_block_resumes_without_advancing_incomplete_cursor(self):
        await self.start_hot()
        previous = {'block': 199, 'hash': '0x' + f'{199:064x}', 'coverageFrom': 190,
                    'selectedBases': [TOKEN], 'canonical': True}
        await self.hot.s.put('pool-live-cursor', POOL, previous)
        logs = [self.log(index=index) for index in range(3)]
        head = self.recent_rpc(logs)
        for expected in (1, 2):
            result = await self.hot.scan_recent_pools(head, event_budget=1)
            self.assertEqual(result['partialPages'], 1)
            self.assertEqual(await self.hot.s.get('pool-live-cursor', POOL), previous)
            self.assertEqual((await self.hot.s.fetchone('SELECT COUNT(*) FROM trades'))[0], expected)
        result = await self.hot.scan_recent_pools(head, event_budget=1)
        self.assertEqual((result['processedLogs'], result['verifiedPools']), (1, 1))
        cursor = await self.hot.s.get('pool-live-cursor', POOL)
        self.assertEqual((cursor['block'], cursor['coverageFrom']), (200, 190))
        self.assertEqual((await self.hot.s.candle_range(self.market.storage, '5m'))[0]['v'], 6)

    async def test_recent_rebase_records_gap_and_keeps_historical_watermark(self):
        await self.start_hot()
        old = {'block': 100, 'hash': '0x' + f'{100:064x}', 'coverageFrom': 80,
               'selectedBases': [TOKEN], 'canonical': True}
        await self.hot.s.put('pool-live-cursor', POOL, old)
        await self.hot.s.put('chain-stream-cursor', 'pools', old)
        await self.hot.scan_recent_pools(self.recent_rpc(height=1000))
        cursor = await self.hot.s.get('pool-live-cursor', POOL)
        self.assertEqual((cursor['block'], cursor['coverageFrom'], cursor['rebasedFromBlock']), (1000, 969, 100))
        gap = await self.hot.s.get('pool-live-gap', POOL + ':101')
        self.assertEqual((gap['fromBlock'], gap['throughBlock']), (101, 968))
        self.assertEqual(await self.hot.s.get('chain-stream-cursor', 'pools'), old)

    async def test_recent_provider_failure_and_selection_change_cannot_publish_proof(self):
        await self.start_hot()
        head = self.recent_rpc()
        original_rpc = self.hot.rpc
        async def failed_rpc(method, params):
            if method == 'eth_getLogs':
                raise RuntimeError('provider-failed')
            return await original_rpc(method, params)
        self.hot.rpc = failed_rpc
        with self.assertRaisesRegex(RuntimeError, 'provider-failed'):
            await self.hot.scan_recent_pools(head)
        self.assertIsNone(await self.hot.s.get('pool-live-cursor', POOL))
        async def changed_rpc(method, params):
            if method == 'eth_getLogs':
                await self.add_other_pool()
                await self.hot.refresh_catalogue()
                return []
            return await original_rpc(method, params)
        self.hot.rpc = changed_rpc
        with self.assertRaisesRegex(RuntimeError, 'market-selection-changed-during-replay'):
            await self.hot.scan_recent_pools(head)
        self.assertIsNone(await self.hot.s.get('pool-live-cursor', POOL))

    async def test_removed_block_immediately_invalidates_recent_empty_range_proof(self):
        await self.start_hot()
        await self.hot.scan_recent_pools(self.recent_rpc([self.log()]))
        await self.hot.process_log({**self.log(), 'removed': True})
        self.assertFalse((await self.hot.s.get('pool-live-cursor', POOL))['canonical'])
        self.assertFalse((await self.hot.s.get('candle-meta', self.market.candle_key('5m')))['poolScan']['canonical'])
        self.assertEqual(await self.hot.s.candle_range(self.market.storage, '5m'), [])

    async def test_changed_recent_anchor_invalidates_then_observes_new_bounded_window(self):
        await self.start_hot()
        previous = {'block': 199, 'hash': '0x' + 'f' * 64, 'coverageFrom': 190,
                    'selectedBases': [TOKEN], 'canonical': True}
        await self.hot.s.put('pool-live-cursor', POOL, previous)
        head = self.recent_rpc()
        with self.assertRaisesRegex(RuntimeError, 'recent-checkpoint-reorged'):
            await self.hot.scan_recent_pools(head)
        self.assertFalse((await self.hot.s.get('pool-live-cursor', POOL))['canonical'])
        await self.hot.scan_recent_pools(head)
        cursor = await self.hot.s.get('pool-live-cursor', POOL)
        self.assertEqual((cursor['canonical'], cursor['block'], cursor['coverageFrom']), (True, 200, 169))

    async def test_removal_between_final_anchor_check_and_publication_cannot_restore_proof(self):
        await self.start_hot()
        head = self.recent_rpc([self.log()])
        original = self.hot.rpc
        checks = 0
        async def rpc(method, params):
            nonlocal checks
            result = await original(method, params)
            if method == 'eth_getBlockByNumber' and params[0] == '0xc8':
                checks += 1
                if checks == 3:
                    await self.hot.process_log({**self.log(), 'removed': True})
            return result
        self.hot.rpc = rpc
        with self.assertRaisesRegex(RuntimeError, 'chain-reorg-during-recent-range'):
            await self.hot.scan_recent_pools(head)
        self.assertIsNone(await self.hot.s.get('pool-live-cursor', POOL))
        self.assertEqual(await self.hot.s.candle_range(self.market.storage, '5m'), [])

    async def test_selection_change_during_scan_cannot_advance_old_cursor(self):
        await self.start_hot()
        self.hot.pending_pools.clear()
        previous_hash = '0x' + f'{199:064x}'
        await self.hot.s.put('chain-stream-cursor', 'pools',
                            {'block': 199, 'hash': previous_hash, 'coverageFrom': 190})

        async def rpc(method, params):
            if method == 'eth_getLogs':
                await self.add_other_pool()
                await self.hot.refresh_catalogue()
                return []
            height = 200 if params[0] == 'latest' else int(params[0], 16)
            return {'number': hex(height), 'timestamp': hex(self.at // 1000),
                    'hash': BLOCK_HASH if height == 200 else '0x' + f'{height:064x}'}

        self.hot.rpc = rpc
        with self.assertRaisesRegex(RuntimeError, 'market-selection-changed-during-replay'):
            await self.hot.catch_up(max_ranges=1)
        self.assertEqual((await self.hot.s.get('chain-stream-cursor', 'pools'))['block'], 199)
        self.assertIn(OTHER_POOL, self.hot.pending_pools)

    async def test_new_base_on_same_pool_replays_raw_without_double_counting_old_side(self):
        await self.start_hot()
        self.hot.pending_pools.clear()
        await self.hot.process_log(self.log())
        generation = self.hot.selection_generation
        watch = {'pool': POOL, 'address': QUOTE, 'expiresAt': int(time.time()*1000) + 60000}
        with patch('app.collectors.live_market.all_leases', AsyncMock(return_value=[watch])):
            await self.hot.refresh_catalogue()
        self.assertGreater(self.hot.selection_generation, generation)
        self.assertIn(POOL, self.hot.pending_pools)
        self.assertEqual((await self.hot.s.fetchone('SELECT processed FROM chain_stream_logs'))[0], 0)
        await self.hot.process_log(self.log())
        inverse = self.hot.market(self.hot.pools[POOL], QUOTE)
        self.assertEqual((await self.hot.s.candle_range(self.market.storage, '1m'))[0]['v'], 2)
        self.assertEqual((await self.hot.s.candle_range(inverse.storage, '1m'))[0]['v'], 6)
        self.assertEqual((await self.hot.s.fetchone('SELECT COUNT(*) FROM trades'))[0], 2)

    async def test_sidecar_watch_wakes_reverse_base_on_already_selected_pool(self):
        await self.start_hot()
        self.hot.capacity = 1
        self.hot.pending_pools.clear()
        await self.hot.process_log(self.log())
        generation = self.hot.selection_generation
        self.assertEqual(self.hot.selected_bases[POOL], {TOKEN})
        self.assertIsNone(self.hot.catalogue_task)
        publish_lease(self.original, 'candle-watch', f'dex:196:{QUOTE}:1m:{POOL}', {
            'chain': '196', 'address': QUOTE, 'pool': POOL, 'venue': 'dex', 'bar': '1m',
            'expiresAt': int(time.time() * 1000) + 120000})
        await flush_lease_writer()
        self.hot.last_watches = 0
        await self.hot.refresh_watches()
        # A fresh catalogue has not reached its periodic refresh deadline.
        # The real sidecar demand must start the existing background refresh.
        self.assertIsNotNone(self.hot.catalogue_task)
        await asyncio.wait_for(self.hot.catalogue_task, 1)
        self.assertIsNone(self.hot.catalogue_error)
        self.assertEqual(set(self.hot.pools), {POOL})
        self.assertEqual(self.hot.selected_bases[POOL], {TOKEN, QUOTE})
        self.assertGreater(self.hot.selection_generation, generation)
        self.assertIn(POOL, self.hot.pending_pools)
        selection = await self.hot.s.get('live-selection', 'pools')
        self.assertEqual(selection['basesByPool'][POOL], sorted((TOKEN, QUOTE)))
        self.assertEqual((await self.hot.s.fetchone('SELECT processed FROM chain_stream_logs'))[0], 0)
        await self.hot.process_log(self.log())
        inverse = self.hot.market(self.hot.pools[POOL], QUOTE)
        self.assertEqual((await self.hot.s.candle_range(self.market.storage, '1m'))[0]['v'], 2)
        self.assertEqual((await self.hot.s.candle_range(inverse.storage, '1m'))[0]['v'], 6)
        self.assertEqual((await self.hot.s.fetchone('SELECT COUNT(*) FROM trades'))[0], 2)

    async def test_retention_keeps_raw_and_trade_evidence_for_same_eight_days(self):
        await self.start_hot()
        now = int(time.time() * 1000)
        async with self.hot.s._guard_write():
            for name, age, processed in (('old', 9 * DAY, 1), ('recent', 3 * DAY, 1),
                                         ('pending', 9 * DAY, 0)):
                at = now - age
                await self.hot.s.db.execute('INSERT INTO chain_stream_logs VALUES (?,?,?,?,?,?,?,?)',
                    ('196', name, POOL, 200, BLOCK_HASH, at, '{}', processed))
                await self.hot.s.db.execute('INSERT INTO trades VALUES (?,?,?,?)',
                    (self.hot.s.key(self.market.storage), name, at, '{}'))
                await self.hot.s.db.execute('INSERT INTO facts VALUES (?,?,?)',
                    (self.hot.s.key('pool-candle-acc'), name, json.dumps({'t': at})))
            await self.hot.s.db.commit()
        self.hot.last_prune = 123
        outcome = await self.hot.prune_journal()
        self.assertNotIn('error', outcome)
        self.assertEqual(self.hot.last_prune, 123)
        self.assertEqual([row[0] for row in await self.hot.s.fetchall(
            'SELECT id FROM chain_stream_logs ORDER BY id')], ['pending', 'recent'])
        self.assertEqual([row[0] for row in await self.hot.s.fetchall(
            'SELECT id FROM trades ORDER BY id')], ['recent'])
        self.assertEqual([key for key, _ in await self.hot.s.all_kv('pool-candle-acc')], ['recent'])

    async def test_retention_event_cap_age_and_closed_history_use_multiple_batches(self):
        await self.start_hot()
        now = int(time.time() * 1000)
        async with self.hot.s._guard_write():
            # A numeric sequence gap tests the cap without writing 200k rows.
            await self.hot.s.db.executemany('INSERT INTO realtime_events(id,event,body,at) VALUES (?,?,?,?)',
                [(index, 'candle', '{}', now) for index in range(1, 451)] +
                [(200500, 'candle', '{}', now), (200501, 'candle', '{}', now - 3_600_001)])
            await self.hot.s.db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)',
                [(self.hot.s.key(self.market.storage), '1m', now - 31 * DAY - index,
                  1, 1, 1, 1, 1, 1, 1) for index in range(451)] +
                [(self.hot.s.key(self.market.storage), '1m', now - 3 * DAY, 1, 1, 1, 1, 1, 1, 1)])
            await self.hot.s.db.commit()
        outcome = await self.hot.prune_journal()
        self.assertNotIn('error', outcome)
        self.assertEqual([row[0] for row in await self.hot.s.fetchall(
            'SELECT id FROM realtime_events')], [200500])
        self.assertEqual((await self.hot.s.fetchone('SELECT COUNT(*) FROM candles'))[0], 1)
        self.assertEqual(outcome['removed']['eventsCap'], 450)
        self.assertEqual(outcome['removed']['candles'], 451)

    async def test_non_owner_retention_does_not_prune_shared_event_tape(self):
        await self.start_hot()
        other = HotMarketStream('56')
        self.streams.append(other)
        await other.init()
        async with self.hot.s._guard_write():
            await self.hot.s.db.execute('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
                                       ('candle', '{}', int(time.time()*1000) - 4_000_000))
            await self.hot.s.db.commit()
        self.assertNotIn('error', await other.prune_journal())
        self.assertEqual((await self.hot.s.fetchone('SELECT COUNT(*) FROM realtime_events'))[0], 1)
        self.assertNotIn('error', await self.hot.prune_journal())
        self.assertEqual((await self.hot.s.fetchone('SELECT COUNT(*) FROM realtime_events'))[0], 0)


class HotSelectionTests(unittest.TestCase):
    def test_watched_markets_take_priority_with_bounded_verified_capacity(self):
        assets = {TOKEN: {'kind': 'candidate', 'volume24h': 1},
                  OTHER: {'kind': 'stock', 'volume24h': 1000}}
        pools = [{'pool': POOL, 'token0': TOKEN, 'token1': QUOTE, 'liquidityUsd': 1},
                 {'pool': OTHER_POOL, 'token0': OTHER, 'token1': QUOTE, 'liquidityUsd': 1000},
                 {'pool': '0x' + '9' * 40, 'token0': OTHER, 'token1': QUOTE,
                  'verificationStatus': 'reorged', 'liquidityUsd': 999999}]
        watches = [{'pool': POOL, 'address': TOKEN, 'expiresAt': int(time.time()*1000) + 60000}]
        selected, bases = choose_markets(pools, assets, watches, capacity=1)
        self.assertEqual(set(selected), {POOL})
        self.assertEqual(bases, {POOL: {TOKEN}})


if __name__ == '__main__':
    unittest.main()
