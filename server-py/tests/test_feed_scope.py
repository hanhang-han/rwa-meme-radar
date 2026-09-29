import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api import misc


class FeedScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_filter_applies_before_market_limit(self):
        async def scoped_store(chain):
            return type('Scope', (), {'scope': chain, 'recent_trades': AsyncMock(return_value=[])})()

        async def scoped_market(scope, limit=100):
            return [{'chainId': scope.scope, 'id': 'trade:1', 't': 1000}]

        with patch.object(misc.state, 'reload_if_stale', AsyncMock()), \
             patch.object(misc.state.DATA, 'assets', []), \
             patch.object(misc.state.DATA, 'relations', []), \
             patch.object(misc.state.DATA, 'signals', [
                 {'chainId': '196', 'id': 'x'}, {'chainId': '56', 'id': 'y'}]), \
             patch.object(misc, 'store', AsyncMock(side_effect=scoped_store)) as stores, \
             patch.object(misc, 'market_trades', AsyncMock(side_effect=scoped_market)):
            result = await misc.get_feed('196')
        self.assertEqual([r['chainId'] for r in result['trades']], ['196'])
        self.assertEqual([r['chainId'] for r in result['relationships']], ['196'])
        stores.assert_awaited_once_with('196')

    async def test_invalid_filter_rejected(self):
        with self.assertRaises(HTTPException) as error:
            await misc.get_feed('999')
        self.assertEqual(error.exception.status_code, 400)


if __name__ == '__main__':
    unittest.main()
