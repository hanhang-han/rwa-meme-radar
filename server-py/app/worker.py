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

from .config import bounded_env_int, load_env

LIVE_BACKLOG_QUEUE_DEPTH = 32


def live_stream_backlogged(stream_for) -> bool:
    """Use in-memory live queues; a stale persisted status can mislead a gate."""
    for chain in ('196', '56', '4663'):
        stream = stream_for(chain)
        if stream is not None and stream.queue.qsize() >= LIVE_BACKLOG_QUEUE_DEPTH:
            return True
    return False


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
    from .collectors.risk_enrichment import refresh_risk_enrichment
    from .collectors.live_quotes import refresh_live_quotes, refresh_base_candidates, refresh_base_stocks
    from .collectors.main_round import refresh_liquidity, refresh_main_round, refresh_discovery
    from .collectors.okx_catalogue import sync_okx_catalogues
    from .collectors.candles import refresh_watched_candles
    from .collectors.comparison_inputs import refresh_comparison_inputs
    from .collectors.scheduler import shutdown_loops, spawn_loop
    from .collectors.trades import refresh_hot_trades
    from .db import (close_all, prepare_quote_stores, quote_store_scope,
                     store, write_lock_snapshot)
    from .stream_hub import flush_legacy

    # Finish additive schema work before concurrent ingestion starts. Large
    # historical indexes must not race live writers or a second connection.
    for chain in ('196','56','4663','system','ai'):
        await store(chain)
    await prepare_quote_stores()
    await chain_stream.prepare_streams()
    async def writer_diagnostics():
        import json
        while True:
            await asyncio.sleep(5)
            snapshot = write_lock_snapshot()
            if (snapshot['heldMs'] > 1000 or snapshot['quote']['heldMs'] > 1000
                    or os.getenv('RESEARCH_WRITER_DIAGNOSTICS') == '1'):
                print('[research-writer] ' + json.dumps(snapshot), flush=True)

    diagnostic_task = asyncio.create_task(writer_diagnostics(), name='writer-diagnostics')
    binance_collector.start_stream()
    binance_alpha.start_stream()
    chain_stream.start_streams()
    # Factory events share each chain's conservative RPC gate with its pool
    # stream. They are distinct from OKX's five-minute, quota-limited scans.
    for factory_chain, prefix, task_name in (('196', 'XLAYER', 'factoryDiscovery'),
                                             ('56', 'BSC', 'factoryDiscoveryBsc')):
        factory_stream = chain_stream.stream_for(factory_chain)
        if not factory_stream or os.environ.get(prefix + '_FACTORY_DISCOVERY_ENABLED', 'true').lower() == 'false':
            continue
        # Bootstrap just behind the tip so fresh pools are not held behind a
        # long historical catch-up; the durable cursor still fills every gap
        # after this point across later restarts and provider outages.
        factory = FactoryDiscovery(await store(factory_chain), factory_stream.rpc, initial_lookback_blocks=160,
            confirmations=bounded_env_int(prefix + '_FACTORY_CONFIRMATIONS', 6, 1, 100))

        async def factory_round(factory=factory, factory_stream=factory_stream):
            if factory_stream.http is None:
                return {'requested': 0, 'accepted': 0, 'skipped': 1}
            scan = await factory.run_once(max_ranges=2)
            processed = await factory.process_confirmed(limit=4)
            return {
                'requested': scan['scannedRanges'] + processed['requested'],
                'accepted': int(scan['scannedRanges'] > 0) + processed['accepted'],
                'updated': len(scan['newEvents']) + processed['updated'],
                'failed': processed['failed'], 'skipped': processed['waitingCatalogue'],
            }

        spawn_loop(task_name, 10, factory_round, 15 if factory_chain == '196' else 75)
    # CPU-heavy projections run in app.projection_worker, on another event loop.
    async def quote_round(fn):
        with quote_store_scope():
            return await fn()

    spawn_loop("liveQuotes", 300, lambda: quote_round(refresh_live_quotes))
    # Baseline quotes start at +5s/+15s and can consume their brief budget
    # without racing a large catalogue recovery. New identities follow at +90s.
    spawn_loop("okxCatalogue", 300, sync_okx_catalogues, 90)
    spawn_loop("binanceApply", 10, lambda: _sync_apply(binance_collector), 3)
    spawn_loop("baseCandidates", 300, lambda: quote_round(refresh_base_candidates), 5)
    spawn_loop("baseStocks", 300, lambda: quote_round(refresh_base_stocks), 15)
    spawn_loop("candles", 10, refresh_watched_candles, 10)
    spawn_loop("binance", 30, binance_collector.refresh_binance, 20)
    spawn_loop("comparisonInputs", 300, refresh_comparison_inputs, 24)
    spawn_loop("aiInsights", 15, insights.refresh_insights, 25)
    spawn_loop("aiBriefing", 60, briefing.refresh_briefing, 30)
    # Defer long, non-live writes while subscribed logs are behind. Each
    # collector retains its normal cadence after running, and the 3-minute
    # ceiling prevents identity/discovery maintenance from starving forever.
    background_gate = {
        'defer_when': lambda: live_stream_backlogged(chain_stream.stream_for),
        'max_deferral_s': 180,
    }
    spawn_loop("mainRound", 300, refresh_main_round, 35,
               resume_stagger_s=0, **background_gate)
    spawn_loop("liquidityRefresh", 30, refresh_liquidity, 45,
               resume_stagger_s=5, **background_gate)
    spawn_loop("discovery", 300, refresh_discovery, 50,
               resume_stagger_s=10, **background_gate)
    spawn_loop("hotTrades", 300, refresh_hot_trades, 60)
    if os.environ.get('GOPLUS_ENABLED', 'true').lower() != 'false':
        spawn_loop("riskEnrichment", 300, refresh_risk_enrichment, 80,
                   resume_stagger_s=15, **background_gate)

    # Alert delivery is independent of ingestion and remains absent without
    # both bot settings; account binding still requires a private /start code.
    from . import telegram_alerts
    if telegram_alerts.configured():
        spawn_loop('telegramAlerts', 5, telegram_alerts.tick, 5)

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

    spawn_loop('maintenance',3600,maintenance,120,
               resume_stagger_s=15, **background_gate)

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
