"""Asset detail endpoint: persisted asset, relations, samples, recent trades,
events and activity aggregates, matching the Node response shape."""
import time

from fastapi import APIRouter, HTTPException

from ..db import store
from ..state import DATA, fresh, reload_if_stale

router = APIRouter()


def _asset_view(a: dict, chain: str) -> dict:
    out = dict(a)
    out.setdefault("chainId", chain)
    out.setdefault("fieldTimes", {})
    return out


@router.get("/token/{chain}/{address}")
async def get_token(chain: str, address: str):
    address = address.lower()
    if chain not in ("196", "56", "4663") or not (address.startswith("0x") and len(address) == 42):
        raise HTTPException(status_code=400, detail="unsupported token")
    await reload_if_stale()
    if chain == 196:
        import asyncio
        from ..collectors.trades import refresh_asset_on_demand
        asyncio.ensure_future(refresh_asset_on_demand(address))

    s = await store(chain)
    asset = await s.get("asset", address)
    if not asset:
        raise HTTPException(status_code=404, detail="该地址尚未进入追踪索引")
    relations = [r for r in DATA.relations if r.get("token") == address or r.get("stock") == address]
    samples = await s.samples(address, 288)
    trades = await s.recent_trades(address, 50)
    events = [e for e in await s.events(address, 50)][:50]
    day_ago = time.time() * 1000 - 86_400_000
    activity = await s.activity(address, day_ago)

    verified = [r for r in relations if r.get("status") == "verified" and fresh(r.get("checkedAt"), 3_600_000)]
    return {
        "asset": _asset_view(asset, chain),
        "stock": None,
        "relations": relations,
        "trades": trades,
        "events": events,
        "samples": samples,
        "pools": [],
        "activity": activity,
        "analysis": {
            "conclusion": "已核验股票配对" if verified else "尚未完成配对池核验。",
            "correlation": {"reason": "等待独立价格样本与同口径对照"},
            "capture": {"reason": f"覆盖 {len(verified)} 个已核验配对池"},
            "safety": "交易活跃度不能证明与股票存在资金联系。",
        },
    }
