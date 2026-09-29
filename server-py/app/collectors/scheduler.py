"""Background collector loops with observable state and graceful shutdown."""
import asyncio
import json
import os
import time


TASKS: dict[str, dict] = {}
_running: set[asyncio.Task] = set()
DEFER_POLL_SECONDS = 5


def _save_health() -> None:
    if os.environ.get("NODE_ENV") == "test":
        return
    path = os.environ.get("WORKER_HEALTH_PATH", "data/worker-health.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = f"{path}.{os.getpid()}.tmp"
    with open(temp, "w") as handle:
        json.dump({"pid": os.getpid(), "updatedAt": int(time.time() * 1000), "tasks": TASKS}, handle)
    os.replace(temp, path)


def apply_result(status, outcome, finished_at):
    status['lastCompletedAt'] = finished_at
    if not isinstance(outcome, dict) or not any(k in outcome for k in ('requested', 'accepted', 'failed', 'quotaBlocked')):
        status.update(status='waiting', outcome='unreported')
        return
    status['result'] = {k: outcome.get(k, 0) for k in ('requested', 'processed', 'accepted', 'changed',
                                                     'observations', 'unavailable', 'updated', 'failed', 'quotaBlocked',
                                                     'unsupported', 'skipped')}
    accepted = outcome.get('accepted', 0)
    failed = outcome.get('failed', 0)
    unsupported = outcome.get('unsupported', 0)
    if accepted or outcome.get('changed'):
        status.update(status='partial' if failed or outcome.get('quotaBlocked') else 'waiting',
                      outcome='partial-data' if unsupported else 'data-accepted',
                      lastSuccessAt=finished_at, lastDataAt=finished_at)
    elif outcome.get('quotaBlocked'):
        status.update(status='quota-blocked', outcome='no-data', error='Provider budget exhausted')
    elif outcome.get('noChange') and not failed:
        skipped = int(outcome.get('skipped') or 0)
        status.update(status='partial' if skipped else 'waiting', outcome='no-change',
                      lastSuccessAt=finished_at,
                      error=f'{skipped} stock rows missing token identity' if skipped else None)
        if skipped:
            status['lastFailureAt'] = finished_at
    elif outcome.get('requested') or failed or unsupported:
        status.update(status='error', outcome='no-data', lastFailureAt=finished_at,
                      error='No valid observations accepted')
    else:
        status.update(status='idle', outcome='nothing-due')


async def _defer_for_live_backlog(status, defer_when, max_deferral_s, resume_stagger_s):
    """Give a backed-up live stream a bounded head start over background work."""
    began = None
    clear_since = None
    while True:
        now = time.monotonic()
        busy = defer_when()
        if began is None:
            if not busy:
                return False
            began = now
            status['deferredSinceAt'] = int(time.time() * 1000)
        if now - began >= max_deferral_s:
            status['lastDeferralMs'] = int((now - began) * 1000)
            status['lastDeferralExpiredAt'] = int(time.time() * 1000)
            status.pop('deferredSinceAt', None)
            return True
        if busy:
            clear_since = None
            reason = 'live-backlog'
            remaining = max_deferral_s - (now - began)
        else:
            clear_since = clear_since or now
            remaining = resume_stagger_s - (now - clear_since)
            if remaining <= 0:
                status['lastDeferralMs'] = int((now - began) * 1000)
                status.pop('deferredSinceAt', None)
                return True
            reason = 'live-recovery-stagger'
        wait = min(DEFER_POLL_SECONDS, remaining, max_deferral_s - (now - began))
        status.update(status='deferred', outcome=reason,
                      nextRunAt=int((time.time() + wait) * 1000))
        _save_health()
        await asyncio.sleep(wait)


def spawn_loop(name: str, interval_s: float, fn, initial_delay_s: float = 0, *,
               defer_when=None, max_deferral_s: float = 180,
               resume_stagger_s: float = 0):
    async def runner():
        if initial_delay_s > 0:
            TASKS[name] = {
                "status": "scheduled",
                "nextRunAt": int((time.time() + initial_delay_s) * 1000),
                "error": None,
            }
            _save_health()
            await asyncio.sleep(initial_delay_s)
        next_run = time.monotonic()
        while True:
            status = TASKS.setdefault(name, {})
            deferred = False
            if defer_when is not None:
                try:
                    deferred = await _defer_for_live_backlog(
                        status, defer_when, max_deferral_s, resume_stagger_s)
                except asyncio.CancelledError:
                    status.update(status='stopped', stoppedAt=int(time.time() * 1000))
                    _save_health()
                    raise
            started = time.time()
            status.update(status="running", startedAt=int(started * 1000), error=None)
            if deferred:
                status.pop('outcome', None)
            _save_health()
            try:
                outcome = await fn()
                apply_result(status, outcome, int(time.time() * 1000))
            except asyncio.CancelledError:
                status.update(status="stopped", stoppedAt=int(time.time() * 1000))
                raise
            except Exception as e:  # noqa: BLE001 - a collector must never die
                status.update(status="error", lastFailureAt=int(time.time() * 1000),
                              error=f"{type(e).__name__}: {str(e)[:160]}")
                print(f"[{name}] {type(e).__name__}: {e}", flush=True)
            finally:
                status["durationMs"] = int((time.time() - started) * 1000)
            # A deferred task must not immediately run a second time because
            # its original cadence elapsed while waiting for live traffic.
            next_run = max(next_run + interval_s,
                           time.monotonic() + (interval_s if deferred else 0))
            status["nextRunAt"] = int((time.time() + max(0, next_run - time.monotonic())) * 1000)
            _save_health()
            await asyncio.sleep(max(0, next_run - time.monotonic()))

    task = asyncio.create_task(runner(), name=name)
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task


async def shutdown_loops() -> None:
    tasks = list(_running)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def task_health() -> dict[str, dict]:
    return {name: dict(value) for name, value in TASKS.items()}
