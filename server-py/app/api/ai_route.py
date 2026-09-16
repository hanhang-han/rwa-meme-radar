"""AI endpoints: pre-generated briefing and per-asset insight narration."""
from fastapi import APIRouter, Query

from .. import briefing, state
from ..ai import ai_enabled, ai_narrate
from ..db import store

router = APIRouter()


@router.get("/ai/briefing")
async def get_briefing(lang: str = Query(default="zh")):
    return await briefing.briefing_endpoint("en" if lang == "en" else "zh")


@router.get("/ai/insight/{chain}/{address}")
async def get_insight(chain: str, address: str, lang: str = Query(default="zh")):
    address = address.lower()
    if chain not in ("196", "56", "4663") or not (address.startswith("0x") and len(address) == 42):
        return {"text": None, "reason": "bad_request"}
    await state.reload_if_stale()
    s = await store(chain)
    asset = await s.get("asset", address)
    if not asset:
        return {"text": None, "reason": "not_indexed"}
    relations = [r for r in state.DATA.relations if r.get("token") == address]
    data = {
        "asset": {
            "symbol": asset.get("symbol"), "name": asset.get("name"),
            "price": asset.get("price"), "change24h": asset.get("change24h"),
            "volume24h": asset.get("volume24h"), "liquidity": asset.get("liquidity"),
            "holders": asset.get("holders"), "kind": asset.get("kind"),
        },
        "relations": [
            {"ticker": r.get("ticker"), "status": r.get("status"),
             "poolLiquidityUsd": r.get("liquidityUsd"), "checkedAt": r.get("checkedAt")}
            for r in relations
        ],
    }
    task = "为这个资产的详情页写一段 120-180 字的数据解读，重点说明它与股票的关系证据状态。"
    result = await ai_narrate(f"insight:{chain}:{address}", "en" if lang == "en" else "zh", 15 * 60_000, data, task)
    if result:
        return {"text": result["text"], "at": result["at"], "cached": result.get("cached", False)}
    return {"text": None, "reason": "upstream_failed" if ai_enabled() else "disabled"}
