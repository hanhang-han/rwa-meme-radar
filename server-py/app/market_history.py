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


def _merge_trade_rows(rows, limit):
    """Match TRADE_ORDER when merging already bounded market pages."""
    def integer(value):
        try:
            return int(value or 0)
        except (ValueError, TypeError, OverflowError):
            return 0
    decoded = [(row, json.loads(row['body'])) for row in rows]
    # Stable second sort keeps the final id tie-break descending.
    decoded.sort(key=lambda pair: pair[0]['id'], reverse=True)
    def key(pair):
        row, body = pair
        source = body.get('sourceId')
        if source is None and row['id'].startswith(('trade:', 'agg:')):
            source = row['id'].partition(':')[2]
        return (-row['t'], -integer(body.get('blockNumber')), -integer(body.get('transactionIndex')),
                -integer(body.get('logIndex')), -integer(source), row['asset'])
    decoded.sort(key=key)
    return [row for row, _ in decoded[:limit]]


async def _bounded_trade_rows(s, storages, limit, offset=0):
    """Index seeks and bounded per-market pages, never sort all tape history.

    The old asset IN (...) query sorted every historical row from all markets.
    Read heads first for large catalogues, then stop once every remaining head
    is older than the page cutoff. Equal-time heads are included for stable
    blockchain/source sequence ordering. Work is independent of history size.
    """
    storages = sorted(set(storages))
    if not storages:
        return []
    take = limit+offset
    heads = [(storage, None) for storage in storages]
    if len(storages) > 16:
        heads = []
        for start in range(0, len(storages), 200):
            chunk = storages[start:start+200]
            query = ('WITH requested(asset) AS (VALUES '+','.join('(?)' for _ in chunk)+') '
                'SELECT asset,(SELECT t FROM trades INDEXED BY trades_time '
                'WHERE trades.asset=requested.asset ORDER BY t DESC LIMIT 1) newest FROM requested')
            heads.extend((row['asset'], row['newest']) for row in await s.fetchall(query, chunk)
                         if row['newest'] is not None)
        heads.sort(key=lambda row: (-row[1], row[0]))
    times = []
    # This first pass uses the covering (asset,t) index only. Loading 100
    # complete JSON bodies from every pool is still expensive on a large cold
    # tape, even when each individual lookup is bounded.
    subquery = ('SELECT t FROM (SELECT t FROM trades INDEXED BY trades_time '
                'WHERE asset=? ORDER BY t DESC LIMIT ?)')
    for start in range(0, len(heads), 16):
        chunk = heads[start:start+16]
        query = 'SELECT t FROM ('+' UNION ALL '.join(subquery for _ in chunk)+') ORDER BY t DESC LIMIT ?'
        params = [value for storage, _ in chunk for value in (storage, take)]+[take]
        times = sorted([*times, *(row['t'] for row in await s.fetchall(query, params))], reverse=True)[:take]
        next_index = start+len(chunk)
        if (len(times) >= take and next_index < len(heads) and heads[next_index][1] is not None
                and heads[next_index][1] < times[-1]):
            break
    if not times:
        return []
    cutoff = times[-1]
    selected = [storage for storage, newest in heads if newest is None or newest >= cutoff]
    merged = []
    # Include every row tied at the cutoff before sequence ordering/limit.
    # An earlier arbitrary LIMIT by rowid could drop the highest logIndex.
    for start in range(0, len(selected), 200):
        chunk = selected[start:start+200]
        query = ('SELECT asset,id,t,body FROM trades INDEXED BY trades_time WHERE asset IN ('
                 +','.join('?' for _ in chunk)+') AND t>=? ORDER BY '+TRADE_ORDER+' LIMIT ?')
        merged = _merge_trade_rows([*merged, *await s.fetchall(query, [*chunk, cutoff, take])], take)
    return merged[offset:offset+limit]


async def token_trades(s, tokens, limit=100):
    """Merge legacy token tapes in one bounded path instead of N API reads."""
    limit = max(1, min(int(limit), 200))
    tokens = {s.key(str(token).lower()): str(token).lower() for token in tokens}
    rows = await _bounded_trade_rows(s, tokens, limit)
    return [{**json.loads(row['body']), 'token': tokens[row['asset']]} for row in rows]


async def market_trades(s, token=None, limit=100, *, offset=0, dex_only=False, tokens=None):
    limit = max(1, min(int(limit), 200))
    offset = max(0, min(int(offset), 10000))
    if token or tokens is not None:
        if tokens is not None:
            tokens = sorted({str(t).lower() for t in tokens})
            if not tokens:
                return []
            found = await s.fetchall("SELECT body FROM facts WHERE kind=? AND lower(json_extract(body,'$.token')) IN (" + ','.join('?' for _ in tokens) + ")",
                                     [s.key('market-registry'), *tokens])
            rows = [json.loads(row[0]) for row in found]
        else:
            rows = await token_market_records(s, "market-registry", token)
        storages = sorted({s.key(r["storage"]) for r in rows
                           if r.get("storage") and (not token or str(r.get("token") or "").lower() == token.lower())
                           and (not dex_only or r.get("venue") == "dex")})
        if not storages:
            return []
        rows = await _bounded_trade_rows(s, storages, limit, offset)
        return [json.loads(row['body']) for row in rows]
    else:
        # Constrain chain before the time walk: a quiet chain must not scan a
        # million newer rows belonging to the other chains merely to find 100.
        query = "SELECT body FROM trades INDEXED BY trades_scope_time WHERE substr(asset,1,instr(asset,':')-1)=? AND (asset LIKE ? OR asset LIKE ? OR asset LIKE ?) ORDER BY " + TRADE_ORDER + " LIMIT ?"
        if dex_only:
            query = "SELECT body FROM trades INDEXED BY trades_scope_time WHERE substr(asset,1,instr(asset,':')-1)=? AND asset LIKE ? ORDER BY " + TRADE_ORDER + " LIMIT ? OFFSET ?"
            params = [s.scope, s.key("dex:") + "%", limit, offset]
        else:
            query += " OFFSET ?"
            params = [s.scope] + [s.key(v) + "%" for v in ("binance:", "binance-alpha:", "dex:")] + [limit, offset]
    return [json.loads(r[0]) for r in await s.fetchall(query, params)]


async def token_market_records(s, kind, token):
    rows = await s.fetchall("SELECT body FROM facts WHERE kind=? AND lower(json_extract(body,'$.token'))=? ORDER BY id LIMIT 200",
                           (s.key(kind), token.lower()))
    return [json.loads(row[0]) for row in rows]


async def attach_market_detail(payload, s, *, canonical=False, limit=100, offset=0):
    token = str((payload.get("asset") or {}).get("token") or "").lower()
    quotes = await token_market_records(s, "market-quote", token)
    if payload.get("stock") and not canonical:
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
    for record in await token_market_records(s, 'market-registry', token):
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
    payload["marketTrades"] = await market_trades(s, token, limit, offset=offset)
    return payload
