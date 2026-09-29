"""Single-owner local projections, isolated from provider ingestion's event loop."""
import asyncio
import fcntl
import os
import signal
from contextlib import contextmanager
from pathlib import Path

from .config import load_env


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
    from .realtime_projection import projection_tick, refresh_derived
    from .comparison_service import refresh_comparisons
    from .collectors.baskets import refresh_baskets
    from .collectors.scheduler import spawn_loop

    async def publish_projection():
        outcome = await projection_tick()
        return {"accepted": 1, "updated": int(outcome.get("changed", False))}

    async def derive_projection():
        outcome = await refresh_derived()
        return {"accepted": int(bool(outcome.get("affected"))),
                "updated": outcome.get("affected", 0),
                "skipped": int(not outcome.get("affected"))}

    # Local facts only. This process owns no upstream subscriptions or polling.
    spawn_loop("realtimeProjection", 1, publish_projection)
    spawn_loop("realtimeDerived", 1, derive_projection, 2)
    spawn_loop("comparisons", 30, refresh_comparisons, 22)
    spawn_loop("baskets", 60, refresh_baskets, 55)


async def run():
    load_env()
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
