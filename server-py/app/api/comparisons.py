import re
from fastapi import APIRouter, HTTPException
from ..comparison_service import read_comparison

router = APIRouter()


@router.get("/comparisons/{chain}/{address}")
async def get_comparison(chain: str, address: str, pool: str | None = None):
    if chain not in ("196", "56", "4663") or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address) or (pool and not re.fullmatch(r"0x[0-9a-fA-F]{40}", pool)):
        raise HTTPException(status_code=400, detail="unsupported instrument")
    return await read_comparison(chain, address.lower(), pool.lower() if pool else None)
