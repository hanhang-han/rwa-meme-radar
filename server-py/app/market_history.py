"""Read persisted market tapes without mixing them into aggregate DEX metrics."""
import json
import math

# Keep time first so the global time index can stop after the newest window.
# Tie-breaking uses normalized numeric wire fields: lexical ids place trade:99
# ahead of trade:100, and transaction hashes cannot order swaps within a block.
TRADE_ORDER = """t DESC,
    COALESCE(CAST(json_extract(body,'$.blockNumber') AS INTEGER),0) DESC,
    COALESCE(CAST(json_extract(body,'$.transactionIndex') AS INTEGER),0) DESC,
    COALESCE(CAST(json_extract(body,'$.logIndex') AS INTEGER),0) DESC,
    COALESCE(CAST(json_extract(body,'$.sourceId') AS INTEGER),
        CASE WHEN id LIKE 'trade:%' OR id LIKE 'agg:%'
             THEN CAST(substr(id,instr(id,':')+1) AS INTEGER) ELSE 0 END) DESC,
    asset, id DESC"""


async def market_trades(s, token=None, limit=100):
    limit = max(1, min(int(limit), 200))
    if token:
        rows = await s.all("market-registry")
        storages = sorted({s.key(r["storage"]) for r in rows
                           if r.get("storage") and str(r.get("token") or "").lower() == token.lower()})
        if not storages:
            return []
        query = "SELECT body FROM trades WHERE asset IN (" + ",".join("?" for _ in storages) + ") ORDER BY " + TRADE_ORDER + " LIMIT ?"
        params = [*storages, limit]
    else:
        # Constrain chain before the time walk: a quiet chain must not scan a
        # million newer rows belonging to the other chains merely to find 100.
        query = "SELECT body FROM trades INDEXED BY trades_scope_time WHERE substr(asset,1,instr(asset,':')-1)=? AND (asset LIKE ? OR asset LIKE ? OR asset LIKE ?) ORDER BY " + TRADE_ORDER + " LIMIT ?"
        params = [s.scope] + [s.key(v) + "%" for v in ("binance:", "binance-alpha:", "dex:")] + [limit]
    return [json.loads(r[0]) for r in await s.fetchall(query, params)]


async def attach_market_detail(payload, s):
    token = str((payload.get("asset") or {}).get("token") or "").lower()
    quotes = [r for r in await s.all("market-quote") if str(r.get("token") or "").lower() == token]
    if payload.get("stock"):
        from .stock_quotes import apply_stock_overlays
        stock = apply_stock_overlays([payload["stock"]], quotes)[0]
        payload["stock"] = stock
        if stock.get("priceScope") == "exchange":
            for field in ("price", "stockPrice", "quoteAt", "quoteStatus", "quoteReason", "provider",
                          "venue", "marketId", "priceCurrency", "priceScope", "volumeScope",
                          "volumeCurrency", "volume24h", "change24h", "fieldTimes", "fieldSources", "priceProvenance"):
                if field in stock:
                    payload["asset"][field] = stock[field]
    markets = [{k: q.get(k) for k in (
        "marketId", "venue", "price", "priceCurrency", "volume24h", "volumeCurrency",
        "exchangeTrades24h", "change24h", "quoteAt", "provider", "sourceEventAt", "receivedAt",
        "priceScope", "volumeScope")}
        for q in quotes if q.get("venue") == "binance-alpha"]
    payload["asset"]["exchangeMarkets"] = markets
    pool_markets=[]
    for record in await s.all('market-registry'):
        if record.get('venue')!='dex' or str(record.get('token') or '').lower()!=token or not record.get('poolId'):
            continue
        pool_id=record['poolId']
        pool=await s.get('pool',pool_id) or {}
        meta=await s.get('candle-meta',f'dex:{s.scope}:{token}:1m:{record["marketId"]}') or {}
        candidates=[pool]+[r for r in payload.get('relations',[]) if r.get('pool')==pool_id]
        valuations=[]
        for candidate in candidates:
            value=candidate.get('liquidityUsd')
            if value is None or isinstance(value,bool):
                continue
            try:
                number=float(value)
                at=float(candidate.get('liquidityAt') or candidate.get('updatedAt') or 0)
                if math.isfinite(number) and number>=0:
                    valuations.append((at,number))
            except (ValueError,TypeError):
                pass
        valuation=max(valuations,key=lambda item:item[0]) if valuations else None
        frame={key:record.get(key) for key in (
            'chainId','token','venue','marketId','poolId','pool','quoteToken','symbol',
            'priceCurrency','volumeCurrency','priceScope','volumeScope','quoteType')}
        pool_markets.append({**frame,'lastTradeAt':meta.get('lastSourceEventAt'),
                             'liquidityUsd':valuation[1] if valuation else None,
                             'liquidityAt':valuation[0] if valuation and valuation[0] else None})
    pool_markets.sort(key=lambda row:(row['liquidityUsd'] is not None,row['liquidityUsd'] or 0,
                                     row['lastTradeAt'] or 0),reverse=True)
    payload['asset']['poolMarkets']=pool_markets
    payload['poolMarkets']=pool_markets
    payload["marketTrades"] = await market_trades(s, token, 100)
    return payload
