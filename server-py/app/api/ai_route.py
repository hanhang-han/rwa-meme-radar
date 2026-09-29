"""Read-only AI products plus cheap demand registration for asset insights."""
from fastapi import APIRouter, Query

from .. import briefing
from ..db import store
from ..insights import request_insight

router = APIRouter()


@router.get("/ai/briefing")
async def get_briefing(lang: str = Query(default="zh")):
    return await briefing.briefing_endpoint("en" if lang == "en" else "zh")


@router.get("/ai/insight/{chain}/{address}")
async def get_insight(chain: str, address: str, lang: str = Query(default="zh")):
    address = address.lower()
    if chain not in ("196", "56", "4663") or not (address.startswith("0x") and len(address) == 42):
        return {"text": None, "reason": "bad_request"}
    s = await store(chain)
    asset = await s.get("asset", address)
    if not asset:
        return {"text": None, "reason": "not_indexed"}
    return await request_insight(chain, address, "en" if lang == "en" else "zh")
