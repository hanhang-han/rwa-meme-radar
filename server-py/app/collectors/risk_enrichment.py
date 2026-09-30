"""Bounded, worker-owned GoPlus observations. No trading or request-time scans.

Public contract: https://docs.gopluslabs.io/reference/response-details
GoPlus percentages are fractions (1 = 100%). Empty fields are unknown.
The API's top ten include pools and custodians; never publish an adjusted
concentration or overwrite holder counts from this response.
"""
from __future__ import annotations

import asyncio
import math
import re
import time

import httpx

from ..config import bounded_env_int
from ..db import store

API = "https://api.gopluslabs.io/api/v1"
CHAINS = ("196", "56", "4663")
ADDRESS = re.compile(r"0x[0-9a-f]{40}\Z", re.I)
BURNS = {"0x" + "0" * 40, "0x" + "0" * 36 + "dead"}
DAY = 86_400_000
# Public tier is 30 calls/minute. Keep room for the support-list lookup.
REQUEST_GAP_SECONDS = 2.1
_round_lock = asyncio.Lock()


def _number(value):
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _boolean(value):
    if value in ("1", 1, True):
        return True
    if value in ("0", 0, False):
        return False
    return None


def _percent(value):
    number = _number(value)
    return number * 100 if number is not None and number <= 1 else None


def _address(value):
    return str(value).lower() if ADDRESS.fullmatch(str(value or "")) else None


def _holders(row):
    holders = row.get("holders")
    if not isinstance(holders, list) or not holders or len(holders) > 10:
        return {"top10RawPercent": None, "sampleHolderCount": 0}
    values, seen = [], set()
    for holder in holders:
        if not isinstance(holder, dict):
            return {"top10RawPercent": None, "sampleHolderCount": 0}
        address, pct = _address(holder.get("address")), _percent(holder.get("percent"))
        if address is None or address in seen or pct is None:
            return {"top10RawPercent": None, "sampleHolderCount": 0}
        seen.add(address)
        values.append(pct)
    total = sum(values)
    count = _number(row.get("holder_count"))
    complete = len(values) == 10 or (count is not None and 0 < count <= len(values) and count.is_integer())
    return {"top10RawPercent": min(100, total) if complete and total <= 100.000001 else None,
            "sampleHolderCount": len(values), "samplePercent": total if total <= 100.000001 else None,
            "exclusionsApplied": False, "scope": "provider-top10-including-pools"}


def _liquidity_lock(row, at):
    unknown = {"identified": False, "reason": "lp-evidence-unavailable"}
    dex = row.get("dex")
    holders = row.get("lp_holders")
    # This endpoint has one LP-holder list without a per-list pool address.
    # Multiple pools or a V3 NFT list cannot be safely assigned to a V2 LP.
    if not isinstance(dex, list) or len(dex) != 1 or not isinstance(dex[0], dict):
        return {**unknown, "reason": "lp-pool-ambiguous"}
    pair = _address(dex[0].get("pair"))
    if dex[0].get("liquidity_type") != "UniV2" or not pair:
        return {**unknown, "reason": "lp-type-unsupported"}
    if not isinstance(holders, list) or not holders or len(holders) > 10:
        return unknown
    supply = _number(row.get("lp_total_supply"))
    covered = locked = burned = unlocked = 0.0
    ends, seen = [], set()
    for holder in holders:
        if not isinstance(holder, dict) or holder.get("NFT_list"):
            return unknown
        address, pct = _address(holder.get("address")), _percent(holder.get("percent"))
        if not address or address in seen or pct is None:
            return unknown
        seen.add(address)
        covered += pct
        if address in BURNS:
            burned += pct
        elif _boolean(holder.get("is_locked")) is False:
            unlocked += pct
        elif _boolean(holder.get("is_locked")) is True:
            detail = holder.get("locked_detail")
            if not isinstance(detail, list) or not supply:
                continue
            amount, expiries = 0.0, []
            for lock in detail:
                if not isinstance(lock, dict):
                    continue
                end, quantity = _number(lock.get("end_time")), _number(lock.get("amount"))
                if end is not None and quantity is not None and end * 1000 > at:
                    amount += quantity
                    expiries.append(end * 1000)
            verified = amount / supply * 100
            if verified <= pct + 0.000001:
                locked += min(pct, verified)
                ends.extend(expiries)
    if covered > 100.000001:
        return unknown
    return {"identified": True, "reason": "provider-v2-lp-holders", "pool": pair,
            "scope": "single-v2-pool", "coveredPercent": min(100, covered),
            "lockedPercentMin": min(100, locked), "burnedPercent": min(100, burned),
            "unlockedPercentMin": min(100, unlocked),
            "nextUnlockAt": min(ends) if ends else None}


def normalize_goplus(chain, token, row, at):
    """Return only security fields, never quote, identity, supply or holders."""
    chain, token = str(chain), _address(token)
    if not token or not isinstance(row, dict) or not row:
        raise ValueError("empty-security-observation")
    scan = {"provider": "GoPlus", "chainId": chain, "token": token,
            "checkedAt": at, "receivedAt": at, "timeKind": "observed",
            "sourceUrl": f"{API}/token_security/{chain}?contract_addresses={token}"}
    for field, raw in (("honeypot", "is_honeypot"), ("mintable", "is_mintable"),
                       ("pausable", "transfer_pausable"), ("blacklist", "is_blacklisted"),
                       ("proxy", "is_proxy"), ("openSource", "is_open_source"),
                       ("ownerChangeBalance", "owner_change_balance"),
                       ("canTakeBackOwnership", "can_take_back_ownership"),
                       ("hiddenOwner", "hidden_owner"), ("selfDestruct", "selfdestruct")):
        scan[field] = _boolean(row.get(raw))
    scan.update(buyTaxPct=_percent(row.get("buy_tax")), sellTaxPct=_percent(row.get("sell_tax")),
                ownerAddress=_address(row.get("owner_address")),
                creatorAddress=_address(row.get("creator_address")))
    fields = ("honeypot", "mintable", "pausable", "blacklist", "buyTaxPct", "sellTaxPct")
    scan["status"] = "complete" if all(scan[field] is not None for field in fields) else "partial"
    observation = {"provider": "GoPlus", "checkedAt": at, "timeKind": "observed",
                   "chainId": chain, "token": token,
                   "holders": _holders(row), "liquidityLock": _liquidity_lock(row, at)}
    return {"tokenScan": scan, "securityObservation": observation}


async def _request(client, path, params=None):
    response = await client.get(API + path, params=params)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("code") != 1:
        raise ValueError("security-provider-error")
    return body.get("result")


async def _due_assets(s, now, ttl, limit):
    rows = await s.fetchall("""SELECT a.id FROM facts a
        LEFT JOIN facts j ON j.kind=? AND j.id=a.id
        WHERE a.kind=? AND json_extract(a.body,'$.kind')='candidate'
          AND COALESCE(json_extract(j.body,'$.nextAttemptAt'),0)<=?
          AND COALESCE(json_extract(a.body,'$.tokenScan.checkedAt'),0)<=?
        ORDER BY COALESCE(json_extract(j.body,'$.lastAttemptAt'),0),a.id LIMIT ?""",
        (s.key("risk-attempt"), s.key("asset"), now, now - ttl, limit))
    return [str(row[0]).lower() for row in rows if _address(row[0])]


async def refresh_risk_enrichment():
    """A restart-safe request budget and per-contract TTL bound public calls."""
    async with _round_lock:
        return await _refresh()


async def _refresh():
    now = int(time.time() * 1000)
    limit = bounded_env_int("GOPLUS_ROUND_LIMIT", 12, 1, 30)
    daily = bounded_env_int("GOPLUS_DAILY_BUDGET", 2000, 1, 10000)
    ttl = bounded_env_int("GOPLUS_TTL_SECONDS", 21600, 300, 86400) * 1000
    system = await store("system")
    budget = await system.get("collector", "goplus-budget") or {}
    if budget.get("day") != now // DAY:
        budget = {"day": now // DAY, "used": 0}
    result = {"requested": 0, "accepted": 0, "updated": 0, "failed": 0,
              "unsupported": 0, "quotaBlocked": 0}

    async def reserve():
        day = int(time.time() * 1000) // DAY
        if budget['day'] != day:
            budget.update(day=day, used=0)
        if budget["used"] >= daily:
            result["quotaBlocked"] += 1
            return False
        budget["used"] += 1
        await system.put("collector", "goplus-budget", budget)
        result["requested"] += 1
        return True

    support = await system.get("collector", "goplus-chains") or {}
    async with httpx.AsyncClient(timeout=8, headers={"Accept": "application/json"}) as client:
        if not 0 <= now - (support.get("checkedAt") or 0) < DAY:
            if not await reserve():
                return result
            try:
                rows = await _request(client, "/supported_chains", {"type": "token_security"})
                if not isinstance(rows, list) or not rows:
                    raise ValueError("missing-security-chains")
                support = {"chains": [str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")],
                           "checkedAt": now}
                await system.put("collector", "goplus-chains", support)
            except (httpx.HTTPError, ValueError):
                result["failed"] += 1
                return result  # A stale support list is not proof of support.
            await asyncio.sleep(REQUEST_GAP_SECONDS)
        queues = []
        for chain in CHAINS:
            s = await store(chain)
            if chain not in support.get("chains", []):
                result["unsupported"] += 1
                continue
            queues.append((s, await _due_assets(s, now, ttl, limit)))
        # Interleave chains so one chain cannot consume a fresh daily budget.
        selected = [(s, tokens[index]) for index in range(limit)
                    for s, tokens in queues if index < len(tokens)][:limit]
        for index, (s, token) in enumerate(selected):
            if not await reserve():
                break
            attempted = int(time.time() * 1000)
            # Save before HTTP so process termination does not hammer one coin.
            await s.put("risk-attempt", token, {"lastAttemptAt": attempted,
                                               "nextAttemptAt": attempted + 900_000})
            try:
                body = await _request(client, f"/token_security/{s.scope}", {"contract_addresses": token})
                row = body.get(token) if isinstance(body, dict) else None
                if not isinstance(row, dict) or not row:
                    raise ValueError("token-security-unavailable")
                patch = normalize_goplus(s.scope, token, row, int(time.time() * 1000))
                await s.patch_fact("asset", token, patch)
                await s.put("risk-attempt", token, {"lastAttemptAt": attempted,
                                                   "nextAttemptAt": attempted + ttl, "status": "accepted"})
                result["accepted"] += 1
                result["updated"] += 1
            except (httpx.HTTPError, ValueError) as exc:
                # Keep prior evidence and its time. The assessment expires it.
                await s.put("risk-attempt", token, {"lastAttemptAt": attempted,
                    "nextAttemptAt": attempted + 900_000, "status": "unavailable", "reason": type(exc).__name__})
                result["failed"] += 1
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                    break
            if index + 1 < len(selected):
                await asyncio.sleep(REQUEST_GAP_SECONDS)
    await system.put("collector", "goplus", {**result, "lastAttemptAt": now,
        "dailyUsed": budget["used"], "dailyLimit": daily, "ttlMs": ttl,
        "supportedChains": support.get("chains", [])})
    return result
