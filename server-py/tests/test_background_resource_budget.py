"""Admission owns real locks, and waits/cancellation never block the live path."""
import asyncio
import contextvars
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import resource_budget as budget, worker
from app.collectors import scheduler


async def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('condition did not become true')
        await asyncio.sleep(.005)


CHILD = textwrap.dedent('''
    import asyncio, os, sys
    from pathlib import Path
    from app import resource_budget as budget
    os.environ['BACKGROUND_BUDGET_PATH'] = sys.argv[1]
    budget.POLL_SECONDS = .005
    budget.host_pressure = lambda root: {'limited': False, 'reasons': []}
    async def run():
        async with budget.background_turn(sys.argv[5], priority=sys.argv[6] == '1',
                on_wait=lambda state: Path(sys.argv[2]).touch(),
                lane=sys.argv[8] if len(sys.argv) > 8 else 'research'):
            Path(sys.argv[3]).touch()
            if sys.argv[7] != '-':
                with open(sys.argv[7], 'a') as log:
                    log.write(sys.argv[5] + '\\n')
            await asyncio.sleep(float(sys.argv[4]))
    asyncio.run(run())
''')


class BackgroundBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_requests_continue_while_cold_and_pool_lanes_are_held(self):
        holder, release = await self.holder()
        pool_entered, pool_release, quoted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def pool():
            async with budget.background_turn('slow-pool', lane='pool-network'):
                pool_entered.set()
                await pool_release.wait()
        async def quote():
            async with budget.background_turn('short-quote', lane='quote-network', max_run_s=1):
                quoted.set()
        pool_task = self.task(pool())
        await asyncio.wait_for(pool_entered.wait(), 1)
        quote_task = self.task(quote())
        await asyncio.wait_for(quoted.wait(), 1)
        self.assertFalse(holder.done())
        self.assertFalse(pool_task.done())
        await quote_task
        release.set()
        pool_release.set()
        await asyncio.gather(holder, pool_task)

    async def test_quote_lane_keeps_pressure_and_excludes_overlapping_quotes(self):
        first_entered, first_release, second_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def quote(name, entered, release=None):
            async with budget.background_turn(name, lane='quote-network'):
                entered.set()
                if release is not None:
                    await release.wait()
        with patch.object(budget, 'host_pressure', return_value={'limited': True, 'reasons': ['low-memory']}):
            first = self.task(quote('first', first_entered, first_release))
            await asyncio.sleep(.025)
            self.assertFalse(first_entered.is_set())
        await asyncio.wait_for(first_entered.wait(), 1)
        second = self.task(quote('second', second_entered))
        await asyncio.sleep(.025)
        self.assertFalse(second_entered.is_set())
        first_release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertTrue(second_entered.is_set())

    async def test_pool_network_turn_advances_while_research_permit_is_held(self):
        async with budget.background_turn('long-research'):
            entered = asyncio.Event()
            async def network():
                # A new task inherits the parent lease, so start from a clean
                # context as independently scheduled collectors do.
                async with budget.background_turn('bounded-pool-page', lane='pool-network'):
                    entered.set()
            task = asyncio.create_task(network(), context=contextvars.Context())
            self.tasks.append(task)
            await asyncio.wait_for(entered.wait(), 1)
            await task

    async def test_independent_pool_lane_still_waits_for_host_pressure(self):
        entered, waiting = asyncio.Event(), asyncio.Event()
        async def network():
            async with budget.background_turn('bounded-pool-page', lane='pool-network',
                    on_wait=lambda state: waiting.set()):
                entered.set()
        with patch.object(budget, 'host_pressure', return_value={'limited': True, 'reasons': ['low-memory']}):
            task=self.task(network())
            await asyncio.wait_for(waiting.wait(), 1)
            self.assertFalse(entered.is_set())
        await asyncio.wait_for(task, 1)
        self.assertTrue(entered.is_set())

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.processes = []
        self.tasks = []
        self.patches = [
            patch.dict(os.environ, {'BACKGROUND_BUDGET_PATH': str(self.root / 'budget'),
                                    'NODE_ENV': 'test'}),
            patch.object(budget, 'POLL_SECONDS', .005),
            patch.object(budget, 'host_pressure', return_value={'limited': False, 'reasons': []}),
        ]
        for item in self.patches:
            item.start()
        scheduler.TASKS.clear()

    async def asyncTearDown(self):
        for child in self.processes:
            if child.poll() is None:
                child.kill()
            await asyncio.to_thread(child.wait, timeout=3)
            child.stderr.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await scheduler.shutdown_loops()
        scheduler.TASKS.clear()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def task(self, action):
        task = asyncio.create_task(action)
        self.tasks.append(task)
        return task

    def child(self, seconds, *, name='other-process', priority=False, event_log=None, lane='research'):
        ident = str(len(self.processes))
        waiting, entered = self.root / ('waiting-' + ident), self.root / ('entered-' + ident)
        child = subprocess.Popen([sys.executable, '-c', CHILD,
                                  str(self.root / 'budget'), str(waiting), str(entered), str(seconds),
                                  name, '1' if priority else '0', str(event_log) if event_log else '-', lane],
                                 cwd=Path(__file__).resolve().parents[1],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(child)
        return child, waiting, entered

    async def holder(self, *, priority=False):
        entered, release = asyncio.Event(), asyncio.Event()
        async def hold():
            async with budget.background_turn('holder', priority=priority):
                entered.set()
                await release.wait()
        task = self.task(hold())
        await asyncio.wait_for(entered.wait(), 2)
        return task, release

    async def test_publication_cross_process_lane_advances_during_cold_turn_and_excludes_overlap(self):
        holder, release = await self.holder()
        first, _, first_entered = self.child(30, name='publish-first', lane='publication')
        await until(first_entered.exists)
        second, waiting, second_entered = self.child(.01, name='publish-second', lane='publication')
        await until(waiting.exists)
        self.assertFalse(second_entered.exists())
        self.assertFalse(holder.done())
        first.kill()
        await asyncio.to_thread(first.wait, timeout=3)
        self.assertEqual(await asyncio.to_thread(second.wait, timeout=3), 0)
        self.assertTrue(second_entered.exists())
        self.assertFalse(holder.done())
        release.set()
        await holder

    async def test_publication_lane_remains_pressure_gated_and_timeout_releases_permit(self):
        entered, waiting = asyncio.Event(), asyncio.Event()
        async def publish():
            async with budget.background_turn('pressure-publish', lane='publication',
                                              on_wait=lambda _: waiting.set()):
                entered.set()
        with patch.object(budget, 'host_pressure', return_value={'limited': True, 'reasons': ['low-memory']}):
            task = self.task(publish())
            await asyncio.wait_for(waiting.wait(), 1)
            self.assertFalse(entered.is_set())
        await asyncio.wait_for(task, 1)
        with self.assertRaises(budget.BackgroundTurnExpired):
            async with budget.background_turn('expired-publish', lane='publication', max_run_s=.02):
                await asyncio.sleep(1)
        async with budget.background_turn('after-timeout', lane='publication', max_run_s=1):
            pass
        self.assertEqual(list((self.root/'budget'/'lanes'/'publication'/'queue').iterdir()), [])

    async def test_real_cross_process_permit_excludes_overlap_then_releases(self):
        holder, release = await self.holder()
        child, waiting, entered = self.child(.03)
        await until(waiting.exists)
        self.assertFalse(entered.exists())
        release.set()
        await holder
        self.assertEqual(await asyncio.to_thread(child.wait, timeout=3), 0)
        self.assertTrue(entered.exists())
        self.assertEqual(list((self.root / 'budget' / 'queue').iterdir()), [])

    async def test_dead_process_does_not_keep_running_permit(self):
        child, _, entered = self.child(30)
        await until(entered.exists)
        queued, ran = asyncio.Event(), asyncio.Event()
        async def next_turn():
            async with budget.background_turn('after-crash', on_wait=lambda _: queued.set()):
                ran.set()
        task = self.task(next_turn())
        await asyncio.wait_for(queued.wait(), 2)
        self.assertFalse(ran.is_set())
        child.kill()
        await asyncio.to_thread(child.wait, timeout=3)
        await asyncio.wait_for(task, 2)
        self.assertTrue(ran.is_set())

    async def test_dead_waiter_ticket_is_cleaned_and_never_blocks_fifo(self):
        holder, release = await self.holder()
        child, waiting, entered = self.child(30)
        await until(waiting.exists)
        child.kill()
        await asyncio.to_thread(child.wait, timeout=3)
        ran = asyncio.Event()
        async def turn():
            async with budget.background_turn('live-waiter'):
                ran.set()
        task = self.task(turn())
        self.assertFalse(entered.exists())
        release.set()
        await holder
        await asyncio.wait_for(task, 2)
        self.assertTrue(ran.is_set())
        self.assertEqual(list((self.root / 'budget' / 'queue').iterdir()), [])

    async def test_fifo_and_realtime_task_continue_while_heavy_jobs_wait(self):
        holder, release = await self.holder()
        order = []
        waiting = [asyncio.Event() for _ in range(3)]
        async def turn(index):
            async with budget.background_turn(str(index), on_wait=lambda _: waiting[index].set()):
                order.append(index)
                await asyncio.sleep(.01)
        jobs = []
        for index in range(3):
            jobs.append(self.task(turn(index)))
            await asyncio.wait_for(waiting[index].wait(), 2)
        live = asyncio.Event()
        async def live_work():
            live.set()
            return {'accepted': 1}
        scheduler.spawn_loop('socket-consumer', 1, live_work)
        await asyncio.wait_for(live.wait(), .1)
        self.assertEqual(order, [])
        release.set()
        await holder
        await asyncio.wait_for(asyncio.gather(*jobs), 2)
        self.assertEqual(order, [0, 1, 2])

    async def test_cross_process_publication_passes_queue_twice_then_oldest_cold(self):
        holder, release = await self.holder()
        event_log = self.root / 'turns.log'
        async def cold(name, queued):
            async with budget.background_turn(name, on_wait=lambda _: queued.set()):
                with event_log.open('a') as log:
                    log.write(name + '\n')
                await asyncio.sleep(.015)
        queued = asyncio.Event()
        oldest = self.task(cold('cold-0', queued))
        await asyncio.wait_for(queued.wait(), 2)
        children = []
        for index in range(3):
            child, waiting, _ = self.child(.015, name=f'publish-{index}', priority=True,
                                           event_log=event_log)
            children.append(child)
            await until(waiting.exists)
        queued = asyncio.Event()
        later = self.task(cold('cold-1', queued))
        await asyncio.wait_for(queued.wait(), 2)
        release.set()
        await holder
        await asyncio.wait_for(asyncio.gather(oldest, later), 3)
        for child in children:
            self.assertEqual(await asyncio.to_thread(child.wait, timeout=3), 0)
        self.assertEqual(event_log.read_text().splitlines(),
                         ['publish-0', 'publish-1', 'cold-0', 'publish-2', 'cold-1'])
        self.assertEqual(json.loads((self.root / 'budget' / 'admission-state.json').read_text())
                         ['priorityStreak'], 0)

    async def test_priority_only_can_continue_but_new_cold_receives_next_turn(self):
        for _ in range(4):
            async with budget.background_turn('publication-only', priority=True):
                pass
        holder, release = await self.holder(priority=True)
        order = []
        async def turn(name, priority, queued):
            async with budget.background_turn(name, priority=priority, on_wait=lambda _: queued.set()):
                order.append(name)
        queued = asyncio.Event()
        publication = self.task(turn('new-publication', True, queued))
        await asyncio.wait_for(queued.wait(), 2)
        queued = asyncio.Event()
        cold = self.task(turn('new-cold', False, queued))
        await asyncio.wait_for(queued.wait(), 2)
        release.set()
        await holder
        await asyncio.wait_for(asyncio.gather(publication, cold), 2)
        self.assertEqual(order, ['new-cold', 'new-publication'])

    async def test_corrupt_priority_state_conservatively_gives_oldest_cold_turn(self):
        holder, release = await self.holder()
        order = []
        async def turn(name, priority, queued):
            async with budget.background_turn(name, priority=priority, on_wait=lambda _: queued.set()):
                order.append(name)
        queued = asyncio.Event()
        cold = self.task(turn('cold', False, queued))
        await asyncio.wait_for(queued.wait(), 2)
        queued = asyncio.Event()
        publication = self.task(turn('publication', True, queued))
        await asyncio.wait_for(queued.wait(), 2)
        (self.root / 'budget' / 'admission-state.json').write_text('{broken')
        release.set()
        await holder
        await asyncio.wait_for(asyncio.gather(cold, publication), 2)
        self.assertEqual(order, ['cold', 'publication'])

    async def test_priority_does_not_bypass_pressure_and_cleanup_still_runs(self):
        queued = asyncio.Event()
        order = []
        pressure = {'limited': True, 'reasons': ['io-stall']}
        async def publish():
            async with budget.background_turn('publication', priority=True,
                                              on_wait=lambda _: queued.set()):
                order.append('publication')
        with patch.object(budget, 'host_pressure', return_value=pressure):
            task = self.task(publish())
            await asyncio.wait_for(queued.wait(), 2)
            async with budget.background_turn('cleanup', maintenance=True):
                order.append('cleanup')
            self.assertEqual(order, ['cleanup'])
            pressure.update(limited=False, reasons=[])
            await asyncio.wait_for(task, 2)
        self.assertEqual(order, ['cleanup', 'publication'])

    async def test_maintenance_bypasses_pressure_but_not_held_permit(self):
        holder, release = await self.holder()
        pressure = {'limited': True, 'reasons': ['low-memory']}
        order, reports = [], []
        queued = asyncio.Event()
        async def turn(name, maintenance=False):
            async with budget.background_turn(name, maintenance=maintenance,
                    on_wait=lambda report: (reports.append((name, report)), queued.set())):
                order.append(name)
        with patch.object(budget, 'host_pressure', return_value=pressure):
            cold = self.task(turn('cold'))
            await asyncio.wait_for(queued.wait(), 2)
            queued.clear()
            cleanup = self.task(turn('cleanup', True))
            await asyncio.wait_for(queued.wait(), 2)
            self.assertEqual(order, [])
            release.set()
            await holder
            await asyncio.wait_for(cleanup, 2)
            self.assertEqual(order, ['cleanup'])
            self.assertFalse(cold.done())
            self.assertTrue(any(name == 'cold' and report['reason'] == 'resource-pressure'
                                for name, report in reports))
            pressure.update(limited=False, reasons=[])
            await asyncio.wait_for(cold, 2)
        self.assertEqual(order, ['cleanup', 'cold'])

    async def test_cancellation_waiting_or_running_releases_tickets_and_permit(self):
        holder, release = await self.holder()
        queued = asyncio.Event()
        async def wait():
            async with budget.background_turn('cancel-wait', on_wait=lambda _: queued.set()):
                self.fail('a cancelled waiter must never enter')
        waiter = self.task(wait())
        await asyncio.wait_for(queued.wait(), 2)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        holder.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await holder
        async with budget.background_turn('after-cancel'):
            pass
        self.assertEqual(list((self.root / 'budget' / 'queue').iterdir()), [])

    async def test_cancellation_during_threaded_admission_waits_then_releases(self):
        entered, release = threading.Event(), threading.Event()
        original = budget._Lease.try_admit
        def pause_admission(lease):
            if lease.name == 'slow-admission':
                entered.set()
                release.wait(2)
            return original(lease)
        async def work():
            async with budget.background_turn('slow-admission'):
                self.fail('cancelled admission must not execute the action')
        try:
            with patch.object(budget._Lease, 'try_admit', pause_admission):
                task = self.task(work())
                await until(entered.is_set)
                task.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            async with budget.background_turn('after-admission-cancel'):
                pass
        finally:
            release.set()

    async def test_time_budget_preserves_permit_until_rpc_thread_really_finishes(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def rpc():
            entered.set()
            release.wait(2)
            finished.set()
        async def work():
            async with budget.background_turn('slow-rpc', max_run_s=.02):
                await budget.run_background_io(rpc)
        task = self.task(work())
        ran, queued = asyncio.Event(), asyncio.Event()
        async def after():
            async with budget.background_turn('after-rpc', on_wait=lambda _: queued.set()):
                self.assertTrue(finished.is_set())
                ran.set()
        try:
            await until(entered.is_set)
            other = self.task(after())
            await asyncio.wait_for(queued.wait(), 2)
            await asyncio.sleep(.04)
            self.assertFalse(task.done())
            self.assertFalse(ran.is_set())
            release.set()
            with self.assertRaises(budget.BackgroundTurnExpired):
                await task
            await asyncio.wait_for(other, 2)
        finally:
            release.set()

    async def test_rpc_inner_timeout_waits_for_thread_and_copies_chain_context(self):
        chain = contextvars.ContextVar('test_chain', default='196')
        handle = chain.set('56')
        self.assertEqual(await budget.run_background_io(chain.get), '56')
        chain.reset(handle)
        completed = threading.Event()
        def rpc():
            time.sleep(.05)
            completed.set()
        started = time.monotonic()
        async with budget.background_turn('transport-timeout'):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(budget.run_background_io(rpc), .01)
            self.assertTrue(completed.is_set())
        self.assertGreaterEqual(time.monotonic() - started, .045)

    async def test_failed_gather_waits_for_orphan_thread_before_permit_release(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def rpc():
            entered.set()
            release.wait(2)
            finished.set()
        async def failing_sibling():
            await until(entered.is_set)
            raise ValueError('one RPC failed')
        async def work():
            async with budget.background_turn('gather-failed'):
                await asyncio.gather(budget.run_background_io(rpc), failing_sibling())
        task = self.task(work())
        try:
            await until(entered.is_set)
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaisesRegex(ValueError, 'one RPC failed'):
                await task
            self.assertTrue(finished.is_set())
            async with budget.background_turn('after-gather'):
                pass
        finally:
            release.set()

    async def test_scheduler_slow_round_has_full_gap_after_completion(self):
        starts, ends = [], []
        twice = asyncio.Event()
        async def work():
            starts.append(time.monotonic())
            await asyncio.sleep(.06)
            ends.append(time.monotonic())
            if len(ends) == 2:
                twice.set()
            return {'accepted': 1}
        scheduler.spawn_loop('slow', .04, work, heavy=True)
        await asyncio.wait_for(twice.wait(), 2)
        self.assertGreaterEqual(starts[1] - ends[0], .035)

    async def test_scheduler_timeout_is_reported_and_next_round_keeps_running(self):
        calls = 0
        completed = asyncio.Event()
        async def work():
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(1)
            completed.set()
            return {'accepted': 1}
        scheduler.spawn_loop('bounded', .05, work, heavy=True, max_run_s=.02)
        await until(lambda: scheduler.TASKS.get('bounded', {}).get('outcome') == 'time-budget-expired')
        self.assertEqual(scheduler.TASKS['bounded']['status'], 'error')
        await asyncio.wait_for(completed.wait(), 2)
        self.assertEqual(calls, 2)
        await until(lambda: scheduler.TASKS['bounded'].get('outcome') == 'data-accepted')
        self.assertEqual(scheduler.TASKS['bounded']['outcome'], 'data-accepted')

    async def test_scheduler_cancellation_during_initial_delay_reports_stopped(self):
        task = scheduler.spawn_loop('delayed', .05, AsyncMock(), initial_delay_s=1)
        await until(lambda: scheduler.TASKS.get('delayed', {}).get('status') == 'scheduled')
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(scheduler.TASKS['delayed']['status'], 'stopped')


class PressureAndHistoryTests(unittest.TestCase):
    def test_available_linux_pressure_metrics_and_disk_space_are_independent(self):
        text = 'MemTotal: 8388608 kB\nMemAvailable: 1048576 kB\n'
        with patch.object(Path, 'read_text', return_value=text), \
                patch.object(budget, '_psi', side_effect=[0.0, 12.0]), \
                patch.object(budget.shutil, 'disk_usage', return_value=
                             type('Disk', (), {'total': 100 * 1024**3, 'free': 20 * 1024**3})()):
            pressure = budget._read_pressure(Path('.'))
        self.assertEqual(pressure['reasons'], ['io-stall'])
        self.assertEqual(pressure['availableMemoryBytes'], 1024**3)

    def test_missing_linux_metrics_do_not_invent_memory_or_io_pressure(self):
        with patch.object(Path, 'read_text', side_effect=FileNotFoundError), \
                patch.object(budget.shutil, 'disk_usage', return_value=
                             type('Disk', (), {'total': 100 * 1024**3, 'free': 20 * 1024**3})()):
            pressure = budget._read_pressure(Path('.'))
        self.assertFalse(pressure['limited'])
        self.assertNotIn('memoryPsiAvg10', pressure)

    def test_history_refresh_merges_duplicate_entry_and_price_only_fallback(self):
        now = [0.0]
        snapshots = AsyncMock(side_effect=[
            {'accepted': 2, 'updated': 2, 'sparklinesRefreshed': True},
            {'accepted': 0, 'updated': 0},
            {'accepted': 0, 'updated': 0},
            {'accepted': 1, 'updated': 1, 'sparklinesRefreshed': True},
        ])
        sparklines = AsyncMock(return_value={'accepted': 1, 'updated': 1, 'sparklinesRefreshed': True})
        turn = worker.combined_market_history_round(snapshots, sparklines)
        with patch.object(worker.time, 'monotonic', side_effect=lambda: now[0]):
            self.assertEqual(asyncio.run(turn())['accepted'], 2)
            now[0] = 299
            self.assertEqual(asyncio.run(turn())['accepted'], 0)
            sparklines.assert_not_awaited()
            now[0] = 300
            self.assertEqual(asyncio.run(turn())['accepted'], 1)
            sparklines.assert_awaited_once()
            now[0] = 360
            asyncio.run(turn())
            sparklines.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
