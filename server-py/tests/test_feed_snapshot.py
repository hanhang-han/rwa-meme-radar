import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.misc import get_feed


class FeedSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_assets_preserve_chain_filter_and_market_units(self):
        assets = [
            {'chainId': '196', 'token': 'same', 'symbol': 'MEME', 'kind': 'candidate', 'tradeAt': 100},
            {'chainId': '56', 'token': 'same', 'symbol': 'MEME', 'kind': 'candidate', 'tradeAt': 200},
            {'chainId': '4663', 'token': 'other', 'symbol': 'OTHER', 'kind': 'candidate', 'tradeAt': 100},
            {'chainId': '196', 'token': 'stable', 'symbol': 'USDT', 'kind': 'candidate', 'tradeAt': 300},
            {'chainId': '196', 'token': 'stock', 'symbol': 'STOCK', 'kind': 'stock', 'tradeAt': 400},
            {'chainId': '196', 'token': 'idle', 'symbol': 'IDLE', 'kind': 'candidate'},
            {'chainId': '196', 'token': 'unverified', 'symbol': 'NEW', 'kind': 'candidate', 'tradeAt': 500},
        ]
        relations = [{'chainId': a['chainId'], 'token': a['token'], 'status': 'verified'}
                     for a in assets if a['chainId'] != '56' and a['token'] != 'unverified']
        data = SimpleNamespace(assets=assets, relations=relations,
                               signals=[{'id': n, 'chainId': '196'} for n in range(35)])
        scopes = {chain: SimpleNamespace(scope=chain,
                  all=AsyncMock(side_effect=AssertionError('The feed must reuse decoded facts')),
                  recent_trades=AsyncMock(return_value=[{'id': chain, 't': int(chain)}]))
                  for chain in ('196', '56', '4663')}

        async def market_tape(scoped, limit):
            return [{'id': 'market-' + scoped.scope, 'chainId': scoped.scope, 't': 10000,
                     'venue': 'binance-alpha', 'priceCurrency': 'USDT', 'price': 1.23,
                     'marketId': 'ALPHA_1USDT'}]

        reload = AsyncMock()
        with patch('app.api.misc.state.reload_if_stale', reload), \
             patch('app.api.misc.state.DATA', data), \
             patch('app.api.misc.store', AsyncMock(side_effect=lambda chain: scopes[chain])), \
             patch('app.api.misc.market_trades', AsyncMock(side_effect=market_tape)):
            result = await get_feed()
        reload.assert_awaited_once()
        scopes['196'].recent_trades.assert_awaited_once_with('same', 12)
        scopes['56'].recent_trades.assert_not_awaited()
        scopes['4663'].recent_trades.assert_awaited_once_with('other', 12)
        for scoped in scopes.values():
            scoped.all.assert_not_awaited()
        self.assertEqual(result['relationships'], data.signals[:30])
        self.assertEqual(len(result['trades']), 5)
        self.assertEqual([row['t'] for row in result['trades']], [10000, 10000, 10000, 4663, 196])
        self.assertTrue(all(row['priceCurrency'] == 'USDT' for row in result['trades'][:3]))
        self.assertEqual(result['trades'][-1]['token'], 'same')
        self.assertEqual(result['trades'][-1]['symbol'], 'MEME')

    async def test_top_ten_selection_and_final_hundred_trade_limit(self):
        assets = [{'chainId': '196', 'token': str(n), 'symbol': 'MEME', 'kind': 'candidate', 'tradeAt': n}
                  for n in range(1, 13)]
        data = SimpleNamespace(assets=assets,
            relations=[{'chainId': '196', 'token': a['token'], 'status': 'verified'} for a in assets], signals=[])
        scopes = {chain: SimpleNamespace(scope=chain, recent_trades=AsyncMock(
            side_effect=lambda token, limit: [{'id': token + '-' + str(n), 't': int(token) * 100 + n} for n in range(limit)]))
            for chain in ('196', '56', '4663')}
        with patch('app.api.misc.state.reload_if_stale', AsyncMock()), \
             patch('app.api.misc.state.DATA', data), \
             patch('app.api.misc.store', AsyncMock(side_effect=lambda chain: scopes[chain])), \
             patch('app.api.misc.market_trades', AsyncMock(return_value=[])):
            result = await get_feed()
        self.assertEqual([call.args[0] for call in scopes['196'].recent_trades.await_args_list],
                         [str(n) for n in range(12, 2, -1)])
        self.assertEqual(len(result['trades']), 100)
        self.assertEqual([r['t'] for r in result['trades']], sorted((r['t'] for r in result['trades']), reverse=True))
