"""Background collector loops: run forever, log instead of crash."""
import asyncio


def spawn_loop(name: str, interval_s: float, fn):
    async def runner():
        while True:
            try:
                await fn()
            except Exception as e:  # noqa: BLE001 - a collector must never die
                print(f"[{name}] {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(interval_s)

    return asyncio.create_task(runner(), name=name)
