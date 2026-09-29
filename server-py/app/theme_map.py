"""Bounded stock-theme market map built from the dashboard's assessed facts.

The display group is an issuer-backed stock ticker.  A member is always a
chain and contract pair; neither a shared symbol nor a name clue can create a
verified relationship.  Every visual metric keeps its own observation time.
"""
from __future__ import annotations

import math


MAX_BUBBLES = 50
MAX_CHANGES = 12
FRESH_MS = 15 * 60_000
CHANGE_WINDOW_MS = 24 * 60 * 60_000
METHOD = "official-a-theme-map-v1"


def _number(value, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    return value


def _time(value):
    return value if _number(value, 1) is not None else None


def _field(asset, field, now, *, unit=None, minimum=None, require_usd=False):
    value = _number(asset.get(field), minimum)
    observed_at = _time((asset.get("fieldTimes") or {}).get(field))
    observation = (asset.get("fieldObservations") or {}).get(field) or {}
    currency = (observation.get("currency") or asset.get(
        "volumeCurrency" if field == "volume24h" else "priceCurrency"
    )) if require_usd else None
    scope = (asset.get("fieldScopes") or {}).get(field)
    if value is None:
        status = "missing"
    elif require_usd and currency != "USD":
        status = "unsupported-currency"
    elif scope not in ("token", "token-aggregate"):
        status = "unsupported-scope"
    elif observed_at is None:
        status = "unknown-time"
    elif not 0 <= now - observed_at <= FRESH_MS:
        status = "stale"
    else:
        status = "current"
    return {
        "value": value if status == "current" else None,
        "unit": unit,
        "currency": currency if require_usd else None,
        "scope": scope,
        "source": (asset.get("fieldSources") or {}).get(field),
        "observedAt": observed_at,
        "status": status,
    }


def _relation_summary(row):
    return {
        "id": row.get("id"), "pool": row.get("pool"),
        "stock": row.get("stock"), "checkedAt": row.get("checkedAt"),
        "poolCreatedAt": row.get("poolCreatedAt"),
        "discoveredAt": row.get("discoveredAt") or row.get("firstSeen"),
        "liquidityUsd": row.get("liquidityUsd"),
        "liquidityAt": row.get("liquidityAt"),
        "issuerSourceUrl": (row.get("stockIdentity") or {}).get("sourceUrl"),
    }


def _relation_order(row):
    return (
        _number(row.get("liquidityUsd"), 0) or 0,
        _time(row.get("liquidityAt")) or 0,
        _time(row.get("checkedAt")) or 0,
        str(row.get("id") or ""),
    )


def build_theme_map(assets: list[dict], relations: list[dict], now: int,
                    stock_tokens: list[dict] | None = None) -> dict:
    """Return all theme breadth denominators and at most 50 visual members."""
    assets_by_key = {
        (str(row.get("chainId") or ""), str(row.get("token") or "").lower()): row
        for row in assets if row.get("kind") == "candidate" and row.get("token")
    }
    localized = {}
    for stock in stock_tokens or ():
        identity = stock.get("stockIdentity") or {}
        ticker = identity.get("code") or stock.get("stockCode")
        if ticker and identity.get("nameZh"):
            localized.setdefault(str(ticker).upper(), identity["nameZh"])

    groups: dict[str, dict[tuple[str, str], list[dict]]] = {}
    names = {}
    for row in relations:
        if row.get("status") != "verified" or row.get("level") != "A":
            continue
        identity = row.get("stockIdentity") or {}
        ticker = identity.get("ticker")
        chain = str(row.get("chainId") or "")
        key = (chain, str(row.get("token") or "").lower())
        if not ticker or key not in assets_by_key or not row.get("pool"):
            continue
        ticker = str(ticker).upper()
        groups.setdefault(ticker, {}).setdefault(key, []).append(row)
        names.setdefault(ticker, identity.get("nameEn"))

    # One bubble per contract, even when it belongs to more than one theme.
    by_asset: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    themes = []
    for ticker in sorted(groups):
        counts = {"rising": 0, "falling": 0, "flat": 0, "unknown": 0}
        for key, matches in groups[ticker].items():
            row = assets_by_key[key]
            change = _field(row, "change24h", now, unit="percent")
            price = _field(row, "price", now, unit="price", minimum=0)
            if change["status"] != "current" or price["status"] != "current":
                counts["unknown"] += 1
            elif change["value"] > 0:
                counts["rising"] += 1
            elif change["value"] < 0:
                counts["falling"] += 1
            else:
                counts["flat"] += 1
            by_asset.setdefault(key, []).append((ticker, max(matches, key=_relation_order)))
        valid = counts["rising"] + counts["falling"] + counts["flat"]
        themes.append({
            "ticker": ticker, "nameEn": names.get(ticker), "nameZh": localized.get(ticker),
            "breadth": {**counts, "valid": valid, "total": len(groups[ticker]), "window": "24h"},
            "assetKeys": [],
        })

    bubbles = []
    for key, associations in by_asset.items():
        chain, token = key
        asset = assets_by_key[key]
        volume = _field(asset, "volume24h", now, unit="volume", minimum=0, require_usd=True)
        change = _field(asset, "change24h", now, unit="percent")
        price = _field(asset, "price", now, unit="price", minimum=0, require_usd=True)
        # Prefer the current, most liquid documented pair, then a stable ticker.
        associations.sort(key=lambda item: (-(_relation_order(item[1])[0]), item[0]))
        primary_ticker, representative = associations[0]
        bubbles.append({
            "key": f"{chain}:{token}", "chainId": chain, "token": token,
            "symbol": asset.get("symbol"), "name": asset.get("name"),
            "primaryTicker": primary_ticker,
            "otherTickers": sorted({ticker for ticker, _ in associations if ticker != primary_ticker}),
            "relation": _relation_summary(representative),
            "volume24h": volume, "change24h": change, "price": price,
        })
    bubbles.sort(key=lambda item: (
        item["volume24h"]["status"] != "current",
        -(item["volume24h"]["value"] or 0), item["key"],
    ))
    bubbles = bubbles[:MAX_BUBBLES]
    theme_index = {theme["ticker"]: theme for theme in themes}
    for bubble in bubbles:
        theme_index[bubble["primaryTicker"]]["assetKeys"].append(bubble["key"])
    themes.sort(key=lambda item: (-len(item["assetKeys"]), -item["breadth"]["total"], item["ticker"]))
    last_observed = max((observed for bubble in bubbles
                         for observed in (
                             bubble["price"]["observedAt"],
                             bubble["volume24h"]["observedAt"],
                             bubble["change24h"]["observedAt"],
                             bubble["relation"]["checkedAt"],
                         ) if _time(observed) is not None and observed <= now), default=None)
    return {
        "version": METHOD, "asOf": last_observed, "window": "24h",
        "scope": "current-official-verified-stock-pools",
        "sizeMetric": "asset-volume24h-usd", "colorMetric": "asset-change24h-percent",
        "totalAssets": len(by_asset), "displayedAssets": len(bubbles),
        "themes": themes, "bubbles": bubbles,
    }


def build_important_changes(signals: list[dict], relations: list[dict], assets: list[dict],
                            now: int) -> dict:
    """A verified event is site verification, never a claim of new pool creation."""
    active = {}
    for row in relations:
        if row.get("status") == "verified" and row.get("level") == "A":
            key = (str(row.get("chainId") or ""),
                   str(row.get("token") or "").lower(), str(row.get("pool") or "").lower())
            selected = active.get(key)
            if selected is None or _relation_order(row) > _relation_order(selected):
                active[key] = row
    symbols = {(str(row.get("chainId") or ""), str(row.get("token") or "").lower()): row.get("symbol")
               for row in assets}
    items = []
    seen = set()
    for signal in signals:
        if signal.get("kind") != "verified" or not signal.get("id"):
            continue
        verified_at = _time(signal.get("t"))
        if verified_at is None or not 0 <= now - verified_at <= CHANGE_WINDOW_MS:
            continue
        chain = str(signal.get("chainId") or "")
        token = str(signal.get("asset") or "").removeprefix(f"{chain}:").lower()
        pool = str(signal.get("pool") or "").lower()
        relation = active.get((chain, token, pool))
        if relation is None:
            continue
        event_id = str(signal["id"])
        if event_id in seen:
            continue
        seen.add(event_id)
        occurred_at = _time(relation.get("poolCreatedAt"))
        if occurred_at is not None and occurred_at > now:
            occurred_at = None
        items.append({
            "id": event_id, "type": "relation-verified", "chainId": chain,
            "token": token, "symbol": symbols.get((chain, token)) or signal.get("symbol"),
            "ticker": (relation.get("stockIdentity") or {}).get("ticker"),
            "occurredAt": occurred_at,
            "discoveredAt": _time(relation.get("discoveredAt") or relation.get("firstSeen")),
            "verifiedAt": verified_at,
            "relation": _relation_summary(relation),
            "window": None, "metrics": None,
            "coverage": {"currentRelationLevel": "A", "poolCreationKnown": occurred_at is not None},
        })
    items.sort(key=lambda item: (-item["verifiedAt"], item["id"]))
    return {"version": "verified-change-feed-v1",
            "asOf": max((item["verifiedAt"] for item in items), default=None),
            "scope": "site-verification-events", "items": items[:MAX_CHANGES]}
