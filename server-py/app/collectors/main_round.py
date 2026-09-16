"""Main collection round (5min): verify pair pools on-chain and persist the
relation facts. Ports the Node refreshXLayer verification core — catalogue
stocks are read from the existing stock facts, pools come from OKX
top-liquidity, identities are resolved through token0/token1 and the
asset()/underlying() wrapper interface."""
import asyncio
import re
import time

from web3 import Web3

from ..db import store
from ..okx_client import okx_get
from ..registry import w3
from .assets import now_ms, save_asset

REVERT = re.compile(r"revert|execution|invalid opcode|method unavailable", re.I)


def word_address(value) -> str | None:
    raw = value.hex() if isinstance(value, bytes) else str(value)
    if re.fullmatch(r"0x[\da-f]{64}", raw, re.I) and raw != "0x" + "0" * 64:
        return Web3.to_checksum_address("0x" + raw[-40:])
    return None


_decimals: dict[str, int] = {}


async def cached_decimals(addr: str) -> int:
    hit = _decimals.get(addr)
    if hit is not None:
        return hit
    try:
        raw = await rpc_call(addr, "0x313ce567")
        hx = raw.hex() if isinstance(raw, bytes) else str(raw)
        d = 18 if hx in ("0x", "0x0") else int(hx, 16)
    except Exception:
        d = 18
    _decimals[addr] = d
    return d


async def rpc_call(to: str, data: str):
    def call():
        return w3().eth.call({"to": Web3.to_checksum_address(to), "data": data})

    try:
        return await asyncio.wait_for(asyncio.to_thread(call), 15)
    except Exception as e:
        raise RuntimeError(str(e)[:120]) from e


async def resolve_stock(address: str, stocks: list[dict]):
    direct = next((s for s in stocks if (s.get("tokenContractAddress") or "").lower() == address.lower()), None)
    if direct:
        return {"stock": direct, "wrapped": False}
    incomplete = False
    for selector in ("0x38d52e0f", "0x6f307dc3"):
        try:
            raw = await rpc_call(address, selector)
            underlying = word_address(raw)
            stock = next((s for s in stocks if (s.get("tokenContractAddress") or "").lower() == (underlying or "").lower()), None)
            if stock:
                return {"stock": stock, "wrapped": True}
        except Exception as e:
            if not REVERT.search(str(e)):
                incomplete = True
    if incomplete:
        raise RuntimeError("Wrapper identity lookup incomplete")
    return None


async def verify_pool(pool: str, stocks: list[dict]):
    raw0, raw1 = await asyncio.gather(rpc_call(pool, "0x0dfe1681"), rpc_call(pool, "0xd21220a7"))
    token0 = word_address(raw0)
    token1 = word_address(raw1)
    if not token0 or not token1 or token0 == token1:
        raise RuntimeError("Pool token addresses invalid")
    stock0, stock1 = await asyncio.gather(resolve_stock(token0, stocks), resolve_stock(token1, stocks))
    if bool(stock0) == bool(stock1):
        return {"token0": token0, "token1": token1, "relation": None}
    resolved = stock0 or stock1
    return {
        "token0": token0, "token1": token1,
        "relation": {
            "token": token1 if stock0 else token0,
            "stockSide": token0 if stock0 else token1,
            "stock": resolved["stock"], "wrapper": resolved["wrapped"],
        },
    }


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def scan_pools(s, address: str, stocks: list[dict], block_hex: str):
    data = await okx_get("/api/v6/dex/market/token/top-liquidity", {
        "chainIndex": "196", "tokenContractAddress": address,
    })
    if not isinstance(data, list):
        raise RuntimeError("Invalid pool response")
    scan = {"token": address, "checkedAt": now_ms(), "poolCount": len(data), "status": "ready", "error": None}
    block_int = int(block_hex, 16)
    for pool_row in data:
        pool_addr = (pool_row.get("poolAddress") or "").lower()
        if not re.fullmatch(r"0x[\da-f]{40}", pool_addr, re.I):
            scan["status"] = "partial"
            continue
        try:
            checked = await verify_pool(pool_addr, stocks)
            pool_fact = {
                "pool": pool_addr, "queryToken": address, "checkedAt": now_ms(), "block": block_int,
                "token0": checked["token0"], "token1": checked["token1"],
                "liquidityUsd": _f(pool_row.get("liquidityUsd")),
                "protocol": str(pool_row.get("protocolName") or ""),
                "feePct": _f(str(pool_row.get("liquidityProviderFeePercent") or "").replace("%", "")),
                "amounts": pool_row.get("liquidityAmount") if isinstance(pool_row.get("liquidityAmount"), list) else [],
                "name": str(pool_row.get("pool") or ""),
            }
            await s.put("pool", pool_addr, pool_fact)
            rel = checked.get("relation")
            if not rel:
                continue
            stock_addr = rel["stock"].get("tokenContractAddress", "").lower()
            if address not in (checked["token0"].lower(), checked["token1"].lower(), stock_addr):
                continue
            token = rel["token"].lower()
            asset = await s.get("asset", token)
            if not asset:
                continue
            if asset.get("kind") in ("quote", "stock", "wrapped_stock"):
                continue
            relation_id = f"196:{pool_addr}:{token}:{stock_addr}"
            previous = await s.get("relation", relation_id)
            stock_balance = None
            try:
                raw = await rpc_call(rel["stockSide"], "0x70a08231" + pool_addr[2:].rjust(64, "0"))
                stock_balance = str(int(raw.hex(), 16)) if raw != "0x" else None
            except Exception:
                pass
            relation = {
                "id": relation_id, "chainId": "196", "token": token, "stock": stock_addr,
                "stockSide": rel["stockSide"].lower(), "ticker": rel["stock"].get("stockCode"),
                "pool": pool_addr, "token0": checked["token0"].lower(), "token1": checked["token1"].lower(),
                "wrapper": rel["wrapper"], "protocol": pool_fact["protocol"],
                "firstSeen": (previous or {}).get("firstSeen") or now_ms(), "checkedAt": now_ms(),
                "block": block_int, "liquidityUsd": pool_fact["liquidityUsd"],
                "liquidityAt": pool_fact["checkedAt"], "stockBalance": stock_balance,
                "feePct": pool_fact["feePct"], "amounts": pool_fact["amounts"],
                "status": "verified", "error": None,
            }
            await s.put("relation", relation_id, relation)
            if not previous or previous.get("status") == "invalid":
                await s.put_event(
                    f"{relation_id}:verified:{relation['checkedAt']}", token,
                    {"kind": "verified", "symbol": asset.get("symbol"), "ticker": relation["ticker"],
                     "pool": pool_addr,
                     "label": "包装股票配对已核验" if rel["wrapper"] else "股票直接配对已核验"},
                    relation["checkedAt"],
                )
        except Exception as e:
            if "budget exhausted" in str(e) or "allowance exhausted" in str(e):
                raise
            scan["status"] = "partial"
            await s.put("pool-error", pool_addr, {"pool": pool_addr, "checkedAt": now_ms(), "reason": "池接口、包装关系或元数据未完成核验"})
    await s.put("scan", address, scan)


async def _block_hex() -> str:
    def get():
        return w3().eth.block_number

    num = await asyncio.wait_for(asyncio.to_thread(get), 15)
    return hex(num)


async def refresh_liquidity() -> None:
    """Lightweight freshness lane: recompute liquidityUsd for verified
    relations straight from on-chain reserves — no OKX quota, no identity
    re-verification. Keeps the priority filter populated between scans."""
    s = await store("196")
    now = now_ms()
    stale = [
        r for r in await s.all("relation")
        if r.get("status") == "verified" and now - (r.get("liquidityAt") or 0) > 900_000
    ]
    stale.sort(key=lambda r: r.get("liquidityAt") or 0)
    done = 0
    for rel in stale[:12]:
        try:
            raw = await rpc_call(rel["pool"], "0x0902f1ac")
            hx = raw.hex() if isinstance(raw, bytes) else str(raw)
            if hx == "0x" or len(hx) < 130:
                continue
            r0 = int(hx[2:66], 16)
            r1 = int(hx[66:130], 16)
            if not r0 or not r1:
                continue
            token = rel.get("token", "")
            meme_is0 = (rel.get("token0") or "").lower() == token
            # Price the pool from the meme side when possible; otherwise use
            # the stock side — most pairs have at least one priced leg.
            meme_price = ((await s.get("asset", token)) or {}).get("price")
            stock_price = ((await s.get("asset", rel.get("stock", ""))) or {}).get("price")
            if meme_price:
                dm = await cached_decimals(rel["token0"] if meme_is0 else rel["token1"])
                units = (r0 if meme_is0 else r1) / 10 ** dm
                rel["liquidityUsd"] = units * meme_price * 2
            elif stock_price:
                ds = await cached_decimals(rel["token1"] if meme_is0 else rel["token0"])
                units = (r1 if meme_is0 else r0) / 10 ** ds
                rel["liquidityUsd"] = units * stock_price * 2
            else:
                continue
            rel["liquidityAt"] = now_ms()
            await s.put("relation", rel["id"], rel)
            done += 1
        except Exception:
            continue
    if stale:
        print(f"[liqRefresh] tried={min(len(stale), 12)} refreshed={done}", flush=True)


async def refresh_main_round() -> None:
    s = await store("196")
    stocks = await s.all("stock")
    if not stocks:
        print("[main] no stock catalogue", flush=True)
        return
    block = await _block_hex()

    # Recheck stale relations first so evidence stays fresh.
    relations = await s.all("relation")
    stale = [r for r in relations if (now_ms() - (r.get("checkedAt") or 0)) > 600_000 or (now_ms() - (r.get("liquidityAt") or 0)) > 600_000]
    stale.sort(key=lambda r: r.get("checkedAt") or 0)
    for rel in stale[:16]:
        try:
            checked = await verify_pool(rel["pool"], stocks)
            if not checked.get("relation") or checked["relation"]["token"].lower() != rel.get("token", "").lower():
                rel["status"] = "invalid"
                rel["error"] = "pair token identity changed"
            else:
                rel["status"] = "verified"
                rel["checkedAt"] = now_ms()
                rel["block"] = int(block, 16)
                # Recompute liquidity from on-chain reserves so the priority
                # filter recovers without waiting for OKX quota.
                try:
                    raw = await rpc_call(rel["pool"], "0x0902f1ac")
                    hx = raw.hex() if isinstance(raw, bytes) else str(raw)
                    if hx != "0x" and len(hx) >= 130:
                        r0 = int(hx[2:66], 16)
                        r1 = int(hx[66:130], 16)
                        token = rel.get("token", "")
                        meme_is0 = rel.get("token0", "").lower() == token
                        dm = await cached_decimals(rel["token0"] if meme_is0 else rel["token1"])
                        meme_units = (r0 if meme_is0 else r1) / 10 ** dm
                        asset = await s.get("asset", token)
                        price = (asset or {}).get("price")
                        if price:
                            rel["liquidityUsd"] = meme_units * price * 2
                            rel["liquidityAt"] = now_ms()
                except Exception:
                    pass
            await s.put("relation", rel["id"], rel)
        except Exception:
            continue

    # Scan the oldest-checked candidates.
    assets = [a for a in await s.all("asset") if a.get("kind") == "candidate"]
    scans = {x["token"]: x for x in await s.all("scan")}
    assets.sort(key=lambda a: (scans.get(a.get("token"), {}).get("checkedAt") or 0))
    for asset in assets[:3]:
        try:
            await scan_pools(s, asset["token"], stocks, block)
        except Exception as e:
            if "budget exhausted" in str(e) or "allowance exhausted" in str(e):
                print(f"[main] scan paused: {e}", flush=True)
                break
            print(f"[main] scan {asset.get('token', '')[:10]}: {type(e).__name__}", flush=True)
