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
import time
from pathlib import Path

from .config import bounded_env_int, load_env

LIVE_BACKLOG_QUEUE_DEPTH = 32
_confirmation_chain_cursor = 0


def confirmation_backlog_probe_due(task):
    """Allow a trickle only in the turn released by the deferral ceiling."""
    expired, started = task.get('lastDeferralExpiredAt'), task.get('startedAt')
    completed = task.get('lastCompletedAt') or 0
    return bool(isinstance(expired, (int, float)) and not isinstance(expired, bool)
                and isinstance(started, (int, float)) and not isinstance(started, bool)
                and 0 <= started-expired <= 2000 and expired > completed
                and not task.get('deferredSinceAt'))


async def refresh_trade_confirmations(stream_for, budget_ms=30000, allow_backlogged=False):
    """One bounded confirmation round; completed chain writes survive expiry."""
    global _confirmation_chain_cursor
    began = time.monotonic()
    totals = {'requested': 0, 'accepted': 0, 'updated': 0, 'failed': 0, 'skipped': 0,
              'timeBudgetExpired': False, 'backloggedProbe': False}
    chains = ('196', '56', '4663')
    offset = _confirmation_chain_cursor % len(chains)
    _confirmation_chain_cursor += 1
    for chain in chains[offset:]+chains[:offset]:
        remaining = budget_ms-(time.monotonic()-began)*1000
        if remaining <= 0:
            totals['timeBudgetExpired'] = True
            break
        stream = stream_for(chain)
        if not stream or stream.http is None:
            totals['skipped'] += 1
            continue
        busy = stream.queue.qsize() >= LIVE_BACKLOG_QUEUE_DEPTH
        if busy and (not allow_backlogged or totals['backloggedProbe']):
            totals['skipped'] += 1
            continue
        if busy:
            totals['backloggedProbe'] = True
        report = await stream.confirm_recent_trades(limit=1 if busy else 12,
            budget_ms=min(5000 if busy else 12000, remaining))
        for key in ('requested', 'accepted', 'updated', 'failed'):
            totals[key] += report.get(key, 0)
        totals['timeBudgetExpired'] |= report.get('timeBudgetExpired', False)
    totals['durationMs'] = round((time.monotonic()-began)*1000)
    totals['noChange'] = not totals['accepted'] and not totals['failed'] and not totals['skipped']
    return totals


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


def combined_market_history_round(snapshot_action, sparkline_action, fallback_s=300):
    """One history entry, preserving price-only sparkline refreshes."""
    last_sparkline = float('-inf')

    async def turn():
        nonlocal last_sparkline
        result = await snapshot_action()
        if result.get('sparklinesRefreshed'):
            last_sparkline = time.monotonic()
        elif time.monotonic() - last_sparkline >= fallback_s:
            sparkline = await sparkline_action()
            if sparkline.get('sparklinesRefreshed'):
                last_sparkline = time.monotonic()
            result = {**result,
                      'accepted': result.get('accepted', 0) + sparkline.get('accepted', 0),
                      'updated': result.get('updated', 0) + sparkline.get('updated', 0),
                      'failed': result.get('failed', 0) + sparkline.get('failed', 0),
                      'sparklinesRefreshed': bool(sparkline.get('sparklinesRefreshed'))}
        return result

    return turn


async def run() -> None:
    os.makedirs("data", exist_ok=True)
    owner_lock = open("data/pyradar-worker.lock", "a+")
    # A second process waits here and never calls providers. PM2 is configured
    # for one instance, while this lock protects manual/accidental starts.
    fcntl.flock(owner_lock, fcntl.LOCK_EX)
    load_env()
    # This process owns cold catalogue/history work. Yield same-host CPU and
    # disk scheduling to the independently running popular market collector.
    from .projection_worker import configure_process_priority
    configure_process_priority()
    from . import briefing, insights
    from .collectors import binance as binance_collector
    from .collectors import binance_alpha, chain_stream
    from .collectors.factory_discovery import FactoryDiscovery
    from .collectors.discovery_coverage import reconcile_all_chains, poll_active_stock_pairs
    from .collectors.pool_gap_repair import PoolGapRepair
    from .collectors.chainlink_anchor import refresh_bnb_anchor
    from .collectors.arc_poller import refresh_arc_poller
    from .collectors.risk_enrichment import refresh_risk_enrichment
    from .collectors.market_enrichment import refresh_market_enrichment
    from .collectors.equity_market import refresh_equity_market
    from .collectors.pool_volume_snapshots import refresh_pool_volume_snapshots, refresh_product_sparklines
    from .collectors.live_quotes import refresh_live_quotes, refresh_base_candidates, refresh_base_stocks
    from .collectors.main_round import refresh_liquidity, refresh_main_round, refresh_discovery
    from .collectors.okx_catalogue import sync_okx_catalogues
    from .collectors.candles import refresh_watched_candles
    from .collectors.comparison_inputs import refresh_comparison_inputs
    from .collectors.scheduler import shutdown_loops, spawn_loop, task_health
    from .collectors.trades import refresh_hot_trades
    from .db import (close_all, prepare_quote_stores, quote_store_scope,
                     store, write_lock_snapshot)
    from .stream_hub import flush_legacy

    # Finish additive schema work before concurrent ingestion starts. Large
    # historical indexes must not race live writers or a second connection.
    for chain in ('196','56','4663','5042','system','ai'):
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
            await (await store('system')).put('collector', 'factory-discovery:'+factory.chain, {
                'chainId': factory.chain, 'head': scan.get('head'), 'cursor': scan.get('cursor'),
                'coverageFrom': scan.get('coverageFrom'), 'caughtUp': scan.get('caughtUp'),
                'lagBlocks': max(0, scan.get('head', 0)-scan.get('cursor', 0)),
                'at': __import__('time').time()*1000, 'method': 'canonical-rpc-factory-logs',
                'createdEvents': len(scan.get('newEvents') or []), 'processed': processed})
            return {
                'requested': scan['scannedRanges'] + processed['requested'],
                'accepted': int(scan['scannedRanges'] > 0) + processed['accepted'],
                'updated': len(scan['newEvents']) + processed['updated'],
                'failed': processed['failed'], 'skipped': processed['waitingCatalogue'],
                'head': scan.get('head'), 'cursor': scan.get('cursor'),
                'coverageFrom': scan.get('coverageFrom'), 'caughtUp': scan.get('caughtUp'),
                'lagBlocks': max(0, scan.get('head', 0)-scan.get('cursor', 0)),
            }

        spawn_loop(task_name, 10, factory_round, 15 if factory_chain == '196' else 75, heavy=True)
    # CPU-heavy projections run in app.projection_worker, on another event loop.
    async def quote_round(fn):
        with quote_store_scope():
            return await fn()

    # Bounded quote requests must not wait for cold catalogue/history turns.
    # One independent permit keeps providers and the shared writer bounded;
    # all lanes still check the same host-pressure and cancellation rules.
    quote_network_gate = {'heavy': True, 'budget_lane': 'quote-network', 'max_run_s': 45}
    spawn_loop("arcPoller", 60, refresh_arc_poller, 95, heavy=True)
    spawn_loop("dexBatchMarket", 60, refresh_market_enrichment, 55, **quote_network_gate)
    spawn_loop("poolVolumeSnapshots", 60,
               combined_market_history_round(refresh_pool_volume_snapshots, refresh_product_sparklines),
               110, heavy=True)
    spawn_loop("liveQuotes", 300, lambda: quote_round(refresh_live_quotes), **quote_network_gate)
    spawn_loop("bnbUsdOracle", 300, refresh_bnb_anchor, 20)
    spawn_loop("equityMarket", 60, refresh_equity_market, 8)
    # Baseline quotes start at +5s/+15s and can consume their brief budget
    # without racing a large catalogue recovery. New identities follow at +90s.
    spawn_loop("okxCatalogue", 300, sync_okx_catalogues, 90, heavy=True)
    spawn_loop("binanceApply", 10, lambda: _sync_apply(binance_collector), 3)
    spawn_loop("baseCandidates", 300, lambda: quote_round(refresh_base_candidates), 5, **quote_network_gate)
    spawn_loop("baseStocks", 300, lambda: quote_round(refresh_base_stocks), 15, **quote_network_gate)
    spawn_loop("candles", 5, refresh_watched_candles, 10)
    spawn_loop("binance", 30, binance_collector.refresh_binance, 20)
    spawn_loop("comparisonInputs", 300, refresh_comparison_inputs, 24, heavy=True)
    spawn_loop("aiInsights", 15, insights.refresh_insights, 25)
    spawn_loop("aiBriefing", 60, briefing.refresh_briefing, 30, heavy=True)
    # Defer long, non-live writes while subscribed logs are behind. Each
    # collector retains its normal cadence after running, and the 3-minute
    # ceiling prevents stream backlog alone from starving discovery forever.
    # The separate host-pressure admission gate has no expiry that would let
    # scans overwhelm an already stalled disk or exhausted memory.
    background_gate = {
        'defer_when': lambda: live_stream_backlogged(chain_stream.stream_for),
        'max_deferral_s': 180,
        'heavy': True,
    }
    # These pages perform a bounded number of asynchronous network reads and
    # short fact commits. Their permit must not queue behind CPU/disk-heavy
    # historical scans; the separate lane still enforces host pressure.
    pool_network_gate = {**background_gate, 'budget_lane': 'pool-network'}
    # External indexed-pool reconciliation is slow and deliberately runs
    # outside the live/quote critical path, no more than once per day.
    spawn_loop("discoveryCoverageBsc", 45, reconcile_all_chains, 20,
               resume_stagger_s=10, max_run_s=75, **pool_network_gate)
    spawn_loop("stockPairMinute", 60, poll_active_stock_pairs, 45, max_run_s=75, **pool_network_gate)
    for gap_chain, gap_prefix in (('196', 'XLAYER'), ('56', 'BSC')):
        gap_stream = chain_stream.stream_for(gap_chain)
        if not gap_stream or os.environ.get(gap_prefix + '_POOL_GAP_REPAIR_ENABLED', 'true').lower() == 'false':
            continue
        gap_repair = PoolGapRepair(await store(gap_chain), await store('system'), gap_stream.rpc)

        async def repair_round(gap_repair=gap_repair, gap_stream=gap_stream):
            if gap_stream.http is None:
                return {'requested': 0, 'accepted': 0, 'skipped': 1}
            return await gap_repair.run_once(limit=2)

        spawn_loop("poolGapRepair" + ('Bsc' if gap_chain == '56' else 'XLayer'), 30, repair_round, 75,
                   resume_stagger_s=10, max_run_s=75, **pool_network_gate)
    spawn_loop("mainRound", 300, refresh_main_round, 35,
               resume_stagger_s=0, **background_gate)
    spawn_loop("liquidityRefresh", 30, refresh_liquidity, 45,
               resume_stagger_s=5, priority=True, max_run_s=75, **background_gate)
    spawn_loop("discovery", 300, refresh_discovery, 50,
               resume_stagger_s=10, **background_gate)
    spawn_loop("hotTrades", 300, refresh_hot_trades, 60, heavy=True)
    async def confirm_pool_trades():
        released = confirmation_backlog_probe_due(task_health().get('tradeConfirmations') or {})
        return await refresh_trade_confirmations(chain_stream.stream_for, allow_backlogged=released)

    # Bounded confirmation probes belong to the live path: they observe its
    # backlog gate but never queue behind a cold all-catalogue scan.
    spawn_loop('tradeConfirmations', 60, confirm_pool_trades, 100,
               defer_when=background_gate['defer_when'], max_deferral_s=180)

    if os.environ.get('GOPLUS_ENABLED', 'true').lower() != 'false':
        spawn_loop("riskEnrichment", 300, refresh_risk_enrichment, 80,
                   resume_stagger_s=15, **background_gate)

    # Automatic X reads require their own opt-in and a verified billing unit.
    # The social worker shares the existing daily budget with manual reads.
    from . import product_social
    if product_social.autopoll_reason() is None:
        spawn_loop('kolMentions', 1200, product_social.poll, 120, heavy=True)

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

    # Cleanup must be able to recover a pressured host. It bypasses pressure
    # and live-backlog postponement, while still holding the global permit.
    spawn_loop('maintenance', 3600, maintenance, 120, heavy=True, maintenance=True)

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
