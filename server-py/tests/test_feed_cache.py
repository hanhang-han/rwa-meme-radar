"""Bounded last-good feeds share chain work without weakening recovery reads."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException

from app.api import misc


class Clock:
    def __init__(self):
        self.seconds = 100.0
        self.wall = 1_800_000_000.0

    def monotonic(self):
        return self.seconds

    def time(self):
        return self.wall

    def advance(self, seconds):
        self.seconds += seconds
        self.wall += seconds


class FeedCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await misc.stop_feed_reads()
        misc._feed_loop = None
        misc._feed_stopping = False
        self.clock = Clock()
        assets = [{'chainId': chain, 'token': 'meme-'+chain, 'symbol': chain}
                  for chain in ('196', '56', '4663')]
        self.snapshot = {'assets': assets, 'trackedAssets': [dict(a) for a in assets], 'signals': []}
        self.calls = {chain: 0 for chain in ('196', '56', '4663')}
        self.active = {chain: 0 for chain in self.calls}
        self.peak = dict(self.active)
        self.gate = None
        self.fail = False
        self.market = AsyncMock(side_effect=self.market_rows)
        self.legacy = AsyncMock(return_value=[])
        self.stores = AsyncMock(side_effect=lambda chain: SimpleNamespace(scope=chain))
        self.patchers = [
            patch.object(misc, 'time', self.clock),
            patch.object(misc, 'read_projection_json', AsyncMock(side_effect=lambda _view: json.dumps(self.snapshot))),
            patch.object(misc, 'store', self.stores),
            patch.object(misc, 'token_trades', self.legacy),
            patch.object(misc, 'market_trades', self.market),
        ]
        for patcher in self.patchers:
            patcher.start()

    async def asyncTearDown(self):
        try:
            await misc.stop_feed_reads()
        finally:
            for patcher in reversed(self.patchers):
                patcher.stop()

    async def market_rows(self, store, **kwargs):
        chain = store.scope
        self.calls[chain] += 1
        self.active[chain] += 1
        self.peak[chain] = max(self.peak[chain], self.active[chain])
        sequence = self.calls[chain]
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.fail:
                raise RuntimeError('synthetic-read-failed')
            return [{'id': f'{chain}-{sequence}', 'token': kwargs['tokens'][0], 'venue': 'dex',
                     't': int(self.clock.time()*1000), 'volume': 1, 'volumeCurrency': 'USD'}]
        finally:
            self.active[chain] -= 1

    async def wait_calls(self, chain, count):
        async with asyncio.timeout(1):
            while self.calls[chain] < count:
                await asyncio.sleep(0)

    async def test_all_and_chain_first_visitors_share_exactly_three_chain_reads(self):
        self.gate = asyncio.Event()
        visitors = [asyncio.create_task(misc.get_feed(key))
                    for key in ('all', '56', '196', '4663') for _ in range(4)]
        for chain in self.calls:
            await self.wait_calls(chain, 1)
        self.assertEqual(self.calls, {'196': 1, '56': 1, '4663': 1})
        self.assertLessEqual(len(misc._feed_chain_tasks), 3)
        self.assertLessEqual(len(misc._feed_tasks), 4)
        self.gate.set()
        results = await asyncio.gather(*visitors)
        self.assertEqual(self.market.await_count, 3)
        self.assertEqual(self.legacy.await_count, 3)
        self.assertEqual(self.stores.await_count, 3)
        self.assertTrue(all(result['cacheStatus'] == 'fresh' for result in results))
        self.assertEqual(len(results[0]['trades']), 3)

    async def test_swr_returns_without_waiting_and_preserves_original_at(self):
        first = await misc.get_feed('56')
        self.clock.advance(3)
        self.gate = asyncio.Event()
        stale = await asyncio.wait_for(misc.get_feed('56'), .3)
        task = misc._feed_tasks['56'][1]
        await self.wait_calls('56', 2)
        self.assertFalse(task.done())
        self.assertEqual(stale['cacheStatus'], 'stale-refreshing')
        self.assertEqual(stale['cacheAgeMs'], 3000)
        self.assertEqual(stale['at'], first['at'])
        self.assertEqual(stale['trades'], first['trades'])
        self.gate.set()
        await task
        renewed = await misc.get_feed('56')
        self.assertEqual(renewed['cacheStatus'], 'fresh')
        self.assertGreater(renewed['at'], first['at'])

    async def test_failed_rebuild_retains_last_good_only_within_thirty_seconds(self):
        first = await misc.get_feed('56')
        self.clock.advance(3)
        self.fail = True
        stale = await misc.get_feed('56')
        task = misc._feed_tasks['56'][1]
        await asyncio.gather(task, return_exceptions=True)
        retained = await misc.get_feed('56')
        self.assertEqual(retained['cacheStatus'], 'stale-error')
        self.assertEqual(retained['at'], first['at'])
        self.assertEqual(retained['trades'], first['trades'])
        self.assertEqual(stale['cacheAgeMs'], 3000)
        self.clock.advance(28)
        with self.assertRaises(HTTPException) as failure:
            await misc.get_feed('56')
        self.assertEqual(failure.exception.status_code, 503)
        self.assertEqual(misc._feed_cache['56'][1]['at'], first['at'])

    async def test_total_deadline_also_bounds_publication_verification(self):
        async def blocked_snapshot(*args, **kwargs):
            await asyncio.Event().wait()
        with patch.object(misc, 'FEED_READ_TIMEOUT_SECONDS', .03), \
             patch.object(misc, 'read_projection_payload', AsyncMock(side_effect=blocked_snapshot)):
            with self.assertRaises(HTTPException) as failure:
                await asyncio.wait_for(misc.get_feed('56'), .3)
        self.assertEqual(failure.exception.status_code, 503)
        self.assertEqual(failure.exception.detail, 'snapshot-not-ready')
        self.assertEqual(self.market.await_count, 0)

    async def test_expired_cache_requires_a_successful_read(self):
        first = await misc.get_feed('56')
        self.clock.advance(31)
        renewed = await misc.get_feed('56')
        self.assertEqual(self.calls['56'], 2)
        self.assertEqual(renewed['cacheStatus'], 'fresh')
        self.assertGreater(renewed['at'], first['at'])

    async def test_cancelled_visitor_does_not_cancel_shared_chain_or_response(self):
        self.gate = asyncio.Event()
        first = asyncio.create_task(misc.get_feed('56'))
        other = asyncio.create_task(misc.get_feed('all'))
        await self.wait_calls('56', 1)
        shared_chain = misc._feed_chain_tasks['56'][1]
        shared_response = misc._feed_tasks['56'][1]
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(shared_chain.cancelled())
        self.assertFalse(shared_response.cancelled())
        self.gate.set()
        self.assertEqual(len((await other)['trades']), 3)
        await shared_response
        self.assertEqual(self.calls['56'], 1)

    async def test_start_warms_once_without_waiting_and_stop_drains_all_work(self):
        self.gate = asyncio.Event()
        await asyncio.wait_for(misc.start_feed_reads(), .3)
        await misc.start_feed_reads()
        await self.wait_calls('56', 1)
        self.assertFalse(misc._feed_warm_task.done())
        tasks = [record[1] for values in (misc._feed_tasks, misc._feed_chain_tasks) for record in values.values()]
        await asyncio.wait_for(misc.stop_feed_reads(), .5)
        self.assertTrue(all(task.done() for task in tasks))
        self.assertEqual(misc._feed_tasks, {})
        self.assertEqual(misc._feed_chain_tasks, {})
        self.assertEqual(misc._feed_cache, {})
        self.assertEqual(misc._feed_chain_cache, {})
        self.assertIsNone(misc._feed_warm_task)
        with self.assertRaises(HTTPException):
            await misc.get_feed('56')

    async def test_changed_tracked_identity_cannot_reuse_even_fresh_wrong_scope(self):
        original = await misc.get_feed('56')
        for group in ('assets', 'trackedAssets'):
            for row in self.snapshot[group]:
                if row['chainId'] == '56':
                    row['token'] = 'changed'
        renewed = await misc.get_feed('56')
        self.assertEqual(self.calls['56'], 2)
        self.assertEqual(renewed['trades'][0]['token'], 'changed')
        self.assertNotEqual(renewed['trades'][0]['token'], original['trades'][0]['token'])

    async def test_changed_scope_waits_for_old_work_then_reads_new_scope_serially(self):
        self.gate = asyncio.Event()
        older = asyncio.create_task(misc.get_feed('56'))
        await self.wait_calls('56', 1)
        for group in ('assets', 'trackedAssets'):
            for row in self.snapshot[group]:
                if row['chainId'] == '56':
                    row['token'] = 'new-scope'
        newer = asyncio.create_task(misc.get_feed('56'))
        await asyncio.sleep(0)
        self.gate.set()
        old_result, new_result = await asyncio.gather(older, newer)
        self.assertEqual(old_result['trades'][0]['token'], 'meme-56')
        self.assertEqual(new_result['trades'][0]['token'], 'new-scope')
        self.assertEqual(self.calls['56'], 2)
        self.assertEqual(self.peak['56'], 1)

    async def test_fresh_query_bypasses_response_and_completed_chain_caches(self):
        first = await misc.get_feed('56')
        self.clock.advance(3)
        application = FastAPI()
        application.include_router(misc.router, prefix='/api')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://test') as client:
            response = await client.get('/api/feed', params={'chain': '56', 'fresh': '1'})
        self.assertEqual(response.status_code, 200)
        renewed = response.json()
        self.assertEqual(self.calls['56'], 2)
        self.assertEqual(renewed['cacheStatus'], 'fresh')
        self.assertNotEqual(renewed['trades'][0]['id'], first['trades'][0]['id'])
        # A second fresh request must read again even within the two-second TTL.
        await misc.get_feed('56', fresh=True)
        self.assertEqual(self.calls['56'], 3)

    async def test_fresh_waits_out_older_swr_then_starts_its_own_new_read(self):
        first = await misc.get_feed('56')
        self.clock.advance(3)
        self.gate = asyncio.Event()
        stale = await misc.get_feed('56')
        await self.wait_calls('56', 2)
        requested = asyncio.create_task(misc.get_feed('56', fresh=True))
        await asyncio.sleep(0)
        self.assertFalse(requested.done())
        self.assertEqual(stale['at'], first['at'])
        self.gate.set()
        recovered = await requested
        self.assertEqual(self.calls['56'], 3)
        self.assertEqual(self.peak['56'], 1)
        self.assertEqual(recovered['cacheStatus'], 'fresh')
        self.assertEqual(recovered['trades'][0]['id'], '56-3')

    async def test_concurrent_fresh_all_and_chain_share_new_reads(self):
        self.gate = asyncio.Event()
        visitors = [asyncio.create_task(misc.get_feed(key, fresh=True))
                    for key in ('all', '56', '196', '4663')]
        for chain in self.calls:
            await self.wait_calls(chain, 1)
        self.assertEqual(self.calls, {'196': 1, '56': 1, '4663': 1})
        self.gate.set()
        results = await asyncio.gather(*visitors)
        self.assertTrue(all(result['cacheStatus'] == 'fresh' for result in results))
        self.assertEqual(self.peak, {'196': 1, '56': 1, '4663': 1})

    async def test_fresh_old_wait_and_rebuild_share_one_absolute_deadline(self):
        await misc.get_feed('56')
        self.clock.advance(3)
        older_gate, newer_gate = asyncio.Event(), asyncio.Event()
        self.gate = older_gate
        await misc.get_feed('56')
        await self.wait_calls('56', 2)
        observed = []
        original_wait = asyncio.wait_for
        async def wait(future, timeout):
            observed.append(timeout)
            return await original_wait(future, timeout)
        with patch.object(misc, 'FEED_READ_TIMEOUT_SECONDS', .2), \
             patch.object(misc.asyncio, 'wait_for', side_effect=wait):
            requested = asyncio.create_task(misc.get_feed('56', fresh=True))
            while not observed:
                await asyncio.sleep(0)
            # Simulate 130ms of the old wait consuming the request's budget.
            self.clock.advance(.13)
            self.gate = newer_gate
            older_gate.set()
            await self.wait_calls('56', 3)
            shared_chain = misc._feed_chain_tasks['56'][1]
            shared_response = misc._feed_tasks['56'][1]
            with self.assertRaises(HTTPException) as failure:
                await requested
            self.assertEqual(failure.exception.status_code, 503)
            self.assertEqual(len(observed), 2)
            self.assertAlmostEqual(observed[0], .2)
            self.assertAlmostEqual(observed[1], .07)
            self.assertFalse(shared_chain.done())
            self.assertFalse(shared_response.done())
            newer_gate.set()
            await shared_response


if __name__ == '__main__':
    unittest.main()
