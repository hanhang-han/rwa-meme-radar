import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import state
from app.api import misc, token as token_api
from app.comparison_service import read_comparison
from app.db import ResearchStore
from app.scoped_reads import candidate_relations, stock_view, token_pools


MEME = '0x' + '1' * 40
OTHER = '0x' + '2' * 40
POOL = '0x' + '3' * 40
OTHER_POOL = '0x' + '4' * 40
STOCK = '0xc845b2894dbddd03858fd2d643b4ef725fe0849d'


class ScopedDetailReads(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.s = await ResearchStore(self.temp.name + '/research.sqlite', '196').connect()
        self.addAsyncCleanup(self.s.close)
        self.addCleanup(self.temp.cleanup)

    async def test_detail_pair_and_registry_use_only_matching_candidate_facts(self):
        now = int(time.time() * 1000)
        relation = {'id': 'candidate-pair', 'token': MEME, 'stock': STOCK,
                    'stockSide': STOCK, 'token0': MEME, 'token1': STOCK,
                    'pool': POOL, 'ticker': 'NVDA', 'status': 'verified',
                    'liquidityUsd': 2500, 'liquidityAt': now - 1000,
                    'checkedAt': now - 1000}
        await self.s.put('asset', MEME, {'token': MEME, 'symbol': 'MEME', 'kind': 'candidate'})
        await self.s.put('asset', OTHER, {'token': OTHER, 'symbol': 'USDT', 'kind': 'candidate'})
        await self.s.put('relation', 'candidate-pair', relation)
        await self.s.put('relation', 'base-pair', {**relation, 'id': 'base-pair', 'token': OTHER})
        await self.s.put('pool', POOL, {'pool': POOL, 'token0': MEME.upper(), 'token1': STOCK})
        await self.s.put('pool', OTHER_POOL, {'pool': OTHER_POOL, 'token0': OTHER, 'token1': STOCK})
        self.assertEqual([r['id'] for r in await candidate_relations(self.s, '196')], ['candidate-pair'])
        self.assertEqual([p['pool'] for p in await token_pools(self.s, MEME)], [POOL])

        with patch.object(token_api, 'store', AsyncMock(return_value=self.s)), \
             patch.object(misc, 'store', AsyncMock(return_value=self.s)), \
             patch.object(state, 'reload_if_stale', AsyncMock(side_effect=AssertionError('full reload'))) as reload, \
             patch.object(self.s, 'all', AsyncMock(side_effect=AssertionError('all facts'))), \
             patch('app.demand_leases.publish_lease'), \
             patch.object(token_api, 'attach_market_detail', AsyncMock(side_effect=lambda body, _: body)), \
             patch.object(misc, 'registry_info', AsyncMock(side_effect=lambda rows: rows)):
            detail = await token_api.get_token('196', MEME)
            pair = await misc.get_pair('196', MEME)
            registry_rows = await misc.get_registry()
            reload.assert_not_awaited()
        self.assertEqual([r['id'] for r in detail['relations']], ['candidate-pair'])
        self.assertEqual([r['id'] for r in pair['relations']], ['candidate-pair'])
        self.assertEqual([r['id'] for r in registry_rows], ['candidate-pair'])
        self.assertEqual([p['pool'] for p in detail['pools']], [POOL])
        self.assertEqual(detail['relations'][0]['level'], pair['relations'][0]['level'])

    async def test_comparison_cache_miss_matches_one_stock_view_without_reload(self):
        stock = {'tokenContractAddress': STOCK, 'tokenSymbol': 'NVDAx', 'stockCode': 'NVDA'}
        asset = {'token': STOCK, 'kind': 'stock', 'symbol': 'NVDAx',
                 'price': 101, 'fieldTimes': {'price': 1}}
        await self.s.put('stock', STOCK, stock)
        await self.s.put('asset', STOCK, asset)
        expected = await stock_view(self.s, '196', STOCK, include_comparison=False)
        dashboard = state.DashboardData()
        dashboard.stock_tokens = [{**stock, 'chainId': '196', 'chain': '196'}]
        dashboard.assets = [{**asset, 'chainId': '196', 'chain': '196',
                             'chainName': state.CHAIN_NAMES['196']}]
        full_view = dashboard.stock_views(include_comparisons=False)[0]
        self.assertEqual(expected, full_view)
        with patch('app.comparison_service.store', AsyncMock(return_value=self.s)), \
             patch.object(state, 'reload_if_stale', AsyncMock(side_effect=AssertionError('full reload'))) as reload, \
             patch.object(self.s, 'all', AsyncMock(side_effect=AssertionError('all facts'))):
            response = await read_comparison('196', STOCK)
            reload.assert_not_awaited()
        self.assertEqual(response['current']['premium'], expected['premium'])
        self.assertEqual(response['current']['pairs'], [])


if __name__ == '__main__':
    unittest.main()
