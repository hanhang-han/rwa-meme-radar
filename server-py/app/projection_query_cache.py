"""Bounded query work and one immutable publication per cached view.

Only in-flight work is merged: each later reader still verifies a committed
database stamp. Cancellation removes a waiter, not another reader's work.
"""
import asyncio
import hashlib
import sys
from contextlib import asynccontextmanager
from contextvars import ContextVar

_admitted = ContextVar('projection_query_admitted', default=None)


class QueryBusy(RuntimeError):
    pass


class SnapshotPayload(dict):
    """Publication identity stays outside JSON fields returned to clients."""

    def __init__(self, payload, stamp):
        super().__init__(payload)
        self.publication_stamp = stamp


class SnapshotBody(bytes):
    """Reusable JSON bytes and a lazy graph; json.loads accepts both old/new."""

    def __new__(cls, text, stamp):
        wire = text.encode('utf-8') if isinstance(text, str) else bytes(text)
        value = super().__new__(cls, wire)
        value.stamp = stamp
        # HTTP responses retain only plain bytes, not the parsed graph and
        # indexes attached to this carrier while a slow client is downloading.
        value.wire = wire
        value.etag = '"' + hashlib.sha1(value.wire).hexdigest()[:20] + '"'
        value.serialized_bytes = sys.getsizeof(value) + len(wire)
        value.payload = None
        value.parse_task = None
        value.token_index = None
        value.index_task = None
        return value

class QueryWork:
    def __init__(self, *, connections=2, workers=2, readers=64, jobs=32):
        self.loop = asyncio.get_running_loop()
        self.read_slots = asyncio.Semaphore(connections)
        self.work_slots = asyncio.Semaphore(workers)
        # Publication validation and the small feed graph must not queue
        # behind full/market graph work. All lanes still share one job budget.
        self.worker_limits = {'default': workers, 'manifest': 1, 'feed': 1}
        self.lane_slots = {'default': self.work_slots,
                           'manifest': asyncio.Semaphore(1),
                           'feed': asyncio.Semaphore(1)}
        self.reader_limit = readers
        self.job_limit = jobs
        self.waiters = 0
        self.inflight = {}
        self.jobs = set()
        self.background = set()
        self.closed = False
        self.stats = {'reads': 0, 'mergedReads': 0, 'cacheHits': 0,
                      'decoded': 0, 'parsed': 0, 'indexed': 0, 'evicted': 0,
                      'busy': 0, 'activeConnections': 0, 'peakConnections': 0,
                      'activeWorkers': 0, 'peakWorkers': 0,
                      'activeManifestWorkers': 0, 'peakManifestWorkers': 0,
                      'activeFeedWorkers': 0, 'peakFeedWorkers': 0,
                      'activeTotalWorkers': 0, 'peakTotalWorkers': 0}

    @staticmethod
    def _observe(task):
        # A disconnected last viewer must not leave an unobserved exception.
        if not task.cancelled():
            task.exception()

    def track(self, task):
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        task.add_done_callback(self._observe)
        return task

    async def read(self, key, action):
        task = self.inflight.get(key)
        if self.closed or (task is None and len(self.inflight) >= self.reader_limit):
            self.stats['busy'] += 1
            raise QueryBusy('projection-query-busy')
        if task is None:
            async def run():
                try:
                    return await action()
                finally:
                    self.inflight.pop(key, None)
            task = asyncio.create_task(run(), name='projection-query-read')
            self.inflight[key] = task
            task.add_done_callback(self._observe)
            self.stats['reads'] += 1
        else:
            self.stats['mergedReads'] += 1
        return await asyncio.shield(task)

    @asynccontextmanager
    async def admit(self):
        if _admitted.get() is self:
            yield
            return
        if self.closed or self.waiters >= self.reader_limit:
            self.stats['busy'] += 1
            raise QueryBusy('projection-query-busy')
        self.waiters += 1
        token = _admitted.set(self)
        try:
            yield
        finally:
            _admitted.reset(token)
            self.waiters -= 1

    async def cpu(self, function, *args, lane='default'):
        if lane not in self.lane_slots:
            raise ValueError('unsupported-projection-query-lane')
        if self.closed or len(self.jobs) >= self.job_limit:
            self.stats['busy'] += 1
            raise QueryBusy('projection-query-busy')
        async def run():
            async with self.lane_slots[lane]:
                active, peak = {'default': ('activeWorkers', 'peakWorkers'),
                                'manifest': ('activeManifestWorkers', 'peakManifestWorkers'),
                                'feed': ('activeFeedWorkers', 'peakFeedWorkers')}[lane]
                self.stats[active] += 1
                self.stats[peak] = max(self.stats[peak], self.stats[active])
                self.stats['activeTotalWorkers'] += 1
                self.stats['peakTotalWorkers'] = max(self.stats['peakTotalWorkers'], self.stats['activeTotalWorkers'])
                try:
                    # The producer, not its cancelable client, owns the slot.
                    return await asyncio.to_thread(function, *args)
                finally:
                    self.stats[active] -= 1
                    self.stats['activeTotalWorkers'] -= 1
        task = asyncio.create_task(run(), name='projection-query-work')
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)
        task.add_done_callback(self._observe)
        return await asyncio.shield(task)

    async def close(self):
        self.closed = True
        # Threaded operations cannot be canceled safely. Drain producers before
        # releasing their slots; query functions own no permanent DB handle.
        tasks = set(self.inflight.values()) | self.jobs | self.background
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
