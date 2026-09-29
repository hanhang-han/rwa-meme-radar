import os
import sqlite3
import tempfile
import unittest

from app.db import ResearchStore


class MarketRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, 'market.sqlite')
        self.store = await ResearchStore(self.path, '56').connect()

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    async def test_prune_keeps_other_markets_and_recent_trades(self):
        for market in ('binance:stock', 'binance-alpha:meme', 'dex:pool', 'legacy'):
            await self.store.put_trades(market, [{'id': 'old', 't': 100}, {'id': 'new', 't': 1000}])
        self.assertEqual(await self.store.prune_market_trades('binance:', 500), 1)
        self.assertEqual([r['id'] for r in await self.store.recent_trades('binance:stock')], ['new'])
        for market in ('binance-alpha:meme', 'dex:pool', 'legacy'):
            self.assertEqual(len(await self.store.recent_trades(market)), 2)
        plan = await self.store.fetchall('''EXPLAIN QUERY PLAN SELECT rowid FROM trades
            INDEXED BY trades_time WHERE asset>=? AND asset<? AND t<? LIMIT 250''',
            ('56:binance:', '56:binance;', 500))
        self.assertIn('COVERING INDEX trades_time', str([tuple(row) for row in plan]))

    async def test_no_expired_market_needs_no_write_transaction(self):
        await self.store.put_trades('binance:stock', [{'id': 'new', 't': 1000}])
        blocker = sqlite3.connect(self.path)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            self.assertEqual(await self.store.prune_market_trades('binance:', 500), 0)
        finally:
            blocker.rollback()
            blocker.close()
