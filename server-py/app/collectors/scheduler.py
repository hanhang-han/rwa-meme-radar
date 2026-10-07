"""Background collector loops with observable state and graceful shutdown."""
import asyncio
import json
import os
import time

from ..resource_budget import BackgroundTurnExpired, background_turn


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
    elif outcome.get('unavailableReason') == 'stream-missing':
        status.update(status='error', outcome='source-unavailable',
                      lastFailureAt=finished_at, error='BNB Chain live stream unavailable')
    elif (outcome.get('deferredReason') == 'live-backlog'
          and not failed and not unsupported):
        status.update(status='deferred', outcome='live-backlog', error=None)
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
               resume_stagger_s: float = 0, heavy: bool = False,
               maintenance: bool = False, max_run_s: float | None = None,
               priority: bool = False, budget_lane: str = 'research'):
    async def runner():
        status = TASKS.setdefault(name, {})
        status['budgetLane'] = budget_lane if heavy else 'direct'
        try:
            if initial_delay_s > 0:
                status.update(status="scheduled",
                              nextRunAt=int((time.time() + initial_delay_s) * 1000),
                              error=None)
                _save_health()
                await asyncio.sleep(initial_delay_s)
            while True:
                deferred = False
                started = None
                last_wait_save = 0.0
                last_wait_reason = None

                def budget_wait(details):
                    nonlocal last_wait_save, last_wait_reason
                    now = time.monotonic()
                    reason = details['reason']
                    status.update(status='deferred', outcome=reason, error=None,
                                  budgetQueuedAt=details['queuedAt'],
                                  budgetWaitMs=details['waitMs'],
                                  resourcePressure=details['pressure'])
                    # Resource admission can wait for minutes. Keep health
                    # alive without writing a file on every permit poll.
                    if reason != last_wait_reason or now - last_wait_save >= 5:
                        last_wait_save, last_wait_reason = now, reason
                        _save_health()

                async def action():
                    nonlocal started
                    started = time.monotonic()
                    status.update(status="running", startedAt=int(time.time() * 1000), error=None)
                    status.pop('budgetQueuedAt', None)
                    status.pop('budgetWaitMs', None)
                    status.pop('resourcePressure', None)
                    if deferred or last_wait_reason is not None:
                        status.pop('outcome', None)
                    _save_health()
                    return await fn()

                try:
                    if defer_when is not None:
                        deferred = await _defer_for_live_backlog(
                            status, defer_when, max_deferral_s, resume_stagger_s)
                    if heavy:
                        async with background_turn(name, maintenance=maintenance,
                                                   on_wait=budget_wait, max_run_s=max_run_s,
                                                   priority=priority, lane=budget_lane) as admission:
                            status['lastBudgetWaitMs'] = admission['waitMs']
                            status['maxRunSeconds'] = admission['maxRunSeconds']
                            outcome = await action()
                    else:
                        outcome = await action()
                    apply_result(status, outcome, int(time.time() * 1000))
                except asyncio.CancelledError:
                    raise
                except BackgroundTurnExpired as e:
                    status.update(status='error', outcome='time-budget-expired',
                                  lastFailureAt=int(time.time() * 1000), error=str(e))
                    print(f'[{name}] {e}; durable work will resume on the next turn', flush=True)
                except Exception as e:  # noqa: BLE001 - a collector must never die
                    status.update(status="error", lastFailureAt=int(time.time() * 1000),
                                  error=f"{type(e).__name__}: {str(e)[:160]}")
                    print(f"[{name}] {type(e).__name__}: {e}", flush=True)
                finally:
                    if started is not None:
                        status["durationMs"] = int((time.monotonic() - started) * 1000)
                # Cadence is a gap after completion, not a wall-clock slot to
                # catch up. Slow jobs must not rerun continuously when a turn
                # or its resource wait exceeded their nominal interval.
                next_run = time.monotonic() + interval_s
                status["nextRunAt"] = int((time.time() + interval_s) * 1000)
                _save_health()
                await asyncio.sleep(max(0, next_run - time.monotonic()))
        except asyncio.CancelledError:
            status.update(status="stopped", stoppedAt=int(time.time() * 1000))
            status.pop('nextRunAt', None)
            _save_health()
            raise

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
