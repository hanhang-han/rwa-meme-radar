import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api import misc
from app.api.misc import get_feed
from app.realtime_projection import _feed_snapshot


class FeedSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        misc._feed_cache.clear()
        misc._feed_locks.clear()

    async def test_feed_keeps_thirty_recent_signals_for_each_chain(self):
        signals = [{'id': f'{chain}-{n}', 'chainId': chain, 't': 1000 - n}
                   for n in range(40) for chain in ('196', '56', '4663')]
        snapshot = _feed_snapshot(SimpleNamespace(assets=[], relations=[], signals=signals))
        self.assertEqual(len(snapshot['signals']), 90)
        read = AsyncMock(return_value=json.dumps(snapshot))
        scoped = SimpleNamespace(recent_trades=AsyncMock(return_value=[]))
        with patch('app.api.misc.read_projection_json', read), \
             patch('app.api.misc.store', AsyncMock(return_value=scoped)), \
             patch('app.api.misc.token_trades', AsyncMock(return_value=[])), \
             patch('app.api.misc.market_trades', AsyncMock(return_value=[])):
            result = await get_feed('56')
        self.assertEqual([row['id'] for row in result['relationships']],
                         [f'56-{n}' for n in range(30)])

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

        async def market_tape(scoped, limit, **kwargs):
            return [{'id': 'market-' + scoped.scope, 'chainId': scoped.scope, 't': 10000,
                     'venue': 'binance-alpha', 'priceCurrency': 'USDT', 'price': 1.23,
                     'marketId': 'ALPHA_1USDT'}]

        async def token_tape(scoped, tokens, limit=100):
            return [{'id': scoped.scope, 'token': token, 't': int(scoped.scope)} for token in tokens]

        snapshot = _feed_snapshot(data)
        read = AsyncMock(return_value=json.dumps(snapshot))
        with patch('app.api.misc.read_projection_json', read), \
             patch('app.api.misc.store', AsyncMock(side_effect=lambda chain: scopes[chain])), \
             patch('app.api.misc.token_trades', AsyncMock(side_effect=token_tape)) as legacy, \
             patch('app.api.misc.market_trades', AsyncMock(side_effect=market_tape)):
            result = await get_feed()
        read.assert_awaited_once_with('feed')
        self.assertEqual([list(call.args[1]) for call in legacy.await_args_list], [['same'], [], ['other']])
        for scoped in scopes.values():
            scoped.all.assert_not_awaited()
            scoped.recent_trades.assert_not_awaited()
        self.assertEqual(result['relationships'], data.signals[:30])
        self.assertEqual(len(result['trades']), 2)
        self.assertEqual([row['t'] for row in result['trades']], [4663, 196])
        self.assertTrue(all(row['venue'] == 'dex' for row in result['trades']))
        self.assertTrue(all(row['usdStatus'] == 'unknown' and row['volume'] is None for row in result['trades']))
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
        async def token_tape(scoped, tokens, limit=100):
            return [{'id': token+'-'+str(n), 'token': token, 't': int(token)*100+n}
                    for token in tokens for n in range(12)][:limit]
        with patch('app.api.misc.read_projection_json', AsyncMock(return_value=json.dumps(_feed_snapshot(data)))), \
             patch('app.api.misc.store', AsyncMock(side_effect=lambda chain: scopes[chain])), \
             patch('app.api.misc.token_trades', AsyncMock(side_effect=token_tape)) as legacy, \
             patch('app.api.misc.market_trades', AsyncMock(return_value=[])):
            result = await get_feed()
        self.assertEqual(list(legacy.await_args_list[0].args[1]), [str(n) for n in range(12, 2, -1)])
        self.assertTrue(all(not scope.recent_trades.called for scope in scopes.values()))
        self.assertEqual(len(result['trades']), 100)
        self.assertEqual([r['t'] for r in result['trades']], sorted((r['t'] for r in result['trades']), reverse=True))
