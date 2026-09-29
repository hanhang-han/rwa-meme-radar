"""Observed deviations, not trade recommendations. Snapshot-only data never alerts."""
from .comparisons import MAX_AGE, LIVE_AGE


def advance(previous, metric, threshold, now):
    previous = previous or {}
    if metric.get("status") != "realtime" or metric.get("value") is None or now > (metric.get("realtimeUntil") or 0):
        # Missing data is not proof that a spread recovered.
        return {"active": previous.get("active", False), "lastValidAt": previous.get("lastValidAt"), "gap": True}, None
    if previous.get("gap") or (previous.get("lastValidAt") is not None and now - previous["lastValidAt"] > LIVE_AGE):
        previous = {"active": previous.get("active", False)}
    value = metric["value"]
    magnitude = abs(value)
    active = previous.get("active", False)
    if active:
        if magnitude < threshold * .6:
            return {"active": False, "lastValidAt": now}, "spread-recovered"
        return {"active": True, "lastValidAt": now}, None
    if magnitude < threshold:
        return {"active": False, "lastValidAt": now}, None
    observation = str(metric.get("inputs"))
    direction = 1 if value >= 0 else -1
    if previous.get("direction") != direction:
        previous = {}
    count = previous.get("count", 0) + (observation != previous.get("observation"))
    first = previous.get("firstAt", now)
    if count >= 3 and now - first >= 30000:
        return {"active": True, "lastValidAt": now}, "spread-expanded"
    return {"active": False, "direction": direction, "count": count, "firstAt": first, "observation": observation, "lastValidAt": now}, None


async def check_alert(s, token, metric, now, *, pool=None, liquidity=None, liquidity_at=None):
    if pool and (liquidity is None or liquidity < 10000 or not liquidity_at or now-liquidity_at > MAX_AGE):
        metric = {"status": "unavailable"}
    threshold = 3 if pool else 2
    key = f"{token}:{pool or 'premium'}"
    before = await s.get("comparison-alert-state", key)
    after, event = advance(before, metric, threshold, now)
    if after != before:
        await s.put("comparison-alert-state", key, after)
    if event:
        await s.put_event(f"comparison:{key}:{event}:{now}", token, {
            "kind": event, "pool": pool, "value": metric["value"], "threshold": threshold,
            "method": metric.get("method"), "quoteAt": metric.get("at"),
            "title": "价差持续扩大" if event == "spread-expanded" else "价差回到观察阈值内",
        }, now)
