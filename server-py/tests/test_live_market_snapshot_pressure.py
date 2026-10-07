"""Concurrent chart viewers share one consistent read, without stale caching."""
import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.api import live_market


class SnapshotPressureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await live_market.stop_market_hubs()

    async def asyncTearDown(self):
        await live_market.stop_market_hubs()

    async def test_identical_viewers_share_only_inflight_read_not_completed_cache(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def database(*identity):
            calls.append(identity)
            entered.set()
            await release.wait()
            return {'cursor': len(calls), 'markets': [{'priceCurrency': 'WBNB', 'poolId': identity[2]}]}

        with patch.object(live_market, '_snapshot_database', database):
            viewers = [asyncio.create_task(live_market._snapshot('196', 'token', 'pool', '5m', 500)) for _ in range(100)]
            await entered.wait()
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(live_market._snapshot_inflight), 1)
            release.set()
            snapshots = await asyncio.gather(*viewers)
            self.assertTrue(all(snapshot['cursor'] == 1 for snapshot in snapshots))
            self.assertFalse(live_market._snapshot_inflight)
            newest = await live_market._snapshot('196', 'token', 'pool', '5m', 500)
            self.assertEqual((len(calls), newest['cursor']), (2, 2))

    async def test_cancelled_client_cannot_cancel_the_shared_reader(self):
        entered, release = asyncio.Event(), asyncio.Event()
        cancellations = []

        async def database(*identity):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellations.append(True)
                raise
            return {'cursor': 7}

        with patch.object(live_market, '_snapshot_database', database):
            first = asyncio.create_task(live_market._snapshot('196', 'token', 'pool', '5m', 500))
            await entered.wait()
            other = asyncio.create_task(live_market._snapshot('196', 'token', 'pool', '5m', 500))
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(cancellations, [])
            release.set()
            self.assertEqual(await other, {'cursor': 7})
            self.assertFalse(live_market._snapshot_inflight)

    async def test_different_market_bar_or_limit_has_its_own_consistent_result(self):
        entered, release = asyncio.Event(), asyncio.Event()
        identities = []

        async def database(*identity):
            identities.append(identity)
            if len(identities) == 5:
                entered.set()
            await release.wait()
            return {'identity': identity, 'priceCurrency': identity[2], 'cursor': identity[-1]}

        requests = [('196', 'token', 'WBNB-pool', '5m', 500), ('196', 'token', 'USD-pool', '5m', 500),
                    ('56', 'token', 'WBNB-pool', '5m', 500), ('196', 'token', 'WBNB-pool', '1m', 500),
                    ('196', 'token', 'WBNB-pool', '5m', 1000)]
        with patch.object(live_market, '_snapshot_database', database):
            viewers = [asyncio.create_task(live_market._snapshot(*identity)) for identity in requests]
            await entered.wait()
            release.set()
            snapshots = await asyncio.gather(*viewers)
            for identity, snapshot in zip(requests, snapshots):
                self.assertEqual(snapshot['identity'], identity)
                self.assertEqual(snapshot['priceCurrency'], identity[2])
                self.assertEqual(snapshot['cursor'], identity[-1])

    async def test_distinct_reads_are_bounded_and_duplicate_can_join_full_admission(self):
        entered, release = asyncio.Event(), asyncio.Event()
        active = peak = 0

        async def database(*identity):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == live_market.SNAPSHOT_CONCURRENCY:
                entered.set()
            try:
                await release.wait()
                return {'pool': identity[2]}
            finally:
                active -= 1

        with patch.object(live_market, '_snapshot_database', database):
            viewers = [asyncio.create_task(live_market._snapshot('196', 'token', f'pool-{index}', '5m', 500))
                       for index in range(live_market.SNAPSHOT_INFLIGHT_LIMIT)]
            await entered.wait()
            self.assertEqual(len(live_market._snapshot_inflight), 128)
            with self.assertRaises(HTTPException) as busy:
                await live_market._snapshot('196', 'token', 'overflow', '5m', 500)
            self.assertEqual(busy.exception.status_code, 503)
            duplicate = asyncio.create_task(live_market._snapshot('196', 'token', 'pool-0', '5m', 500))
            await asyncio.sleep(0)
            self.assertEqual(len(live_market._snapshot_inflight), 128)
            release.set()
            await asyncio.gather(*viewers)
            self.assertEqual(await duplicate, {'pool': 'pool-0'})
            self.assertEqual(peak, 8)
            self.assertEqual(active, 0)

    async def test_shutdown_cancels_active_and_queued_readers_and_can_restart(self):
        entered = asyncio.Event()
        active = 0

        async def database(*identity):
            nonlocal active
            active += 1
            if active == 8:
                entered.set()
            try:
                await asyncio.Future()
            finally:
                active -= 1

        with patch.object(live_market, '_snapshot_database', database):
            viewers = [asyncio.create_task(live_market._snapshot('196', 'token', f'pool-{index}', '5m', 500)) for index in range(12)]
            await entered.wait()
            await live_market.stop_market_hubs()
            results = await asyncio.gather(*viewers, return_exceptions=True)
            self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
            self.assertEqual(active, 0)
            self.assertFalse(live_market._snapshot_inflight)
        async def new_read(*identity):
            return {'cursor': 99}
        with patch.object(live_market, '_snapshot_database', new_read):
            self.assertEqual(await live_market._snapshot('196'), {'cursor': 99})
