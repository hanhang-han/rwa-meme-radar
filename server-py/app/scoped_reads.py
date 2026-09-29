"""Small fact reads for detail pages and comparison cache misses.

These projections follow DashboardData.reload/stock_views without rebuilding
the dashboard's unrelated chains, assets, events and market state.
"""
import json
import time

from . import stock_quotes
from .comparisons import VERSION, independent_stock_row
from .market_quotes import enrich_asset, enrich_relation
from .state import BASE_QUOTE_SYMBOLS, unquoted_market_status
from .stock_identity import token_identity


async def candidate_relations(s, chain: str, address: str | None = None) -> list[dict]:
    """Load relations whose meme remains a visible candidate asset."""
    query = """SELECT relation.body, candidate.body
        FROM facts AS relation
        JOIN facts AS candidate
          ON candidate.kind = ? AND candidate.id = json_extract(relation.body, '$.token')
        WHERE relation.kind = ?"""
    params: list[str] = [s.key("asset"), s.key("relation")]
    if address is not None:
        query += " AND (json_extract(relation.body, '$.token') = ? OR json_extract(relation.body, '$.stock') = ?)"
        params.extend((address, address))
    query += " ORDER BY relation.id"
    rows = await s.fetchall(query, tuple(params))
    result = []
    for relation_body, candidate_body in rows:
        relation, candidate = json.loads(relation_body), json.loads(candidate_body)
        if (candidate.get("token") != relation.get("token")
                or candidate.get("kind") != "candidate"
                or str(candidate.get("symbol") or "").upper() in BASE_QUOTE_SYMBOLS):
            continue
        relation["chainId"] = chain
        relation.setdefault("liquidityUsd", None)
        result.append(enrich_relation(relation))
    return result


async def token_pools(s, address: str) -> list[dict]:
    """Only pools containing the detail address, in the old fact order."""
    rows = await s.fetchall("""SELECT body FROM facts WHERE kind = ?
        AND (lower(json_extract(body, '$.token0')) = ? OR lower(json_extract(body, '$.token1')) = ?)
        ORDER BY id""", (s.key("pool"), address, address))
    return [json.loads(row[0]) for row in rows]


async def stock_view(s, chain: str, address: str, asset: dict | None = None,
                     *, include_comparison: bool = True) -> dict | None:
    """Render one directory stock using the same overlays as stock_views."""
    persisted = await s.get("stock", address)
    snapshot = (stock_quotes.binance_token(address) if chain == "56" else
                stock_quotes.robinhood_token(address) if chain == "4663" else None)
    if not persisted and not snapshot:
        return None
    row = {**(persisted or {}), **(snapshot or {})}
    row.update(chainId=chain, chain=chain)
    row.setdefault("tokenContractAddress", address)
    row["issuerIdentity"] = token_identity(chain, row.get("tokenContractAddress"), row.get("tokenSymbol"))
    row["verificationStatus"] = row["issuerIdentity"]["verificationStatus"]
    if asset is None:
        asset = await s.get("asset", address)
    if asset:
        fact = enrich_asset({**asset, "chainId": chain, "chain": chain})
        for field in ("price", "volume24h", "change24h", "fieldTimes", "fieldSources", "quoteAt",
                      "provider", "priceProvenance", "fieldScopes", "fieldTimeKinds", "fieldStatus",
                      "fieldObservations"):
            if fact.get(field) is not None:
                row[field] = fact[field]
    market_quotes = []
    if chain == "56":
        quotes = await s.fetchall("""SELECT body FROM facts WHERE kind = ?
            AND json_extract(body, '$.token') = ? AND json_extract(body, '$.venue') = 'binance'
            AND json_extract(body, '$.priceCurrency') = 'USDT'""",
            (s.key("market-quote"), address))
        market_quotes = [{**json.loads(item[0]), "chainId": chain} for item in quotes]
    row = stock_quotes.apply_stock_overlays([row], market_quotes)[0]
    job = (await s.get("collector-job", "quote:" + address)
           if asset and asset.get("kind") in ("candidate", "stock") and asset.get("price") is None else None)
    now = time.time() * 1000
    row = unquoted_market_status(row, job, now)
    if include_comparison:
        from .comparison_service import public_metric
        packet = await s.get("comparison", address)
        if packet and packet.get("method") == VERSION:
            metric = public_metric(packet.get("premium"), now)
            inputs = metric.get("inputs") or {}
            reference = independent_stock_row(row)
            if (metric.get("value") is not None and inputs.get("referenceAt") == reference.get("referenceAt")
                    and inputs.get("ratioVersion") == reference.get("ratioVersion")):
                row["premium"] = metric
    return row
