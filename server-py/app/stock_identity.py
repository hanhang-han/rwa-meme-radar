"""Pinned issuer deployment identities and conservative A/B relation rules.

The xStocks API is sampled when preparing a release, never during a user
request or a collection round. An OKX/RH catalogue row or an ERC-4626
``asset()`` response alone is not issuer authorisation of a contract.
"""
from __future__ import annotations

import json
import math
import re
import time
from functools import lru_cache
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parents[2] / "src" / "catalogues"
MANIFEST = DATA_DIR / "official-stock-tokens.v1.json"
KEYWORDS = DATA_DIR / "stock-keywords.v1.json"
ADDRESS = re.compile(r"0x[0-9a-f]{40}\Z", re.I)
RULE_VERSION = "official-pool-a-b-v1"
MIN_PAIR_LIQUIDITY_USD = 1_000
MAX_LIQUIDITY_AGE_MS = 900_000
GENERAL_TERMS = frozenset({"BTC", "BITCOIN", "比特币", "ETH", "ETHEREUM", "AI", "CRYPTO"})


def _address(value) -> str | None:
    text = str(value or "").lower()
    return text if ADDRESS.fullmatch(text) else None


@lru_cache(maxsize=1)
def _manifest() -> tuple[dict, dict[tuple[str, str], dict]]:
    source = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if source.get("provenance") != "official-api-pinned-snapshot" or not source.get("version"):
        raise ValueError("Stock identity manifest provenance/version missing")
    index: dict[tuple[str, str], dict] = {}
    for asset in source["assets"]:
        for chain, deployment in asset["deployments"].items():
            for field, kind, version in (
                ("native", "native", "v2"),
                ("wrapperCurrent", "wrapper-current", "v2"),
                ("wrapperLegacy", "wrapper-legacy", "v1"),
            ):
                address = _address(deployment.get(field))
                if not address:
                    if deployment.get(field):
                        raise ValueError("Invalid official deployment address")
                    continue
                key = (chain, address)
                if key in index:
                    raise ValueError("Conflicting official deployment addresses")
                index[key] = {
                    "chainId": chain, "address": address,
                    "underlyingId": f"xstocks:{asset['assetId']}",
                    "ticker": asset["ticker"], "tokenSymbol": asset["tokenSymbol"],
                    "nameEn": asset["nameEn"], "underlyingIsin": asset.get("underlyingIsin"),
                    "issuer": "xStocks", "tokenKind": kind, "version": version,
                    "verificationStatus": "legacy" if kind == "wrapper-legacy" else "official",
                    "eligibleForPair": kind != "wrapper-legacy",
                    "sourceUrl": source["sourceUrl"],
                    "sourceDocs": source["sourceDocs"],
                    "sourceAt": source["capturedAt"], "manifestVersion": source["version"],
                }
    return source, index


def manifest_status() -> dict:
    """Expose an unavailable manifest instead of silently trusting a ticker."""
    try:
        source, index = _manifest()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {"status": "unavailable", "version": None, "entries": 0,
                "reason": type(exc).__name__}
    return {"status": "ready", "version": source["version"],
            "sourceAt": source["capturedAt"], "sourceUrl": source["sourceUrl"],
            "entries": len(index)}


def token_identity(chain_id, address, reported_ticker=None) -> dict:
    chain = str(chain_id or "")
    canonical = _address(address)
    try:
        hit = _manifest()[1].get((chain, canonical)) if canonical else None
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        hit = None
    if hit:
        return dict(hit)
    return {
        "chainId": chain, "address": canonical, "underlyingId": None,
        "ticker": None, "reportedTicker": str(reported_ticker).strip().upper() if reported_ticker else None,
        "issuer": None, "tokenKind": None, "version": None,
        "verificationStatus": "unverified", "eligibleForPair": False,
        "sourceUrl": None, "sourceAt": None,
        "manifestVersion": manifest_status().get("version"),
    }


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def assess_pool_relation(relation: dict, now_ms: int | None = None) -> dict:
    """Re-evaluate stored evidence at read time so an old A cannot persist.

    A requires an official native/current wrapper on the actual pool side,
    matching issuer underlying, an on-chain verified pair, and fresh, known
    USD liquidity of at least $1,000. A failed condition stays a visible
    legacy observation with a reason; it does not delete the relation.
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    chain = str(relation.get("chainId") or "")
    stock = token_identity(chain, relation.get("stock"), relation.get("ticker"))
    side_address = relation.get("stockSide") or relation.get("stock")
    side = token_identity(chain, side_address, relation.get("ticker"))
    evaluated = {
        "level": None, "evidenceStatus": "unverified",
        "verificationStatus": side["verificationStatus"],
        "tokenKind": side.get("tokenKind"),
        "stockIdentity": stock, "sideIdentity": side,
        "pairLiquidityUsd": None, "evaluatedAt": now,
        "ruleVersion": RULE_VERSION, "validUntil": None,
    }

    def rejected(reason: str) -> dict:
        evaluated["evidenceStatus"] = reason
        return evaluated

    if relation.get("status") != "verified":
        return rejected("pool-unverified")
    pool, meme = _address(relation.get("pool")), _address(relation.get("token"))
    token0, token1 = _address(relation.get("token0")), _address(relation.get("token1"))
    side_addr = _address(side_address)
    if not pool or not meme or not token0 or not token1 or not side_addr:
        return rejected("pool-evidence-incomplete")
    if token0 == token1 or {meme, side_addr} != {token0, token1}:
        return rejected("pool-side-mismatch")
    if stock["verificationStatus"] == "legacy" or side["verificationStatus"] == "legacy":
        return rejected("legacy-wrapper-ineligible")
    if not stock["eligibleForPair"] or not side["eligibleForPair"]:
        return rejected("issuer-deployment-unverified")
    if stock["underlyingId"] != side["underlyingId"]:
        return rejected("wrapper-underlying-mismatch")
    liquidity = _number(relation.get("liquidityUsd"))
    observed_at = _number(relation.get("liquidityAt"))
    if liquidity is None or observed_at is None:
        return rejected("liquidity-unknown")
    if observed_at <= 0 or not 0 <= now - observed_at <= MAX_LIQUIDITY_AGE_MS:
        return rejected("liquidity-stale")
    evaluated["pairLiquidityUsd"] = liquidity
    evaluated["validUntil"] = int(observed_at) + MAX_LIQUIDITY_AGE_MS
    if liquidity < MIN_PAIR_LIQUIDITY_USD:
        return rejected("below-minimum-liquidity")
    evaluated["level"] = "A"
    evaluated["evidenceStatus"] = "qualified"
    return evaluated


@lru_cache(maxsize=1)
def _keywords() -> tuple[str, list[dict]]:
    source = json.loads(KEYWORDS.read_text(encoding="utf-8"))
    rules = source["rules"]
    seen = set()
    for row in rules:
        ticker = row["ticker"]
        if ticker in seen or not row.get("terms"):
            raise ValueError("Duplicate/empty stock name rule")
        seen.add(ticker)
        if any(term.upper() in GENERAL_TERMS for term in row["terms"]):
            raise ValueError("Generic crypto name used as stock keyword")
    return source["version"], rules


@lru_cache(maxsize=4096)
def _match_name_cached(symbol: str, name: str) -> tuple[str, str, str, str] | None:
    """Cache immutable evidence; callers must not share a mutable result."""
    version, rules = _keywords()
    # Symbol evidence wins even if a different company name appears in the
    # token name. Neither path promotes B into a financial/pool assertion.
    for haystack, match_type in ((symbol, "symbol"), (name, "name")):
        for row in rules:
            for term in row["terms"]:
                if re.search(r"[A-Za-z0-9]", term):
                    pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
                    matched = bool(re.search(pattern, haystack, re.I))
                else:
                    # CJK has no whitespace word separator; exact registered
                    # phrase matching is intentional, without fuzzy expansion.
                    matched = term in haystack
                if matched:
                    return row["ticker"], match_type, term, version
    return None


def match_name(symbol: str | None, name: str | None) -> dict | None:
    """Curated exact terms only; prefixes, substrings and BTC are excluded."""
    hit = _match_name_cached(str(symbol or "").strip(), str(name or ""))
    if not hit:
        return None
    ticker, match_type, term, version = hit
    return {"level": "B", "ticker": ticker, "matchType": match_type,
            "keyword": term, "ruleVersion": version,
            "evidenceStatus": "name-only"}
