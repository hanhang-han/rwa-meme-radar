"""One same-host admission queue for cold collectors and local projections.

The permit protects resource use, not a database transaction. Live socket
receivers and the independently owned hot-market stream never enter it. File
locks survive neither cancellation nor process exit; locked queue tickets
provide FIFO order without treating an old PID file as proof of ownership.
"""
from __future__ import annotations

import asyncio
import contextvars
import fcntl
import json
import math
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

POLL_SECONDS = 1.0
PRESSURE_CACHE_SECONDS = 5.0
MAX_PRIORITY_STREAK = 2
_pressure_cache: dict[str, tuple[float, dict]] = {}
_inside_turn = contextvars.ContextVar('inside_background_turn', default=None)


class BackgroundTurnExpired(TimeoutError):
    """The admitted turn reached its wall-clock budget, not its queue wait."""


def turn_seconds(override=None):
    if override is None:
        return _limit('BACKGROUND_MAX_TURN_SECONDS', 180, 30, 600)
    value = float(override)
    if not math.isfinite(value) or value <= 0 or value > 600:
        raise ValueError('background turn limit must be positive and at most 600 seconds')
    return value


def _limit(name, fallback, minimum, maximum):
    try:
        value = float(os.environ.get(name, fallback))
    except (TypeError, ValueError):
        return fallback
    return min(maximum, max(minimum, value)) if math.isfinite(value) else fallback


def _psi(path, scope):
    try:
        for line in Path(path).read_text().splitlines():
            fields = line.split()
            if fields and fields[0] == scope:
                values = dict(field.split('=', 1) for field in fields[1:])
                return float(values['avg10'])
    except (OSError, ValueError, KeyError):
        pass
    return None


def _read_pressure(root: Path):
    """Use available OS evidence; a missing Linux metric is not pressure."""
    evidence = {}
    reasons = []
    try:
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            memory[key] = int(value.split()[0]) * 1024
        total, available = memory.get('MemTotal'), memory.get('MemAvailable')
        if total and available is not None:
            evidence['availableMemoryBytes'] = available
            evidence['availableMemoryRatio'] = round(available / total, 4)
            minimum = _limit('BACKGROUND_MIN_AVAILABLE_MB', 256, 0, 65536) * 1024**2
            ratio = _limit('BACKGROUND_MIN_AVAILABLE_RATIO', .1, 0, .5)
            if available < max(minimum, total * ratio):
                reasons.append('low-memory')
    except (OSError, ValueError, IndexError):
        pass
    for key, path, scope, setting, default in (
            ('memoryPsiAvg10', '/proc/pressure/memory', 'some', 'BACKGROUND_MEMORY_PSI_LIMIT', 5),
            ('ioPsiAvg10', '/proc/pressure/io', 'full', 'BACKGROUND_IO_PSI_LIMIT', 10)):
        value = _psi(path, scope)
        if value is not None:
            evidence[key] = value
            if value >= _limit(setting, default, .1, 100):
                reasons.append('memory-stall' if key == 'memoryPsiAvg10' else 'io-stall')
    try:
        disk = shutil.disk_usage(root)
        evidence['diskFreeBytes'] = disk.free
        evidence['diskFreeRatio'] = round(disk.free / disk.total, 4)
        floor = _limit('BACKGROUND_MIN_FREE_DISK_MB', 256, 0, 65536) * 1024**2
        ratio = _limit('BACKGROUND_MIN_FREE_DISK_RATIO', .02, 0, .5)
        if disk.free < max(floor, disk.total * ratio):
            reasons.append('low-disk-space')
    except OSError:
        pass
    return {'limited': bool(reasons), 'reasons': reasons, **evidence}


def host_pressure(root: Path):
    key = str(root)
    now = time.monotonic()
    previous = _pressure_cache.get(key)
    if previous is None or now - previous[0] >= PRESSURE_CACHE_SECONDS:
        previous = (now, _read_pressure(root))
        _pressure_cache[key] = previous
    return dict(previous[1])


async def _finish_io(operation, owner=None):
    """Finish a short local I/O operation before honoring cancellation.

    Cancelling to_thread cannot stop its thread. Waiting for that thread is
    essential: otherwise it can acquire a permit after the caller's finally
    already closed its old descriptors, leaking the permit until process exit.
    """
    # An executor Future is not an asyncio Task, so application shutdown's
    # cancellation of all tasks cannot cancel it separately behind our shield.
    pending = asyncio.get_running_loop().run_in_executor(None, operation)
    if owner is not None:
        owner.pending_io.add(pending)
        pending.add_done_callback(owner.pending_io.discard)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(pending)
            break
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def run_background_io(operation, *args, **kwargs):
    """Run a non-cancellable thread and wait for it before propagating cancel.

    Web3's blocking RPC calls still have their own transport timeout. Unlike
    bare to_thread, cancellation of an enclosing cold turn cannot leave that
    thread running after the shared permit was released. Copy ContextVars so
    chain selection and provider lanes retain their original meaning.
    """
    context = contextvars.copy_context()
    call = partial(operation, *args, **kwargs)
    owner = _inside_turn.get()
    if owner is not None and owner.closing:
        # A sibling left behind by gather must not launch a new thread after
        # its admitted parent already failed or exhausted its time budget.
        raise asyncio.CancelledError
    return await _finish_io(partial(context.run, call), owner)


async def _settle_pending_io(lease):
    cancelled = False
    while lease.pending_io:
        pending = asyncio.gather(*list(lease.pending_io), return_exceptions=True)
        while True:
            try:
                await asyncio.shield(pending)
                break
            except asyncio.CancelledError:
                cancelled = True
    if cancelled:
        raise asyncio.CancelledError


class _Lease:
    def __init__(self, root, name, maintenance, priority=False):
        self.root = root
        self.name = name
        self.maintenance = maintenance
        self.priority = priority
        self.queued_at = int(time.time() * 1000)
        self.began = time.monotonic()
        self.ticket_fd = None
        self.permit_fd = None
        self.ticket_path = None
        self.pending_path = None
        self.admitted = False
        self.pending_io = set()
        self.closing = False

    def register(self):
        self.root.mkdir(parents=True, exist_ok=True)
        queue = self.root / 'queue'
        queue.mkdir(exist_ok=True)
        ident = f'{time.monotonic_ns():020d}-{uuid.uuid4().hex}'
        self.pending_path = queue / ('.pending-' + ident)
        self.ticket_fd = os.open(self.pending_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        fcntl.flock(self.ticket_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        body = json.dumps({'name': self.name, 'maintenance': self.maintenance, 'priority': self.priority,
                           'queuedAt': self.queued_at, 'pid': os.getpid()}).encode()
        with os.fdopen(os.dup(self.ticket_fd), 'wb') as target:
            target.write(body)
        self.ticket_path = queue / (ident + '.json')
        # The queue only sees a complete ticket whose owner already holds its
        # lock. A competing process cannot clean up a half-published request.
        os.replace(self.pending_path, self.ticket_path)
        self.pending_path = None
        self.permit_fd = os.open(self.root / 'permit.lock', os.O_CREAT | os.O_RDWR, 0o600)

    def _priority_streak(self):
        try:
            payload = json.loads((self.root / 'admission-state.json').read_text())
            value = payload.get('priorityStreak') if isinstance(payload, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_PRIORITY_STREAK:
                return value
        except FileNotFoundError:
            return 0
        except (ValueError, TypeError):
            pass
        # Corrupt state conservatively gives cold work the next eligible turn.
        return MAX_PRIORITY_STREAK

    def _record_admission(self, previous):
        streak = min(MAX_PRIORITY_STREAK, previous + 1) if self.priority else 0
        if streak == previous:
            return
        target = self.root / 'admission-state.json'
        temporary = target.with_name('.admission-state-' + uuid.uuid4().hex)
        try:
            temporary.write_text(json.dumps({'priorityStreak': streak}, separators=(',', ':')))
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _head(self, limited, streak):
        queue = self.root / 'queue'
        priority_head, cold_head = None, None
        for path in sorted(queue.iterdir(), key=lambda path: path.name):
            if not (path.name.endswith('.json') or path.name.startswith('.pending-')):
                continue
            if path == self.ticket_path:
                payload = {'maintenance': self.maintenance, 'priority': self.priority}
                if not limited or self.maintenance:
                    if self.priority and priority_head is None:
                        priority_head = path
                    elif not self.priority and cold_head is None:
                        cold_head = path
                continue
            try:
                descriptor = os.open(path, os.O_RDONLY)
            except FileNotFoundError:
                continue
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if path.name.startswith('.pending-'):
                        continue
                    with os.fdopen(os.dup(descriptor), 'rb') as source:
                        payload = json.load(source)
                    if limited and not payload.get('maintenance'):
                        continue
                    if payload.get('priority') is True:
                        if priority_head is None:
                            priority_head = path
                    elif cold_head is None:
                        cold_head = path
                else:
                    # A kernel-released ticket cannot still belong to a live
                    # waiter. Crashed processes therefore never block the queue.
                    path.unlink(missing_ok=True)
            finally:
                os.close(descriptor)
        # Publication passes cold work twice at most. The oldest eligible cold
        # ticket then owns the next turn; each class preserves FIFO internally.
        if priority_head is not None and (cold_head is None or streak < MAX_PRIORITY_STREAK):
            return priority_head
        return cold_head or priority_head

    def try_admit(self):
        pressure = host_pressure(self.root)
        if pressure['limited'] and not self.maintenance:
            return False, pressure, 'resource-pressure'
        try:
            fcntl.flock(self.permit_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False, pressure, 'background-budget'
        try:
            # Both selection and the streak update run under the shared permit
            # lock. Different processes cannot each claim a fresh priority run.
            streak = self._priority_streak()
            if self._head(pressure['limited'], streak) != self.ticket_path:
                return False, pressure, 'background-budget'
            self._record_admission(streak)
            # Running work is no longer queued. The separate permit stays
            # locked through the action and prevents another job from starting.
            self.ticket_path.unlink(missing_ok=True)
            self.admitted = True
            return True, pressure, None
        finally:
            if not self.admitted:
                fcntl.flock(self.permit_fd, fcntl.LOCK_UN)

    def close(self):
        try:
            for path in (self.pending_path, self.ticket_path):
                if path is not None:
                    path.unlink(missing_ok=True)
        finally:
            # Closing releases flock too, including cancellation during
            # register/admission. Keep the permit until the action has ended.
            for attribute in ('ticket_fd', 'permit_fd'):
                descriptor = getattr(self, attribute)
                if descriptor is not None:
                    os.close(descriptor)
                    setattr(self, attribute, None)


@asynccontextmanager
async def background_turn(name, *, maintenance=False, on_wait=None, max_run_s=None, priority=False, lane='research'):
    """Acquire the global one-job budget without blocking an event loop.

    Maintenance may pass cold tickets while the host is under pressure; it
    still uses the same permit. Publication-priority work can pass ordinary
    tickets twice, then must admit the oldest eligible ordinary ticket. Cold
    tickets retain their FIFO position until pressure recovers. Failure to open
    the budget fails closed. Nested callers are rejected instead of deadlocking.
    """
    if _inside_turn.get():
        raise RuntimeError('background_turn must not be nested')
    maximum = turn_seconds(max_run_s)
    root = Path(os.environ.get('BACKGROUND_BUDGET_PATH', 'data/background-budget')).absolute()
    if lane not in ('research', 'pool-network', 'quote-network', 'publication'):
        raise ValueError('unsupported-background-lane')
    if lane != 'research':
        # Bounded network collectors and the single publication owner must not
        # wait for a long research/history turn. Each lane still owns one
        # permit and retains pressure, cancellation, FIFO and time bounds.
        root = root / 'lanes' / lane
    lease = _Lease(root, name, bool(maintenance), bool(priority))
    token = None
    try:
        await _finish_io(lease.register)
        while True:
            admitted, pressure, reason = await _finish_io(lease.try_admit)
            if admitted:
                break
            if on_wait is not None:
                on_wait({'reason': reason, 'pressure': pressure, 'queuedAt': lease.queued_at,
                         'waitMs': int((time.monotonic() - lease.began) * 1000)})
            await asyncio.sleep(POLL_SECONDS)
        token = _inside_turn.set(lease)
        deadline = asyncio.timeout(maximum)
        try:
            async with deadline:
                yield {'queuedAt': lease.queued_at, 'maxRunSeconds': maximum,
                       'waitMs': int((time.monotonic() - lease.began) * 1000)}
        except TimeoutError:
            if deadline.expired():
                raise BackgroundTurnExpired(f'Background turn exceeded {maximum:g}s') from None
            raise
    finally:
        lease.closing = True
        if token is not None:
            _inside_turn.reset(token)
        try:
            # gather can return on one failing sibling while another blocking
            # RPC thread is still running. Keep the permit until it is done.
            await _settle_pending_io(lease)
        finally:
            await _finish_io(lease.close)
