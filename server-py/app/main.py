"""RWA Meme Radar query API. Collectors run in app.worker."""
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path


from .config import load_env

load_env()
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from .api import ai_route, candles, comparisons, dashboard, developer, misc, public_v1, stream, token
from . import developer_access


@asynccontextmanager
async def lifespan(_: FastAPI):
    from .stream_hub import start_hub, stop_hub
    from .db import store
    await asyncio.to_thread(developer_access.init)
    for chain in ('196','56','4663','system','ai'):
        await store(chain)  # schema/indexes before accepting concurrent queries
    await start_hub()
    try:
        yield
    finally:
        from .demand_leases import stop_lease_writer
        await stop_lease_writer()
        await stop_hub()
        from .db import close_all
        await close_all()


app = FastAPI(title="RWA Meme Radar API", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.include_router(dashboard.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(token.router, prefix="/api")
app.include_router(comparisons.router, prefix="/api")
app.include_router(candles.router, prefix="/api")
app.include_router(ai_route.router, prefix="/api")
app.include_router(misc.router, prefix="/api")
app.include_router(developer.router, prefix="/api")
app.include_router(public_v1.router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "pyradar-api"}


@app.get("/api/health/live")
async def health_live():
    return {"ok": True}


@app.get("/api/health/ready")
async def health_ready():
    from .db import store
    try:
        await (await store("196")).fetchone("SELECT 1")
        await asyncio.to_thread(developer_access.ready)
        return {"ok": True, "storage": "ready"}
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "storage": "error", "error": type(exc).__name__},
        )


def process_health(path, now, stale_after_ms=180_000):
    """A recent file from an exited process is not a healthy worker."""
    import json
    import os
    try:
        record = json.loads(Path(path).read_text())
        if not isinstance(record, dict):
            raise ValueError("invalid health document")
        updated = int(record.get("updatedAt") or 0)
        pid = int(record.get("pid") or 0)
        tasks = record.get("tasks")
        if not isinstance(tasks, dict):
            tasks = {}
        age = now - updated
        alive = False
        if pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except PermissionError:
                alive = True
            except ProcessLookupError:
                pass
        ok = alive and bool(tasks) and 0 <= age < stale_after_ms
        return {**record, "tasks": tasks, "ageMs": age, "alive": alive, "ok": ok}
    except (OSError, ValueError, TypeError):
        return {"tasks": {}, "ageMs": None, "alive": False, "ok": False}


def disk_history(path="data/disk-health.json"):
    import json
    try:
        rows = json.loads(Path(path).read_text()).get("samples", [])
        if not isinstance(rows, list):
            return []
        return [row for row in rows[-48:] if isinstance(row, dict)
                and isinstance(row.get("at"), int)
                and isinstance(row.get("freeBytes"), int)
                and isinstance(row.get("totalBytes"), int) and row["totalBytes"] > 0]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def classify_sources(sources):
    """A missing optional feed limits one capability, not the whole collector."""
    required_kinds = {"dex-quotes", "exchange-token-quotes", "issuer-quotes"}
    failures = {"budget-exhausted", "quota-exhausted", "entitlement-required", "error", "stale", "unconfigured", "unknown"}
    cautions = {"partial", "reserved", "critical-only", "starting"}
    issues, warnings, annotated = [], [], []
    for source in sources:
        required = source.get("kind") in required_kinds
        status = source.get("status") or "unknown"
        annotated.append({**source, "status": status, "required": required})
        if status in failures or status in cautions:
            code = str(source.get("provider") or source.get("id") or "source") + "-" + str(status)
            (issues if required and status in failures else warnings).append(code)
    return annotated, issues, warnings


def chain_streams_degraded(rows):
    """A connected worker is not healthy when its trade replay cannot catch up."""
    for row in rows:
        queued = row.get('queueDepth') or 0
        lag_ms = row.get('sourceLagMs') or 0
        near_tip_lag = row.get('nearTipLagBlocks') or 0
        if queued >= 4096 or near_tip_lag >= 1000 or (lag_ms >= 300_000 and near_tip_lag >= 100):
            return True
    return False


def okx_lane_usage(day, path=None):
    """Read the committed lane ledger without taking a quota writer lock."""
    import os
    import sqlite3
    ledger = Path(path or os.environ.get("OKX_LEDGER_PATH", "data/okx-budget.sqlite"))
    if not day or not ledger.exists():
        return []
    # These limits match the collectors' current lane allowances. The shared
    # global budget remains the final gate for every provider call.
    limits = {"core-quotes": 1152, "base-candidates": 1728, "base-stocks": 1728,
              "discovery": 1544, "trades": 1000, "candles": 800}
    try:
        with sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
            used = dict(db.execute("SELECT lane,used FROM lane_budget WHERE day=?", (day,)))
        return [{"name": name, "used": int(used.get(name, 0)), "limit": limit,
                 "remaining": max(0, limit-int(used.get(name, 0)))} for name, limit in limits.items()]
    except sqlite3.Error:
        return []


@app.get("/api/health/data")
async def health_data():
    from . import state
    await state.reload_if_stale()
    now = int(time.time() * 1000)
    latest_asset = max((a.get("updatedAt") or 0 for a in state.DATA.assets), default=0)
    collector = process_health("data/worker-health.json", now)
    projection = process_health("data/projection-health.json", now)
    # Keep the existing worker.tasks contract for dashboards and release checks,
    # while exposing each process separately for fault isolation.
    tasks = {**collector["tasks"], **projection["tasks"]}
    worker_ok = collector["ok"] and projection["ok"]
    ages = [item["ageMs"] for item in (collector, projection) if item["ageMs"] is not None]
    worker = {**collector, "tasks": tasks, "ok": worker_ok,
              "ageMs": max(ages) if len(ages) == 2 else None}
    import shutil
    from .db import store
    disk = shutil.disk_usage('data')
    coverage = {}
    enriched = {(str(a.get('chainId')), str(a.get('token') or '').lower()): state.enrich_asset(a)
                for a in state.DATA.assets}
    candidates = state.DATA.visible_assets(enriched=enriched, now=now)
    stocks = state.DATA.stock_views(enriched=enriched)
    for name,rows,threshold in [('candidates',candidates,1800000),('stocks',stocks,3600000)]:
        known = [r for r in rows if r.get('price') is not None]
        times = [float((r.get('fieldTimes') or {}).get('price') or r.get('quoteAt') or 0) for r in known]
        ages = sorted(now-t for t in times if 0<t<=now+1000)
        coverage[name] = {'total':len(rows),'known':len(known),'missing':len(rows)-len(known),
                          'withinTarget':sum(0<=now-t<=threshold for t in times),'targetMs':threshold,
                          'unknownTime':sum(not t for t in times),'olderThanDay':sum(now-t>86400000 for t in times if t),
                          'ageP95Ms':ages[min(len(ages)-1,int(len(ages)*.95))] if ages else None}
        unavailable = sum(r.get('price') is None and r.get('quoteStatus') == 'no-verified-market' for r in rows)
        coverage[name].update({'quoteUnavailable': unavailable, 'quotableTotal': len(rows)-unavailable,
                               'pending': len(rows)-len(known)-unavailable,
                               'quotableWithinTarget': coverage[name]['withinTarget']})
    discovery = {}
    unsupported_pools = 0
    for chain in ('196','56','4663'):
        s = await store(chain)
        checks = await s.all('collector-job')
        domains={}
        for job in checks:
            domain=str(job.get('domain') or 'unknown')
            group=domains.setdefault(domain,{'total':0,'failed':0,'waitingRetry':0,'lastSuccessAt':0,'lastAttemptAt':0})
            group['total']+=1
            group['failed']+=int(bool(job.get('failureCount')))
            group['waitingRetry']+=int((job.get('nextRetryAt') or 0)>now)
            group['lastSuccessAt']=max(group['lastSuccessAt'],job.get('lastSuccessAt') or 0)
            group['lastAttemptAt']=max(group['lastAttemptAt'],job.get('lastAttemptAt') or 0)
        scans = await s.all('scan')
        scan_quality = {
            'total': len(scans),
            'partial': sum(row.get('status') == 'partial' for row in scans),
            'unsupportedPools': sum(int(row.get('unsupportedPools') or 0) for row in scans),
            'failedPools': sum(int(row.get('failedPools') or 0) for row in scans),
        }
        unsupported_pools += scan_quality['unsupportedPools']
        discovery[chain]={'latestEventAt':max((r.get('t') or 0 for r in state.DATA.signals if str(r.get('chainId'))==chain),default=0) or None,
                          'domains':domains, 'scanQuality':scan_quality}
    issues=[]
    if not state.DATA.assets:issues.append('assets-unavailable')
    if not collector['ok']:issues.append('worker-unavailable')
    if not projection['ok']:issues.append('projection-unavailable')
    if disk.free/disk.total<.2:issues.append('disk-low')
    for name,c in coverage.items():
        denominator = c['quotableTotal']
        if c['total'] and (not denominator or c['withinTarget']/denominator<.9):
            issues.append(name+'-coverage-degraded')
    failed_tasks=[name for name,task in worker.get('tasks',{}).items() if task.get('status') in ('error','partial','quota-blocked')]
    if any(name in collector['tasks'] for name in failed_tasks):issues.append('collector-degraded')
    if any(name in projection['tasks'] for name in failed_tasks):issues.append('projection-degraded')
    raw_sources = state._source_statuses(stocks,candidates)
    for source in raw_sources:
        if source.get('id') == 'okx:dex' and source.get('budget'):
            source['budget'] = {**source['budget'], 'lanes': okx_lane_usage(source['budget'].get('day'))}
    sources, source_issues, warnings = classify_sources(raw_sources)
    issues.extend(source_issues)
    if coverage['candidates']['quoteUnavailable']:
        warnings.append('candidate-no-verified-market')
    if coverage['stocks']['quoteUnavailable']:
        warnings.append('stock-no-verified-market')
    def public_scan_diagnostic(value):
        scan = value if isinstance(value, dict) else {}
        fields = (
            'lastAttemptAt', 'lastCompletedAt', 'lastSuccessAt', 'lastSuccessfulBlock',
            'lastErrorAt', 'lastPageAt', 'lastPageFromBlock', 'lastPageToBlock',
            'lastPageAddressCount', 'lastPageDurationMs', 'lastPageRpcMs', 'lastPageLogs',
            'lastRangeFromBlock', 'lastRangeToBlock', 'lastRangeDurationMs',
            'lastRangeRpcMs', 'lastRangeLogs', 'lastRangePages',
        )
        result = {key: scan.get(key) for key in fields if isinstance(scan.get(key), (int, float))}
        kind = str(scan.get('lastError') or '').split(':', 1)[0]
        if kind and len(kind) <= 64 and kind.replace('_', '').isalnum():
            result['lastErrorKind'] = kind
        return result

    chain_streams = [
        {**{key: row.get(key) for key in (
            'chainId', 'status', 'updatedAt', 'sourceLagMs', 'queueDepth',
            'decodedEvents', 'reconnects', 'lastEventAt', 'lastReceivedAt',
            'lastHead', 'lastProcessedBlock', 'lastNearTipBlock',
            'nearTipLagBlocks', 'historicalGapBlocks', 'nearTipCoverageFrom',
            'nearTipVerifiedAt', 'lastErrorAt')},
         'historicalScan': public_scan_diagnostic(row.get('historicalScan')),
         'nearTipScan': public_scan_diagnostic(row.get('nearTipScan'))}
        for row in getattr(state.DATA, 'stream_states', [])
        if row.get('provider') == 'Chain RPC' and str(row.get('id', '')).startswith('chain-stream:')
    ]
    if chain_streams_degraded(chain_streams):
        issues.append('chain-stream-degraded')
    if any((task.get('result') or {}).get('unsupported', 0) for task in collector['tasks'].values()):
        warnings.append('collector-unsupported-observations')
    if unsupported_pools:
        warnings.append('discovery-unsupported-pools')
    references=[r for r in stocks if r.get('stockPrice') is not None]
    reference_coverage={'total':len(stocks),'known':len(references),
                        'independent':sum(r.get('referenceScope')=='equity-exchange' for r in references),
                        'issuer':sum(r.get('referenceScope')=='issuer-reference' for r in references)}
    total_references = reference_coverage['total']
    reference_coverage['missing'] = total_references-reference_coverage['known']
    reference_coverage['independentPercent'] = round(reference_coverage['independent']/total_references*100,2) if total_references else None
    reference_coverage['knownPercent'] = round(reference_coverage['known']/total_references*100,2) if total_references else None
    eodhd = next((source for source in sources if source.get('id') == 'eodhd:exchange'), {})
    reference_capability = {
        **reference_coverage,
        'status': ('unavailable' if not reference_coverage['independent'] else
                   'available' if reference_coverage['independent'] == total_references else 'partial') if total_references else 'not-applicable',
        'reason': eodhd.get('status') if eodhd.get('status') in ('entitlement-required','quota-exhausted','error') else None,
    }
    if total_references and reference_coverage['independent'] < total_references:
        warnings.append('stock-references-coverage-limited')
    history = disk_history()
    disk_samples = [*history, {'at': now, 'freeBytes': disk.free, 'totalBytes': disk.total}][-49:]
    core_ok = not issues
    return {
        "ok": core_ok,
        "status": 'degraded' if issues else 'limited' if warnings else 'healthy',
        "issues":issues,"warnings":warnings,"coverage":coverage,"discovery":discovery,
        "chainStreams":chain_streams,
        "failedTasks":failed_tasks,"disk":{"freeBytes":disk.free,"freePercent":round(disk.free/disk.total*100,2)},
        "diskSamples":disk_samples,"references":reference_coverage,
        "capabilities":{"stockReferences":reference_capability},"sources":sources,
        "assets": len(state.DATA.assets),
        "relations": len(state.DATA.relations),
        "latestAssetAt": latest_asset or None,
        "latestAssetAgeMs": now - latest_asset if latest_asset else None,
        "worker": worker,
        "collector": collector,
        "projection": projection,
    }
