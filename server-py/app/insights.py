"""Persisted, worker-owned per-asset AI readouts.

HTTP requests only publish a short demand lease and read the latest result.
The collector worker is the sole owner of paid model calls, so page refreshes
cannot multiply upstream requests.
"""
import hashlib
import json
import time

from .ai import MODEL, ai_enabled, ai_failure, ai_narrate
from .db import store

CHAINS = ("196", "56", "4663")
FRESH_MS = 15 * 60_000
DEMAND_MS = 2 * 60_000
RETRY_MS = 5 * 60_000


def _id(address: str, lang: str) -> str:
    return f"{address}:{lang}"


async def request_insight(chain: str, address: str, lang: str) -> dict:
    """Read the latest result and enqueue a refresh without calling AI."""
    now = int(time.time() * 1000)
    s = await store(chain)
    ident = _id(address, lang)
    saved = await s.get("insight", ident)
    fresh = bool(saved and now - int(saved.get("at") or 0) < FRESH_MS)
    if not fresh:
        from .demand_leases import publish_lease
        publish_lease(s, "insight-demand", ident, {
            "chainId": chain,
            "address": address,
            "lang": lang,
            "requestedAt": now,
            "expiresAt": now + DEMAND_MS,
        })
    if saved:
        return {**saved, "stale": not fresh,
                "status": "ready" if fresh else "refreshing"}
    job = await s.get("insight-job", ident) or {}
    status = job.get("status") or ("queued" if ai_enabled() else "disabled")
    return {"text": None, "status": status,
            "reason": "disabled" if not ai_enabled() else None}


async def refresh_insights() -> None:
    """Generate due demand leases; called only by the collector worker."""
    now = int(time.time() * 1000)
    for chain in CHAINS:
        s = await store(chain)
        for ident, demand in await s.all_kv("insight-demand"):
            if int(demand.get("expiresAt") or 0) < now:
                continue
            address = str(demand.get("address") or "").lower()
            lang = "en" if demand.get("lang") == "en" else "zh"
            saved = await s.get("insight", ident)
            if saved and now - int(saved.get("at") or 0) < FRESH_MS:
                continue
            job = await s.get("insight-job", ident) or {}
            if int(job.get("nextRetryAt") or 0) > now:
                continue
            if not ai_enabled():
                await s.put("insight-job", ident, {
                    "status": "disabled", "updatedAt": now, "attempts": 0,
                })
                continue
            asset = await s.get("asset", address)
            if not asset:
                await s.put("insight-job", ident, {
                    "status": "not_indexed", "updatedAt": now,
                })
                continue
            relations = [
                r for r in await s.all("relation")
                if r.get("token") == address or r.get("stock") == address
            ]
            data = {
                "asset": {
                    "symbol": asset.get("symbol"), "name": asset.get("name"),
                    "price": asset.get("price"), "change24h": asset.get("change24h"),
                    "volume24h": asset.get("volume24h"), "liquidity": asset.get("liquidity"),
                    "holders": asset.get("holders"), "kind": asset.get("kind"),
                    "dataAsOf": asset.get("updatedAt"),
                },
                "relations": [
                    {"ticker": r.get("ticker"), "status": r.get("status"),
                     "poolLiquidityUsd": r.get("liquidityUsd"),
                     "checkedAt": r.get("checkedAt")}
                    for r in relations
                ],
            }
            input_hash = hashlib.sha256(
                json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            attempts = int(job.get("attempts") or 0) + 1
            await s.put("insight-job", ident, {
                "status": "running", "startedAt": now, "updatedAt": now,
                "attempts": attempts, "inputHash": input_hash,
            })
            task = (
                "Write an 80-120 word data readout for this asset detail page. "
                "Focus on the evidence status of its stock relationship."
                if lang == "en" else
                "为这个资产的详情页写一段 120-180 字的数据解读，重点说明它与股票的关系证据状态。"
            )
            ai_key = f"insight:{chain}:{address}:{input_hash}"
            result = await ai_narrate(
                ai_key, lang, FRESH_MS,
                data, task, force=True,
            )
            finished = int(time.time() * 1000)
            if not result:
                failure = ai_failure(ai_key, lang) or {}
                await s.put("insight-job", ident, {
                    "status": "upstream_failed", "updatedAt": finished,
                    "attempts": attempts,
                    "nextRetryAt": failure.get("retryAt") or finished + RETRY_MS,
                    "errorReason": failure.get("reason", "upstream_failed"),
                    "inputHash": input_hash,
                })
                continue
            record = {
                "text": result["text"], "at": result["at"], "lang": lang,
                "chainId": chain, "address": address, "status": "ready",
                "inputHash": input_hash, "model": MODEL,
                "dataAsOf": asset.get("updatedAt"),
            }
            await s.put("insight", ident, record)
            await s.put("insight-job", ident, {
                "status": "success", "updatedAt": finished,
                "lastSuccessAt": finished, "attempts": attempts,
                "inputHash": input_hash,
            })
