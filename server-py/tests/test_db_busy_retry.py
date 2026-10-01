"""Live fact patches must survive a brief external SQLite writer collision."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

from app import db as storage


class FactPatchBusyRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_patch_rereads_external_writer_value_after_busy_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/research.sqlite'
            scoped = await storage.ResearchStore(path, '56', busy_timeout_ms=1).connect()
            try:
                await scoped.put('asset', 'token', {'price': 1, 'source': 'old'})
                with closing(sqlite3.connect(path, timeout=0.01)) as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    writer.execute(
                        'UPDATE facts SET body=? WHERE kind=? AND id=?',
                        (json.dumps({'price': 9, 'source': 'new'}), '56:asset', 'token'),
                    )

                    async def release_writer(_):
                        self.assertFalse(scoped._write_lock.locked())
                        self.assertFalse(scoped.db.in_transaction)
                        writer.commit()

                    try:
                        with patch.object(storage.asyncio, 'sleep', side_effect=release_writer) as sleep:
                            result = await scoped.patch_fact('asset', 'token', {'tradeAt': 100})
                        sleep.assert_awaited_once()
                    finally:
                        if writer.in_transaction:
                            writer.rollback()

                self.assertEqual(result, {'price': 9, 'source': 'new', 'tradeAt': 100})
                self.assertEqual(await scoped.get('asset', 'token'), result)
            finally:
                await scoped.close()

    async def test_patch_stops_after_bounded_busy_retries_without_open_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/research.sqlite'
            scoped = await storage.ResearchStore(path, '56', busy_timeout_ms=1).connect()
            try:
                await scoped.put('asset', 'token', {'price': 1})
                with closing(sqlite3.connect(path, timeout=0.01)) as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    try:
                        with patch.object(storage.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
                            with self.assertRaises(sqlite3.OperationalError) as caught:
                                await scoped.patch_fact('asset', 'token', {'price': 2})
                        self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                        self.assertEqual(sleep.await_count, 2)
                        self.assertFalse(scoped._write_lock.locked())
                        self.assertFalse(scoped.db.in_transaction)
                    finally:
                        writer.rollback()
                self.assertEqual(await scoped.get('asset', 'token'), {'price': 1})
                self.assertEqual(await scoped.patch_fact('asset', 'token', {'tradeAt': 100}),
                                 {'price': 1, 'tradeAt': 100})
            finally:
                await scoped.close()


if __name__ == '__main__':
    unittest.main()
