"""On-demand trade refresh for assets a detail page is watching: one page of
recent trades, persisted and broadcast, so the activity feed keeps rolling
between scan rounds. Ports the Node refreshAssetOnDemand core."""
import time

from ..db import store
from ..okx_client import okx_get
from ..stream_hub import broadcast
from .assets import now_ms
from .live_quotes import watch_asset


def _num(v):
    try:
        n = float(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def normalize_trade(row: dict, token: str) -> dict | None:
    t = _num(row.get("time"))
    try:
        t = int(row.get("time"))
    except (TypeError, ValueError):
        t = None
    if str(row.get("chainIndex")) != "196" or (row.get("tokenContractAddress") or "").lower() != token:
        return None
    if not row.get("id") or not t or row.get("type") not in ("buy", "sell"):
        return None
    tx = row.get("txHashUrl")
    return {
        "id": str(row["id"]), "t": t, "type": row["type"],
        "price": _num(row.get("price")), "volume": _num(row.get("volume")),
        "user": (row.get("userAddress") or "").lower() or None,
        "hash": tx if isinstance(tx, str) and tx.startswith("0x") and len(tx) == 66 else None,
        "dex": str(row.get("dexName") or ""), "source": "OKX trades",
    }


async def refresh_asset_on_demand(address: str) -> None:
    watch_asset(address)
    s = await store("196")
    asset = await s.get("asset", address)
    if not asset or asset.get("kind") != "candidate":
        return
    if asset.get("tradeAt") and now_ms() - asset["tradeAt"] < 45_000:
        return
    try:
        data = await okx_get("/api/v6/dex/market/trades", {
            "chainIndex": "196", "tokenContractAddress": address, "limit": "100",
        })
        if not isinstance(data, list):
            raise RuntimeError("Invalid trades response")
        rows = [r for r in (normalize_trade(x, address) for x in data) if r]
        fresh = [r for r in rows if not await s.has_trade(address, r["id"])]
        await s.put_trades(address, rows)
        if asset.get("oldestTradeAt"):
            asset["oldestTradeAt"] = min(asset["oldestTradeAt"], min((r["t"] for r in rows), default=asset["oldestTradeAt"]))
        else:
            asset["oldestTradeAt"] = min((r["t"] for r in rows), default=None)
        asset["tradeAt"] = now_ms()
        await s.put("asset", address, asset)
        if fresh:
            broadcast("trade", {"chainId": "196", "token": address, "fresh": fresh})
    except Exception as e:
        print(f"[ondemand] {type(e).__name__}: {e}", flush=True)
