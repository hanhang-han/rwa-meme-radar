"""Daily, bounded comparison with DexScreener's *indexed* BNB stock pools.

This is an external-index reconciliation, not proof that every on-chain pool
exists in DexScreener. A 30-row token result may be truncated, so it never
contributes to an unqualified coverage percentage.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable

import httpx

from ..db import store
from ..stock_identity import _manifest


ADDRESS = re.compile(r"0x[0-9a-f]{40}\Z", re.I)
CHAIN = "56"
NETWORK = "bsc"
PROVIDER = "DexScreener"
URL = "https://api.dexscreener.com/token-pairs/v1/bsc/"
REQUEST_SPACING_SECONDS = 0.6  # <=100/min, leaving headroom for quote enrichment.
SUSPECTED_RESULT_CAP = 30
REPORT_KIND = "discovery-coverage"


def _address(value) -> str | None:
    value = str(value or "").lower()
    return value if ADDRESS.fullmatch(value) else None


def official_contracts(chain: str = CHAIN) -> list[str]:
    """All current native/wrapper deployments, including inactive tickers."""
    _, index = _manifest()
    return sorted({address for (cid, address), identity in index.items()
                   if cid == chain and identity["eligibleForPair"]})


def normalize_pairs(rows, token: str) -> tuple[dict[str, dict], int, bool]:
    """Reject malformed/cross-chain results instead of inflating a score."""
    if not isinstance(rows, list):
        raise ValueError("invalid-provider-response")
    pairs: dict[str, dict] = {}
    malformed = 0
    for row in rows:
        if not isinstance(row, dict):
            malformed += 1
            continue
        base_token = row.get("baseToken")
        quote_token = row.get("quoteToken")
        if not isinstance(base_token, dict) or not isinstance(quote_token, dict):
            malformed += 1
            continue
        pool = _address(row.get("pairAddress"))
        base = _address(base_token.get("address"))
        quote = _address(quote_token.get("address"))
        if (row.get("chainId") != NETWORK or not pool or not base or not quote
                or base == quote or token not in (base, quote)):
            malformed += 1
            continue
        liquidity_row = row.get("liquidity")
        liquidity = liquidity_row.get("usd") if isinstance(liquidity_row, dict) else None
        if isinstance(liquidity, bool) or not isinstance(liquidity, (float, int)) or liquidity < 0:
            liquidity = None
        pairs[pool] = {"pool": pool, "token0": base, "token1": quote,
                       "stockSide": token, "liquidityUsd": liquidity}
    return pairs, malformed, len(rows) >= SUSPECTED_RESULT_CAP


def compare_pairs(indexed: dict[str, dict], local_pools: list[dict]) -> dict:
    """A local match requires both pool identity and on-chain token sides."""
    local = {}
    for row in local_pools:
        pool = _address(row.get("pool"))
        sides = {_address(row.get("token0")), _address(row.get("token1"))}
        if (pool and len(sides) == 2 and None not in sides
                and row.get("creationStatus") != "orphaned"):
            local[pool] = sides
    missing = [row for pool, row in indexed.items()
               if pool not in local or local[pool] != {row["token0"], row["token1"]}]
    missing.sort(key=lambda row: (-(row.get("liquidityUsd") or 0), row["pool"]))
    return {"eligible": len(indexed), "covered": len(indexed)-len(missing),
            "missing": len(missing), "samples": missing[:12]}


async def _fetch(client: httpx.AsyncClient, token: str):
    response = await client.get(URL + token)
    response.raise_for_status()
    return response.json()


async def reconcile_bnb(*, fetch: Callable[[str], Awaitable[object]] | None = None,
                        contracts: list[str] | None = None, request_spacing: float = REQUEST_SPACING_SECONDS,
                        now_ms: Callable[[], int] | None = None) -> dict:
    """Check every pinned BNB contract; retain an honest partial report on error.

    The comparison does not add third-party pools to the product. Chain-side
    token and factory verification is required before any missing pool enters
    the asset catalogue or a relation ranking.
    """
    clock = now_ms or (lambda: int(time.time() * 1000))
    system = await store("system")
    subjects = contracts if contracts is not None else official_contracts()
    subjects = sorted(set(subjects))
    if not subjects or any(_address(subject) != subject for subject in subjects):
        raise ValueError("invalid-official-contracts")
    local_pools = await (await store(CHAIN)).all("pool")
    active_sides = {_address(row.get(side)) for row in local_pools if isinstance(row, dict)
                    for side in ("token0", "token1")}
    subjects.sort(key=lambda address: (address not in active_sides, address))
    started = clock()
    report = {"chainId": CHAIN, "status": "partial", "scope": "dexscreener-indexed-official-stock-pools",
              "provider": PROVIDER, "sourceUrl": "https://docs.dexscreener.com/api/reference",
              "manifestVersion": _manifest()[0]["version"], "startedAt": started,
              "checkedAt": None, "updatedAt": started, "subjectCount": len(subjects),
              "checkedContracts": 0, "failedContracts": 0, "cappedContracts": 0,
              "malformedPairs": 0, "eligible": 0, "covered": 0, "missing": 0, "samples": []}
    await system.put(REPORT_KIND, CHAIN, report)
    indexed: dict[str, dict] = {}
    failures = []

    async def collect(get):
        consecutive_failures = 0
        for token in subjects:
            try:
                rows = await get(token)
                pairs, malformed, capped = normalize_pairs(rows, token)
                indexed.update(pairs)
                report["checkedContracts"] += 1
                report["malformedPairs"] += malformed
                report["cappedContracts"] += int(capped)
                consecutive_failures = 0
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                report["failedContracts"] += 1
                consecutive_failures += 1
                if len(failures) < 5:
                    failures.append({"token": token, "reason": type(exc).__name__})
            if (report["checkedContracts"] + report["failedContracts"]) % 64 == 0:
                report.update(compare_pairs(indexed, local_pools))
                report["updatedAt"] = clock()
                report["failureSamples"] = failures
                await system.put(REPORT_KIND, CHAIN, report)
            if consecutive_failures >= 8:
                break  # Provider outage/rate limit: do not waste 2,248 requests.
            if request_spacing:
                await asyncio.sleep(request_spacing)

    if fetch is not None:
        await collect(fetch)
    else:
        async with httpx.AsyncClient(timeout=10, headers={"Accept": "application/json",
                                                         "User-Agent": "CliperX pool-coverage/1"}) as client:
            await collect(lambda token: _fetch(client, token))
    report.update(compare_pairs(indexed, local_pools))
    report["checkedAt"] = clock()
    report["updatedAt"] = report["checkedAt"]
    report["unqueriedContracts"] = len(subjects) - report["checkedContracts"] - report["failedContracts"]
    report["failureSamples"] = failures
    report["status"] = ("complete" if report["eligible"] > 0
                        and not report["failedContracts"] and not report["cappedContracts"]
                        and not report["malformedPairs"] and report["checkedContracts"] == len(subjects)
                        else "partial")
    await system.put(REPORT_KIND, CHAIN, report)
    return {"requested": len(subjects), "accepted": report["checkedContracts"],
            "updated": report["eligible"], "failed": report["failedContracts"],
            "unsupported": report["cappedContracts"] + report["malformedPairs"]}


async def discovery_coverage_snapshot(now_ms: int) -> dict:
    """Small, explicit health contract for all displayed chains."""
    found = await (await store("system")).get(REPORT_KIND, CHAIN)
    bnb = dict(found) if isinstance(found, dict) else {
        "chainId": CHAIN, "status": "unverified", "scope": "dexscreener-indexed-official-stock-pools",
        "checkedAt": None, "eligible": 0, "covered": 0, "missing": 0,
    }
    if bnb.get("checkedAt") and now_ms - bnb["checkedAt"] > 36 * 3600_000:
        bnb["status"] = "stale"
    return {"chains": [
        {"chainId": "196", "status": "unverified", "scope": "external-pool-index-unconfirmed",
         "checkedAt": None, "eligible": 0, "covered": 0, "missing": 0},
        bnb,
        {"chainId": "4663", "status": "unverified", "scope": "official-stock-manifest-unavailable",
         "checkedAt": None, "eligible": 0, "covered": 0, "missing": 0},
    ]}
