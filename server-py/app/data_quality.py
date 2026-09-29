"""Explicit data eligibility rules used by rankings, themes and alerts.

The dashboard keeps historical facts, but historical presence is not the same
as current market evidence.  These rules travel with every asset so clients do
not need to guess whether a value is safe to rank or aggregate.
"""
import time


QUOTE_MAX_AGE_MS = 15 * 60_000
POOL_MAX_AGE_MS = 15 * 60_000
TRADE_BUCKET_MAX_AGE_MS = 15 * 60_000


def _is_fresh(at, now: float, max_age: int) -> bool:
    try:
        return bool(at) and 0 <= now - float(at) <= max_age
    except (TypeError, ValueError):
        return False


def evaluate_asset(asset: dict, relations: list[dict], now: float | None = None) -> dict:
    now = now or time.time() * 1000
    times = asset.get("fieldTimes") or {}
    verified = [r for r in relations if r.get("status") == "verified"]
    price_fresh = asset.get("price") is not None and _is_fresh(times.get("price"), now, QUOTE_MAX_AGE_MS)
    volume_fresh = asset.get("volume24h") is not None and _is_fresh(times.get("volume24h"), now, QUOTE_MAX_AGE_MS)
    pool_fresh = any(
        r.get("liquidityUsd") is not None
        and _is_fresh(r.get("liquidityAt"), now, POOL_MAX_AGE_MS)
        for r in verified
    )
    bucket_at = times.get("observedVolume5m")
    bucket_complete = bool(asset.get("observedBucket5mComplete")) and _is_fresh(
        bucket_at, now, TRADE_BUCKET_MAX_AGE_MS
    )

    market_ranking = price_fresh and volume_fresh
    relation_ranking = market_ranking and bool(verified) and pool_fresh
    theme = price_fresh and bool(verified) and pool_fresh
    market_alert = price_fresh and bool(verified) and bucket_complete
    relationship_alert = bool(verified)

    market_time = (asset.get("priceProvenance") or {}).get("timeKind") == "market"
    if relation_ranking and bucket_complete and market_time and _is_fresh(times.get("price"), now, 30_000):
        tier = "realtime"
    elif price_fresh and (volume_fresh or verified):
        tier = "current"
    elif any(asset.get(k) is not None for k in ("price", "volume24h", "marketCap")) or verified:
        tier = "historical"
    else:
        tier = "insufficient"

    reasons = []
    if not price_fresh:
        reasons.append("price-missing-or-stale")
    if not volume_fresh:
        reasons.append("volume-missing-or-stale")
    if not verified:
        reasons.append("no-verified-relation")
    elif not pool_fresh:
        reasons.append("pool-valuation-stale")
    if not bucket_complete:
        reasons.append("complete-5m-trade-bucket-unavailable")

    return {
        "tier": tier,
        "fresh": {
            "price": price_fresh,
            "volume24h": volume_fresh,
            "poolLiquidity": pool_fresh,
            "tradeBucket5m": bucket_complete,
        },
        "eligible": {
            "marketRanking": market_ranking,
            "relationRanking": relation_ranking,
            "theme": theme,
            "marketAlert": market_alert,
            "relationshipAlert": relationship_alert,
        },
        "verifiedRelations": len(verified),
        "reasons": reasons,
    }


def quality_summary(assets: list[dict]) -> dict:
    tiers = {"realtime": 0, "current": 0, "historical": 0, "insufficient": 0}
    eligible = {"marketRanking": 0, "relationRanking": 0, "theme": 0, "marketAlert": 0}
    for asset in assets:
        quality = asset.get("dataQuality") or {}
        tier = quality.get("tier")
        if tier in tiers:
            tiers[tier] += 1
        for key in eligible:
            eligible[key] += int(bool((quality.get("eligible") or {}).get(key)))
    return {"tiers": tiers, "eligible": eligible, "total": len(assets)}
