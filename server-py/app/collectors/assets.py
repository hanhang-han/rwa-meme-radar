"""Asset normalization and persistence: the field-level merge semantics of the
Node normalizeXAsset/saveAsset — only fields present in the response move,
each carrying its own observation timestamp."""
import time

# OKX response key -> asset field
ALIASES = {
    "price": "price",
    "marketCap": "marketCap",
    "volume24H": "volume24h",
    "txs24H": "txs24h",
    "txsBuy": "buys24h",
    "txsSell": "sells24h",
    "holders": "holders",
    "liquidity": "liquidity",
    "priceChange24H": "change24h",
}

NUMERIC_FIELDS = [f for f in ALIASES.values()] + ["volume5M", "volume1H"]


def now_ms() -> int:
    return int(time.time() * 1000)


def _num(value):
    try:
        n = float(value)
        return n if n == n and n not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


async def save_asset(s, row: dict) -> dict | None:
    """Merge an OKX quote row into the stored asset (field-level last write)."""
    token = (row.get("tokenContractAddress") or "").lower()
    if not token.startswith("0x") or len(token) != 42:
        return None
    old = await s.get("asset", token) or {}
    observed = _num(row.get("time")) or now_ms()
    asset = dict(old)
    asset["token"] = token
    asset.setdefault("kind", "candidate")
    ft = dict(old.get("fieldTimes") or {})

    for raw, field in ALIASES.items():
        if raw in row:
            value = _num(row.get(raw))
            if value is not None or raw in ("holders",):
                asset[field] = value
                ft[field] = observed
    if "tokenSymbol" in row and row.get("tokenSymbol"):
        asset["symbol"] = str(row["tokenSymbol"])
    if "tokenName" in row and row.get("tokenName"):
        asset["name"] = str(row["tokenName"])
    if "tokenLogoUrl" in row and row.get("tokenLogoUrl"):
        asset["logoUrl"] = row["tokenLogoUrl"]
    asset["fieldTimes"] = ft
    asset.setdefault("firstSeen", observed)
    asset["updatedAt"] = observed

    await s.put("asset", token, asset)
    # Keep the 5-minute sample line alive on every priced observation,
    # matching the Node saveAsset semantics.
    if "price" in row and asset.get("price") is not None:
        await s.sample(token, asset["price"], asset.get("marketCap"), observed)
    return asset


def price_changed(old: dict | None, new: dict | None) -> bool:
    if not new:
        return False
    if not old:
        return new.get("price") is not None
    return old.get("price") != new.get("price")
