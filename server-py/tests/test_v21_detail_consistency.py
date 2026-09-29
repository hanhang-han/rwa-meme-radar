import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api import token as token_api


class DetailConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_a_relation_is_not_claimed_on_detail_page(self):
        now = int(time.time() * 1000)
        meme = '0x' + '1' * 40
        stock = '0xc845b2894dbddd03858fd2d643b4ef725fe0849d'
        relation = {
            'chainId': '196', 'token': meme, 'stock': stock, 'stockSide': stock,
            'token0': meme, 'token1': stock, 'pool': '0x' + '2' * 40,
            'ticker': 'NVDA', 'status': 'verified', 'level': 'A',
            'liquidityUsd': 2500, 'liquidityAt': now - 900_001,
            'checkedAt': now - 1000,
        }
        scoped = SimpleNamespace(
            get=AsyncMock(side_effect=lambda kind, key: {'token': meme, 'chainId': '196',
                'symbol': 'MEME', 'kind': 'candidate'} if kind == 'asset' else None),
            samples=AsyncMock(return_value=[]), recent_trades=AsyncMock(return_value=[]),
            events=AsyncMock(return_value=[]), all=AsyncMock(return_value=[]),
            activity=AsyncMock(return_value={}), trade_buckets=AsyncMock(return_value=[]),
        )
        with patch.object(token_api, 'reload_if_stale', AsyncMock()), \
             patch.object(token_api, 'store', AsyncMock(return_value=scoped)), \
             patch('app.demand_leases.publish_lease'), \
             patch.object(token_api, 'DATA', SimpleNamespace(relations=[relation], stock_views=lambda: [])), \
             patch.object(token_api, '_asset_view', return_value={'token': meme, 'chainId': '196',
                                                                  'symbol': 'MEME', 'kind': 'candidate'}), \
             patch.object(token_api, 'attach_market_detail', AsyncMock(side_effect=lambda body, _: body)):
            result = await token_api.get_token('196', meme)
        self.assertIsNone(result['relations'][0]['level'])
        self.assertIsNone(result['asset']['pairLiquidityUsd'])
        self.assertIsNone(result['asset']['relationLevel'])
        self.assertIn('尚未完成', result['analysis']['conclusion'])


if __name__ == '__main__':
    unittest.main()
