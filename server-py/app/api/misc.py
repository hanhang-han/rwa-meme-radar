"""Events, pair and registry endpoints completing the Node API contract."""
import json

from fastapi import APIRouter, HTTPException, Query

from ..db import store
from ..registry import registry_info
from .. import state

router = APIRouter()


@router.get("/events")
async def get_events(chain: str = Query(default="196"), before: str | None = Query(default=None)):
    if chain not in ("196", "56", "4663"):
        raise HTTPException(status_code=400, detail="Unsupported chain")
    cursor = None
    if before:
        try:
            cursor = json.loads(before)
            assert isinstance(cursor.get("t"), (int, float)) and isinstance(cursor.get("id"), str)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid cursor")
    s = await store(chain)
    items = await s.events_page({"t": int(cursor["t"]), "id": cursor["id"]} if cursor else None)
    next_cursor = None
    if items:
        last = items[-1]
        next_cursor = {"t": last.get("t"), "id": last.get("id")}
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
    relations = [r for r in state.DATA.relations if r.get("token") == stock or r.get("stock") == stock]
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
    return await registry_info(state.DATA.relations)
