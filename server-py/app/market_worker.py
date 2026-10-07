"""Single owner of the small native-market journal; owns no research writers."""
import asyncio
import fcntl
import json
import os
import signal
import time
from pathlib import Path

from .config import load_env


async def run():
    load_env()
    from .live_market_store import configure_demand_leases, close_all, market_db_path
    from .collectors.live_market import HotMarketStream
    from .collectors.pool_market import PoolMarketCollector
    configure_demand_leases()
    Path('data').mkdir(parents=True, exist_ok=True)
    owner = open('data/pyradar-market.lock', 'a+')
    fcntl.flock(owner, fcntl.LOCK_EX)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    streams = [HotMarketStream(chain) for chain in ('196', '56', '4663')]
    pool_market = PoolMarketCollector()
    health_path = Path(os.environ.get('MARKET_WORKER_HEALTH_PATH', 'data/market-worker-health.json'))

    async def supervise(stream):
        while True:
            try:
                await stream.run()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print('[live-market-' + stream.chain + '] retry ' + type(error).__name__, flush=True)
                await asyncio.sleep(5)

    async def telemetry():
        while True:
            # Retention and demand refresh remain alive even when a chain's
            # provider is disconnected. X Layer owns the shared event tape,
            # so its network availability must not determine disk growth.
            for stream in streams:
                if stream.initialized:
                    stream.schedule_housekeeping()
            body = {'pid': os.getpid(), 'updatedAt': int(time.time()*1000), 'mode': 'isolated-native-markets',
                    'ready': all(stream.initialized and stream.catalogue_ready for stream in streams),
                    'poolMarket': pool_market.health,
                    'chains': {stream.chain: {'status': stream.status, 'poolCount': len(stream.pools),
                        'queueDepth': stream.queue.qsize(), 'processed': stream.processed,
                        'lastReceivedAt': stream.last_received, 'lastProcessedAt': stream.last_event,
                        'lastSourceEventAt': stream.last_source_event, 'catalogueAt': stream.last_catalogue,
                        'catalogueError': stream.catalogue_error,
                        'recentScan': stream.recent_scan_metrics} for stream in streams}}
            health_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = health_path.with_suffix('.tmp')
            await asyncio.to_thread(temporary.write_text, json.dumps(body, separators=(',', ':')))
            os.replace(temporary, health_path)
            await asyncio.sleep(5)

    tasks = [asyncio.create_task(supervise(stream), name='live-market-' + stream.chain) for stream in streams]
    tasks.append(asyncio.create_task(telemetry(), name='live-market-health'))
    tasks.append(asyncio.create_task(pool_market.run(), name='direct-pool-market'))
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(stream.close() for stream in streams))
        await close_all()
        fcntl.flock(owner, fcntl.LOCK_UN)
        owner.close()


if __name__ == '__main__':
    asyncio.run(run())
