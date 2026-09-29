import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api import misc


class PairGradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pair_endpoint_reassesses_expired_official_grade(self):
        now = int(time.time() * 1000)
        meme = '0x' + '1' * 40
        stock = '0xc845b2894dbddd03858fd2d643b4ef725fe0849d'
        relation = {'chainId': '196', 'token': meme, 'stock': stock,
                    'stockSide': stock, 'token0': meme, 'token1': stock,
                    'pool': '0x' + '2' * 40, 'ticker': 'NVDA',
                    'status': 'verified', 'level': 'A', 'liquidityUsd': 2500,
                    'liquidityAt': now - 900_001}
        scoped = SimpleNamespace(
            get=AsyncMock(return_value={'token': meme}),
            recent_trades=AsyncMock(return_value=[]),
            events=AsyncMock(return_value=[]), samples=AsyncMock(return_value=[]),
            activity=AsyncMock(return_value={}),
        )
        with patch.object(misc.state, 'reload_if_stale', AsyncMock()), \
             patch.object(misc.state, 'DATA', SimpleNamespace(relations=[relation])), \
             patch.object(misc, 'store', AsyncMock(return_value=scoped)):
            result = await misc.get_pair('196', meme)
        self.assertIsNone(result['relations'][0]['level'])
        self.assertEqual(result['relations'][0]['evidenceStatus'], 'liquidity-stale')


if __name__ == '__main__':
    unittest.main()
