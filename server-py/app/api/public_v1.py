"""Read-only, explicitly whitelisted data contracts for invited API users.

These routes never call a collector, create a demand lease, or rebuild the
dashboard.  The relationship index is decoded once per published revision.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import aiosqlite
from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..storage_runtime import async_connect
from .. import developer_access as access
from ..db import store
from ..market_quotes import enrich_asset
from ..realtime_projection import _decode_snapshot
from ..risk_assessment import FLAGS


router = APIRouter(prefix="/v1", tags=["public-api-beta"])
CHAINS = {"196", "56", "4663"}
ADDRESS = re.compile(r"0x[0-9a-f]{40}\Z", re.I)
# Exchange symbols may be numeric (for example 9992), not only US tickers.
TICKER = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,14}\Z")
_index_lock = asyncio.Lock()
_index_cache: tuple[tuple, dict, float] | None = None
INDEX_REFRESH_SECONDS = 30


def _error(exc: access.AccessError):
    headers = {"Cache-Control": "no-store"}
    if exc.status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="CliperX trial API"'
    if exc.retry_after is not None:
        headers["Retry-After"] = str(max(1, exc.retry_after))
    raise HTTPException(exc.status, detail=exc.code, headers=headers)


async def _authorize(request: Request, response: Response):
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not key or " " in key:
        raise HTTPException(401, detail="api-key-required", headers={
            "WWW-Authenticate": 'Bearer realm="CliperX trial API"', "Cache-Control": "no-store"})
    try:
        _, quota = await asyncio.to_thread(access.authenticate_and_consume, key)
    except access.AccessError as exc:
        _error(exc)
    response.headers["RateLimit-Limit"] = str(quota["limit"])
    response.headers["RateLimit-Remaining"] = str(quota["remaining"])
    response.headers["RateLimit-Reset"] = str(max(1, quota["reset"] - int(time.time())))
    response.headers["Cache-Control"] = "private, no-store"


def _a_relation(row: dict) -> dict:
    return {
        "grade": "A", "ticker": str(row.get("ticker") or "").upper(),
        "chainId": str(row.get("chainId")),
        "tokenAddress": str(row.get("token") or "").lower(),
        "poolAddress": str(row.get("pool") or "").lower(),
        "stockTokenAddress": str(row.get("stock") or "").lower() or None,
        "evidence": {"type": "verified-pool", "status": "qualified",
                     "source": "onchain-pool-and-pinned-issuer-manifest",
                     "ruleVersion": row.get("ruleVersion"),
                     "checkedAt": row.get("checkedAt"),
                     "validUntil": row.get("validUntil")},
    }


def _b_relation(row: dict) -> dict:
    match = row.get("match") or {}
    return {
        "grade": "B", "ticker": str(match.get("ticker") or "").upper(),
        "chainId": str(row.get("chainId")),
        "tokenAddress": str(row.get("token") or "").lower(),
        "poolAddress": None, "stockTokenAddress": None,
        "evidence": {"type": "name-match", "status": "name-only",
                     "source": "curated-name-rule", "matchType": match.get("matchType"),
                     "keyword": match.get("keyword"), "ruleVersion": match.get("ruleVersion"),
                     "checkedAt": row.get("updatedAt"), "validUntil": None},
    }


def _build_index(payload: dict, revision: int, built_at: int) -> dict:
    unified = payload.get("unified") or {}
    by_ticker: dict[str, list[dict]] = {}
    a_keys = set()
    now = int(time.time() * 1000)
    for row in unified.get("relations") or []:
        if (row.get("status") != "verified" or row.get("level") != "A"
                or row.get("evidenceStatus") != "qualified"
                or not row.get("validUntil") or row["validUntil"] <= now
                or not ADDRESS.fullmatch(str(row.get("token") or ""))):
            continue
        ticker = str(row.get("ticker") or "").upper()
        if not TICKER.fullmatch(ticker):
            continue
        item = _a_relation(row)
        by_ticker.setdefault(ticker, []).append(item)
        a_keys.add((ticker, item["chainId"], item["tokenAddress"]))
    for row in unified.get("assets") or []:
        match = row.get("match") or {}
        ticker = str(match.get("ticker") or "").upper()
        chain = str(row.get("chainId") or "")
        address = str(row.get("token") or "").lower()
        if (row.get("kind") != "candidate" or not TICKER.fullmatch(ticker)
                or chain not in CHAINS or not ADDRESS.fullmatch(address)
                or (ticker, chain, address) in a_keys):
            continue
        by_ticker.setdefault(ticker, []).append(_b_relation(row))
    for rows in by_ticker.values():
        rows.sort(key=lambda item: (item["grade"] != "A", item["chainId"], item["tokenAddress"], item["poolAddress"] or ""))
    return {"revision": revision, "asOf": payload.get("now") or built_at,
            "publishedAt": built_at, "byTicker": by_ticker,
            "assetCount": len(unified.get("assets") or []),
            "verifiedRelationCount": sum(len([r for r in rows if r["grade"] == "A"]) for rows in by_ticker.values()),
            "nameClueCount": sum(len([r for r in rows if r["grade"] == "B"]) for rows in by_ticker.values())}


async def _index() -> dict:
    """Never force a missing dashboard projection on a public request."""
    global _index_cache
    # Relationships change far less often than quotes. A bounded 30-second
    # publication lag avoids decoding a multi-MB market snapshot on each quote
    # revision while still exposing the exact revision used for these rows.
    if _index_cache and time.monotonic() - _index_cache[2] < INDEX_REFRESH_SECONDS:
        return _index_cache[1]
    scoped = await store("196")
    async with _index_lock:
        if _index_cache and time.monotonic() - _index_cache[2] < INDEX_REFRESH_SECONDS:
            return _index_cache[1]
        async with async_connect(scoped.path, readonly=True, timeout=10) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            cursor = await db.execute("SELECT revision,built_at FROM dashboard_projection WHERE name='full'")
            version = await cursor.fetchone()
            if version is None:
                raise HTTPException(503, detail="snapshot-unavailable", headers={"Retry-After": "30"})
            stamp = (scoped.path, int(version[0]), int(version[1]))
            if _index_cache and _index_cache[0] == stamp:
                _index_cache = (stamp, _index_cache[1], time.monotonic())
                return _index_cache[1]
            cursor = await db.execute("SELECT body FROM dashboard_projection WHERE name='full'")
            row = await cursor.fetchone()
        if row is None:
            raise HTTPException(503, detail="snapshot-unavailable", headers={"Retry-After": "30"})
        payload = json.loads(_decode_snapshot(row[0]))
        index = _build_index(payload, stamp[1], stamp[2])
        _index_cache = (stamp, index, time.monotonic())
        return index


@router.get("/relations")
async def relations(request: Request, response: Response,
                    stock: str = Query(min_length=1, max_length=15),
                    chain: str | None = None, grade: str | None = None,
                    limit: int = Query(default=50, ge=1, le=50),
                    offset: int = Query(default=0, ge=0, le=1000)):
    await _authorize(request, response)
    ticker = stock.upper()
    if not TICKER.fullmatch(ticker) or chain not in (None, *CHAINS) or grade not in (None, "A", "B"):
        raise HTTPException(400, detail="invalid-filter")
    index = await _index()
    now = int(time.time() * 1000)
    rows = [row for row in index["byTicker"].get(ticker, [])
            if (not chain or row["chainId"] == chain) and (not grade or row["grade"] == grade)
            and (row["grade"] != "A" or (row["evidence"]["validUntil"] or 0) > now)]
    page = rows[offset:offset + limit]
    return {"schemaVersion": "1.0", "revision": index["revision"],
            "asOf": index["asOf"], "publishedAt": index["publishedAt"],
            "source": "cliperx-curated-relations", "stock": ticker,
            "items": page, "total": len(rows), "nextOffset": offset + len(page) if offset + len(page) < len(rows) else None}


_EVIDENCE_FIELDS = frozenset({
    "threshold", "provider", "checkedAt", "volumeLiquidityRatio", "transactionsPerHolder",
    "change24hPct", "buyTaxPct", "sellTaxPct", "top10AdjustedPercent", "triggers",
    "coverage", "volumeAt", "liquidityAt", "transactionsAt", "holdersAt", "changeAt",
    "numeratorScope", "denominatorScope", "changeScope", "liquidityScope", "holderScope",
})


def _public_checks(assessment: dict) -> dict:
    out = {}
    for name in FLAGS:
        check = (assessment.get("checks") or {}).get(name) or {}
        raw = check.get("evidence") or {}
        out[name] = {"status": check.get("status") or "unknown",
                     "reason": check.get("reason") or "missing-evidence",
                     "evidence": {key: raw[key] for key in _EVIDENCE_FIELDS if key in raw}}
    return out


@router.get("/token/{chain}/{address}/risk")
async def token_risk(chain: str, address: str, request: Request, response: Response):
    await _authorize(request, response)
    if chain not in CHAINS or not ADDRESS.fullmatch(address):
        raise HTTPException(400, detail="unsupported-token")
    asset = await (await store(chain)).get("asset", address.lower())
    if not asset:
        raise HTTPException(404, detail="token-not-indexed")
    # This is a latest local-facts assessment, separate from the published
    # relationship projection. It must not claim that projection's revision.
    # enrich_asset uses only already-persisted local source snapshots and
    # exactly the same risk rules as the website's asset view.
    row = enrich_asset({**asset, "chainId": chain})
    assessment = row["riskAssessment"]
    return {"schemaVersion": "1.0", "assessmentVersion": assessment["version"],
            "calculatedAt": assessment["checkedAt"], "factUpdatedAt": asset.get("updatedAt"),
            "source": "cliperx-local-facts-and-cached-source-observations", "chainId": chain,
            "tokenAddress": address.lower(), "status": assessment["status"],
            "flags": assessment["flags"], "ruleVersion": assessment["version"],
            "checks": _public_checks(assessment)}


@router.get("/snapshot/latest")
async def snapshot_latest(request: Request, response: Response):
    await _authorize(request, response)
    index = await _index()
    now = int(time.time() * 1000)
    current_a = sum(1 for rows in index["byTicker"].values() for row in rows
                    if row["grade"] == "A" and (row["evidence"]["validUntil"] or 0) > now)
    return {"schemaVersion": "1.0", "revision": index["revision"],
            "asOf": index["asOf"], "publishedAt": index["publishedAt"],
            "source": "cliperx-curated-projection", "scope": "metadata-only",
            "counts": {"indexedTokens": index["assetCount"],
                       "verifiedRelations": current_a,
                       "nameClues": index["nameClueCount"]},
            "notes": "Counts describe indexed evidence, not market-wide coverage."}
