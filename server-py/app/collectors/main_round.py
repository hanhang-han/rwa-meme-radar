"""Main collection round (5min): verify pair pools on-chain and persist the
relation facts. Ports the Node refreshXLayer verification core — catalogue
stocks are read from the existing stock facts, pools come from OKX
top-liquidity, identities are resolved through token0/token1 and the
asset()/underlying() wrapper interface."""
import asyncio
import math
import re
import time
from contextvars import ContextVar

from web3 import Web3
from web3.exceptions import ContractLogicError
from web3.middleware import ExtraDataToPOAMiddleware

from ..db import store
from ..demand_leases import all_leases
from ..okx_client import okx_get, request_lane, QuotaExceeded
from ..registry import w3
from ..resource_budget import run_background_io
from ..stream_hub import broadcast
from ..stock_identity import assess_pool_relation, match_name, token_identity
from .assets import now_ms, save_asset
from .queue import checkpoint, combine, due, jobs, result

CHAIN = ContextVar("collector_chain", default="196")
RPC = {"56": "https://bsc.publicnode.com", "4663": "https://rpc.mainnet.chain.robinhood.com"}
_CLIENTS = {}
_RPC_IDENTITIES = set()


def chain_web3():
    cid = CHAIN.get()
    if cid == "196":
        return w3()
    if cid not in _CLIENTS:
        _CLIENTS[cid] = Web3(Web3.HTTPProvider(RPC[cid], request_kwargs={"timeout": 12}))
        if cid == "56":
            # BSC block headers carry PoA validator data longer than Ethereum's
            # 32-byte extraData limit. Normalize it before Web3 validation.
            _CLIENTS[cid].middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return _CLIENTS[cid]


REVERT = re.compile(r"revert|execution|invalid opcode|method unavailable", re.I)


class ContractReverted(RuntimeError):
    """A contract rejected a read, distinct from an unavailable RPC endpoint.

    Keep RuntimeError compatibility for the V2 reserves -> V3 slot0 fallback.
    Custom Solidity errors need this type because their message can be hex only.
    """

    def __init__(self, message, *, address=None, selector=None):
        super().__init__(message)
        self.address = address
        self.selector = selector


class PoolInterfaceUnsupported(RuntimeError):
    """The target does not expose the token0/token1 pool interface we read."""

    def __init__(self, pool, selector=None):
        super().__init__("pool-token-interface-reverted")
        self.pool = pool
        self.selector = selector


def word_address(value) -> str | None:
    raw = abi_bytes(value)
    if len(raw) == 32 and any(raw) and not any(raw[:12]):
        return Web3.to_checksum_address(raw[-20:])
    return None


def abi_bytes(value) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    raw = str(value)
    if raw.startswith("0x"):
        raw = raw[2:]
    if not raw or len(raw) % 2 or not re.fullmatch(r"[\da-f]+", raw, re.I):
        return b""
    try:
        return bytes.fromhex(raw)
    except ValueError:
        return b""


def uint_words(value, count: int) -> tuple[int, ...] | None:
    raw = abi_bytes(value)
    if len(raw) < count * 32:
        return None
    return tuple(int.from_bytes(raw[i * 32:(i + 1) * 32], "big") for i in range(count))


_decimals: dict[str, int] = {}


async def cached_decimals(addr: str) -> int | None:
    cache_key = f"{CHAIN.get()}:{addr.lower()}"
    hit = _decimals.get(cache_key)
    if hit is not None:
        return hit
    try:
        raw = await rpc_call(addr, "0x313ce567")
        words = uint_words(raw, 1)
        if not words or not 0 <= words[0] <= 255:
            return None
        d = words[0]
    except Exception:
        return None
    _decimals[cache_key] = d
    return d


async def rpc_call(to: str, data: str, block="latest"):
    def call():
        return chain_web3().eth.call({"to": Web3.to_checksum_address(to), "data": data}, block_identifier=block)

    try:
        return await asyncio.wait_for(run_background_io(call), 15)
    except ContractLogicError as e:
        raise ContractReverted(str(e)[:120], address=to, selector=data[:10]) from e
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
        except ContractReverted:
            # asset()/underlying() are optional. A deterministic revert is not
            # a transport failure and must not reject an ordinary meme token.
            continue
        except Exception as e:
            if not REVERT.search(str(e)):
                incomplete = True
    if incomplete:
        raise RuntimeError("Wrapper identity lookup incomplete")
    return None


async def verify_pool(pool: str, stocks: list[dict]):
    try:
        raw0, raw1 = await asyncio.gather(rpc_call(pool, "0x0dfe1681"), rpc_call(pool, "0xd21220a7"))
    except ContractReverted as e:
        raise PoolInterfaceUnsupported(pool, e.selector) from e
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


def _pool_block_reason(pool: dict | None, relation: dict | None = None) -> str | None:
    """A liquidity or identity rescan cannot repair revoked chain evidence."""
    pool, relation = pool or {}, relation or {}
    if (pool.get("verificationStatus") == "reorged"
            or relation.get("verificationStatus") == "reorged"):
        return "verification-block-reorg"
    if (pool.get("creationStatus") == "orphaned"
            or relation.get("confirmationStatus") == "orphaned"):
        return "creation-orphaned"
    return None


async def _invalidate_blocked_relation(s, relation_id: str, pool: dict | None) -> bool:
    """Keep a quarantined pool's relation invalid until chain replay repairs it."""
    current = await s.get("relation", relation_id)
    reason = _pool_block_reason(pool, current)
    if not reason:
        return False
    if current:
        patch = {"status": "invalid", "level": None,
                 "pairLiquidityUsd": None, "validUntil": None,
                 "liquidityUsd": None, "liquidityAt": None,
                 "evidenceStatus": reason, "checkedAt": now_ms()}
        if reason == "verification-block-reorg":
            patch["verificationStatus"] = "reorged"
        else:
            patch["confirmationStatus"] = "orphaned"
        await s.patch_fact("relation", relation_id, patch)
    return True


async def emit_relation_event(s, relation: dict, kind: str, label: str, symbol: str | None = None) -> str:
    """Persist and publish one relationship transition under the same ID."""
    at = int(relation.get("checkedAt") or now_ms())
    token = str(relation.get("token") or "").lower()
    event_id = f"{relation['id']}:{kind}:{at}"
    if symbol is None and token:
        symbol = ((await s.get("asset", token)) or {}).get("symbol")
    await s.put_event(
        event_id,
        token,
        {
            "kind": kind,
            "symbol": symbol,
            "ticker": relation.get("ticker"),
            "pool": relation.get("pool"),
            "poolCreatedAt": relation.get("poolCreatedAt"),
            "discoveredAt": relation.get("discoveredAt") or relation.get("firstSeen"),
            "creationTx": relation.get("creationTx"),
            "label": label,
        },
        at,
    )
    canonical_id = s.key(event_id)
    broadcast("relationship", {
        "id": canonical_id,
        "asset": token,
        "t": at,
        "chainId": str(relation.get("chainId") or "196"),
        "kind": kind,
        "relation": relation,
        "label": label,
        "at": at,
    })
    return canonical_id


async def emit_candidate_event(s, asset):
    """A durable pending marker bridges crashes between asset/event writes."""
    if asset.get("historicalDiscovery"):
        if asset.get("discoveryEventPending"):
            await s.patch_fact("asset", asset["token"], {"discoveryEventPending": False})
        return
    if asset.get("kind") != "candidate" or not asset.get("discoveryEventPending"):
        return
    at = int(asset.get("firstSeen") or now_ms())
    token = asset["token"]
    event_id = f"candidate:{token}:{at}"
    match = match_name(asset.get("symbol"), asset.get("name"))
    label = f"发现名称相关候选 · {match['ticker']}" if match else "发现新候选，股票关联待核验"
    event = {"kind": "discovered", "symbol": asset.get("symbol"), "label": label,
             "match": match}
    await s.put_event(event_id, token, event, at)
    broadcast("discovery", {**event, "id": s.key(event_id), "asset": {**asset, "match": match, "chainId": s.scope},
                            "chainId": s.scope, "t": at, "at": at})
    await s.patch_fact("asset", token, {"discoveryEventPending": False, "discoveryEmittedAt": now_ms()})


def _decode_text(raw):
    data = abi_bytes(raw)
    if len(data) == 32:
        return data.rstrip(b"\x00").decode("utf-8", errors="replace")[:128]
    words = uint_words(data, 2)
    if words and words[0] == 32 and 0 < words[1] <= 1024 and len(data) >= 64 + words[1]:
        return data[64:64 + words[1]].decode("utf-8", errors="replace")[:128]
    return None


async def refresh_identity(s, token):
    row = {"chainIndex": CHAIN.get(), "tokenContractAddress": token}
    for key, selector in (("tokenSymbol", "0x95d89b41"), ("tokenName", "0x06fdde03")):
        try:
            value = _decode_text(await rpc_call(token, selector))
            if value:
                row[key] = value
        except Exception:
            pass
    accepted = bool(row.get("tokenSymbol"))
    if accepted:
        await save_asset(s, row)
    await checkpoint(s, "metadata", token, success=accepted, reason=None if accepted else "erc20-metadata-unavailable")
    return accepted


async def scan_pools(s, address: str, stocks: list[dict], block_hex: str):
    data = await okx_get("/api/v6/dex/market/token/top-liquidity", {
        "chainIndex": CHAIN.get(), "tokenContractAddress": address,
    }, {"priority": "background"})
    if not isinstance(data, list):
        raise RuntimeError("Invalid pool response")
    scan = {"token": address, "checkedAt": now_ms(), "poolCount": len(data), "status": "ready", "error": None,
            "verifiedPools": 0, "unsupportedPools": 0, "failedPools": 0}
    block_int = int(block_hex, 16)
    for index, pool_row in enumerate(data):
        pool_addr = str(pool_row.get("poolAddress") or "").lower()
        if not re.fullmatch(r"0x[\da-f]{40}", pool_addr, re.I):
            scan["status"] = "partial"
            scan["unsupportedPools"] += 1
            await s.put("pool-error", pool_addr or f"{address}:row:{index}", {
                "pool": pool_addr, "queryToken": address, "checkedAt": now_ms(),
                "reason": "unsupported-pool-reference", "classification": "unsupported",
                "protocol": str(pool_row.get("protocolName") or ""),
            })
            continue
        try:
            prior_pool = await s.get("pool", pool_addr) or {}
            if reason := _pool_block_reason(prior_pool):
                scan["status"] = "partial"
                scan["unsupportedPools"] += 1
                await s.put("pool-error", pool_addr, {
                    "pool": pool_addr, "queryToken": address, "checkedAt": now_ms(),
                    "reason": reason, "classification": "quarantined",
                })
                continue
            checked = await verify_pool(pool_addr, stocks)
            # A replay can mark the anchor reorged while RPC verification is
            # in flight. Re-read the durable verdict before any write.
            prior_pool = await s.get("pool", pool_addr) or {}
            if reason := _pool_block_reason(prior_pool):
                scan["status"] = "partial"
                scan["unsupportedPools"] += 1
                await s.put("pool-error", pool_addr, {
                    "pool": pool_addr, "queryToken": address, "checkedAt": now_ms(),
                    "reason": reason, "classification": "quarantined",
                })
                continue
            pool_fact = {
                "pool": pool_addr, "queryToken": address, "checkedAt": now_ms(), "block": block_int,
                "token0": checked["token0"], "token1": checked["token1"],
                "liquidityUsd": _f(pool_row.get("liquidityUsd")),
                "protocol": str(pool_row.get("protocolName") or ""),
                "feePct": _f(str(pool_row.get("liquidityProviderFeePercent") or "").replace("%", "")),
                "amounts": pool_row.get("liquidityAmount") if isinstance(pool_row.get("liquidityAmount"), list) else [],
                "name": str(pool_row.get("pool") or ""),
            }
            observed_liquidity = pool_fact["liquidityUsd"]
            # A liquidity scan owns only its observation fields. Factory
            # creation evidence and independently verified historical-pool
            # metadata must survive routine OKX rescans and concurrent writes.
            if prior_pool.get("protocol") and (prior_pool.get("verificationSource")
                                               or prior_pool.get("factoryEventId")
                                               or not pool_fact["protocol"]):
                pool_fact["protocol"] = prior_pool["protocol"]
            for field in ("liquidityUsd", "feePct"):
                if pool_fact[field] is None:
                    del pool_fact[field]
            for field in ("amounts", "name"):
                if not pool_fact[field]:
                    del pool_fact[field]
            pool_fact = await s.patch_fact("pool", pool_addr, pool_fact)
            if reason := _pool_block_reason(pool_fact):
                scan["status"] = "partial"
                scan["unsupportedPools"] += 1
                await s.put("pool-error", pool_addr, {
                    "pool": pool_addr, "queryToken": address, "checkedAt": now_ms(),
                    "reason": reason, "classification": "quarantined",
                })
                continue
            scan["verifiedPools"] += 1
            # A repair candidate must pass explicit Meme classification.
            # Legacy scans without a classification retain their old route.
            if pool_fact.get("classification") and pool_fact["classification"] != "stock-meme":
                continue
            rel = checked.get("relation")
            if not rel:
                continue
            stock_addr = rel["stock"].get("tokenContractAddress", "").lower()
            if address not in (checked["token0"].lower(), checked["token1"].lower(), stock_addr):
                continue
            token = rel["token"].lower()
            asset = await s.get("asset", token)
            if not asset:
                # The stock catalogue's pool can be the first evidence of a
                # candidate. Persist identity immediately, even if metadata is
                # temporarily unavailable, so the baseline quote queue sees it.
                asset = await save_asset(s, {"chainIndex": CHAIN.get(), "tokenContractAddress": token,
                                            "tokenSymbol": token[:8], "tokenName": token})
                await refresh_identity(s, token)
                asset = await s.get("asset", token)
            if not asset:
                continue
            await emit_candidate_event(s, asset)
            if asset.get("kind") in ("quote", "stock", "wrapped_stock"):
                continue
            relation_id = f"{CHAIN.get()}:{pool_addr}:{token}:{stock_addr}"
            previous = await s.get("relation", relation_id)
            if await _invalidate_blocked_relation(s, relation_id, await s.get("pool", pool_addr)):
                continue
            stock_balance = None
            try:
                raw = await rpc_call(rel["stockSide"], "0x70a08231" + pool_addr[2:].rjust(64, "0"))
                stock_balance = str(int(raw.hex(), 16)) if raw != "0x" else None
            except Exception:
                pass
            relation = {
                "id": relation_id, "chainId": CHAIN.get(), "token": token, "stock": stock_addr,
                "stockSide": rel["stockSide"].lower(), "ticker": rel["stock"].get("stockCode"),
                "pool": pool_addr, "token0": checked["token0"].lower(), "token1": checked["token1"].lower(),
                "wrapper": rel["wrapper"], "protocol": pool_fact["protocol"],
                "firstSeen": (previous or {}).get("firstSeen") or now_ms(), "checkedAt": now_ms(),
                "block": block_int, "stockBalance": stock_balance,
                "status": "verified", "error": None,
            }
            if observed_liquidity is not None:
                relation.update({"liquidityUsd": observed_liquidity,
                                 "liquidityAt": pool_fact["checkedAt"]})
            for field in ("feePct", "amounts"):
                if field in pool_fact:
                    relation[field] = pool_fact[field]
            if (previous or {}).get("historicalDiscovery") or (
                    pool_fact.get("verificationSource") == "index-plus-factory-getter"
                    and not pool_fact.get("factoryEventId")):
                relation.update({"historicalDiscovery": True,
                                 "creationAnnouncementPending": False})
            for field in ("poolCreatedAt", "discoveredAt", "creationTx", "creationBlock",
                          "creationBlockHash", "creationLogIndex", "confirmationStatus",
                          "factoryEventId"):
                if field not in (previous or {}) and field in pool_fact:
                    relation[field] = pool_fact[field]
            if "confirmationStatus" not in (previous or {}) and pool_fact.get("creationStatus"):
                relation["confirmationStatus"] = pool_fact["creationStatus"]
            relation.update(assess_pool_relation({**(previous or {}), **relation}, relation["checkedAt"]))
            if await _invalidate_blocked_relation(s, relation_id, await s.get("pool", pool_addr)):
                continue
            relation = await s.patch_fact("relation", relation_id, relation)
            if await _invalidate_blocked_relation(s, relation_id, await s.get("pool", pool_addr)):
                continue
            if (not previous or previous.get("status") == "invalid"
                    or previous.get("level") != relation.get("level")) and not relation.get("historicalDiscovery"):
                qualified = relation.get("level") == "A"
                await emit_relation_event(
                    s,
                    relation,
                    "verified" if qualified else "pair-observed",
                    "官方股票配对池已核验" if qualified else "链上池已核验，股票身份或流动性未达 A 级",
                    asset.get("symbol"),
                )
        except PoolInterfaceUnsupported as e:
            scan["status"] = "partial"
            scan["unsupportedPools"] += 1
            await s.put("pool-error", pool_addr, {
                "pool": pool_addr, "queryToken": address, "checkedAt": now_ms(),
                "reason": "pool-token-interface-reverted", "classification": "unsupported",
                "selector": e.selector, "protocol": str(pool_row.get("protocolName") or ""),
            })
        except Exception as e:
            if "budget exhausted" in str(e) or "allowance exhausted" in str(e):
                raise
            scan["status"] = "partial"
            scan["failedPools"] += 1
            await s.put("pool-error", pool_addr, {
                "pool": pool_addr, "queryToken": address, "checkedAt": now_ms(),
                "reason": "pool-verification-failed", "classification": "failed",
                "errorType": type(e).__name__, "causeType": type(e.__cause__).__name__ if e.__cause__ else None,
            })
    await s.put("scan", address, scan)
    return scan


async def _check_rpc_identity():
    cid = CHAIN.get()
    if cid not in _RPC_IDENTITIES:
        actual = await asyncio.wait_for(run_background_io(lambda: chain_web3().eth.chain_id), 15)
        if str(actual) != cid:
            raise ValueError("RPC chain identity mismatch")
        _RPC_IDENTITIES.add(cid)


async def _block_hex() -> str:
    await _check_rpc_identity()
    def get():
        return chain_web3().eth.block_number

    num = await asyncio.wait_for(run_background_io(get), 15)
    return hex(num)


async def _pool_quote(s, rel, block_number, block_at):
    """Read actual pool state; a wrapper unit is never assumed to be one stock."""
    from ..pool_quotes import pool_ratio
    current = await s.get("relation", rel["id"])
    pool = await s.get("pool", rel["pool"])
    if _pool_block_reason(pool, current or rel) or (current and current.get("status") != "verified"):
        return 'pool-invalidated'
    d0, d1 = await asyncio.gather(cached_decimals(rel["token0"]), cached_decimals(rel["token1"]))
    if d0 is None or d1 is None:
        raise ValueError("token-decimals-unavailable")
    reserves, sqrt_price = None, None
    try:
        reserves = uint_words(await rpc_call(rel["pool"], "0x0902f1ac", block_number), 2)
    except RuntimeError:
        pass
    if not reserves:
        words = uint_words(await rpc_call(rel["pool"], "0x3850c7bd", block_number), 1)
        sqrt_price = words[0] if words else None
    ratio = pool_ratio(rel["token0"], rel["token"], d0, d1, reserves=reserves, sqrt_price_x96=sqrt_price)
    if ratio is None:
        raise ValueError("unsupported-or-empty-pool-state")
    await s.put("pool-quote", rel["pool"], {
        "chainId": CHAIN.get(), "pool": rel["pool"], "stockSide": rel.get("stockSide") or rel["stock"],
        "token": rel["token"], "memePerStock": ratio, "at": block_at, "block": block_number,
        "timeKind": "market", "method": "v2-reserves" if reserves else "v3-slot0",
        "decimals0": d0, "decimals1": d1,
    })
    now = now_ms()
    meme_is0 = rel["token0"].lower() == rel["token"].lower()
    meme_asset = await s.get("asset", rel["token"]) or {}
    side_address = str(rel.get("stockSide") or rel["stock"]).lower()
    side_asset = await s.get("asset", side_address) or {}
    meme_at = (meme_asset.get("fieldTimes") or {}).get("price") or 0
    side_at = (side_asset.get("fieldTimes") or {}).get("price") or 0
    def usd_price(asset, at):
        value = asset.get('price')
        currency = asset.get('priceCurrency') or ((asset.get('fieldObservations') or {}).get('price') or {}).get('currency')
        return value if (currency == 'USD' and isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value) and value > 0
            and isinstance(at, (int, float)) and 0 < at <= now and now-at <= 900_000) else None
    meme_price = usd_price(meme_asset, meme_at)
    side_price = usd_price(side_asset, side_at)
    valuation_method = "reserves_valuation"
    if not side_price and side_address != str(rel["stock"]).lower():
        underlying = await s.get("asset", rel["stock"]) or {}
        underlying_at = (underlying.get("fieldTimes") or {}).get("price") or 0
        underlying_price = usd_price(underlying, underlying_at)
        if underlying_price:
            try:
                side_decimals = d1 if meme_is0 else d0
                underlying_decimals = await cached_decimals(rel["stock"])
                encoded = hex(10 ** side_decimals)[2:].rjust(64, "0")
                converted = uint_words(await rpc_call(side_address, "0x07a2d13a" + encoded, block_number), 1)
                if converted and converted[0] > 0 and underlying_decimals is not None:
                    factor = converted[0] / 10 ** underlying_decimals
                    side_price, side_at = underlying_price * factor, underlying_at
                    valuation_method = "erc4626_reserves_valuation"
                    await s.put("wrapper-ratio", side_address, {"underlying": rel["stock"], "factor": factor,
                                "at": block_at, "block": block_number, "method": "convertToAssets"})
            except Exception:
                pass
    if not reserves:
        # V3 balances measure pool TVL, not executable depth. Anchor to a
        # fresh independent USD quote and price the other side at this pool's
        # block-pinned marginal exchange rate, as for V2 reserve valuation.
        if not meme_price and not side_price:
            return 'usd-price-unavailable'
        derived = False
        if not side_price:
            side_price, side_at = meme_price*ratio, meme_at
            derived = True
        elif not meme_price:
            meme_price, meme_at = side_price/ratio, side_at
            derived = True
        calldata = '0x70a08231' + rel['pool'][2:].rjust(64, '0')
        balances = await asyncio.gather(*(rpc_call(token, calldata, block_number)
                                         for token in (rel['token0'], rel['token1'])))
        amounts = [uint_words(raw, 1) for raw in balances]
        if any(not amount for amount in amounts):
            raise ValueError('v3-balances-unavailable')
        units0, units1 = amounts[0][0]/10**d0, amounts[1][0]/10**d1
        price0, price1 = (meme_price, side_price) if meme_is0 else (side_price, meme_price)
        value, price_at = units0*price0 + units1*price1, min(meme_at, side_at)
        valuation_method = 'v3_balances_tvl_spot_usd' if derived else 'v3_balances_tvl_usd'
    elif not all(reserves):
        return 'empty-pool-reserves'
    elif meme_price:
        units = (reserves[0] if meme_is0 else reserves[1]) / 10 ** (d0 if meme_is0 else d1)
        value, price_at = units * meme_price * 2, meme_at
    elif side_price:
        units = (reserves[1] if meme_is0 else reserves[0]) / 10 ** (d1 if meme_is0 else d0)
        value, price_at = units * side_price * 2, side_at
    else:
        return 'usd-price-unavailable'
    if not math.isfinite(value) or not 0 <= value < 1e10:
        return 'invalid-usd-valuation'
    # Identity verification and valuation run independently; patch only the
    # valuation fields so a concurrent verification cannot be rolled back.
    valuation = {"liquidityUsd": value, "reservesAt": block_at,
        "priceAt": price_at, "valuationAt": now, "liquidityAt": min(block_at, price_at),
        "liquidityMethod": valuation_method}
    valuation.update(assess_pool_relation({**rel, **valuation}, now))
    current = await s.get("relation", rel["id"])
    if (_pool_block_reason(await s.get("pool", rel["pool"]), current or rel)
            or (current and current.get("status") != "verified")):
        return 'pool-invalidated'
    await s.patch_fact("relation", rel["id"], valuation)
    return 'valued'


async def refresh_liquidity():
    totals, now = result(), now_ms()
    work = []
    for cid in ("196", "56", "4663"):
        s = await store(cid)
        queue = await jobs(s, "pool-quote")
        watched = {w.get("token") for w in await all_leases(s, "watch") if (w.get("expiresAt") or 0) > now}
        for rel in await s.all("relation"):
            job = queue.get(rel.get("pool"), {})
            interval = 30_000 if rel.get("token") in watched or rel.get("stock") in watched else 240_000
            if (rel.get("status") == "verified"
                    and rel.get("verificationStatus") != "reorged"
                    and rel.get("confirmationStatus") != "orphaned"
                    and due(job, now, interval)):
                side = rel.get('stockSide') or rel.get('stock')
                if not token_identity(cid, side)['eligibleForPair']:
                    totals['skipped'] += 1
                    continue
                hot = rel.get('token') in watched or rel.get('stock') in watched or rel.get('level') == 'A'
                work.append((job.get("lastAttemptAt") or 0, cid, rel, hot))
    work.sort(key=lambda item: (item[0], item[1], item[2]["pool"]))
    blocks = {}
    # Reserve cold slots so successful markets cannot permanently starve
    # unseen pools, while watched/current pools get a shorter refresh cycle.
    hot = [item for item in work if item[3]]
    cold = [item for item in work if not item[3]]
    selected = hot[:8] + cold[:4]
    selected += [item for item in work if item not in selected][:12-len(selected)]
    for _, cid, rel, _ in selected:
        handle = CHAIN.set(cid)
        s = await store(cid)
        totals["requested"] += 1
        try:
            if cid not in blocks:
                await _check_rpc_identity()
                blocks[cid] = await asyncio.wait_for(run_background_io(lambda: chain_web3().eth.get_block("latest")), 15)
            block = blocks[cid]
            outcome = await _pool_quote(s, rel, int(block["number"]), int(block["timestamp"]) * 1000)
            valued = outcome == 'valued'
            await checkpoint(s, "pool-quote", rel["pool"], success=valued,
                             reason=None if valued else outcome or 'valuation-unavailable')
            totals['accepted' if valued else 'failed'] += 1
            totals['updated'] += int(valued)
        except Exception as error:
            await checkpoint(s, "pool-quote", rel["pool"], success=False, reason=type(error).__name__)
            totals["failed"] += 1
        finally:
            CHAIN.reset(handle)
    return totals


async def refresh_main_round():
    """RPC-only identity maintenance. Discovery owns upstream pool requests."""
    totals, now = result(), now_ms()
    for cid in ("196", "56", "4663"):
        handle = CHAIN.set(cid)
        s = await store(cid)
        try:
            pending_events = [a for a in await s.all("asset") if a.get("discoveryEventPending")]
            for asset in pending_events[:50]:
                await emit_candidate_event(s, asset)
            stocks = await s.all("stock")
            if not stocks:
                totals["skipped"] += 1
                continue
            metadata_jobs = await jobs(s, "metadata")
            pending_metadata = sorted((j for j in metadata_jobs.values() if not j.get("lastSuccessAt") and due(j, now)),
                                      key=lambda j: j.get("lastAttemptAt") or 0)
            for job in pending_metadata[:2]:
                totals["requested"] += 1
                accepted = await refresh_identity(s, job["key"])
                totals["accepted"] += int(accepted)
                totals["unsupported"] += int(not accepted)
            queue = await jobs(s, "verify")
            stale = [r for r in await s.all("relation") if due(queue.get(r["id"], {}), now, 600_000)]
            stale.sort(key=lambda r: (queue.get(r["id"], {}).get("lastAttemptAt") or 0))
            if not stale:
                continue
            block = await _block_hex()
            for rel in stale[:16]:
                totals["requested"] += 1
                try:
                    if await _invalidate_blocked_relation(s, rel["id"], await s.get("pool", rel["pool"])):
                        await checkpoint(s, "verify", rel["id"], success=True)
                        totals["skipped"] += 1
                        continue
                    checked = await verify_pool(rel["pool"], stocks)
                    if await _invalidate_blocked_relation(s, rel["id"], await s.get("pool", rel["pool"])):
                        await checkpoint(s, "verify", rel["id"], success=True)
                        totals["skipped"] += 1
                        continue
                    actual = checked.get("relation")
                    valid = bool(actual and actual["token"].lower() == rel["token"].lower()
                                 and actual["stock"]["tokenContractAddress"].lower() == rel["stock"].lower()
                                 and actual["stockSide"].lower() == str(rel.get("stockSide") or rel["stock"]).lower()
                                 and checked["token0"].lower() == rel["token0"].lower()
                                 and checked["token1"].lower() == rel["token1"].lower())
                    status = "verified" if valid else "invalid"
                    patch = {"status": status, "error": None if valid else "pair token identity changed",
                             "checkedAt": now_ms(), "block": int(block, 16)}
                    patch.update(assess_pool_relation({**rel, **patch}, patch["checkedAt"]))
                    if await _invalidate_blocked_relation(s, rel["id"], await s.get("pool", rel["pool"])):
                        await checkpoint(s, "verify", rel["id"], success=True)
                        totals["skipped"] += 1
                        continue
                    updated = await s.patch_fact("relation", rel["id"], patch)
                    if await _invalidate_blocked_relation(s, rel["id"], await s.get("pool", rel["pool"])):
                        await checkpoint(s, "verify", rel["id"], success=True)
                        totals["skipped"] += 1
                        continue
                    await checkpoint(s, "verify", rel["id"], success=True)
                    totals["accepted"] += 1
                    totals["updated"] += 1
                    if (status != rel.get("status") or patch.get("level") != rel.get("level")) and not updated.get("historicalDiscovery"):
                        qualified = patch.get("level") == "A"
                        await emit_relation_event(s, updated, "verified" if qualified else "invalidated" if not valid else "pair-observed",
                                                  "官方股票配对池已核验" if qualified else "配对池证据或资格变化，标记待核验")
                except Exception as error:
                    await checkpoint(s, "verify", rel["id"], success=False, reason=type(error).__name__)
                    totals["failed"] += 1
        except Exception as error:
            totals["failed"] += 1
            totals["error"] = type(error).__name__
        finally:
            CHAIN.reset(handle)
    return totals


async def refresh_discovery():
    """5m hot lists + two fair catalogue/candidate scans, <= 1440 calls/day.

    Node's six-hour catalogue sync shares the remaining discovery allowance.
    Queue state survives restarts and failed/unlisted assets get backoff.
    """
    totals, now = result(), now_ms()
    # Reserve enough daily background capacity for both quote baselines.
    # Recent discovery usage is far below this protected 1,544-call ceiling.
    with request_lane("discovery", 1544):
        for cid in ("196", "56", "4663"):
            s = await store(cid)
            hot_job = await s.get("collector-job", "hot:all") or {}
            if not due(hot_job, now, 300_000):
                continue
            totals["requested"] += 1
            try:
                data = await okx_get("/api/v6/dex/market/token/hot-token", {
                    "chainIndex": cid, "rankingType": "4", "rankingTimeFrame": "4", "limit": "40",
                }, {"skip_round": True, "priority": "background"})
                rows = data if isinstance(data, list) else data.get("list") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise ValueError("Invalid discovery schema")
                accepted = 0
                for row in rows:
                    if str(row.get("chainIndex")) != cid:
                        continue
                    asset = await save_asset(s, row)
                    if asset:
                        await emit_candidate_event(s, asset)
                        accepted += 1
                await checkpoint(s, "hot", "all", success=accepted > 0, reason=None if accepted else "empty-result")
                totals["accepted"] += accepted
                totals["updated"] += accepted
                if not accepted:
                    totals["unsupported"] += 1
            except QuotaExceeded:
                totals["quotaBlocked"] += 1
                return totals
            except Exception as error:
                totals["failed"] += 1
                await checkpoint(s, "hot", "all", success=False, reason=type(error).__name__)
        # Separate stock and candidate slots protect catalogue discovery from
        # a large candidate set, and vice versa. Globally oldest attempt wins.
        cursor_store = await store("196")
        cursor = await cursor_store.get("collector-cursor", "discovery") or {}
        chain_order = ("196", "56", "4663")
        for kind in ("stock", "candidate"):
            work = []
            for cid in ("196", "56", "4663"):
                s = await store(cid)
                queue = await jobs(s, "scan")
                historical = {r.get("token"): r for r in await s.all("scan")}
                for asset in await s.all("asset"):
                    token = asset.get("token")
                    old_scan = historical.get(token, {})
                    job = queue.get(token, {"lastAttemptAt": old_scan.get("checkedAt"), "lastSuccessAt": old_scan.get("checkedAt") if old_scan.get("status") == "ready" else None})
                    if asset.get("kind") == kind and due(job, now, 3_600_000):
                        work.append((job.get("lastAttemptAt") or 0, -(asset.get("volume24h") or 0), cid, token))
            # A persistent chain turn prevents an unscanned XLayer catalogue
            # from pushing BSC and Robinhood behind several days of work.
            offset = int(cursor.get(kind, 0)) % len(chain_order)
            priorities = {chain_order[(offset + i) % 3]: i for i in range(3)}
            work.sort(key=lambda item: (priorities[item[2]], item[0], item[1], item[3]))
            if not work:
                continue
            _, _, cid, address = work[0]
            cursor[kind] = (chain_order.index(cid) + 1) % 3
            await cursor_store.put("collector-cursor", "discovery", cursor)
            s = await store(cid)
            handle = CHAIN.set(cid)
            totals["requested"] += 1
            try:
                scan = await scan_pools(s, address, await s.all("stock"), await _block_hex())
                success, unsupported = discovery_scan_quality(scan)
                await checkpoint(s, "scan", address, success=success,
                                 reason="unsupported-pool-interface" if success and unsupported else
                                        None if success else "partial-pool-verification")
                totals["accepted"] += int(success)
                totals["failed"] += int(not success)
                totals["skipped"] += unsupported
                totals["scannedPools"] = totals.get("scannedPools", 0) + scan.get("poolCount", 0)
            except QuotaExceeded:
                totals["quotaBlocked"] += 1
                return totals
            except Exception as error:
                totals["failed"] += 1
                await checkpoint(s, "scan", address, success=False, reason=type(error).__name__)
            finally:
                CHAIN.reset(handle)
    return totals


def discovery_scan_quality(scan):
    """Unsupported pool interfaces are coverage gaps, not provider failures.

    A scan with transport/verification failures remains retryable and marks
    the task partial. The persisted scan and pool-error facts retain both
    reasons regardless of the aggregate task status.
    """
    unsupported = int(scan.get("unsupportedPools") or 0)
    failed = int(scan.get("failedPools") or 0)
    return scan.get("status") in ("ready", "partial") and failed == 0, unsupported
