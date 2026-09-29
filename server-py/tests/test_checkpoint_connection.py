"""Collector checkpoints must not stall behind the chain connection's read queue."""
import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ['NODE_ENV'] = 'test'

from app.collectors.queue import checkpoint
from app.db import ResearchStore, WriterLock


class CheckpointConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = await ResearchStore(
            self.temp.name + '/research.sqlite', '196', write_lock=WriterLock()).connect()

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    async def test_checkpoint_bypasses_busy_shared_connection_queue(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_read():
            entered.set()
            release.wait(5)
            return 1

        await self.store.db.create_function('slow_read', 0, slow_read)
        read_task = asyncio.create_task(self.store.fetchall('SELECT slow_read()'))
        try:
            self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait), 1))
            saved = await asyncio.wait_for(
                checkpoint(self.store, 'pool-quote', '0xpool', success=True, now=1000), 1)
            self.assertEqual(saved['lastSuccessAt'], 1000)
            self.assertFalse(self.store._write_lock.locked())
        finally:
            release.set()
            await read_task
        self.assertEqual((await self.store.get('collector-job', 'pool-quote:0xpool'))['lastSuccessAt'], 1000)

    async def test_cancelled_checkpoint_rolls_back_private_connection(self):
        await checkpoint(self.store, 'pool-quote', '0xpool', success=True, now=1000)
        connection = self.store._checkpoint_db
        original_execute = connection.execute
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pause_insert(sql, parameters=None):
            if sql.startswith('INSERT INTO facts'):
                entered.set()
                await release.wait()
            return await original_execute(sql, parameters)

        task = None
        try:
            with patch.object(connection, 'execute', new=pause_insert):
                task = asyncio.create_task(checkpoint(
                    self.store, 'pool-quote', '0xpool', success=False, now=2000))
                await asyncio.wait_for(entered.wait(), 1)
                self.assertTrue(connection.in_transaction)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertFalse(connection.in_transaction)
        self.assertFalse(self.store._write_lock.locked())
        previous = await self.store.get('collector-job', 'pool-quote:0xpool')
        self.assertEqual(previous['lastSuccessAt'], 1000)
        self.assertEqual(previous['failureCount'], 0)
        await asyncio.wait_for(checkpoint(
            self.store, 'pool-quote', '0xpool', success=False, now=3000), 1)


if __name__ == '__main__':
    unittest.main()
