"""Single-owner local projections, isolated from provider ingestion's event loop."""
import asyncio
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .config import bounded_env_int, load_env


def configure_process_priority():
    """Yield CPU/I/O to market workers; optional OS controls never stop work."""
    result = {'cpu': 'unavailable', 'io': 'unavailable'}
    target = bounded_env_int('PROCESS_NICE', 10, 0, 19)
    try:
        current = os.getpriority(os.PRIO_PROCESS, 0)
        desired = max(current, target)
        if desired > current:
            os.setpriority(os.PRIO_PROCESS, 0, desired)
        result['cpu'] = desired
    except (AttributeError, OSError):
        pass

    tool = shutil.which('ionice') if sys.platform.startswith('linux') else None
    if tool:
        try:
            environment = {**os.environ, 'LC_ALL': 'C'}
            current = subprocess.run([tool, '-p', str(os.getpid())],
                                     capture_output=True, text=True, timeout=2,
                                     env=environment)
            value = current.stdout.strip().lower()
            target = bounded_env_int('PROCESS_IO_NICE', 7, 0, 7)
            if current.returncode == 0 and value.startswith('idle'):
                # Idle is already below all best-effort priorities.
                result['io'] = 'idle'
            elif current.returncode == 0 and value.startswith(('none', 'best-effort', 'realtime')):
                existing = re.search(r'prio(?:rity)?\s*[:=]?\s*(\d+)', value)
                if value.startswith('best-effort'):
                    # Without a recognized value we cannot prove this change
                    # only lowers priority, so leave the current policy alone.
                    if existing is None:
                        return result
                    target = max(target, int(existing.group(1)))
                elif value.startswith('none'):
                    # Linux derives an unset I/O priority from CPU nice.
                    # Explicit class 2 must not accidentally raise it.
                    cpu = result['cpu']
                    inherited = min(7, max(0, (cpu + 20) // 5)) if isinstance(cpu, int) else 7
                    target = max(target, inherited)
                changed = subprocess.run(
                    [tool, '-c', '2', '-n', str(target), '-p', str(os.getpid())],
                    capture_output=True, text=True, timeout=2, env=environment)
                if changed.returncode == 0:
                    result['io'] = f'best-effort:{target}'
        except (OSError, subprocess.SubprocessError):
            pass
    return result


def cooldown_job(action, seconds):
    """Do not rerun a long batch immediately; all durable work stays pending."""
    ready_at = 0.0

    async def turn():
        nonlocal ready_at
        if time.monotonic() < ready_at:
            return {'accepted': 0, 'updated': 0, 'skipped': 1}
        try:
            return await action()
        finally:
            # Set only after the action's transaction has finished/rolled back.
            # This wrapper never owns a database lock or advances a cursor.
            ready_at = time.monotonic() + seconds

    return turn


@contextmanager
def projection_owner(path="data/pyradar-projection.lock"):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+") as owner:
        # An accidental second process waits before starting any projection job.
        fcntl.flock(owner, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(owner, fcntl.LOCK_UN)


def start_projection_loops():
    from .realtime_projection import projection_tick, refresh_derived, prune_consumed_outbox
    from .comparison_service import refresh_comparisons
    from .collectors.baskets import refresh_baskets
    from .collectors.scheduler import TASKS, spawn_loop

    cooldown = bounded_env_int('PROJECTION_COOLDOWN_SECONDS', 10, 0, 300)
    publication_gap = bounded_env_int('PROJECTION_MIN_PUBLISH_GAP_SECONDS', max(5, cooldown), 5, 300)

    async def publish_projection():
        outcome = await projection_tick()
        TASKS.setdefault('realtimeProjection', {}).update(
            projectionDurationMs=outcome.get('durationMs'),
            projectionStagesMs=outcome.get('stagesMs'),
            projectionRevision=outcome.get('revision'),
            projectionInputCursor=outcome.get('inputCursor'))
        return {"accepted": 1, "updated": int(outcome.get("changed", False))}

    async def derive_projection():
        outcome = await refresh_derived()
        return {"accepted": int(bool(outcome.get("affected"))),
                "updated": outcome.get("affected", 0),
                "skipped": int(not outcome.get("affected"))}

    # Local facts only. This process owns no upstream subscriptions or polling.
    # The priority queue cannot preempt a running 180s history turn. Give the
    # single publication owner its own pressure-gated, time-bounded permit.
    # Keep a minimum gap after completion: a complete legacy event can be tens
    # of MiB and also creates mirror/retention work. This removes cold queue
    # delay without turning every incoming quote into a catalogue rewrite.
    spawn_loop("realtimeProjection", 2, cooldown_job(publish_projection, publication_gap),
               heavy=True, priority=True, budget_lane='publication',
               max_run_s=bounded_env_int('PROJECTION_MAX_TURN_SECONDS', 90, 30, 180))
    # One short batch at a time. This independent maintenance must neither
    # block publication on failure nor queue behind full-catalogue rebuilds.
    spawn_loop("outboxMaintenance", 2, prune_consumed_outbox, 1)
    # Derived basket/comparison rebuilds read the full dashboard and can take
    # tens of seconds. A one-second cadence kept them running continuously and
    # competing with live trade persistence; direct trade/candle SSE is separate.
    spawn_loop("realtimeDerived", 30, cooldown_job(derive_projection, cooldown), 2, heavy=True)
    # Incremental comparison updates run with each dirty batch. The full
    # catalogue pass still calibrates expiry, but need not scan every 30s.
    spawn_loop("comparisons", 120, cooldown_job(refresh_comparisons, cooldown), 22, heavy=True)
    spawn_loop("baskets", 60, cooldown_job(refresh_baskets, cooldown), 55, heavy=True)


async def run():
    load_env()
    print('[projection] process priority', configure_process_priority(), flush=True)
    os.environ.setdefault("WORKER_HEALTH_PATH", "data/projection-health.json")
    with projection_owner():
        from .db import store, close_all
        from .collectors.scheduler import shutdown_loops
        from .stream_hub import flush_legacy

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        try:
            for chain in ("196", "56", "4663", "system", "ai"):
                await store(chain)
            start_projection_loops()
            await stop.wait()
        finally:
            await shutdown_loops()
            await flush_legacy()
            await close_all()


if __name__ == "__main__":
    asyncio.run(run())
