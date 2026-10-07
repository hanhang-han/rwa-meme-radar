import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import live_market_store
from app import market_main


class MarketQueryStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = os.path.join(self.tmp.name, 'research.sqlite')
        self.market = os.path.join(self.tmp.name, 'market.sqlite')
        self.research_path = patch('app.db.DB_PATH', self.original)
        self.research_path.start()
        self.env = patch.dict(os.environ, {'LIVE_MARKET_DB': self.market,
                              'DEMAND_LEASE_DB': self.original+'.leases.sqlite'})
        self.env.start()

    async def asyncTearDown(self):
        await market_main.live_market.stop_market_hubs()
        await live_market_store.close_all()
        self.env.stop()
        self.research_path.stop()
        self.tmp.cleanup()

    async def assert_available(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=market_main.app), base_url='http://market') as client:
            ready = await client.get('/api/health/ready')
            self.assertEqual(ready.status_code, 200)
            self.assertTrue(ready.json()['ok'])
            status = await client.get('/api/live-market/status')
            self.assertEqual(status.status_code, 200)
            self.assertEqual(len(status.json()['chains']), 3)

    async def test_startup_without_research_file_never_opens_or_creates_catalogue(self):
        with patch('app.db.store', new=AsyncMock(side_effect=AssertionError('research store used'))), \
             patch.object(live_market_store.CatalogueReadStore, 'connect', new=AsyncMock(side_effect=AssertionError('catalogue opened'))):
            async with market_main.lifespan(market_main.app):
                await asyncio.wait_for(self.assert_available(), 2)
                self.assertFalse(os.path.exists(self.original))
                self.assertFalse(os.path.exists(self.original+'.leases.sqlite'))
        self.assertTrue(os.path.isfile(self.market))

    async def test_cold_start_readiness_and_status_work_while_research_writer_is_locked(self):
        old = sqlite3.connect(self.original)
        old.execute('CREATE TABLE unrelated (value INTEGER)')
        old.commit()
        old.execute('BEGIN EXCLUSIVE')
        old.execute('INSERT INTO unrelated VALUES (1)')
        try:
            async with market_main.lifespan(market_main.app):
                await asyncio.wait_for(self.assert_available(), 2)
                self.assertTrue(old.in_transaction)
        finally:
            old.rollback()
            old.close()


if __name__ == '__main__':
    unittest.main()
