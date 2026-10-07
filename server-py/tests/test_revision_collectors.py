import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.db import ResearchStore
from app.collectors.chain_stream import ChainPoolStream
from app.collectors.market_streams import Market
from app.collectors.pool_volume_snapshots import refresh_pool_volume_snapshots, read_theme_pool_history
from app.collectors.market_enrichment import refresh_market_enrichment
from app.collectors.scheduler import apply_result
from app.worker import confirmation_backlog_probe_due, refresh_trade_confirmations

NOW = 1_800_000_000_000
TOKEN = '0x'+'1'*40
QUOTE = '0x'+'2'*40
POOL = '0x'+'3'*40
SENDER = '0x'+'4'*40
TX = '0x'+'5'*64
HASH = '0x'+'6'*64
OTHER = '0x'+'7'*64


class CollectorIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stores = {chain: await ResearchStore(self.tmp.name+'/research.sqlite', chain).connect()
            for chain in ('56', '196', '4663', '5042', 'system')}

    async def asyncTearDown(self):
        for s in self.stores.values():
            await s.close()
        self.tmp.cleanup()

    async def test_market_fdv_merge_preserves_primary_quote_field_metadata(self):
        s = self.stores['56']
        await s.put('asset', TOKEN, {'chainId': '56', 'token': TOKEN, 'kind': 'candidate',
            'price': 1.5, 'provider': 'OKX', 'fieldTimes': {'price': NOW},
            'fieldSources': {'price': 'OKX'}, 'fieldScopes': {'price': 'token'}})
        rows = [{'chainId': 'bsc', 'pairAddress': POOL, 'baseToken': {'address': TOKEN},
            'quoteToken': {'address': QUOTE}, 'liquidity': {'usd': 2000}, 'fdv': 8000}]
        response = Mock(); response.raise_for_status = Mock(); response.json.return_value = rows
        client = AsyncMock(); client.__aenter__.return_value = client; client.get.return_value = response
        with patch('app.collectors.market_enrichment.store', AsyncMock(side_effect=lambda chain: self.stores[chain])), \
             patch('app.collectors.market_enrichment.all_leases', AsyncMock(return_value=[])), \
             patch('app.collectors.market_enrichment.httpx.AsyncClient', return_value=client), \
             patch('app.collectors.market_enrichment.time.time', return_value=NOW/1000), \
             patch('app.collectors.market_enrichment.asyncio.sleep', AsyncMock()), \
             patch('app.collectors.arc_poller.configured', return_value=False):
            result = await refresh_market_enrichment()
        self.assertEqual(result['accepted'], 1)
        asset = await s.get('asset', TOKEN)
        self.assertEqual(asset['price'], 1.5)
        self.assertEqual(asset['provider'], 'OKX')
        self.assertEqual(asset['fieldTimes'], {'price': NOW, 'fdv': NOW})
        self.assertEqual(asset['fieldSources'], {'price': 'OKX', 'fdv': 'DexScreener'})
        self.assertEqual(asset['fdv'], 8000)

    async def test_rolling_h24_history_never_becomes_hourly_volume(self):
        s = self.stores['56']
        relation = {'id': 'r', 'chainId': '56', 'token': TOKEN, 'stock': QUOTE, 'ticker': 'NVDA',
            'pool': POOL, 'status': 'verified', 'level': 'A', 'liquidityUsd': 2000, 'liquidityAt': NOW,
            'poolMarket': {'scope': 'pool:'+POOL, 'volume24h': 1234, 'volumeCurrency': 'USD', 'provider': 'DexScreener', 'updatedAt': NOW}}
        await s.put('relation', 'r', relation)
        with patch('app.collectors.pool_volume_snapshots.store', AsyncMock(side_effect=lambda chain: self.stores[chain])), \
             patch('app.market_quotes._read_snapshot', return_value={}), \
             patch('app.market_quotes.time.time', return_value=NOW/1000):
            await refresh_pool_volume_snapshots(NOW)
            await refresh_pool_volume_snapshots(NOW)
            result = await read_theme_pool_history('NVDA', [relation], NOW, 2)
        self.assertEqual(len(await s.all('pool-volume-hour')), 1)
        current = result['series'][-1]
        self.assertEqual(current['volume24hUsd'], 1234)
        self.assertIsNone(current['volumeUsd'])
        self.assertEqual(current['metric'], 'rolling-volume-24h')
        self.assertIsNone(result['series'][0]['volume24hUsd'])

    async def test_empty_first_hour_retries_and_keeps_latest_real_observation(self):
        s = self.stores['56']
        relation = {'id': 'r', 'chainId': '56', 'token': TOKEN, 'stock': QUOTE, 'ticker': 'NVDA',
            'pool': POOL, 'status': 'verified', 'level': 'A', 'liquidityUsd': 2000, 'liquidityAt': NOW,
            'poolMarket': {'scope': 'pool:'+POOL, 'volume24h': 1234, 'volumeCurrency': 'USD', 'provider': 'DexScreener', 'updatedAt': NOW-1800001}}
        await s.put('relation', 'r', relation)
        with patch('app.collectors.pool_volume_snapshots.store', AsyncMock(side_effect=lambda chain: self.stores[chain])), \
             patch('app.market_quotes._read_snapshot', return_value={}), \
             patch('app.market_quotes.time.time', return_value=NOW/1000):
            initial = await refresh_pool_volume_snapshots(NOW)
            self.assertEqual(initial['accepted'], 0)
            relation['poolMarket'].update(updatedAt=NOW+60000)
            await s.put('relation', 'r', relation)
            second = await refresh_pool_volume_snapshots(NOW+60000)
            self.assertEqual(second['accepted'], 1)
            relation['poolMarket'].update(updatedAt=NOW+120000, volume24h=5678)
            await s.put('relation', 'r', relation)
            third = await refresh_pool_volume_snapshots(NOW+120000)
            self.assertEqual(third['accepted'], 1)
            duplicate = await refresh_pool_volume_snapshots(NOW+180000)
            self.assertEqual(duplicate['accepted'], 0)
        points = await s.all('pool-volume-hour')
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]['hour'], (NOW+120000)//3600000*3600000)
        self.assertEqual(points[0]['observedAt'], NOW+120000)
        self.assertEqual(points[0]['volume24hUsd'], 5678)
        status = await self.stores['system'].get('collector', 'pool-volume-snapshots')
        self.assertEqual(status['cadenceMs'], 60000)
        self.assertEqual(status['observationCadenceMs'], 3600000)

    async def test_missing_snapshot_observation_is_not_reported_as_missing_stock_identity(self):
        s = self.stores['56']
        await s.put('relation', 'r', {'pool': POOL, 'status': 'verified', 'poolMarket': {}})
        with patch('app.collectors.pool_volume_snapshots.store', AsyncMock(side_effect=lambda chain: self.stores[chain])), \
             patch('app.market_quotes._read_snapshot', return_value={}):
            await refresh_pool_volume_snapshots(NOW)
            result = await refresh_pool_volume_snapshots(NOW+60000)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['skipped'], 1)
        task = {}
        apply_result(task, result, NOW+60000)
        self.assertEqual(task['status'], 'idle')
        self.assertNotIn('error', task)
        fact = await self.stores['system'].get('collector', 'pool-volume-snapshots')
        self.assertEqual(fact['missingCurrentHourPools'], 1)
        self.assertEqual(fact['skipped'], 1)

    async def test_time_coverage_merges_adjacent_blocks_but_not_gaps_and_reorg_invalidates(self):
        s = self.stores['56']
        collector = ChainPoolStream('56'); collector.s = s
        collector.pools[POOL] = {'pool': POOL, 'token0': TOKEN, 'token1': QUOTE, 'queryToken': TOKEN}
        collector.assets[TOKEN] = {'token': TOKEN}
        base_seconds = NOW//1000-1000
        async def rpc(method, params):
            height = int(params[0], 16)
            return {'number': hex(height), 'hash': HASH, 'timestamp': hex(base_seconds+height)}
        collector.rpc = AsyncMock(side_effect=rpc)
        with patch('app.collectors.chain_stream.now_ms', return_value=NOW):
            first = await collector.record_time_coverage(100, 110, await rpc('', ['0x6e']), [POOL], [])
            second = await collector.record_time_coverage(111, 120, await rpc('', ['0x78']), [POOL], [])
            await collector.record_time_coverage(130, 140, await rpc('', ['0x8c']), [POOL], [])
            await collector.record_time_coverage(141, 150, await rpc('', ['0x96']), [POOL], [])
        self.assertEqual((first, second), (1, 1))
        rows = sorted(await s.all('pool-time-coverage'), key=lambda row: row['startBlock'])
        self.assertEqual([(row['startBlock'], row['endBlock']) for row in rows], [(100, 120), (130, 150)])
        self.assertEqual(rows[0]['fromMs'], (base_seconds+99)*1000+1000)
        self.assertEqual(rows[0]['throughMs'], (base_seconds+120)*1000-1)
        self.assertTrue(all(row['canonical'] and row['decoded'] for row in rows))
        await collector.invalidate_time_coverage(125)
        self.assertEqual(len(await s.all('pool-time-coverage')), 1)

    async def test_missing_decoded_swap_cannot_claim_empty_time_coverage(self):
        from app.collectors.chain_stream import SWAP_V2
        s = self.stores['56']
        collector = ChainPoolStream('56'); collector.s = s
        pool = {'pool': POOL, 'token0': TOKEN, 'token1': QUOTE, 'queryToken': TOKEN}
        collector.pools[POOL] = pool
        collector.assets[TOKEN] = {'token': TOKEN}
        log = {'address': POOL, 'topics': [SWAP_V2], 'blockHash': HASH, 'transactionHash': TX, 'logIndex': '0x0'}
        collector.rpc = AsyncMock(return_value={'number': '0x63', 'hash': HASH, 'timestamp': hex(NOW//1000-100)})
        end = {'number': '0x6e', 'hash': HASH, 'timestamp': hex(NOW//1000-90)}
        with patch('app.collectors.chain_stream.now_ms', return_value=NOW):
            accepted = await collector.record_time_coverage(100, 110, end, [POOL], [log])
        self.assertEqual(accepted, 0)
        self.assertEqual(await s.all('pool-time-coverage'), [])

    async def test_confirmation_uses_transaction_from_and_canonical_hash(self):
        s = self.stores['56']
        market = Market('56', TOKEN, 'dex', POOL, QUOTE, pool_id=POOL, quote_token=QUOTE)
        trade = {**market.frame(), 'id': 'trade', 't': NOW-10000, 'provider': 'Chain RPC',
            'finality': 'provisional', 'blockHash': HASH, 'blockNumber': 100, 'hash': TX, 'type': 'buy',
            'volume': 1234, 'volumeCurrency': 'USD', 'volumeProvenance': {
                'method': 'native-quote-quantity-times-dated-usd-price', 'quoteAt': NOW-10000,
                'timeKind': 'market', 'provider': 'OKX', 'quoteToken': QUOTE, 'quotePriceUsd': 100}}
        await s.db.execute('INSERT INTO trades VALUES (?,?,?,?)', (s.key(market.storage), 'trade', trade['t'], json.dumps(trade)))
        await s.db.commit()
        collector = ChainPoolStream('56')
        collector.s = s
        collector.market_registry[market.storage] = market.record()
        async def rpc(method, params):
            if method == 'eth_getBlockByNumber':
                return {'number': '0xc8' if params[0] == 'latest' else '0x64', 'hash': OTHER if params[0] == 'latest' else HASH}
            if method == 'eth_getTransactionByHash':
                return {'hash': TX, 'blockHash': HASH, 'blockNumber': '0x64', 'from': SENDER}
            raise AssertionError(method)
        collector.rpc = AsyncMock(side_effect=rpc)
        with patch('app.collectors.chain_stream.now_ms', return_value=NOW):
            result = await collector.confirm_recent_trades()
        self.assertEqual(result['accepted'], 1)
        body = json.loads((await s.fetchone('SELECT body FROM trades WHERE id=?', ('trade',)))[0])
        self.assertEqual(body['finality'], 'confirmed')
        self.assertEqual(body['txFrom'], SENDER)
        self.assertTrue(body['txFromVerified'])
        self.assertEqual(body['txFromSource'], 'eth_getTransactionByHash')
        self.assertEqual(body['canonicalBlockHash'], HASH)
        self.assertEqual(body['usdObservation']['value'], 1234)
        self.assertEqual(body['usdObservation']['method'], 'trade-time-quote')

    async def test_orphan_or_missing_transaction_never_invents_wallet(self):
        s = self.stores['56']
        market = Market('56', TOKEN, 'dex', POOL, QUOTE, pool_id=POOL, quote_token=QUOTE)
        trade = {**market.frame(), 'id': 'trade', 't': NOW-10000, 'provider': 'Chain RPC',
            'finality': 'provisional', 'blockHash': HASH, 'blockNumber': 100, 'hash': TX}
        await s.db.execute('INSERT INTO trades VALUES (?,?,?,?)', (s.key(market.storage), 'trade', trade['t'], json.dumps(trade)))
        await s.db.commit()
        collector = ChainPoolStream('56'); collector.s = s
        collector.market_registry[market.storage] = market.record()
        collector.rpc = AsyncMock(side_effect=[{'number': '0xc8', 'hash': OTHER}, {'number': '0x64', 'hash': OTHER}])
        with patch('app.collectors.chain_stream.now_ms', return_value=NOW):
            result = await collector.confirm_recent_trades()
        self.assertEqual(result['accepted'], 0)
        body = json.loads((await s.fetchone('SELECT body FROM trades WHERE id=?', ('trade',)))[0])
        self.assertEqual(body['finality'], 'provisional')
        self.assertNotIn('txFrom', body)
        collector.rpc = AsyncMock(side_effect=[{'number': '0xc8', 'hash': OTHER}, {'number': '0x64', 'hash': HASH}, None])
        with patch('app.collectors.chain_stream.now_ms', return_value=NOW):
            result = await collector.confirm_recent_trades()
        self.assertEqual(result['accepted'], 1)
        body = json.loads((await s.fetchone('SELECT body FROM trades WHERE id=?', ('trade',)))[0])
        self.assertEqual(body['finality'], 'confirmed')
        self.assertNotIn('txFrom', body)

    async def test_confirmation_uses_bounded_exact_market_index_reads(self):
        s = self.stores['56']
        market = Market('56', TOKEN, 'dex', POOL, QUOTE, pool_id=POOL, quote_token=QUOTE)
        collector = ChainPoolStream('56'); collector.s = s
        collector.market_registry[market.storage] = market.record()
        rows = []
        for index in range(100):
            trade = {**market.frame(), 'id': str(index), 't': NOW-1000-index, 'provider': 'Chain RPC',
                'finality': 'provisional', 'blockHash': HASH, 'blockNumber': 100, 'hash': TX}
            rows.append((s.key(market.storage), str(index), trade['t'], json.dumps(trade)))
        await s.db.executemany('INSERT INTO trades VALUES (?,?,?,?)', rows)
        await s.db.commit()
        collector.rpc = AsyncMock(side_effect=lambda method, params: {'number': '0xc8', 'hash': HASH} if params[0] == 'latest' else {'number': '0x64', 'hash': HASH} if method == 'eth_getBlockByNumber' else {'hash': TX, 'blockHash': HASH, 'blockNumber': '0x64', 'from': SENDER})
        with patch('app.collectors.chain_stream.now_ms', return_value=NOW), patch.object(s, 'fetchall', wraps=s.fetchall) as reads:
            result = await collector.confirm_recent_trades(limit=2)
        self.assertEqual(result['accepted'], 2)
        self.assertEqual(result['scannedTrades'], 24)
        self.assertEqual(result['scannedMarkets'], 1)
        query, params = reads.call_args_list[0].args
        self.assertIn('INDEXED BY trades_time', query)
        self.assertIn('asset=?', query)
        plan = await s.fetchall('EXPLAIN QUERY PLAN '+query, params)
        self.assertFalse(any('TEMP B-TREE' in row[-1] for row in plan))
        self.assertTrue(any('trades_time' in row[-1] and 'asset=?' in row[-1] for row in plan))

    async def test_confirmation_budget_keeps_committed_trade_and_releases_rpc_lane(self):
        s = self.stores['56']
        market = Market('56', TOKEN, 'dex', POOL, QUOTE, pool_id=POOL, quote_token=QUOTE)
        collector = ChainPoolStream('56'); collector.s = s
        collector.market_registry[market.storage] = market.record()
        for ident, height, tx in (('first', 100, TX), ('second', 101, '0x'+'8'*64)):
            trade = {**market.frame(), 'id': ident, 't': NOW-1000 if ident == 'first' else NOW-2000,
                'provider': 'Chain RPC', 'finality': 'provisional', 'blockHash': HASH, 'blockNumber': height, 'hash': tx}
            await s.db.execute('INSERT INTO trades VALUES (?,?,?,?)', (s.key(market.storage), ident, trade['t'], json.dumps(trade)))
        await s.db.commit()
        cancelled = asyncio.Event()
        async def rpc(method, params):
            async with collector.rpc_gate:
                if method == 'eth_getBlockByNumber' and params[0] == 'latest':
                    return {'number': '0xc8', 'hash': OTHER}
                if method == 'eth_getBlockByNumber' and params[0] == '0x65':
                    try:
                        await asyncio.sleep(10)
                    finally:
                        cancelled.set()
                if method == 'eth_getBlockByNumber':
                    return {'number': '0x64', 'hash': HASH}
                return {'hash': TX, 'blockHash': HASH, 'blockNumber': '0x64', 'from': SENDER}
        collector.rpc = AsyncMock(side_effect=rpc)
        with patch('app.collectors.chain_stream.now_ms', return_value=NOW):
            result = await collector.confirm_recent_trades(budget_ms=100)
        self.assertTrue(result['timeBudgetExpired'])
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(collector.rpc_gate.locked())
        values = {row[0]: json.loads(row[1]) for row in await s.fetchall('SELECT id,body FROM trades')}
        self.assertEqual(values['first']['finality'], 'confirmed')
        self.assertEqual(values['second']['finality'], 'provisional')

    async def test_worker_round_rotates_chains_and_respects_total_budget(self):
        visits = []
        class Stream:
            http = True
            queue = asyncio.Queue()
            async def confirm_recent_trades(self, limit, budget_ms):
                visits.append(budget_ms)
                await asyncio.sleep(budget_ms/1000)
                return {'requested': 1, 'accepted': 1, 'updated': 1}
        streams = {chain: Stream() for chain in ('196', '56', '4663')}
        order = []
        def lookup(chain):
            order.append(chain)
            return streams[chain]
        with patch('app.worker._confirmation_chain_cursor', 0):
            first = await refresh_trade_confirmations(lookup, budget_ms=30)
            second = await refresh_trade_confirmations(lookup, budget_ms=30)
        self.assertEqual(order, ['196', '56'])
        self.assertEqual(first['accepted'], 1)
        self.assertEqual(second['accepted'], 1)
        self.assertTrue(first['timeBudgetExpired'])
        self.assertLessEqual(first['durationMs'], 100)
        self.assertTrue(all(0 < budget <= 30 for budget in visits))

    async def test_confirmation_backlog_skips_are_not_missing_stock_identity(self):
        result = await refresh_trade_confirmations(lambda chain: None)
        self.assertEqual(result['skipped'], 3)
        self.assertEqual(result['failed'], 0)
        task = {}
        apply_result(task, result, NOW)
        self.assertEqual(task['status'], 'idle')
        self.assertNotIn('error', task)

    async def test_deferral_release_allows_one_five_second_backlogged_trade(self):
        class Queue:
            def qsize(self):
                return 100
        class Stream:
            http = True
            queue = Queue()
            def __init__(self):
                self.confirm_recent_trades = AsyncMock(return_value={'requested': 3, 'accepted': 1, 'updated': 1})
        streams = {chain: Stream() for chain in ('196', '56', '4663')}
        task = {'lastDeferralExpiredAt': NOW, 'startedAt': NOW+1, 'lastCompletedAt': NOW-240000}
        self.assertTrue(confirmation_backlog_probe_due(task))
        with patch('app.worker._confirmation_chain_cursor', 0):
            blocked = await refresh_trade_confirmations(streams.get)
            self.assertEqual(blocked['requested'], 0)
            released = await refresh_trade_confirmations(streams.get, allow_backlogged=confirmation_backlog_probe_due(task))
            self.assertEqual(released['accepted'], 1)
            self.assertEqual(released['skipped'], 2)
            streams['56'].confirm_recent_trades.assert_awaited_once_with(limit=1, budget_ms=5000)
            streams['196'].confirm_recent_trades.assert_not_awaited()
            streams['4663'].confirm_recent_trades.assert_not_awaited()
            later = await refresh_trade_confirmations(streams.get, allow_backlogged=confirmation_backlog_probe_due({**task, 'lastCompletedAt': NOW+5000, 'startedAt': NOW+65000}))
            self.assertEqual(later['requested'], 0)
            self.assertFalse(later['backloggedProbe'])
        self.assertFalse(confirmation_backlog_probe_due({**task, 'deferredSinceAt': NOW-60000}))
        self.assertFalse(confirmation_backlog_probe_due({**task, 'lastDeferralExpiredAt': NOW+5000}))


if __name__ == '__main__':
    unittest.main()
