"""Single-owner collector worker.

The HTTP application is deliberately not imported here: scaling or restarting
the query API must not duplicate provider calls, AI generation, or registry
transactions.
"""
import asyncio
import fcntl
import json
import os
import signal
from pathlib import Path

from .config import load_env


def record_disk_sample(usage, at_ms: int, path="data/disk-health.json"):
    """Keep a small independent resource history for the data-health page."""
    target = Path(path)
    try:
        old = json.loads(target.read_text())
        samples = old.get("samples", []) if isinstance(old, dict) else []
    except (OSError, ValueError, TypeError):
        samples = []
    valid = [row for row in samples if isinstance(row, dict) and isinstance(row.get("at"), int)
             and isinstance(row.get("freeBytes"), int) and isinstance(row.get("totalBytes"), int)]
    valid.append({"at": at_ms, "freeBytes": usage.free, "totalBytes": usage.total})
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"samples": valid[-48:]}, separators=(",", ":")))
    os.replace(temporary, target)


async def _sync_apply(collector) -> None:
    collector.apply_live()
    from .db import store
    s = await store('56')
    accepted=0
    for row in collector.state.get('tokens') or []:
        address=str(row.get('tokenContractAddress') or '').lower()
        price,at=row.get('price'),row.get('quoteAt')
        if address and price and at:
            await s.sample('binance:'+address,price,None,at,metadata={
                'provider':'Binance','venue':'exchange','scope':'exchange','currency':'USDT',
                'marketAt':at,'timeKind':'market','verified':True})
            accepted+=1
    return {'accepted':accepted,'updated':accepted}


async def run() -> None:
    os.makedirs("data", exist_ok=True)
    owner_lock = open("data/pyradar-worker.lock", "a+")
    # A second process waits here and never calls providers. PM2 is configured
    # for one instance, while this lock protects manual/accidental starts.
    fcntl.flock(owner_lock, fcntl.LOCK_EX)
    load_env()
    from . import briefing, insights
    from .collectors import binance as binance_collector
    from .collectors import binance_alpha, chain_stream
    from .collectors.factory_discovery import FactoryDiscovery
    from .collectors.live_quotes import refresh_live_quotes, refresh_base_candidates, refresh_base_stocks
    from .collectors.main_round import refresh_liquidity, refresh_main_round, refresh_discovery
    from .collectors.candles import refresh_watched_candles
    from .collectors.comparison_inputs import refresh_comparison_inputs
    from .collectors.scheduler import shutdown_loops, spawn_loop
    from .collectors.trades import refresh_hot_trades
    from .db import close_all, store, write_lock_snapshot
    from .stream_hub import flush_legacy

    # Finish additive schema work before concurrent ingestion starts. Large
    # historical indexes must not race live writers or a second connection.
    for chain in ('196','56','4663','system','ai'):
        await store(chain)
    await chain_stream.prepare_streams()
    async def writer_diagnostics():
        import json
        while True:
            await asyncio.sleep(5)
            snapshot = write_lock_snapshot()
            if snapshot['heldMs'] > 1000 or os.getenv('RESEARCH_WRITER_DIAGNOSTICS') == '1':
                print('[research-writer] ' + json.dumps(snapshot), flush=True)

    diagnostic_task = asyncio.create_task(writer_diagnostics(), name='writer-diagnostics')
    binance_collector.start_stream()
    binance_alpha.start_stream()
    chain_stream.start_streams()
    # Factory events use the same conservative X Layer RPC gate as the pool
    # stream. They are distinct from OKX's five-minute, quota-limited scans.
    xlayer_stream = chain_stream.stream_for('196')
    if xlayer_stream and os.environ.get('XLAYER_FACTORY_DISCOVERY_ENABLED', 'true').lower() != 'false':
        # Bootstrap just behind the tip so fresh pools are not held behind a
        # long historical catch-up; the durable cursor still fills every gap
        # after this point across later restarts and provider outages.
        factory = FactoryDiscovery(await store('196'), xlayer_stream.rpc, initial_lookback_blocks=160)

        async def factory_round():
            if xlayer_stream.http is None:
                return {'requested': 0, 'accepted': 0, 'skipped': 1}
            scan = await factory.run_once(max_ranges=2)
            processed = await factory.process_confirmed(limit=4)
            return {
                'requested': scan['scannedRanges'] + processed['requested'],
                'accepted': int(scan['scannedRanges'] > 0) + processed['accepted'],
                'updated': len(scan['newEvents']) + processed['updated'],
                'failed': processed['failed'], 'skipped': processed['waitingCatalogue'],
            }

        spawn_loop('factoryDiscovery', 10, factory_round, 15)
    # CPU-heavy projections run in app.projection_worker, on another event loop.
    spawn_loop("liveQuotes", 300, refresh_live_quotes)
    spawn_loop("binanceApply", 10, lambda: _sync_apply(binance_collector), 3)
    spawn_loop("baseCandidates", 300, refresh_base_candidates, 5)
    spawn_loop("baseStocks", 300, refresh_base_stocks, 15)
    spawn_loop("candles", 10, refresh_watched_candles, 10)
    spawn_loop("binance", 30, binance_collector.refresh_binance, 20)
    spawn_loop("comparisonInputs", 300, refresh_comparison_inputs, 24)
    spawn_loop("aiInsights", 15, insights.refresh_insights, 25)
    spawn_loop("aiBriefing", 60, briefing.refresh_briefing, 30)
    spawn_loop("mainRound", 300, refresh_main_round, 35)
    spawn_loop("liquidityRefresh", 30, refresh_liquidity, 45)
    spawn_loop("discovery", 300, refresh_discovery, 50)
    spawn_loop("hotTrades", 300, refresh_hot_trades, 60)

    async def maintenance():
        import shutil
        import time
        deleted=0
        for chain in ('196','56','4663'):
            s=await store(chain)
            deleted+=await s.prune_comparisons(int(time.time()*1000)-14*86400000)
            # Streams may be delisted/disconnected; retention must not depend
            # on a particular market receiving its next trade.
            for prefix in ('binance:', 'binance-alpha:'):
                deleted += await s.prune_market_trades(prefix, int(time.time()*1000)-86400000)
        usage=shutil.disk_usage('data')
        record_disk_sample(usage, int(time.time()*1000))
        return {'updated':deleted,'accepted':1,'diskFreeBytes':usage.free,
                'diskFreePercent':round(usage.free/usage.total*100,2), 'failed':0}

    spawn_loop('maintenance',3600,maintenance,120)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        diagnostic_task.cancel()
        await asyncio.gather(diagnostic_task, return_exceptions=True)
        await shutdown_loops()
        await binance_collector.stop_stream()
        await binance_alpha.stop_stream()
        await chain_stream.stop_streams()
        await flush_legacy()
        await close_all()
        fcntl.flock(owner_lock, fcntl.LOCK_UN)
        owner_lock.close()


if __name__ == "__main__":
    asyncio.run(run())
