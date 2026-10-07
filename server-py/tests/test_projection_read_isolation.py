"""Small feed reads remain bounded and current during large projection work."""
import asyncio
import hashlib
import json
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import read_model_store as storage
from app import realtime_projection as projection
from app.projection_query_cache import QueryBusy, QueryWork


def publication(revision=1, *, epoch='source'):
    bodies = {view: projection._encode_snapshot(json.dumps({
        'now': revision * 1000, 'realtime': {'revision': revision, 'cursor': revision * 10},
        'view': view, 'assets': [{'chainId': '56', 'token': '0xa', 'price': revision}],
    })) for view in ('full', 'overview', 'market', 'feed')}
    manifest = {
        'sourceEpoch': epoch, 'publication': hashlib.sha256(str(revision).encode()).hexdigest(),
        'checkedAtMs': int(time.time() * 1000),
        'views': {view: {'revision': revision, 'cursor': revision * 10,
                         'builtAt': revision * 1000, 'sha256': hashlib.sha256(raw).hexdigest()}
                  for view, raw in bodies.items()},
    }
    return manifest, bodies


class WorkerLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_lanes_have_four_total_workers_and_one_shared_job_budget(self):
        work = QueryWork(jobs=12)
        release = threading.Event()
        entered = {lane: threading.Event() for lane in ('default', 'manifest', 'feed')}
        counts, lock = {}, threading.Lock()

        def blocked(lane):
            with lock:
                counts[lane] = counts.get(lane, 0) + 1
                if counts[lane] == work.worker_limits[lane]:
                    entered[lane].set()
            if not release.wait(5):
                raise RuntimeError('test worker was not released')
            return lane

        tasks = [asyncio.create_task(work.cpu(blocked, lane, lane=lane))
                 for lane in ('default', 'manifest', 'feed') for _ in range(4)]
        try:
            for event in entered.values():
                self.assertTrue(await asyncio.to_thread(event.wait, 2))
            self.assertEqual(counts, {'default': 2, 'manifest': 1, 'feed': 1})
            self.assertEqual(work.stats['activeTotalWorkers'], 4)
            self.assertEqual(len(work.jobs), 12)
            for lane in work.worker_limits:
                with self.assertRaises(QueryBusy):
                    await work.cpu(lambda: None, lane=lane)
            self.assertEqual(len(work.jobs), 12)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await work.close()
        self.assertEqual(work.stats['peakWorkers'], 2)
        self.assertEqual(work.stats['peakManifestWorkers'], 1)
        self.assertEqual(work.stats['peakFeedWorkers'], 1)
        self.assertEqual(work.stats['peakTotalWorkers'], 4)
        self.assertEqual(work.stats['activeTotalWorkers'], 0)
        self.assertFalse(work.jobs)

    async def test_cancelled_feed_waiter_retains_one_producer_until_close_drains(self):
        work = QueryWork()
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocked():
            calls.append(True)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test worker was not released')
            return 'current'

        action = lambda: work.cpu(blocked, lane='feed')
        first = asyncio.create_task(work.read(('feed', 1), action))
        second, closing = None, None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(work.read(('feed', 1), action))
            await asyncio.sleep(0)
            closing = asyncio.create_task(work.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.assertEqual(work.stats['activeFeedWorkers'], 1)
            self.assertEqual(len(work.jobs), 1)
            self.assertEqual(len(work.inflight), 1)
            self.assertEqual(calls, [True])
        finally:
            release.set()
            if second is not None:
                self.assertEqual(await second, 'current')
            await (closing or work.close())
        self.assertEqual(work.stats['mergedReads'], 1)
        self.assertFalse(work.jobs)
        self.assertFalse(work.inflight)

    async def test_shared_reader_limit_is_not_multiplied_by_new_lanes(self):
        work = QueryWork(readers=2)
        release = asyncio.Event()

        async def reader(lane):
            async with work.admit():
                await release.wait()
                return await work.cpu(lambda: lane, lane=lane)

        tasks = [asyncio.create_task(reader(lane)) for lane in ('default', 'feed')]
        try:
            await asyncio.sleep(0)
            self.assertEqual(work.waiters, 2)
            with self.assertRaises(QueryBusy):
                await reader('manifest')
        finally:
            release.set()
            await asyncio.gather(*tasks)
            await work.close()
        self.assertEqual(work.waiters, 0)


class ProjectionReadIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await projection.stop_projection_queries()
        self.config = {'enabled': True, 'dsn': 'fixture', 'maxVerifiedAgeMs': 6000}
        self.manifest, self.bodies = publication()
        self.settings = patch.object(storage, 'settings', return_value=self.config)
        self.manifests = patch.object(storage, 'shared_manifest', side_effect=self.current_manifest)
        self.body_reader = patch.object(storage, 'shared_body', side_effect=self.current_body)
        self.settings.start()
        self.checked = self.manifests.start()
        self.fetched = self.body_reader.start()
        self.large_tasks = []
        self.release_large = threading.Event()

    async def asyncTearDown(self):
        self.release_large.set()
        await asyncio.gather(*self.large_tasks, return_exceptions=True)
        await projection.stop_projection_queries()
        self.body_reader.stop()
        self.manifests.stop()
        self.settings.stop()

    def current_manifest(self, config):
        return self.manifest if storage.valid_manifest(config, self.manifest) else None

    def current_body(self, config, manifest, view):
        return self.bodies[view] if storage.valid_manifest(config, manifest) else None

    async def occupy_large_workers(self):
        entered = [threading.Event(), threading.Event()]

        def blocked(event):
            event.set()
            if not self.release_large.wait(5):
                raise RuntimeError('test large worker was not released')

        self.large_tasks = [asyncio.create_task(projection.run_projection_query(blocked, event))
                            for event in entered]
        for event in entered:
            self.assertTrue(await asyncio.to_thread(event.wait, 2))
        self.assertEqual(projection._query_coordinator().stats['activeWorkers'], 2)

    async def test_cached_feed_checks_each_manifest_while_large_view_still_waits(self):
        original = await projection.read_projection_payload('feed')
        await self.occupy_large_workers()
        large_view = asyncio.create_task(projection.read_projection_payload('market'))
        try:
            for _ in range(3):
                before = self.checked.call_count
                result = await asyncio.wait_for(projection.read_projection_payload('feed'), 1)
                self.assertIs(result, original)
                self.assertEqual(self.checked.call_count, before + 1)
            self.assertEqual(self.fetched.call_count, 1)
            self.assertFalse(large_view.done())
            health = projection.projection_query_health()
            self.assertEqual(health['limits']['workerLanes'], {'default': 2, 'manifest': 1, 'feed': 1})
            self.assertEqual(health['stats']['peakManifestWorkers'], 1)
        finally:
            self.release_large.set()
            await large_view

    async def test_feed_new_publication_reads_decodes_and_parses_with_large_workers_busy(self):
        original = await projection.read_projection_payload('feed')
        projection._serialized_projection['feed'][1].shared_verified_at -= 10_000
        self.manifest, self.bodies = publication(2)
        await self.occupy_large_workers()
        latest = await asyncio.wait_for(projection.read_projection_payload('feed'), 1)
        self.assertIsNot(latest, original)
        self.assertEqual(latest['realtime']['revision'], 2)
        self.assertEqual(latest['assets'][0]['price'], 2)
        self.assertEqual(self.fetched.call_count, 2)
        self.assertEqual(projection._query_coordinator().stats['parsed'], 2)
        self.assertEqual(projection._query_coordinator().stats['peakFeedWorkers'], 1)

    async def test_plain_legacy_feed_also_prepares_and_parses_in_feed_lane(self):
        await self.occupy_large_workers()
        text = json.dumps({'assets': [{'price': 0}], 'realtime': {'revision': 9}})
        reader = AsyncMock(return_value=text)
        payload = await asyncio.wait_for(projection.read_projection_payload('feed', json_reader=reader), 1)
        self.assertEqual(payload['assets'][0]['price'], 0)
        self.assertEqual(payload['realtime']['revision'], 9)
        self.assertEqual(projection._query_coordinator().stats['peakFeedWorkers'], 1)

    async def test_manifest_only_coalesces_overlapping_views_and_never_completed_checks(self):
        entered, release = threading.Event(), threading.Event()
        for view in ('full', 'overview', 'market', 'feed'):
            await projection.read_projection_payload(view)
        before = self.checked.call_count

        def delayed(config):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test manifest was not released')
            return self.current_manifest(config)

        with patch.object(storage, 'shared_manifest', side_effect=delayed) as checked:
            readers = [asyncio.create_task(projection.read_projection_payload(view))
                       for view in ('full', 'overview', 'market', 'feed')]
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await asyncio.sleep(0)
                self.assertEqual(checked.call_count, 1)
                self.assertEqual(projection._query_coordinator().stats['activeManifestWorkers'], 1)
            finally:
                release.set()
                await asyncio.gather(*readers)
            self.assertEqual(checked.call_count, 1)
            await projection.read_projection_payload('feed')
            self.assertEqual(checked.call_count, 2)
        self.assertEqual(self.checked.call_count, before)
        self.assertEqual(self.fetched.call_count, 4)

    async def test_expired_current_manifest_uses_original_fallback_despite_feed_cache(self):
        await projection.read_projection_payload('feed')
        self.manifest['checkedAtMs'] -= 10_000
        with patch.object(projection, 'store', AsyncMock(side_effect=RuntimeError('original-path'))) as fallback:
            with self.assertRaisesRegex(RuntimeError, 'original-path'):
                await projection.read_projection_payload('feed')
        fallback.assert_awaited_once_with('196')
        self.assertEqual(self.fetched.call_count, 1)

    async def test_source_reset_and_revision_regression_cannot_serve_previous_feed(self):
        for revision, epoch in ((3, 'source'), (4, 'reset'), (2, 'reset')):
            self.manifest, self.bodies = publication(revision, epoch=epoch)
            payload = await projection.read_projection_payload('feed')
            self.assertEqual(payload['realtime']['revision'], revision)
            self.assertEqual(payload.publication_stamp[1], epoch)
        self.assertEqual(self.fetched.call_count, 3)
        self.assertEqual(projection._query_coordinator().stats.get('previousPublicationsServed', 0), 0)

    async def test_existing_previous_publication_grace_is_preserved(self):
        previous = await projection.read_projection_payload('feed')
        self.manifest, self.bodies = publication(2)
        # The verified old graph may finish while preparation starts, using
        # the original rule; isolating work must not widen that grace.
        self.assertIs(await projection.read_projection_payload('feed'), previous)
        tasks = list(projection._shared_refreshes.values())
        await asyncio.gather(*tasks)
        current = await projection.read_projection_payload('feed')
        self.assertEqual(current['realtime']['revision'], 2)
        self.assertIsNot(current, previous)
        self.assertEqual(projection._query_coordinator().stats['previousPublicationsServed'], 1)

    async def test_refresh_manifest_expiring_during_body_read_keeps_original_fallback(self):
        entered, release = threading.Event(), threading.Event()

        def expire_after_fetch(config, manifest, view):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test body was not released')
            manifest['checkedAtMs'] -= 10_000
            return self.bodies[view]

        with patch.object(storage, 'shared_body', side_effect=expire_after_fetch), \
             patch.object(projection, 'store', AsyncMock(side_effect=RuntimeError('original-path'))):
            request = asyncio.create_task(projection.read_projection_payload('feed'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            finally:
                release.set()
            with self.assertRaisesRegex(RuntimeError, 'original-path'):
                await request
        self.assertNotIn('feed', projection._serialized_projection or {})
