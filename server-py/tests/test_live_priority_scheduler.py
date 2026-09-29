import asyncio
import time
import unittest
from unittest.mock import patch

from app import worker
from app.collectors import scheduler


class LivePriorityPressureTest(unittest.TestCase):
    def test_any_chain_at_32_queued_logs_defers_background_work(self):
        class Queue:
            def __init__(self, depth):
                self.depth = depth

            def qsize(self):
                return self.depth

        class Stream:
            def __init__(self, depth):
                self.queue = Queue(depth)

        streams = {'196': Stream(31), '56': Stream(0), '4663': Stream(0)}
        self.assertFalse(worker.live_stream_backlogged(streams.get))
        streams['56'].queue.depth = 32
        self.assertTrue(worker.live_stream_backlogged(streams.get))


class LivePrioritySchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        scheduler.TASKS.clear()

    async def asyncTearDown(self):
        await scheduler.shutdown_loops()
        scheduler.TASKS.clear()

    async def test_deferred_task_reports_status_and_staggers_after_queue_drains(self):
        pressure = {'busy': True}
        fired = asyncio.Event()
        runs = []
        snapshots = []

        async def work():
            runs.append(time.monotonic())
            fired.set()
            return {'accepted': 1}

        def save():
            snapshots.append(dict(scheduler.TASKS.get('low', {})))

        with patch.object(scheduler, 'DEFER_POLL_SECONDS', .005), \
                patch.object(scheduler, '_save_health', side_effect=save):
            scheduler.spawn_loop('low', .2, work, defer_when=lambda: pressure['busy'],
                                 max_deferral_s=.15, resume_stagger_s=.025)
            await asyncio.sleep(.02)
            self.assertFalse(fired.is_set())
            self.assertTrue(any(s.get('status') == 'deferred' and
                                s.get('outcome') == 'live-backlog' for s in snapshots))
            pressure['busy'] = False
            released = time.monotonic()
            await asyncio.wait_for(fired.wait(), .12)
            self.assertGreaterEqual(runs[0] - released, .02)
            self.assertEqual(scheduler.TASKS['low']['status'], 'waiting')
            await asyncio.sleep(.03)
            self.assertEqual(len(runs), 1)

    async def test_continuous_pressure_cannot_starve_background_task(self):
        fired = asyncio.Event()

        async def work():
            fired.set()
            return {'accepted': 1}

        with patch.object(scheduler, 'DEFER_POLL_SECONDS', .005), \
                patch.object(scheduler, '_save_health'):
            scheduler.spawn_loop('low', .2, work, defer_when=lambda: True,
                                 max_deferral_s=.03, resume_stagger_s=.01)
            await asyncio.wait_for(fired.wait(), .12)
        self.assertIn('lastDeferralExpiredAt', scheduler.TASKS['low'])
