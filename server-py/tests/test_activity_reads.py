import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import activity_reads
from app.db import ResearchStore


class AggregateReads(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.s = await ResearchStore(self.temp.name + '/research.sqlite', '56').connect()
        self.addAsyncCleanup(self.s.close)
        self.addCleanup(self.temp.cleanup)

    async def test_activity_keeps_window_semantics_without_fetching_trade_bodies(self):
        at = int(time.time() * 1000)
        rows = [
            {'id': str(i), 't': at - i * 1000, 'type': 'buy' if i % 3 else 'sell',
             'volume': i / 10 if i % 7 else None, 'user': 'wallet-' + str(i % 31)}
            for i in range(1500)
        ]
        rows += [{'id': 'missing-user', 't': at, 'type': 'unknown', 'volume': 0},
                 {'id': 'empty-user', 't': at, 'type': 'buy', 'user': '', 'volume': None}]
        await self.s.put_trades('token', rows)
        await self.s.put_trades('other', [{'id': 'other', 't': at, 'type': 'sell', 'volume': 999}])
        cutoff = at - 1000 * 1100
        selected = [r for r in rows if r['t'] >= cutoff]
        expected = {'count': len(selected),
                    'buys': sum(r.get('type') == 'buy' for r in selected),
                    'sells': sum(r.get('type') == 'sell' for r in selected),
                    'volume': sum(r['volume'] for r in selected if r.get('volume') is not None) or None,
                    'traders': len({r.get('user') for r in selected if r.get('user')})}
        with patch.object(self.s, 'fetchall', side_effect=AssertionError('trade body transfer')):
            actual = await self.s.activity('token', cutoff)
        self.assertEqual({k: v for k, v in actual.items() if k != 'volume'},
                         {k: v for k, v in expected.items() if k != 'volume'})
        self.assertAlmostEqual(actual['volume'], expected['volume'])
        plan = await self.s.fetchall('EXPLAIN QUERY PLAN SELECT body FROM trades WHERE asset=? AND t>=?',
                                     (self.s.key('token'), cutoff))
        self.assertTrue(any('trades_time' in str(tuple(row)) for row in plan))

    async def test_empty_and_zero_volume_preserve_unknown_volume(self):
        self.assertEqual(await self.s.activity('empty', 0),
                         {'count': 0, 'buys': 0, 'sells': 0, 'volume': None, 'traders': 0})
        await self.s.put_trades('zero', [{'id': 'a', 't': 1, 'volume': 0, 'type': 'buy', 'user': 'x'}])
        self.assertIsNone((await self.s.activity('zero', 0))['volume'])

    async def test_selected_facts_are_batched_and_scope_isolated(self):
        await self.s.db.executemany('INSERT INTO facts VALUES (?,?,?)',
                                   [('56:attempt', str(i), '{"at":1}') for i in range(600)] +
                                   [('196:attempt', 'foreign', '{"at":2}')])
        await self.s.db.commit()
        with patch.object(self.s, 'fetchall', wraps=self.s.fetchall) as reads:
            result = await self.s.get_many('attempt', [*(str(i) for i in range(600)), 'foreign', '0'])
        self.assertEqual(len(result), 600)
        self.assertEqual(reads.await_count, 3)
        self.assertNotIn('foreign', result)
        self.assertEqual(await self.s.get_many('attempt', []), {})


class SharedActivityCache(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        activity_reads._cache.clear()
        activity_reads._pending.clear()
        activity_reads._read_slots = asyncio.Semaphore(2)
        self.s = SimpleNamespace(scope='56', path='test', activity=AsyncMock(return_value={'count': 1}))

    async def test_concurrent_same_token_reads_once_and_returns_independent_dicts(self):
        results = await asyncio.gather(*(activity_reads.read_activity(self.s, 'token') for _ in range(12)))
        self.s.activity.assert_awaited_once()
        results[0]['scope'] = 'mutated'
        cached = await activity_reads.read_activity(self.s, 'token')
        self.assertNotIn('scope', cached)
        self.assertEqual(cached['observedAt'], results[1]['observedAt'])

    async def test_failed_read_can_retry(self):
        self.s.activity.side_effect = [RuntimeError('busy'), {'count': 2}]
        with self.assertRaises(RuntimeError):
            await activity_reads.read_activity(self.s, 'token')
        self.assertEqual((await activity_reads.read_activity(self.s, 'token'))['count'], 2)
        self.assertEqual(self.s.activity.await_count, 2)

    async def test_disconnected_request_does_not_cancel_shared_read(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def load(*args):
            entered.set()
            await release.wait()
            return {'count': 3}
        self.s.activity.side_effect = load
        first = asyncio.create_task(activity_reads.read_activity(self.s, 'token'))
        await entered.wait()
        second = asyncio.create_task(activity_reads.read_activity(self.s, 'token'))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual((await second)['count'], 3)
        self.s.activity.assert_awaited_once()
        self.assertEqual(activity_reads._pending, {})

    async def test_cache_and_pending_have_bounds(self):
        with patch.object(activity_reads, 'MAX_ENTRIES', 2):
            for token in ('a', 'b', 'c'):
                await activity_reads.read_activity(self.s, token)
            self.assertEqual(len(activity_reads._cache), 2)
        activity_reads._pending['sentinel'] = object()
        with patch.object(activity_reads, 'MAX_PENDING', 1):
            with self.assertRaises(HTTPException) as error:
                await activity_reads.read_activity(self.s, 'd')
        self.assertEqual(error.exception.status_code, 503)
        activity_reads._pending.clear()
        for key, (_, value) in list(activity_reads._cache.items()):
            activity_reads._cache[key] = (0, value)
        await activity_reads.read_activity(self.s, 'b')
        self.assertEqual(len(activity_reads._cache), 1)
