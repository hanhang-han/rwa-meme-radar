import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.db import ResearchStore
from app.collectors import market_enrichment as dex
from app.collectors import pool_volume_snapshots as volume
from app.collectors.chain_stream import ChainPoolStream
from app.collectors.live_market import HotMarketStream
from app.product_metrics import DAY_MS, HOUR_MS, build_product_theme_metrics

NOW = 1_800_000_000_000
QUOTE = '0x'+'f'*40


def address(index):
    return '0x'+format(index, '040x')


class CollectorWorkBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stores = {chain: await ResearchStore(self.tmp.name+'/research.sqlite', chain).connect()
                       for chain in (*volume.CHAINS, 'system')}

    async def asyncTearDown(self):
        for store in self.stores.values():
            await store.close()
        self.tmp.cleanup()

    async def seed(self, chain, kind, values):
        store = self.stores[chain]
        async with store._guard_write():
            await store.db.executemany('INSERT INTO facts VALUES (?,?,?)',
                                      [(store.key(kind), ident, json.dumps(body)) for ident, body in values])
            await store.db.commit()

    def client(self, response):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.side_effect = response
        return client

    def dex_patches(self, client, at, leases=None, limit=8):
        return (patch('app.collectors.market_enrichment.store', AsyncMock(side_effect=lambda chain: self.stores[chain])),
                patch('app.collectors.market_enrichment.all_leases', AsyncMock(side_effect=leases or (lambda *_: []))),
                patch('app.collectors.market_enrichment.httpx.AsyncClient', return_value=client),
                patch('app.collectors.market_enrichment.time.time', return_value=at/1000),
                patch('app.collectors.market_enrichment.asyncio.sleep', AsyncMock()),
                patch('app.collectors.arc_poller.configured', return_value=False),
                patch.dict('os.environ', {'DEX_MARKET_BATCH_LIMIT': str(limit), 'DEX_MARKET_SCAN_LIMIT': '256'}))

    async def test_bounded_scan_reaches_every_asset_and_watched_tail_without_n_plus_one(self):
        tokens = [address(index) for index in range(1, 701)]
        await self.seed('56', 'asset', [(token, {'token': token, 'kind': 'candidate'}) for token in tokens])
        seen = set()
        calls = []

        async def response(url):
            batch = url.rsplit('/', 1)[1].split(',')
            self.assertLessEqual(len(batch), 30)
            calls.append(batch)
            seen.update(batch)
            rows = [{'chainId': 'bsc', 'pairAddress': address(int(token, 16)+10_000),
                     'baseToken': {'address': token}, 'quoteToken': {'address': QUOTE},
                     'liquidity': {'usd': 2000}} for token in batch]
            result = Mock()
            result.raise_for_status = Mock()
            result.json.return_value = rows
            return result

        client = self.client(response)
        watches = lambda store, _: [{'token': tokens[-1], 'expiresAt': NOW+DAY_MS}] if store.scope == '56' else []
        store = self.stores['56']
        with patch.object(store, 'get', wraps=store.get) as get, patch.object(store, 'all', wraps=store.all) as all_rows:
            for turn in range(5):
                before = len(calls)
                patches = self.dex_patches(client, NOW+turn*61_000, watches)
                with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                    result = await dex.refresh_market_enrichment()
                self.assertLessEqual(result['requested'], 8)
                self.assertLessEqual(len(calls)-before, 8)
                status = await self.stores['system'].get('collector', 'dex-batch-market')
                self.assertLessEqual(status['assetsExamined'], 257)
                if turn == 0:
                    self.assertIn(tokens[-1], seen)
            self.assertFalse(any(call.args[0] == 'dex-market-attempt' for call in get.call_args_list))
            self.assertFalse(any(call.args[0] in ('asset', 'relation') for call in all_rows.call_args_list))
        self.assertEqual(seen, set(tokens))

    async def test_relation_refresh_rechecks_verification_and_is_chain_scoped(self):
        token, pool = address(1), address(2)
        for chain in ('196', '56'):
            await self.seed(chain, 'asset', [(token, {'token': token, 'kind': 'candidate'})])
            await self.seed(chain, 'relation', [('r', {'id': 'r', 'token': token, 'pool': pool,
                'token0': token, 'token1': QUOTE, 'status': 'verified'})])

        async def response(url):
            network = url.split('/')[-2]
            if network == 'bsc':
                await self.stores['56'].patch_fact('relation', 'r', {'status': 'rejected'})
            result = Mock(); result.raise_for_status = Mock()
            result.json.return_value = [{'chainId': network, 'pairAddress': pool,
                'baseToken': {'address': token}, 'quoteToken': {'address': QUOTE},
                'liquidity': {'usd': 2000}, 'volume': {'h24': 100 if network == 'xlayer' else 200}}]
            return result

        patches = self.dex_patches(self.client(response), NOW)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = await dex.refresh_market_enrichment()
        self.assertEqual(result['updated'], 1)
        self.assertEqual((await self.stores['196'].get('relation', 'r'))['poolMarket']['volume24h'], 100)
        rejected = await self.stores['56'].get('relation', 'r')
        self.assertEqual(rejected['status'], 'rejected')
        self.assertNotIn('poolMarket', rejected)

    async def test_provider_rate_limit_keeps_cursor_and_existing_cooldown(self):
        tokens = [address(index) for index in range(1, 301)]
        await self.seed('56', 'asset', [(token, {'token': token, 'kind': 'candidate'}) for token in tokens])
        response = httpx.Response(429, request=httpx.Request('GET', 'https://api.dexscreener.com/test'))
        patches = self.dex_patches(self.client(lambda _: response), NOW)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            result = await dex.refresh_market_enrichment()
        self.assertEqual(result['requested'], 1)
        self.assertEqual(result['failed'], 1)
        status = await self.stores['system'].get('collector', 'dex-batch-market')
        self.assertEqual(status['scanCursors']['56'], tokens[255])
        self.assertEqual(status['nextAttemptAt'], NOW+300_000)
        self.assertEqual(status['status'], 'rate-limited')

    async def test_theme_history_is_loaded_once_per_chain_with_identical_pool_addresses(self):
        pool, second_pool, token = address(30), address(31), address(32)
        rows = [('56', 'NVDA', pool, 1000, 100), ('196', 'NVDA', pool, 2000, 200),
                ('56', 'AMC', second_pool, 500, 50)]
        for chain, ticker, ident, current, historical in rows:
            relation_id = ticker+ident
            await self.seed(chain, 'relation', [(relation_id, {'id': relation_id, 'chainId': chain,
                'token': token, 'stock': QUOTE, 'ticker': ticker, 'pool': ident,
                'status': 'verified', 'level': 'A', 'liquidityUsd': 2000, 'liquidityAt': NOW,
                'poolMarket': {'scope': 'pool:'+ident, 'volume24h': current,
                              'volumeCurrency': 'USD', 'provider': 'DexScreener', 'updatedAt': NOW}})])
            await self.seed(chain, volume.KIND, [(f'{ident}:{NOW//HOUR_MS*HOUR_MS-day*DAY_MS}',
                {'chainId': chain, 'pool': ident, 'hour': NOW//HOUR_MS*HOUR_MS-day*DAY_MS,
                 'volume24hUsd': historical, 'provider': 'DexScreener', 'scope': 'pool:'+ident})
                for day in range(1, 8)])
        for chain in ('56', '196'):
            await self.seed(chain, 'asset', [(token, {'token': token, 'symbol': 'REAL',
                'aggregateMarket': {'provider': 'DexScreener', 'volume24h': 5000,
                                    'volumeAt': NOW}})])
        with patch('app.collectors.pool_volume_snapshots.store', AsyncMock(side_effect=lambda chain: self.stores[chain])), \
             patch('app.stock_identity.assess_pool_relation', return_value={'level': 'A'}), \
             patch('app.market_quotes._read_snapshot', return_value={}), \
             patch('app.market_quotes.time.time', return_value=NOW/1000), \
             patch.object(self.stores['56'], 'fetchall', wraps=self.stores['56'].fetchall) as bnb_reads, \
             patch.object(self.stores['196'], 'fetchall', wraps=self.stores['196'].fetchall) as xlayer_reads, \
             patch.object(self.stores['56'], 'all', wraps=self.stores['56'].all) as bnb_all, \
             patch('app.product_metrics.build_product_theme_metrics', wraps=build_product_theme_metrics) as build:
            result = await volume.refresh_pool_volume_snapshots(NOW)
        self.assertEqual(result['accepted'], 3)
        for reads in (bnb_reads, xlayer_reads):
            histories = [call for call in reads.call_args_list if 'SELECT body FROM facts WHERE kind=? AND CAST' in call.args[0]]
            self.assertEqual(len(histories), 1)
        self.assertFalse(any(call.args[0] == 'asset' for call in bnb_all.call_args_list))
        self.assertTrue(all({volume._ticker(row) for row in call.args[2]} == {call.args[0]} for call in build.call_args_list))
        nvda = await self.stores['system'].get('product-theme', 'NVDA')
        amc = await self.stores['system'].get('product-theme', 'AMC')
        self.assertEqual(nvda['volume24hUsd'], 3000)
        self.assertEqual(nvda['coverage']['poolCount'], 2)
        self.assertEqual(nvda['volumeRatio7d'], 10)
        self.assertEqual(amc['volumeRatio7d'], 10)
        self.assertEqual({row['chainId'] for row in nvda['contributions']}, {'56', '196'})

    async def test_sparkline_entry_points_share_cadence_without_inventing_missing_points(self):
        store = self.stores['56']
        relation = {'pool': address(1), 'chainId': '56', 'stock': QUOTE, 'token': address(2),
                    'ticker': 'NVDA', 'status': 'verified', 'level': 'A'}
        await self.seed('56', 'relation', [('r', relation)])
        for index in range(6):
            at = NOW-1_800_000+index*300_000
            await store.sample(QUOTE, index+1, None, at,
                               metadata={'currency': 'USD', 'scope': 'token',
                                         'marketAt': at, 'provider': 'OKX'})
        with patch('app.collectors.pool_volume_snapshots.store', AsyncMock(side_effect=lambda chain: self.stores[chain])), \
             patch('app.stock_identity.assess_pool_relation', return_value={'level': 'A'}), \
             patch('app.market_quotes._read_snapshot', return_value={}), \
             patch('app.market_quotes.time.time', return_value=NOW/1000):
            first = await volume.refresh_stock_sparklines([relation], NOW)
            with patch.object(store, 'all', wraps=store.all) as reads, \
                 patch.object(store, 'samples', wraps=store.samples) as samples, \
                 patch('app.collectors.pool_volume_snapshots.time.time', return_value=(NOW+60_000)/1000):
                second = await volume.refresh_product_sparklines()
                self.assertEqual(reads.call_count, 0)
                self.assertEqual(samples.call_count, 0)
            with patch('app.collectors.pool_volume_snapshots.time.time', return_value=(NOW+300_000)/1000):
                third = await volume.refresh_product_sparklines()
        self.assertEqual(first['updated'], 1)
        self.assertEqual(second['skipped'], 1)
        self.assertEqual(third['updated'], 1)
        sparkline = await store.get('stock-sparkline', QUOTE)
        self.assertEqual(len(sparkline['points']), 6)
        self.assertFalse(sparkline['coverage']['complete'])


class ChainReplayBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_replay_uses_budget_and_hot_replay_bypasses_it(self):
        entered = []

        @asynccontextmanager
        async def budget(name):
            entered.append(name)
            yield

        expected = {'caughtUp': True, 'head': 1, 'processedBlock': 1}
        old, hot = ChainPoolStream('56'), HotMarketStream('56')
        old.reconcile_step = AsyncMock(return_value=expected)
        hot.reconcile_step = AsyncMock(return_value=expected)
        with patch('app.resource_budget.background_turn', side_effect=budget) as gate:
            self.assertEqual(await old._reconcile_turn(), expected)
            self.assertEqual(await hot._reconcile_turn(), expected)
        self.assertEqual(entered, ['chainHistory:56'])
        self.assertEqual(gate.call_count, 1)

    async def test_replay_cancellation_exits_shared_turn(self):
        released = asyncio.Event()
        running = asyncio.Event()

        @asynccontextmanager
        async def budget(_):
            try:
                yield
            finally:
                released.set()

        async def step():
            running.set()
            await asyncio.Event().wait()

        old = ChainPoolStream('56')
        old.reconcile_step = step
        with patch('app.resource_budget.background_turn', side_effect=budget):
            task = asyncio.create_task(old._reconcile_turn())
            await running.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(released.is_set())
