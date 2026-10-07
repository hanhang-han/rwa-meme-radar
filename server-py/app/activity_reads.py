"""Small, bounded cache for the rolling detail statistics, separate from prices."""
import asyncio
import time
from collections import OrderedDict

from fastapi import HTTPException

TTL_SECONDS = 2.0
MAX_ENTRIES = 128
MAX_PENDING = 16
_cache = OrderedDict()
_pending = {}
_read_slots = asyncio.Semaphore(2)


async def read_activity(scoped, asset: str) -> dict:
    if not hasattr(scoped, 'scope'):
        # Compatibility for lightweight scoped adapters without a durable
        # database identity; do not mix them in the production cache.
        at = int(time.time() * 1000)
        return {**await scoped.activity(asset, at - 86_400_000), 'observedAt': at}
    key = (getattr(scoped, 'path', None), scoped.scope, asset)
    now = time.monotonic()
    for old_key in list(_cache):
        if _cache[old_key][0] <= now:
            del _cache[old_key]
    cached = _cache.get(key)
    if cached:
        _cache.move_to_end(key)
        return dict(cached[1])
    task = _pending.get(key)
    if task is None:
        if len(_pending) >= MAX_PENDING:
            raise HTTPException(503, 'activity-query-busy', headers={'Retry-After': '2'})

        async def load():
            try:
                async with _read_slots:
                    at = int(time.time() * 1000)
                    result = await scoped.activity(asset, at - 86_400_000)
                result = {**result, 'observedAt': at}
                _cache[key] = (time.monotonic() + TTL_SECONDS, result)
                _cache.move_to_end(key)
                while len(_cache) > MAX_ENTRIES:
                    _cache.popitem(last=False)
                return result
            finally:
                _pending.pop(key, None)

        task = asyncio.create_task(load())
        # A disconnected caller must neither cancel a shared read nor leave
        # an unobserved failure if all callers have disconnected.
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        _pending[key] = task
    return dict(await asyncio.shield(task))


async def stop_activity_reads():
    if _pending:
        await asyncio.gather(*list(_pending.values()), return_exceptions=True)
    _cache.clear()
