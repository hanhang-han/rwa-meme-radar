"""Events, pair, feed and registry endpoints completing the Node API contract."""
import base64
import binascii
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Query

from ..db import store
from ..registry import registry_info
from ..state import _assessed_relations
from ..scoped_reads import candidate_relations
from ..market_history import market_trades, token_trades
from ..realtime_projection import read_projection_json, read_projection_payload, ProjectionUnavailable

router = APIRouter()
EVENT_PAGE_SIZE = 50
SUPPORTED_CHAINS = {"196", "56", "4663"}
FEED_CACHE_SECONDS = 2
FEED_MAX_STALE_SECONDS = 30
FEED_READ_TIMEOUT_SECONDS = 20
_feed_cache = {}
_feed_locks = {}
_feed_tasks = {}
_feed_chain_cache = {}
_feed_chain_tasks = {}
_feed_retry_at = {}
_feed_loop = None
_feed_stopping = False
_feed_warm_task = None
_feed_sequence = 0


class _FeedPayload(dict):
    def __init__(self, body, scope, created, read_started):
        super().__init__(body)
        self.scope, self.created = scope, created
        self.read_started = read_started


def _next_feed_sequence():
    global _feed_sequence
    _feed_sequence += 1
    return _feed_sequence


def _ensure_feed_loop():
    global _feed_loop, _feed_stopping, _feed_warm_task
    loop = asyncio.get_running_loop()
    if _feed_loop is not loop:
        if _feed_loop is not None and not _feed_loop.is_closed():
            raise HTTPException(503, 'feed-loop-unavailable')
        _feed_cache.clear(); _feed_locks.clear(); _feed_tasks.clear()
        _feed_chain_cache.clear(); _feed_chain_tasks.clear(); _feed_retry_at.clear()
        _feed_loop, _feed_stopping, _feed_warm_task = loop, False, None
    if _feed_stopping:
        raise HTTPException(503, 'feed-stopping')


def _chain_feed_scope(snapshot, chain):
    hot = {str(a['token']).lower(): a for a in snapshot['assets']
           if str(a.get('chainId') or '196') == chain}
    tracked = {str(a.get('token') or '').lower(): a
               for a in snapshot.get('trackedAssets', snapshot['assets'])
               if str(a.get('chainId') or '196') == chain}
    signature = tuple(tuple((token, values[token].get('symbol')) for token in sorted(values))
                      for values in (hot, tracked))
    return signature, hot, tracked


def _response_feed_scope(snapshot, key):
    chains = (key,) if key in SUPPORTED_CHAINS else ('196', '56', '4663')
    return tuple((chain, _chain_feed_scope(snapshot, chain)[0]) for chain in chains)


def _observe_feed_task(task):
    if not task.cancelled():
        task.exception()  # Refresh failures are observed even without a waiter.


async def _read_feed_chain(chain, snapshot, *, required_read=None):
    signature, hot, tracked = _chain_feed_scope(snapshot, chain)
    while True:
        cached = _feed_chain_cache.get(chain)
        if (required_read is None and cached and cached[0] == signature
                and 0 <= time.monotonic()-cached[1] < FEED_CACHE_SECONDS):
            return cached
        running = _feed_chain_tasks.get(chain)
        if running is None or running[1].done():
            read_started = (time.monotonic(), _next_feed_sequence())
            async def load():
                async with asyncio.timeout(FEED_READ_TIMEOUT_SECONDS):
                    s = await store(chain)
                    trades = []
                    for row in await token_trades(s, hot, limit=100):
                        token = str(row.get('token') or '').lower()
                        if token in hot:
                            trades.append({**row, 'chainId': chain, 'symbol': hot[token].get('symbol')})
                    for row in await market_trades(s, limit=100, dex_only=True, tokens=list(tracked)):
                        token = str(row.get('token') or '').lower()
                        if row.get('venue') == 'dex' and token in tracked:
                            trades.append({**row, 'chainId': chain, 'symbol': tracked[token].get('symbol')})
                value = (signature, time.monotonic(), int(time.time()*1000), trades, read_started)
                if not _feed_stopping:
                    _feed_chain_cache[chain] = value
                return value
            task = asyncio.create_task(load(), name='feed-chain-'+chain)
            running = (signature, task, read_started)
            _feed_chain_tasks[chain] = running
            def done(finished):
                if _feed_chain_tasks.get(chain, (None, None))[1] is finished:
                    _feed_chain_tasks.pop(chain, None)
                _observe_feed_task(finished)
            task.add_done_callback(done)
        try:
            result = await asyncio.shield(running[1])
        except Exception:
            if running[0] == signature and (required_read is None or running[2][1] > required_read[1]):
                raise
            continue
        if running[0] == signature and (required_read is None or running[2][1] > required_read[1]):
            return result


def _start_feed_refresh(key, snapshot, signature, *, required_read=None):
    running = _feed_tasks.get(key)
    if running and not running[1].done():
        return running
    read_started = (time.monotonic(), _next_feed_sequence())
    async def refresh():
        try:
            async with asyncio.timeout(FEED_READ_TIMEOUT_SECONDS):
                result = await _load_feed(None if key == 'all' else key, snapshot=snapshot, required_read=required_read)
            if _feed_stopping:
                raise asyncio.CancelledError
            _feed_cache[key] = (getattr(result, 'created', time.monotonic()), result)
            _feed_retry_at.pop(key, None)
            return result
        except Exception:
            _feed_retry_at[key] = time.monotonic()+FEED_CACHE_SECONDS
            raise
    task = asyncio.create_task(refresh(), name='feed-response-'+key)
    running = (signature, task, read_started)
    _feed_tasks[key] = running
    def done(finished):
        if _feed_tasks.get(key, (None, None))[1] is finished:
            _feed_tasks.pop(key, None)
        _observe_feed_task(finished)
    task.add_done_callback(done)
    return running


def _cached_feed_response(entry, status):
    age = max(0, time.monotonic()-entry[0])
    return {**entry[1], 'cacheAgeMs': int(age*1000), 'cacheStatus': status}


async def start_feed_reads():
    """Warm four bounded response keys once without delaying API readiness."""
    global _feed_stopping, _feed_warm_task
    _feed_stopping = False
    _ensure_feed_loop()
    if _feed_warm_task is None:
        async def warm():
            await asyncio.gather(*(get_feed(key) for key in ('all', '196', '56', '4663')), return_exceptions=True)
        _feed_warm_task = asyncio.create_task(warm(), name='feed-startup-warm')
        _feed_warm_task.add_done_callback(_observe_feed_task)


async def stop_feed_reads():
    """Drain refreshes before their shared storage connections are closed."""
    global _feed_stopping, _feed_warm_task
    _feed_stopping = True
    tasks = {record[1] for values in (_feed_tasks, _feed_chain_tasks) for record in values.values()}
    if _feed_warm_task is not None:
        tasks.add(_feed_warm_task)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _feed_cache.clear(); _feed_locks.clear(); _feed_tasks.clear()
    _feed_chain_cache.clear(); _feed_chain_tasks.clear(); _feed_retry_at.clear()
    _feed_warm_task = None


def _encode_event_cursor(item: dict) -> str:
    raw = json.dumps({"t": int(item["t"]), "id": str(item["id"])}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_event_cursor(value: str) -> dict:
    try:
        # Continue accepting the former JSON cursor during a rolling deploy.
        if value.lstrip().startswith("{"):
            cursor = json.loads(value)
        else:
            padded = value + "=" * (-len(value) % 4)
            cursor = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if not isinstance(cursor, dict):
            raise ValueError("cursor is not an object")
        t = cursor.get("t")
        event_id = cursor.get("id")
        if not isinstance(t, (int, float)) or not isinstance(event_id, str) or not event_id:
            raise ValueError("cursor fields are invalid")
        return {"t": int(t), "id": event_id}
    except (ValueError, TypeError, OverflowError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc


def _normalize_event(item: dict, chain: str) -> dict:
    row = {**item, "chainId": chain}
    asset = str(row.get("asset") or "").strip()
    prefix = f"{row['chainId']}:"
    while asset.startswith(prefix):
        asset = asset[len(prefix):]
    row["asset"] = asset.lower()
    return row


@router.get("/feed")
async def get_feed(chain: str | None = None, fresh: bool = False):
    """Homepage activity feed: the latest persisted trades across the most
    recently active assets plus the recent relationship events. SSE appends
    on top; this is the initial load and the polling fallback."""
    if chain is not None and chain not in SUPPORTED_CHAINS and chain != 'all':
        raise HTTPException(status_code=400, detail='Unsupported chain')
    _ensure_feed_loop()
    required_read = (time.monotonic(), _next_feed_sequence()) if fresh else None
    key = chain if chain in SUPPORTED_CHAINS else 'all'
    deadline = time.monotonic()+FEED_READ_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout(FEED_READ_TIMEOUT_SECONDS):
            snapshot = await read_projection_payload('feed', json_reader=read_projection_json)
    except (ProjectionUnavailable, ValueError, TimeoutError):
        raise HTTPException(503, 'snapshot-not-ready', headers={'Retry-After': '3'}) from None
    signature = _response_feed_scope(snapshot, key)
    while True:
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise HTTPException(503, 'feed-read-deadline', headers={'Retry-After': '2'})
        cached = _feed_cache.get(key)
        matching = cached and getattr(cached[1], 'scope', signature) == signature
        age = time.monotonic()-cached[0] if matching else float('inf')
        if not fresh and 0 <= age < FEED_CACHE_SECONDS:
            return _cached_feed_response(cached, 'fresh')
        retry = _feed_retry_at.get(key, 0) > time.monotonic()
        running = _feed_tasks.get(key)
        if fresh or not retry:
            running = _start_feed_refresh(key, snapshot, signature, required_read=required_read)
        if not fresh and 0 <= age <= FEED_MAX_STALE_SECONDS:
            return _cached_feed_response(cached, 'stale-error' if retry else 'stale-refreshing')
        if running is None or running[1].done():
            raise HTTPException(503, 'feed-unavailable', headers={'Retry-After': '2'})
        try:
            result = await asyncio.wait_for(asyncio.shield(running[1]), remaining)
        except asyncio.CancelledError:
            raise
        except Exception:
            if fresh and running[2][1] <= required_read[1]:
                continue
            raise HTTPException(503, 'feed-unavailable', headers={'Retry-After': '2'}) from None
        if running[0] == signature:
            if fresh and (running[2][1] <= required_read[1]
                          or getattr(result, 'read_started', (0, -1))[1] <= required_read[1]):
                continue
            entry = (getattr(result, 'created', time.monotonic()), result)
            if 0 <= time.monotonic()-entry[0] <= FEED_MAX_STALE_SECONDS:
                return _cached_feed_response(entry, 'fresh' if fresh or time.monotonic()-entry[0] < FEED_CACHE_SECONDS else 'stale-refreshing')


async def _load_feed(chain, *, snapshot=None, required_read=None):
    selected_chains = (chain,) if chain in SUPPORTED_CHAINS else ('196', '56', '4663')
    if snapshot is None:
        try:
            snapshot = await read_projection_payload('feed', json_reader=read_projection_json)
        except ProjectionUnavailable as exc:
            raise HTTPException(status_code=503, detail='snapshot-not-ready', headers={'Retry-After': '3'}) from exc
    signals = snapshot['signals']
    results = await asyncio.gather(*(_read_feed_chain(cid, snapshot, required_read=required_read) for cid in selected_chains))
    trades = [row for result in results for row in result[3]]
    now = min(result[2] for result in results)
    unique = {}
    for row in trades:
        if row.get('venue') in ('binance', 'binance-alpha', 'exchange') or row.get('priceScope') == 'exchange':
            continue
        row = {**row, 'venue': 'dex', 'tradesScope': 'dex'}
        at = row.get('sourceEventAt') or row.get('t')
        row['delayMs'] = max(0, now-at) if isinstance(at, (int, float)) else None
        row['usdStatus'] = 'known' if row.get('volume') is not None and row.get('volumeCurrency', 'USD') == 'USD' else 'unknown'
        if row['usdStatus'] == 'unknown':
            row['volume'] = None
        identity = (str(row.get('chainId')), row.get('token'), row.get('id'))
        unique[identity] = row
    trades = list(unique.values())
    trades.sort(key=lambda r: -(r.get("t") or 0))
    return _FeedPayload({
        "trades": trades[:100],
        "relationships": [row for row in signals if str(row.get('chainId')) in selected_chains][:30],
        "at": now,
        "scope": "indexed-assets-dex-swaps",
    }, _response_feed_scope(snapshot, chain if chain in SUPPORTED_CHAINS else 'all'),
       min(result[1] for result in results), min((result[4] for result in results), key=lambda stamp: stamp[1]))


@router.get("/events")
async def get_events(chain: str = Query(default="196"), before: str | None = Query(default=None)):
    if chain not in SUPPORTED_CHAINS:
        raise HTTPException(status_code=400, detail="Unsupported chain")
    cursor = _decode_event_cursor(before) if before else None
    s = await store(chain)
    items = [_normalize_event(item, chain) for item in await s.events_page(cursor, limit=EVENT_PAGE_SIZE)]
    next_cursor = None
    if len(items) == EVENT_PAGE_SIZE:
        next_cursor = _encode_event_cursor(items[-1])
    return {"items": items, "next": next_cursor}


@router.get("/pair/{chain}/{stock}")
async def get_pair(chain: str, stock: str):
    stock = stock.lower()
    if chain not in ("196", "56", "4663") or not (stock.startswith("0x") and len(stock) == 42):
        raise HTTPException(status_code=400, detail="Invalid identity")
    s = await store(chain)
    asset = await s.get("asset", stock)
    if not asset:
        raise HTTPException(status_code=404, detail="Not indexed")
    relations = _assessed_relations(await candidate_relations(s, chain, stock), time.time() * 1000)
    return {
        "asset": asset,
        "stock": None,
        "relations": relations,
        "trades": await s.recent_trades(stock, 50),
        "events": (await s.events(stock, 50))[:50],
        "samples": await s.samples(stock, 288),
        "pools": [],
        "activity": await s.activity(stock, int(__import__("time").time() * 1000) - 86_400_000),
        "analysis": {"conclusion": "配对分析", "correlation": {"reason": ""}, "capture": {"reason": ""}, "safety": ""},
    }


@router.get("/registry")
async def get_registry():
    s = await store("196")
    return await registry_info(await candidate_relations(s, "196"))
