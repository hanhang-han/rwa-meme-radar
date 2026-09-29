"""Quote writes must keep moving when chain replay owns its own writer lane."""
import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import aiosqlite

from app import db as storage
from app.collectors.queue import checkpoint

os.environ['NODE_ENV'] = 'test'


class QuoteStoreIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/research.sqlite'
        self.db_path = patch.object(storage, 'DB_PATH', self.path)
        self.db_path.start()
        await storage.close_all()
        self.shared = await storage.store('196')
        await storage.prepare_quote_stores(('196',))

    async def asyncTearDown(self):
        await storage.close_all()
        self.db_path.stop()
        self.temp.cleanup()

    async def test_quote_asset_and_checkpoint_do_not_wait_for_chain_writer_lock(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def stalled_chain_writer():
            async with self.shared._write_lock:
                entered.set()
                await release.wait()

        chain = asyncio.create_task(stalled_chain_writer(), name='chain-stream-196')
        try:
            await entered.wait()
            with storage.quote_store_scope():
                quote = await storage.store('196')
                self.assertIsNot(quote.db, self.shared.db)
                self.assertIsNot(quote._write_lock, self.shared._write_lock)
                at = int(time.time() * 1000)
                await asyncio.wait_for(quote.merge_asset_observation(
                    '0xabc', {'kind': 'stock', 'token': '0xabc'},
                    {'price': (12.5, at)}, (12.5, None, at)), timeout=2)
                saved = await asyncio.wait_for(
                    checkpoint(quote, 'quote', '0xabc', success=True, now=at), timeout=2)
            self.assertEqual(saved['lastSuccessAt'], at)
            self.assertEqual((await self.shared.get('asset', '0xabc'))['price'], 12.5)
            self.assertTrue(self.shared._write_lock.locked())
        finally:
            release.set()
            await chain

    async def test_external_sqlite_writer_fails_fast_and_quote_recovers(self):
        with storage.quote_store_scope():
            quote = await storage.store('196')
        await quote.db.execute('PRAGMA busy_timeout=100')
        external = sqlite3.connect(self.path, timeout=1)
        try:
            external.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            with self.assertRaises(aiosqlite.OperationalError):
                await asyncio.wait_for(
                    checkpoint(quote, 'quote', '0xabc', success=False, now=1000),
                    timeout=2)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertFalse(quote._write_lock.locked())
        finally:
            external.rollback()
            external.close()
        saved = await checkpoint(quote, 'quote', '0xabc', success=True, now=2000)
        self.assertEqual(saved['lastSuccessAt'], 2000)
        self.assertEqual(saved['failureCount'], 0)

    async def test_checkpoint_preserves_previous_success_and_failure_backoff(self):
        with storage.quote_store_scope():
            quote = await storage.store('196')
        first = await checkpoint(quote, 'quote', '0xabc', success=True, now=1000)
        failed = await checkpoint(quote, 'quote', '0xabc', success=False,
                                  reason='missing-price-row', now=2000)
        recovered = await checkpoint(quote, 'quote', '0xabc', success=True, now=3000)
        self.assertEqual(first['lastSuccessAt'], 1000)
        self.assertEqual(failed['lastSuccessAt'], 1000)
        self.assertEqual(failed['failureCount'], 1)
        self.assertGreater(failed['nextRetryAt'], 2000)
        self.assertEqual(recovered['failureCount'], 0)
        self.assertEqual(recovered['lastSuccessAt'], 3000)
        self.assertEqual(recovered['nextRetryAt'], 0)


if __name__ == '__main__':
    unittest.main()
