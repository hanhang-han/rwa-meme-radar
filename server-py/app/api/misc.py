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
from ..realtime_projection import read_projection_json, ProjectionUnavailable

router = APIRouter()
EVENT_PAGE_SIZE = 50
SUPPORTED_CHAINS = {"196", "56", "4663"}
FEED_CACHE_SECONDS = 2
_feed_cache = {}
_feed_locks = {}


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
async def get_feed(chain: str | None = None):
    """Homepage activity feed: the latest persisted trades across the most
    recently active assets plus the recent relationship events. SSE appends
    on top; this is the initial load and the polling fallback."""
    if chain is not None and chain not in SUPPORTED_CHAINS and chain != 'all':
        raise HTTPException(status_code=400, detail='Unsupported chain')
    key = chain if chain in SUPPORTED_CHAINS else 'all'
    cached = _feed_cache.get(key)
    if cached and time.monotonic()-cached[0] < FEED_CACHE_SECONDS:
        return cached[1]
    lock = _feed_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _feed_cache.get(key)
        if cached and time.monotonic()-cached[0] < FEED_CACHE_SECONDS:
            return cached[1]
        result = await _load_feed(chain)
        _feed_cache[key] = (time.monotonic(), result)
        return result


async def _load_feed(chain):
    selected_chains = (chain,) if chain in SUPPORTED_CHAINS else ('196', '56', '4663')
    try:
        snapshot = json.loads(await read_projection_json('feed'))
    except ProjectionUnavailable as exc:
        raise HTTPException(status_code=503, detail='snapshot-not-ready', headers={'Retry-After': '3'}) from exc
    assets, signals = snapshot['assets'], snapshot['signals']
    hot_by_chain = {chain: [] for chain in SUPPORTED_CHAINS}
    for asset in assets:
        asset_chain = str(asset.get("chainId") or "196")
        if asset_chain in selected_chains:
            hot_by_chain[asset_chain].append(asset)
    tracked = {(str(a.get("chainId") or "196"), str(a.get("token") or "").lower()): a
               for a in snapshot.get("trackedAssets", assets)}
    async def chain_trades(selected_chain):
        trades = []
        s = await store(selected_chain)
        hot = {str(a['token']).lower(): a for a in hot_by_chain[selected_chain]}
        for r in await token_trades(s, hot, limit=100):
            token = str(r.get('token') or '').lower()
            if token in hot:
                trades.append({**r, 'chainId': selected_chain, 'symbol': hot[token].get('symbol')})
        # Market tapes are limited to indexed site assets and actual DEX
        # swaps before pagination. Exchange trades belong on the token's
        # explicitly labelled markets tab, never in this on-chain feed.
        tokens = [token for (cid, token) in tracked if cid == selected_chain]
        for r in await market_trades(s, limit=100, dex_only=True, tokens=tokens):
            identity = (selected_chain, str(r.get('token') or '').lower())
            if r.get('venue') != 'dex' or identity not in tracked:
                continue
            trades.append({**r, 'chainId': selected_chain, 'symbol': tracked[identity].get('symbol')})
        return trades
    trades = [row for rows in await asyncio.gather(*(chain_trades(cid) for cid in selected_chains)) for row in rows]
    now = int(time.time()*1000)
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
    return {
        "trades": trades[:100],
        "relationships": [row for row in signals if str(row.get('chainId')) in selected_chains][:30],
        "at": now,
        "scope": "indexed-assets-dex-swaps",
    }


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
