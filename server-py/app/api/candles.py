"""OHLCV candles: OKX DEX REST proxy with per-bar TTL memory cache and
sqlite persistence so history extends beyond the API's recent window."""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Query

from ..db import store
from ..okx_client import okx_get

router = APIRouter()

BAR_TTL = {"1m": 25, "5m": 120, "15m": 300, "1H": 600}  # seconds
BARS = list(BAR_TTL)

_mem: dict[str, tuple[float, list]] = {}
_inflight: dict[str, asyncio.Future] = {}


def _num(v):
    try:
        n = float(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


async def candle_series(chain: str, address: str, bar: str, limit: int):
    key = f"{chain}:{address}:{bar}"
    hit = _mem.get(key)
    now = time.time()
    if hit and now - hit[0] < BAR_TTL[bar]:
        return {"bar": bar, "rows": hit[1], "at": hit[0]}
    pending = _inflight.get(key)
    if pending:
        rows = await pending
        cached = _mem.get(key)
        return {"bar": bar, "rows": rows, "at": cached[0] if cached else now}

    async def task():
        data = await okx_get("/api/v6/dex/market/candles", {
            "chainIndex": chain, "tokenContractAddress": address, "bar": bar, "limit": str(min(limit, 300)),
        })
        if not isinstance(data, list):
            raise RuntimeError("Invalid candles response")
        parsed = []
        for row in data:
            t, o, h, l, c, v, vu, confirmed = (list(row) + [None] * 8)[:8]
            if not t or not o or not c:
                continue
            parsed.append({
                "t": int(t), "o": float(o), "h": float(h or o), "l": float(l or o), "c": float(c),
                "v": _num(v), "vu": _num(vu), "confirmed": confirmed == "1",
            })
        s = await store(chain)
        await s.put_candles(address, bar, parsed)
        rows = await s.candle_range(address, bar, min(limit, 300))
        print(f'[candles] {chain}:{address}:{bar} upstream={len(parsed)} stored={len(rows)} cwd_ok={__import__("os").path.exists("data/research.sqlite")}', flush=True)
        _mem[key] = (time.time(), rows)
        if len(_mem) > 600:
            for k in list(_mem)[: len(_mem) - 500]:
                _mem.pop(k, None)
        return rows

    fut = asyncio.ensure_future(task())
    _inflight[key] = fut
    try:
        rows = await fut
        cached = _mem.get(key)
        return {"bar": bar, "rows": rows, "at": cached[0] if cached else time.time()}
    finally:
        _inflight.pop(key, None)


@router.get("/candles/{chain}/{address}")
async def get_candles(chain: str, address: str, bar: str = Query(default="5m"), limit: int = Query(default=150, ge=30, le=300)):
    address = address.lower()
    if bar not in BARS or chain not in ("196", "56", "4663") or not (address.startswith("0x") and len(address) == 42):
        raise HTTPException(status_code=400, detail="bad request")
    try:
        return await candle_series(chain, address, bar, limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:120])
