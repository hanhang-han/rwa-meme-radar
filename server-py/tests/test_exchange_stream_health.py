"""A failed health write must not kill the market supervisor's recovery loop."""
import asyncio
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.collectors.exchange_stream import BinanceMarketFeed


class ExchangeHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_health_write_is_throttled_and_retried(self):
        scoped = SimpleNamespace(put=AsyncMock(side_effect=[sqlite3.OperationalError('database is locked'), None]))
        feed = BinanceMarketFeed('binance-alpha', 'unused', AsyncMock(return_value=[]))
        with patch('app.collectors.exchange_stream.store', AsyncMock(return_value=scoped)), \
             patch('app.collectors.exchange_stream.now_ms', return_value=10000) as clock:
            await feed._status(status='live', lastMessageAt=9999)
            self.assertEqual(feed.state['healthWriteError'], 'OperationalError')
            self.assertEqual(feed.state['healthWriteFailedAt'], 10000)
            self.assertEqual(feed.state['status'], 'live')
            self.assertEqual(feed.state['lastMessageAt'], 9999)
            await feed._status(status='live', lastMessageAt=10000)
            self.assertEqual(scoped.put.await_count, 1)
            clock.return_value = 13000
            await feed._status(status='live', lastMessageAt=12999)
            self.assertEqual(scoped.put.await_count, 2)
            self.assertIsNone(feed.state['healthWriteError'])
            written = scoped.put.await_args.args[2]
            self.assertEqual(written['lastMessageAt'], 12999)
            self.assertEqual(written['healthWriteFailedAt'], 10000)

    async def test_supervisor_survives_error_while_reporting_error(self):
        failed_status = asyncio.Event()

        async def fail_write(*args):
            failed_status.set()
            raise sqlite3.OperationalError('database is locked')

        scoped = SimpleNamespace(put=AsyncMock(side_effect=fail_write))
        feed = BinanceMarketFeed('binance-alpha', 'unused', AsyncMock(side_effect=RuntimeError('directory unavailable')))
        with patch('app.collectors.exchange_stream.store', AsyncMock(return_value=scoped)):
            supervisor = asyncio.create_task(feed._supervise())
            try:
                await asyncio.wait_for(failed_status.wait(), .5)
                await asyncio.sleep(0)
                self.assertFalse(supervisor.done())
                self.assertEqual(feed.state['error'], 'RuntimeError')
                self.assertEqual(feed.state['healthWriteError'], 'OperationalError')
            finally:
                supervisor.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await supervisor

    async def test_shutdown_cancellation_is_not_swallowed(self):
        scoped = SimpleNamespace(put=AsyncMock(side_effect=asyncio.CancelledError()))
        feed = BinanceMarketFeed('binance', 'unused', AsyncMock(return_value=[]))
        with patch('app.collectors.exchange_stream.store', AsyncMock(return_value=scoped)):
            with self.assertRaises(asyncio.CancelledError):
                await feed._status(status='live')
