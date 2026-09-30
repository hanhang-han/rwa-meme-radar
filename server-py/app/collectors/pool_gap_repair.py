"""Bounded repair of stock pools found by an external index but absent locally.

The index only supplies candidates. Registration requires independent on-chain
proof against the pinned BNB factories. Historical discovery is never treated
as a new creation event, and index liquidity is never promoted to USD evidence.
"""
from __future__ import annotations

import re
import math
import time
from collections.abc import Awaitable, Callable

from ..stock_identity import assess_pool_relation, manifest_status, token_identity
from .factory_discovery import (
    BSC_FACTORIES, FACTORY_SELECTOR, GET_PAIR_SELECTOR, GET_POOL_SELECTOR,
    QUOTE_SYMBOLS, _abi_text, _address_word, _word_address,
)


Rpc = Callable[[str, list], Awaitable[object]]
CHAIN = "56"
KIND = "discovery-pool-gap"
POOL_CODE = re.compile(r"0x(?:[0-9a-fA-F]{2}){16,}\Z")
HEX_WORD = re.compile(r"0x[0-9a-fA-F]{64}\Z")
ADDRESS = re.compile(r"0x[0-9a-f]{40}\Z")
KNOWN_QUOTE_TOKENS = frozenset({
    "0x55d398326f99059ff775485246999027b3197955",  # USDT
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
})
RETRY_BASE_MS = 60_000
RETRY_MAX_MS = 6 * 3600_000
MAX_RETRY_ATTEMPTS = 8
CONFIRMATIONS = 6
CLASSIFICATION_RETRY_MS = 300_000


class QuarantinedPool(ValueError):
    """Deterministically failed chain/contract proof; never register it."""


class CatalogueNotReady(RuntimeError):
    """The issuer manifest or stock catalogue is temporarily unavailable."""


class RetryableVerification(RuntimeError):
    """A temporarily incomplete chain proof must not register a candidate."""


def candidate_id(row: dict) -> str:
    """Keep a provider side change separate from the original pool claim."""
    return ":".join((row["pool"], *sorted((row["token0"], row["token1"]))))


async def queue_indexed_gap(system, row: dict, *, seen_at: int) -> bool:
    """Durably enqueue a valid official-side index observation once."""
    official = [side for side in (row["token0"], row["token1"])
                if token_identity(CHAIN, side)["eligibleForPair"]]
    if len(official) != 1 or official[0] != row["stockSide"]:
        return False
    ident = candidate_id(row)
    old = await system.get(KIND, ident)
    if old:
        await system.patch_fact(KIND, ident, {"lastIndexedAt": seen_at,
                         "indexedLiquidityUsd": row.get("liquidityUsd")})
        return False
    await system.patch_fact(KIND, ident, {
        "id": ident, "chainId": CHAIN, "pool": row["pool"],
        "token0": row["token0"], "token1": row["token1"],
        "stockSide": official[0], "indexProvider": "DexScreener",
        "indexDexId": row.get("dexId"),
        "indexedLiquidityUsd": row.get("liquidityUsd"),
        "firstIndexedAt": seen_at, "lastIndexedAt": seen_at,
        "status": "pending", "attempts": 0, "nextRetryAt": 0,
    })
    return True


def _uint_word(raw: object) -> int:
    if not isinstance(raw, str) or not HEX_WORD.fullmatch(raw):
        raise QuarantinedPool("invalid-uint-word")
    return int(raw, 16)


async def _call(rpc: Rpc, to: str, data: str, block: str) -> object:
    try:
        return await rpc("eth_call", [{"to": to, "data": data}, block])
    except Exception as exc:
        # A deterministic contract revert is not a provider outage. Keep
        # network timeouts and rate-limit errors retriable.
        detail = str(exc).lower()
        if any(marker in detail for marker in ("execution reverted", "invalid opcode", "function selector")):
            raise QuarantinedPool("contract-interface-reverted") from exc
        raise


async def _metadata(rpc: Rpc, token: str, block: str) -> dict[str, str]:
    values = {}
    for field, selector in (("symbol", "0x95d89b41"), ("name", "0x06fdde03")):
        try:
            value = _abi_text(await _call(rpc, token, selector, block))
            if value:
                values[field] = value
        except Exception:
            # ERC-20 metadata is optional and is not identity evidence.
            pass
    return values


async def verify_indexed_pool(rpc: Rpc, row: dict) -> dict:
    """Require code, exact sides, pinned factory, and canonical factory getter."""
    if str(row.get("chainId")) != CHAIN:
        raise QuarantinedPool("wrong-index-chain")
    pool, stock_side = row["pool"], row["stockSide"]
    expected = (row["token0"], row["token1"])
    if (any(not isinstance(value, str) or not ADDRESS.fullmatch(value)
            for value in (pool, stock_side, *expected))
            or stock_side not in expected or expected[0] == expected[1]):
        raise QuarantinedPool("invalid-index-sides")
    if manifest_status()["status"] != "ready":
        raise CatalogueNotReady("official-manifest-unavailable")
    official = [(side, identity) for side in expected
                if (identity := token_identity(CHAIN, side))["eligibleForPair"]]
    if len(official) != 1 or official[0][0] != stock_side:
        raise QuarantinedPool("no-single-official-stock-side")
    chain_id = await rpc("eth_chainId", [])
    try:
        chain_number = int(chain_id, 16) if isinstance(chain_id, str) and chain_id.startswith("0x") else int(chain_id)
    except (ValueError, TypeError) as exc:
        raise QuarantinedPool("invalid-rpc-chain-id") from exc
    if chain_number != 56:
        raise QuarantinedPool("rpc-chain-mismatch")
    # Pin all reads to one confirmed canonical block. Its predecessor is the
    # durable replay floor before the pool reaches the WebSocket filter.
    tip = await rpc("eth_getBlockByNumber", ["latest", False])
    try:
        tip_number = int(tip["number"], 16) if str(tip["number"]).startswith("0x") else int(tip["number"])
    except (TypeError, KeyError, ValueError) as exc:
        raise RetryableVerification("verified-head-unavailable") from exc
    if tip_number < CONFIRMATIONS + 1:
        raise RetryableVerification("confirmed-head-unavailable")
    verification_block = tip_number - CONFIRMATIONS
    block = hex(verification_block)
    header = await rpc("eth_getBlockByNumber", [block, False])
    if not isinstance(header, dict) or not isinstance(header.get("hash"), str) or not HEX_WORD.fullmatch(header["hash"]):
        raise RetryableVerification("confirmed-header-unavailable")
    try:
        anchored_number = int(header["number"], 16) if str(header["number"]).startswith("0x") else int(header["number"])
    except (TypeError, KeyError, ValueError) as exc:
        raise RetryableVerification("confirmed-header-unavailable") from exc
    if anchored_number != verification_block:
        raise RetryableVerification("confirmed-header-mismatch")
    block_hash = header["hash"].lower()
    for address in (pool, *expected):
        code = await rpc("eth_getCode", [address, block])
        if not isinstance(code, str) or not POOL_CODE.fullmatch(code):
            raise RetryableVerification("contract-code-unavailable")
    try:
        token0 = _word_address(await _call(rpc, pool, "0x0dfe1681", block))
        token1 = _word_address(await _call(rpc, pool, "0xd21220a7", block))
        factory_address = _word_address(await _call(rpc, pool, FACTORY_SELECTOR, block))
    except ValueError as exc:
        raise QuarantinedPool("pool-interface-invalid") from exc
    if {token0, token1} != set(expected) or int(token0, 16) >= int(token1, 16):
        raise QuarantinedPool("pool-sides-mismatch")
    factory = next((factory for factory in BSC_FACTORIES if factory.address == factory_address), None)
    if factory is None:
        raise QuarantinedPool("unsupported-factory")
    factory_code = await rpc("eth_getCode", [factory.address, block])
    if not isinstance(factory_code, str) or not POOL_CODE.fullmatch(factory_code):
        raise RetryableVerification("factory-code-unavailable")
    calldata = (GET_POOL_SELECTOR if factory.kind == "uniswap_v3" else GET_PAIR_SELECTOR)
    calldata += _address_word(token0) + _address_word(token1)
    fee = None
    if factory.kind == "uniswap_v3":
        fee = _uint_word(await _call(rpc, pool, "0xddca3f43", block))
        if not 0 < fee < 1_000_000:
            raise QuarantinedPool("invalid-v3-fee")
        calldata += f"{fee:064x}"
    try:
        canonical_pool = _word_address(await _call(rpc, factory.address, calldata, block))
    except ValueError as exc:
        raise QuarantinedPool("factory-getter-invalid") from exc
    if canonical_pool != pool:
        raise QuarantinedPool("factory-getter-mismatch")
    counter_token = token1 if token0 == stock_side else token0
    metadata = await _metadata(rpc, counter_token, block)
    after = await rpc("eth_getBlockByNumber", [block, False])
    if (not isinstance(after, dict) or str(after.get("hash") or "").lower() != block_hash
            or str(after.get("number") or "").lower() != str(header["number"]).lower()):
        raise RetryableVerification("verification-block-reorganized")
    return {"pool": pool, "token0": token0, "token1": token1,
            "stockSide": stock_side, "counterToken": counter_token,
            "factory": factory.address, "dex": factory.kind,
            "protocol": factory.label, "fee": fee,
            "metadata": metadata,
            "verificationBlock": verification_block,
            "verificationBlockHash": block_hash,
            "streamBackfillFromBlock": verification_block - 1}


async def _catalogue_stock(chain, stock_side: str) -> dict | None:
    identity = token_identity(CHAIN, stock_side)
    stocks = await chain.all("stock")
    options = [stock for stock in stocks
               if token_identity(CHAIN, stock.get("tokenContractAddress"))["underlyingId"] == identity["underlyingId"]]
    return next((stock for stock in options
                 if str(stock.get("tokenContractAddress") or "").lower() == stock_side),
                next((stock for stock in options
                      if token_identity(CHAIN, stock.get("tokenContractAddress"))["tokenKind"] == "native"),
                     options[0] if options else None))


def _independent_market_evidence(asset: dict | None) -> bool:
    if not isinstance(asset, dict):
        return False
    try:
        price = float(asset.get("price"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(price) or price <= 0:
        return False
    observation = (asset.get("fieldObservations") or {}).get("price") or {}
    provider = str(observation.get("provider") or (asset.get("fieldSources") or {}).get("price") or "")
    return provider.lower() in {"okx", "binance", "robinhood"}


async def register_verified_pool(chain, indexed: dict, proof: dict, *, now_ms: int) -> str:
    """Feed only verified pool/asset/relation facts into existing collectors."""
    pool = proof["pool"]
    previous_pool = await chain.get("pool", pool)
    if previous_pool:
        old_sides = (str(previous_pool.get("token0") or "").lower(),
                     str(previous_pool.get("token1") or "").lower())
        if set(old_sides) != {proof["token0"], proof["token1"]}:
            raise QuarantinedPool("local-pool-conflict")
        if previous_pool.get("creationStatus") == "orphaned":
            raise QuarantinedPool("orphaned-creation-requires-factory-event")
    stock = await _catalogue_stock(chain, proof["stockSide"])
    if not stock:
        raise CatalogueNotReady("official-stock-catalogue-missing")
    counter = proof["counterToken"]
    asset = await chain.get("asset", counter)
    # A ticker string can be spoofed. An unknown counter-token remains an
    # isolated candidate; only an already classified site asset can form a
    # stock/Meme relation. Known quote contracts never become Meme relations.
    independent = _independent_market_evidence(asset)
    quote_named = str(proof["metadata"].get("symbol") or "").upper() in QUOTE_SYMBOLS
    if counter in KNOWN_QUOTE_TOKENS:
        classification = "stock-quote"
    elif quote_named:
        classification = "stock-unclassified"
    elif asset and asset.get("kind") in ("candidate", "meme") and independent:
        classification = "stock-meme"
    else:
        classification = "stock-unclassified"
    stock_address = str(stock["tokenContractAddress"]).lower()
    relation_id = f"{CHAIN}:{pool}:{counter}:{stock_address}"
    previous = await chain.get("relation", relation_id) or {}
    if previous.get("confirmationStatus") == "orphaned":
        raise QuarantinedPool("orphaned-relation-requires-factory-event")
    await chain.patch_fact("pool", pool, {
        "pool": pool, "token0": proof["token0"], "token1": proof["token1"],
        "queryToken": counter if classification == "stock-meme" else proof["stockSide"],
        "protocol": proof["protocol"], "factory": proof["factory"],
        "fee": proof["fee"], "classification": classification,
        "indexedLiquidityUsd": indexed.get("indexedLiquidityUsd"),
        "verifiedAt": now_ms, "verificationSource": "index-plus-factory-getter",
        "verificationStatus": "verified", "gapCandidateId": indexed.get("id"),
        "verificationBlock": proof["verificationBlock"],
        "verificationBlockHash": proof["verificationBlockHash"],
        "streamBackfillFromBlock": proof["streamBackfillFromBlock"],
        "checkedAt": now_ms,
    })
    if classification != "stock-meme":
        if classification == "stock-unclassified" and not asset:
            await chain.patch_fact("asset", counter, {
                "token": counter, "chain": CHAIN, "chainId": CHAIN,
                "kind": "candidate", "firstSeen": now_ms,
                "symbol": proof["metadata"].get("symbol") or counter[:8],
                "name": proof["metadata"].get("name") or proof["metadata"].get("symbol") or counter,
                "discoveryEventPending": False, "historicalDiscovery": True,
                "classificationStatus": "unverified",
            })
        return classification
    if not asset:
        await chain.patch_fact("asset", counter, {
            "token": counter, "chain": CHAIN, "chainId": CHAIN,
            "kind": "candidate", "firstSeen": now_ms,
            "symbol": proof["metadata"].get("symbol") or counter[:8],
            "name": proof["metadata"].get("name") or proof["metadata"].get("symbol") or counter,
            "discoveryEventPending": False, "historicalDiscovery": True,
        })
    relation = {**previous,
        "id": relation_id, "chainId": CHAIN, "pool": pool,
        "token": counter, "stock": stock_address,
        "stockSide": proof["stockSide"],
        "ticker": token_identity(CHAIN, stock_address).get("ticker") or stock.get("stockCode"),
        "token0": proof["token0"], "token1": proof["token1"],
        "wrapper": stock_address != proof["stockSide"],
        "protocol": proof["protocol"], "factory": proof["factory"],
        "firstSeen": previous.get("firstSeen") or now_ms,
        "checkedAt": now_ms, "status": "verified", "error": None,
        "historicalDiscovery": True, "creationAnnouncementPending": False,
    }
    # Indexed liquidity is not trusted for A-grade qualification; a separate
    # on-chain reserve/quote round must first establish current USD value.
    if (not previous or previous.get("verificationStatus") == "reorged"
            or previous.get("evidenceStatus") == "verification-block-reorg"):
        relation["liquidityUsd"] = None
        relation["liquidityAt"] = None
    relation["verificationStatus"] = "verified"
    relation.update(assess_pool_relation(relation, now_ms))
    await chain.patch_fact("relation", relation_id, relation)
    return classification


class PoolGapRepair:
    def __init__(self, chain, system, rpc: Rpc, *, clock=None):
        if str(chain.scope) != CHAIN or str(system.scope) != "system":
            raise ValueError("wrong-store-scope")
        self.chain, self.system, self.rpc = chain, system, rpc
        self.clock = clock or (lambda: int(time.time() * 1000))

    async def run_once(self, *, limit=2) -> dict:
        if not 1 <= limit <= 4:
            raise ValueError("invalid-repair-limit")
        now = self.clock()
        pending = [row for row in await self.system.all(KIND)
                   if row.get("status") in ("pending", "retry", "awaiting-classification")
                   and (row.get("nextRetryAt") or 0) <= now]
        pending.sort(key=lambda row: (row.get("status") == "awaiting-classification",
                                      -(row.get("indexedLiquidityUsd") or 0), row["id"]))
        result = {"requested": min(len(pending), limit), "accepted": 0,
                  "processed": 0, "updated": 0, "failed": 0,
                  "unsupported": 0, "skipped": 0}
        for row in pending[:limit]:
            ident = row["id"]
            try:
                if row.get("status") == "awaiting-classification":
                    asset = await self.chain.get("asset", row.get("counterToken") or "")
                    if (row.get("counterToken") not in KNOWN_QUOTE_TOKENS
                            and not _independent_market_evidence(asset)):
                        await self.system.patch_fact(KIND, ident, {
                            "nextRetryAt": self.clock() + CLASSIFICATION_RETRY_MS,
                            "checkedAt": self.clock(),
                        })
                        result["processed"] += 1
                        continue
                proof = await verify_indexed_pool(self.rpc, row)
                classification = await register_verified_pool(self.chain, row, proof, now_ms=self.clock())
                quote_named = (str(proof["metadata"].get("symbol") or "").upper() in QUOTE_SYMBOLS
                               and proof["counterToken"] not in KNOWN_QUOTE_TOKENS)
                await self.system.patch_fact(KIND, ident, {
                    "status": ("needs-review" if classification == "stock-unclassified" and quote_named
                               else "awaiting-classification" if classification == "stock-unclassified"
                               else "registered"),
                    "classification": classification,
                    "counterToken": proof["counterToken"],
                    "counterSymbol": proof["metadata"].get("symbol"),
                    "verifiedAt": self.clock(),
                    "nextRetryAt": self.clock() + CLASSIFICATION_RETRY_MS if classification == "stock-unclassified" else 0,
                    "reason": None,
                })
                result["accepted"] += 1
                result["processed"] += 1
                result["updated"] += 1
            except QuarantinedPool as exc:
                await self.system.patch_fact(KIND, ident, {
                    "status": "quarantined", "classification": "unsupported",
                    "checkedAt": self.clock(), "reason": str(exc)[:96],
                })
                result["unsupported"] += 1
                result["processed"] += 1
            except Exception as exc:
                # Transient RPC/catalogue errors retain the candidate and a
                # bounded retry; a restart never loses the unfinished row.
                attempts = min(MAX_RETRY_ATTEMPTS, int(row.get("attempts") or 0) + 1)
                wait = min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** min(attempts - 1, 9))
                await self.system.patch_fact(KIND, ident, {
                    "status": "needs-review" if attempts >= MAX_RETRY_ATTEMPTS else "retry",
                    "attempts": attempts, "nextRetryAt": self.clock() + wait if attempts < MAX_RETRY_ATTEMPTS else 0,
                    "checkedAt": self.clock(), "reason": type(exc).__name__,
                })
                result["failed"] += 1
                result["processed"] += 1
        # Quarantining an unsupported pool completes its review, even though
        # it cannot create an accepted market observation.
        if result["processed"] and not (result["accepted"] or result["failed"]):
            result["noChange"] = True
        return result
