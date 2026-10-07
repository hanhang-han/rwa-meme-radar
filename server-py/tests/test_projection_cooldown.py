"""Projection batches yield resources without changing their durable work."""
import asyncio
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import projection_worker as worker


class ProjectionCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_completed_batch_cannot_be_immediately_repeated(self):
        clock = [100.0]
        outcome = {'cursor': 91, 'affected': 3}

        async def run_batch():
            clock[0] += 40
            return outcome

        action = AsyncMock(side_effect=run_batch)
        with patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]):
            turn = worker.cooldown_job(action, 15)
            self.assertIs(await turn(), outcome)
            self.assertEqual(await turn(), {'accepted': 0, 'updated': 0, 'skipped': 1})
            clock[0] = 154.99
            await turn()
            action.assert_awaited_once()
            clock[0] = 155
            self.assertIs(await turn(), outcome)
        self.assertEqual(action.await_count, 2)
        self.assertEqual(outcome, {'cursor': 91, 'affected': 3})

    async def test_failed_batch_also_leaves_a_gap_then_retries(self):
        clock = [0.0]
        action = AsyncMock(side_effect=[RuntimeError('rollback complete'), {'cursor': 4}])
        with patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]):
            turn = worker.cooldown_job(action, 5)
            with self.assertRaisesRegex(RuntimeError, 'rollback complete'):
                await turn()
            self.assertEqual((await turn())['skipped'], 1)
            clock[0] = 5
            self.assertEqual(await turn(), {'cursor': 4})
        self.assertEqual(action.await_count, 2)

    async def test_cancellation_is_not_swallowed_and_zero_gap_is_supported(self):
        cancelled = worker.cooldown_job(AsyncMock(side_effect=asyncio.CancelledError), 10)
        with self.assertRaises(asyncio.CancelledError):
            await cancelled()
        action = AsyncMock(return_value={'accepted': 1})
        turn = worker.cooldown_job(action, 0)
        await turn()
        await turn()
        self.assertEqual(action.await_count, 2)

    async def test_all_registered_rebuilds_honor_operator_cooldown(self):
        jobs = {}
        clock = [100.0]
        publish = AsyncMock(return_value={'changed': True})
        derive = AsyncMock(return_value={'affected': 3})
        comparisons = AsyncMock(return_value={'accepted': 5})
        baskets = AsyncMock(return_value={'accepted': 7})
        maintenance = AsyncMock(return_value={'accepted': 64, 'updated': 64, 'skipped': 0})
        with patch.dict(os.environ, {'PROJECTION_COOLDOWN_SECONDS': '23'}), \
             patch('app.collectors.scheduler.spawn_loop',
                   side_effect=lambda name, interval, fn, delay=0, **options: jobs.update({name: fn})), \
             patch('app.realtime_projection.projection_tick', publish), \
             patch('app.realtime_projection.refresh_derived', derive), \
             patch('app.realtime_projection.prune_consumed_outbox', maintenance), \
             patch('app.comparison_service.refresh_comparisons', comparisons), \
             patch('app.collectors.baskets.refresh_baskets', baskets), \
             patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]):
            worker.start_projection_loops()
            for name, fn in jobs.items():
                if name == 'outboxMaintenance':
                    continue
                await fn()
                self.assertEqual((await fn())['skipped'], 1)
            # Maintenance is already bounded and independent from the heavy
            # batch cooldown; its scheduler alone supplies the two-second gap.
            self.assertEqual((await jobs['outboxMaintenance']())['updated'], 64)
            self.assertEqual((await jobs['outboxMaintenance']())['updated'], 64)
            clock[0] = 123
            for name, fn in jobs.items():
                if name != 'outboxMaintenance':
                    await fn()
        for action in (publish, derive, comparisons, baskets):
            self.assertEqual(action.await_count, 2)
        self.assertEqual(maintenance.await_count, 2)

    async def test_publication_keeps_minimum_rebuild_gap_when_cold_cooldown_is_zero(self):
        jobs, clock = {}, [100.0]
        publish = AsyncMock(return_value={'changed': True})
        with patch.dict(os.environ, {'PROJECTION_COOLDOWN_SECONDS': '0',
                                    'PROJECTION_MIN_PUBLISH_GAP_SECONDS': '0'}), \
             patch('app.collectors.scheduler.spawn_loop',
                   side_effect=lambda name, interval, fn, delay=0, **options: jobs.update({name: fn})), \
             patch('app.realtime_projection.projection_tick', publish), \
             patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]):
            worker.start_projection_loops()
            await jobs['realtimeProjection']()
            self.assertEqual((await jobs['realtimeProjection']())['skipped'], 1)
            clock[0] = 104.99
            self.assertEqual((await jobs['realtimeProjection']())['skipped'], 1)
            clock[0] = 105
            await jobs['realtimeProjection']()
        self.assertEqual(publish.await_count, 2)


class ProjectionPriorityTests(unittest.TestCase):
    def priority_patches(self, current=0):
        return (patch.object(worker.os, 'getpriority', return_value=current),
                patch.object(worker.os, 'setpriority'),
                patch.object(worker.sys, 'platform', 'linux'),
                patch.object(worker.shutil, 'which', return_value='/usr/bin/ionice'))

    def test_process_priority_can_only_be_lowered(self):
        for current, target, expected in ((0, '10', 10), (15, '10', 15), (19, '0', 19)):
            with self.subTest(current=current, target=target), \
                 patch.dict(os.environ, {'PROCESS_NICE': target}), \
                 patch.object(worker.os, 'getpriority', return_value=current), \
                 patch.object(worker.os, 'setpriority') as setter, \
                 patch.object(worker.shutil, 'which', return_value=None):
                self.assertEqual(worker.configure_process_priority()['cpu'], expected)
                if expected > current:
                    setter.assert_called_once_with(os.PRIO_PROCESS, 0, expected)
                else:
                    setter.assert_not_called()

    def test_existing_idle_io_priority_is_not_raised(self):
        a, b, c, d = self.priority_patches()
        with a, b, c, d, patch.object(worker.subprocess, 'run', return_value=
                SimpleNamespace(returncode=0, stdout='idle\n')) as command:
            self.assertEqual(worker.configure_process_priority()['io'], 'idle')
            self.assertEqual(command.call_count, 1)

    def test_existing_lower_best_effort_priority_is_preserved(self):
        a, b, c, d = self.priority_patches()
        with a, b, c, d, patch.dict(os.environ, {'PROCESS_IO_NICE': '3'}), \
             patch.object(worker.subprocess, 'run', side_effect=[
                 SimpleNamespace(returncode=0, stdout='best-effort: prio 7\n'),
                 SimpleNamespace(returncode=0, stdout='')]) as command:
            self.assertEqual(worker.configure_process_priority()['io'], 'best-effort:7')
            self.assertIn('7', command.call_args.args[0])
            self.assertEqual(command.call_args.args[0][1:5], ['-c', '2', '-n', '7'])

    def test_unset_io_priority_is_not_raised_above_cpu_derived_policy(self):
        a, b, c, d = self.priority_patches()
        with a, b, c, d, patch.dict(os.environ, {'PROCESS_NICE': '10', 'PROCESS_IO_NICE': '0'}), \
             patch.object(worker.subprocess, 'run', side_effect=[
                 SimpleNamespace(returncode=0, stdout='none: prio 0\n'),
                 SimpleNamespace(returncode=0, stdout='')]) as command:
            self.assertEqual(worker.configure_process_priority()['io'], 'best-effort:6')
            self.assertEqual(command.call_args.args[0][1:5], ['-c', '2', '-n', '6'])

    def test_permission_or_tool_failure_does_not_stop_projection(self):
        a, b, c, d = self.priority_patches()
        with a, b, c, d, \
             patch.object(worker.os, 'setpriority', side_effect=PermissionError), \
             patch.object(worker.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ionice', 2)):
            self.assertEqual(worker.configure_process_priority(), {'cpu': 'unavailable', 'io': 'unavailable'})

    def test_unrecognized_io_policy_is_not_modified(self):
        a, b, c, d = self.priority_patches()
        with a, b, c, d, patch.object(worker.subprocess, 'run', return_value=
                SimpleNamespace(returncode=0, stdout='unrecognized policy')) as command:
            self.assertEqual(worker.configure_process_priority()['io'], 'unavailable')
            self.assertEqual(command.call_count, 1)


if __name__ == '__main__':
    unittest.main()
