"""Events, pair, feed and registry endpoints completing the Node API contract."""
import base64
import binascii
import json
import time

from fastapi import APIRouter, HTTPException, Query

from ..db import store
from ..registry import registry_info
from .. import state
from ..state import _assessed_relations
from ..market_history import market_trades

router = APIRouter()
EVENT_PAGE_SIZE = 50
SUPPORTED_CHAINS = {"196", "56", "4663"}


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
    selected_chains = (chain,) if chain in SUPPORTED_CHAINS else ('196', '56', '4663')
    await state.reload_if_stale()
    # The shared reload has already decoded these facts. Capture the same
    # published view before yielding; repeated feed requests must not decode
    # every asset and relationship again merely to select ten active tokens.
    assets, relations, signals = state.DATA.assets, state.DATA.relations, state.DATA.signals
    related = {(str(r.get("chainId") or "196"), r.get("token"))
               for r in relations if r.get("status") == "verified"}
    hot_by_chain = {chain: [] for chain in SUPPORTED_CHAINS}
    for asset in assets:
        asset_chain = str(asset.get("chainId") or "196")
        if (asset_chain in selected_chains and asset.get("tradeAt")
                and asset.get("kind") == "candidate"
                and (asset_chain, asset.get("token")) in related
                and str(asset.get("symbol") or "").upper() not in state.BASE_QUOTE_SYMBOLS):
            hot_by_chain[asset_chain].append(asset)
    trades: list = []
    for selected_chain in selected_chains:
        s = await store(selected_chain)
        hot = sorted(hot_by_chain[selected_chain], key=lambda a: -(a.get("tradeAt") or 0))[:10]
        for a in hot:
            for r in await s.recent_trades(a["token"], 12):
                r.setdefault("chainId", selected_chain)
                r.setdefault("token", a["token"])
                r.setdefault("symbol", a.get("symbol"))
                trades.append(r)
        # Live exchange/pool trades are stored under their own market keys.
        # Include that tape on initial load/reconnect, preserving currency and
        # market identity; never recompute DEX aggregate counts from it.
        trades.extend(await market_trades(s, limit=100))
    trades.sort(key=lambda r: -(r.get("t") or 0))
    return {
        "trades": trades[:100],
        "relationships": [row for row in signals if str(row.get('chainId')) in selected_chains][:30],
        "at": int(time.time() * 1000),
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
    await state.reload_if_stale()
    s = await store(chain)
    asset = await s.get("asset", stock)
    if not asset:
        raise HTTPException(status_code=404, detail="Not indexed")
    relations = _assessed_relations([
        r for r in state.DATA.relations
        if str(r.get("chainId") or "196") == chain
        and (r.get("token") == stock or r.get("stock") == stock)
    ], time.time() * 1000)
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
    await state.reload_if_stale()
    return await registry_info([r for r in state.DATA.relations if str(r.get("chainId") or "196") == "196"])
