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
    "volume5M": "volume5m",
    "volume1H": "volume1h",
    "volume": "volume24h", "txs": "txs24h", "change": "change24h",
}

NUMERIC_FIELDS = list(ALIASES.values())


def now_ms() -> int:
    return int(time.time() * 1000)


def _num(value):
    try:
        n = float(value)
        return n if n == n and n not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


async def save_asset(s, row: dict, *, return_changed: bool = False, checkpoint=None):
    """Merge an OKX quote row, optionally reporting a durable data change."""
    token = (row.get("tokenContractAddress") or "").lower()
    if not token.startswith("0x") or len(token) != 42:
        return (None, False) if return_changed else None
    if str(row.get("chainIndex") or "") != str(s.scope):
        return (None, False) if return_changed else None
    received = now_ms()
    market_at = _num(row.get("time"))
    if market_at is not None and (market_at <= 0 or market_at > received + 60_000):
        market_at = None
    observed = market_at or received
    base_patch = {"token": token, "chain": str(s.scope), "chainId": str(s.scope)}
    old = await s.get("asset", token) or {}
    symbol = str(row.get("tokenSymbol") or old.get("symbol") or "").upper()
    if old.get("kind") == "candidate" and symbol in {"USDT", "USDC", "USDG", "DAI", "WOKB", "OKB", "WETH", "ETH", "WBTC", "BTC", "XBTC", "WBNB", "BTCB", "USD1", "XUSD"}:
        base_patch["kind"] = "quote"
    if not old.get("kind"):
        stock = await s.get("stock", token)
        base_patch["kind"] = "stock" if stock else "quote" if symbol in {
            "USDT", "USDC", "USDG", "DAI", "WOKB", "OKB", "WETH", "ETH", "WBTC", "BTC", "XBTC", "WBNB", "BTCB", "USD1", "XUSD"
        } else "candidate"
    if base_patch.get("kind") == "candidate" and not old:
        base_patch["discoveryEventPending"] = True
    timed_fields = {}
    field_sources = {}

    for raw, field in ALIASES.items():
        if raw in row:
            value = _num(row.get(raw))
            # Zero/negative price is an absent market quote, not a free token.
            # Keep valid zero activity and liquidity in their own fields.
            if field == 'price' and (value is None or value <= 0):
                continue
            if value is not None or raw in ("holders",):
                timed_fields[field] = (value, observed)
                field_sources[field] = {
                    "provider": "OKX", "scope": "token", "venue": "dex", "currency": "USD",
                    "timeKind": "market" if market_at is not None else "received",
                    "marketAt": market_at, "receivedAt": received,
                    "window": "24h" if field.endswith("24h") else "5m" if field.endswith("5m") else "1h" if field.endswith("1h") else None,
                }
    if "tokenSymbol" in row and row.get("tokenSymbol"):
        base_patch["symbol"] = str(row["tokenSymbol"])
    if "tokenName" in row and row.get("tokenName"):
        base_patch["name"] = str(row["tokenName"])
    if "tokenLogoUrl" in row and row.get("tokenLogoUrl"):
        base_patch["logoUrl"] = row["tokenLogoUrl"]
    base_patch["firstSeen"] = old.get("firstSeen") or received
    base_patch["fieldSources"] = {field: value["provider"] for field, value in field_sources.items()}
    base_patch["fieldObservations"] = field_sources
    base_patch["fieldScopes"] = {field: "token" for field in field_sources}
    base_patch["fieldTimeKinds"] = {field: value["timeKind"] for field, value in field_sources.items()}
    price = timed_fields.get("price", (None, observed))[0]
    cap = timed_fields.get("marketCap", (old.get("marketCap"), observed))[0]
    sample = (price, cap, observed) if price is not None else None
    options = {'return_changed': return_changed}
    if checkpoint is not None:
        options['checkpoint'] = checkpoint
    return await s.merge_asset_observation(token, base_patch, timed_fields, sample,
                                           **options)


def price_changed(old: dict | None, new: dict | None) -> bool:
    if not new:
        return False
    if not old:
        return new.get("price") is not None
    return old.get("price") != new.get("price")
