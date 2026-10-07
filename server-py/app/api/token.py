"""Asset detail endpoint: persisted asset, relations, samples, recent trades,
events and activity aggregates, matching the Node response shape.

Opening a detail page only writes a short-lived watch lease. The collector
worker batches those leases and remains the sole owner of upstream calls."""
import time
import copy
import json
import math

from fastapi import APIRouter, HTTPException

from ..db import store
from ..activity_reads import read_activity
from ..data_quality import evaluate_asset
from ..state import _assessed_relations, _official_pools, _pool_totals, unquoted_market_status
from ..scoped_reads import candidate_relations, stock_view, token_pools
from .. import stock_quotes
from ..market_quotes import enrich_asset
from ..market_history import attach_market_detail
from ..realtime_projection import read_token_projection, ProjectionUnavailable
from ..dashboard_projection import asset_summary, stock_summary, relation_summary, ASSET_FIELDS, TIME_FIELDS
from ..stock_identity import match_name, token_identity

router = APIRouter()


def _asset_view(a: dict, chain: str) -> dict:
    out = dict(a)
    out.setdefault("chainId", chain)
    out.setdefault("fieldTimes", {})
    return enrich_asset(out)


def _stock_view(token: dict, chain: str) -> dict:
    out = dict(token)
    out.setdefault("chainId", chain)
    out.setdefault("chain", chain)
    view = stock_quotes.apply_stock_overlays([out])[0]
    view['issuerIdentity'] = token_identity(chain, view.get('tokenContractAddress'), view.get('tokenSymbol'))
    view['verificationStatus'] = view['issuerIdentity']['verificationStatus']
    return view


def _analysis(kind: str, verified: list[dict], issuer: str | None) -> dict:
    if kind == "stock":
        return {
            "conclusion": f"该股票代币已进入统一目录，已核验 {len(verified)} 个关联配对池。",
            "correlation": {"reason": "等待独立 Meme 池和对齐样本"},
            "capture": {"reason": "等待完整池覆盖"},
            "safety": "股票参考价、发行方估值和链上成交属于不同市场；缺少独立报价时不提供实时溢价结论。",
        }
    if verified:
        return {
            "conclusion": "已核验股票配对",
            "correlation": {"reason": "等待独立价格样本与同口径对照"},
            "capture": {"reason": f"覆盖 {len(verified)} 个已核验配对池"},
            "safety": "交易活跃度不能证明与股票存在资金联系。",
        }
    return {
        "conclusion": "尚未完成配对池核验。",
        "correlation": {"reason": "等待独立价格样本与同口径对照"},
        "capture": {"reason": "等待完整池覆盖"},
        "safety": "交易活跃度不能证明与股票存在资金联系。",
    }


def _empty_activity() -> dict:
    return {"count": None, "buys": None, "sells": None, "volume": None, "traders": None,"scope":"dex","status":"not-observed"}


def sample_metadata(samples, asset):
    scope,currency=asset.get('priceScope','dex'),asset.get('priceCurrency','USD')
    def matches(row):
        p=row.get('provenance') or {}
        venue=p.get('venue') or p.get('scope')
        venue='exchange' if venue=='binance' else venue
        at=p.get('marketAt') if p.get('timeKind')=='market' else p.get('receivedAt')
        return bool(p.get('provider') and p.get('currency')==currency and venue==scope
                    and p.get('timeKind') in ('market','received','observed') and at
                    and row['t']<=at<row['t']+300000)
    known=sum(matches(row) for row in samples)
    return {'verified':bool(samples) and known==len(samples),'known':known,'total':len(samples),
            'scope':scope,'currency':currency,'status':'verified' if samples and known==len(samples) else 'partial' if known else 'unknown'}


SECTION_NAMES = {'summary', 'trades', 'holders', 'relations', 'markets'}
CANONICAL_FIELDS = ASSET_FIELDS | {'fieldTimes', 'fieldSources', 'fieldScopes', 'fieldStatus',
    'fieldTimeKinds', 'priceProvenance', 'dataQuality', 'riskAssessment', 'primaryQuote', 'quoteAlternatives'}


def _canonical_asset(snapshot, chain, address):
    row = copy.deepcopy(snapshot.get('asset'))
    stock = snapshot.get('stock')
    if row is None and stock:
        row = {**copy.deepcopy(stock), 'token': address, 'chainId': chain, 'kind': 'stock',
               'symbol': stock.get('tokenSymbol'), 'name': stock.get('tokenName')}
    return row


def _stamp(snapshot):
    return {'snapshotAt': snapshot.get('snapshotAt'), 'realtime': snapshot.get('realtime'),
            'revision': (snapshot.get('realtime') or {}).get('revision')}


def _canonical_overlay(body, snapshot, chain, address):
    canonical = _canonical_asset(snapshot, chain, address)
    if canonical is not None:
        row = body['asset']
        for field in CANONICAL_FIELDS:
            if field in canonical:
                row[field] = copy.deepcopy(canonical[field])
            elif field in row and field not in {'token', 'chainId', 'symbol', 'name', 'kind', 'firstSeen'}:
                row.pop(field, None)
        if snapshot.get('stock'):
            body['stock'] = copy.deepcopy(snapshot['stock'])
    body.update(_stamp(snapshot))
    return body


async def _token_section(section, s, snapshot, chain, address, limit, offset):
    asset = _canonical_asset(snapshot, chain, address)
    if asset is None:
        raise HTTPException(status_code=404, detail='token-not-in-published-snapshot')
    base = _stamp(snapshot)
    relations = snapshot.get('relations') or []
    if section == 'summary':
        verified = [r for r in relations if r.get('level') == 'A']
        result = {**base, 'asset': asset_summary(asset),
                  'stock': stock_summary(snapshot['stock']) if snapshot.get('stock') else None,
                  'relations': [relation_summary(r) for r in relations[:8]], 'relationCount': len(relations),
                  'analysis': _analysis(asset.get('kind'), verified, None)}
        result['asset'].pop('marketQuotes', None)
        result['asset'].pop('exchangeMarkets', None)
        if result['stock']:
            result['stock'].pop('marketQuotes', None)
        # Detailed comparative evidence is loaded in the relations section.
        for relation in result['relations']:
            for field in ('priceComparison', 'amounts', 'stockIdentity', 'sideIdentity'):
                relation.pop(field, None)
        while len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) > 25_000 and result['relations']:
            result['relations'].pop()
        return result
    if section == 'holders':
        persisted = await s.get('asset', address) or {}
        raw_risk = persisted.get('risk') or {}
        observation = persisted.get('securityObservation') or {}
        goplus_holders = observation.get('holders') or {}
        candidates = [
            {'top10Percent': raw_risk.get('top10'), 'checkedAt': raw_risk.get('checkedAt'), 'provider': raw_risk.get('provider')},
            {'top10Percent': goplus_holders.get('top10RawPercent'), 'checkedAt': observation.get('checkedAt'), 'provider': observation.get('provider')},
        ]
        valid = [r for r in candidates if isinstance(r['top10Percent'], (int, float))
                 and not isinstance(r['top10Percent'], bool) and math.isfinite(r['top10Percent']) and 0 <= r['top10Percent'] <= 100]
        distribution = max(valid, key=lambda r: r.get('checkedAt') or 0) if valid else {'top10Percent': None}
        distribution.update(exclusionsApplied=False, scope='raw-top10-including-pools')
        adjusted = persisted.get('holderDistribution') or {}
        share = adjusted.get('top10AdjustedPercent')
        if (adjusted.get('excludedKnownAddresses') is True and isinstance(share, (int, float))
                and not isinstance(share, bool) and math.isfinite(share) and 0 <= share <= 100
                and adjusted.get('provider') and adjusted.get('checkedAt')):
            distribution = {**adjusted, 'top10Percent': share, 'exclusionsApplied': True,
                            'scope': adjusted.get('scope') or 'adjusted-top10-excluding-known-addresses'}
        return {**base, 'asset': {k: asset.get(k) for k in ('token', 'chainId', 'holders', 'fieldTimes', 'fieldSources', 'riskFlags', 'riskStatus', 'riskAssessment')},
                'holdersSummary': {'count': asset.get('holders'), 'observedAt': (asset.get('fieldTimes') or {}).get('holders'),
                    'provider': (asset.get('fieldSources') or {}).get('holders'),
                    'distribution': distribution, 'securityObservation': persisted.get('securityObservation')}}
    if section == 'trades':
        rows = await s.fetchall('SELECT body FROM trades WHERE asset=? ORDER BY t DESC,id DESC LIMIT ? OFFSET ?',
                                (s.key(address), limit+1, offset))
        trades = [json.loads(row[0]) for row in rows]
        for trade in trades:
            trade.setdefault('wallet', trade.get('user'))
            trade.setdefault('venue', 'dex')
            trade.setdefault('sourceEventAt', trade.get('t'))
        activity = await read_activity(s, address)
        activity.update(scope='dex', status='observed')
        return {**base, 'trades': trades[:limit], 'tradesScope': 'dex', 'activity': activity,
                'next': offset+limit if len(trades)>limit else None}
    if section == 'relations':
        rows = relations[offset:offset+limit+1]
        return {**base, 'relations': copy.deepcopy(rows[:limit]), 'pools': (await token_pools(s, address))[:limit],
                'scan': await s.get('scan', address), 'next': offset+limit if len(rows)>limit else None}
    market = await attach_market_detail({'asset': asset, 'stock': copy.deepcopy(snapshot.get('stock')),
                                         'relations': relations}, s, canonical=True, limit=limit+1, offset=offset)
    trades = market.get('marketTrades') or []
    return {**base, 'asset': {key: market['asset'].get(key) for key in ('token', 'chainId', 'exchangeMarkets', 'poolMarkets', 'primaryQuote', 'quoteAlternatives')},
            'poolMarkets': market.get('poolMarkets', []), 'marketTrades': trades[:limit],
            'markets': [*(market['asset'].get('exchangeMarkets') or []), *(market.get('poolMarkets') or [])],
            'next': offset+limit if len(trades)>limit else None}


@router.get("/token/{chain}/{address}")
async def get_token(chain: str, address: str, section: str | None = None, limit: int = 50, offset: int = 0):
    address = address.lower()
    import re
    if chain not in ("196", "56", "4663", "5042") or not re.fullmatch(r'0x[0-9a-f]{40}', address):
        raise HTTPException(status_code=400, detail="unsupported token")
    if section is not None and section not in SECTION_NAMES:
        raise HTTPException(status_code=400, detail='unsupported token section')
    if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
        raise HTTPException(status_code=400, detail='invalid pagination')
    s = await store(chain)
    try:
        snapshot = await read_token_projection(chain, address)
    except ProjectionUnavailable as exc:
        raise HTTPException(status_code=503, detail='snapshot-not-ready', headers={'Retry-After': '3'}) from exc
    if section:
        from ..demand_leases import publish_lease
        if section == 'summary' and (snapshot.get('asset') or snapshot.get('stock')):
            publish_lease(s, 'watch', address, {'chainId': chain, 'token': address,
                          'expiresAt': int(time.time()*1000)+90_000})
        return await _token_section(section, s, snapshot, chain, address, limit, offset)
    asset = await s.get("asset", address)
    if not snapshot.get('asset') and not snapshot.get('stock') and asset:
        raise HTTPException(status_code=503, detail='token-snapshot-pending', headers={'Retry-After': '3'})
    if asset:
        # Query processes only publish a short-lived demand lease.  The sole
        # worker batches these leases and owns every upstream request.
        from ..demand_leases import publish_lease
        publish_lease(s, "watch", address, {
            "chainId": chain, "token": address,
            "expiresAt": int(time.time() * 1000) + 90_000,
        })

    # Issuer-directory stock rows (Binance bStocks on 56, Robinhood Stock
    # Tokens on 4663): the catalogue lives in quote state, not in facts.
    if not asset:
        if chain == "56":
            token = stock_quotes.binance_token(address)
            if token:
                t = _stock_view(token, chain)
                now = token.get("quoteAt") or time.time() * 1000
                body = await attach_market_detail({
                    "asset": {
                        "token": address, "chainId": chain, "chainName": "BNB Smart Chain",
                        "symbol": token.get("tokenSymbol"), "name": token.get("tokenName"),
                        "firstSeen": now, "updatedAt": now, "kind": "stock",
                        "price": token.get("price"), "volume24h": token.get("volume24h"),
                        "txs24h": token.get("exchangeTrades24h"), "change24h": token.get("change24h"),
                        "marketCap": None, "buys24h": None, "sells24h": None, "holders": None,
                        "liquidity": None, "fieldTimes": t.get('fieldTimes',{}), "provider": "Binance",
                        "quoteAt":t.get('quoteAt'),"quoteStatus":t.get('quoteStatus'),"fieldSources":t.get('fieldSources',{}),
                        "volumeScope": "exchange", "tokenToAssetRatio": t.get("tokenToAssetRatio"),
                        "priceCurrency": "USDT", "priceScope": "exchange",
                    },
                    "stock": t, "relations": [], "trades": [], "tradesScope": "dex", "events": [], "samples": [],
                    "pools": [], "scan": None, "activity": _empty_activity(),
                    "analysis": _analysis("stock", [], None),
                }, s)
                return _canonical_overlay(body, snapshot, chain, address)
        if chain == "4663":
            token = stock_quotes.robinhood_token(address)
            if token:
                t = _stock_view(token, chain)
                now = token.get("quoteAt") or time.time() * 1000
                body = await attach_market_detail({
                    "asset": {
                        "token": address, "chainId": chain, "chainName": "Robinhood Chain",
                        "symbol": token.get("tokenSymbol"), "name": token.get("tokenName"),
                        "firstSeen": now, "updatedAt": now, "kind": "stock",
                        "price": token.get("price"), "stockPrice": token.get("stockPrice"),
                        "marketCap": None, "volume24h": None, "txs24h": None,
                        "buys24h": None, "sells24h": None, "holders": None,
                        "liquidity": None, "change24h": None, "fieldTimes": t.get('fieldTimes',{}),
                        "quoteAt":t.get('quoteAt'),"quoteStatus":t.get('quoteStatus'),"fieldSources":t.get('fieldSources',{}),
                        "priceScope":"issuer-derived","priceCurrency":"USD","volumeScope":"unsupported",
                        "provider": "Robinhood", "tokenToAssetRatio": token.get("tokenToAssetRatio"),
                    },
                    "stock": t, "relations": [], "trades": [], "tradesScope": "dex", "events": [], "samples": [],
                    "pools": [], "scan": None, "activity": _empty_activity(),
                    "analysis": _analysis("stock", [], None),
                }, s)
                return _canonical_overlay(body, snapshot, chain, address)
        raise HTTPException(status_code=404, detail="该地址尚未进入追踪索引")

    now = time.time() * 1000
    relations = _assessed_relations(await candidate_relations(s, chain, address), now)
    samples = await s.samples(address, 288)
    trades = await s.recent_trades(address, 50)
    events = [e for e in await s.events(address, 50)][:50]
    pools = await token_pools(s, address)
    scan = await s.get("scan", address)
    activity = await read_activity(s, address)
    activity.update(scope='dex',status='observed')
    trade_buckets = {
        "1m": await s.trade_buckets(address, "1m", 120),
        "5m": await s.trade_buckets(address, "5m", 96),
        "1h": await s.trade_buckets(address, "1h", 48),
    }

    verified = [r for r in relations if r.get("level") == "A"]
    quote_job = (await s.get('collector-job', 'quote:' + address)
                 if asset.get('kind') in ('candidate', 'stock') and asset.get('price') is None else None)
    asset_view = unquoted_market_status(_asset_view(asset, chain), quote_job, now)
    pool_totals = _pool_totals(_official_pools(verified), now)
    asset_view['pairLiquidityUsd'] = pool_totals['liquidityUsd']
    asset_view['pairLiquidityCoverage'] = pool_totals['coverage']
    asset_view['pairLiquidityStatus'] = ('unknown' if not pool_totals['pools'] else
                                          'current' if pool_totals['coverage']['complete'] else 'partial')
    asset_view['match'] = match_name(asset_view.get('symbol'), asset_view.get('name'))
    asset_view['relationLevel'] = 'A' if verified else 'B' if asset_view['match'] else None
    asset_view["dataQuality"] = evaluate_asset(asset_view, verified, now)

    # Stock quote overlay for catalogue tokens that also exist as facts.
    stock_row = await stock_view(s, chain, address, asset)
    if not stock_row and chain == "56":
        token = stock_quotes.binance_token(address)
        if token:
            stock_row = _stock_view(token, chain)
    elif not stock_row and chain == "4663":
        token = stock_quotes.robinhood_token(address)
        if token:
            stock_row = _stock_view(token, chain)

    if stock_row and asset.get('kind')=='stock':
        for field in ('price','stockPrice','quoteAt','provider','priceScope','volumeScope','priceCurrency',
                      'volume24h','change24h','fieldTimes','fieldSources','quoteStatus','marketQuotes','priceProvenance'):
            if field in stock_row:asset_view[field]=stock_row[field]
        if stock_row.get('priceScope')=='exchange':
            samples = await s.samples('binance:'+address,288)
            asset_view.update(txs24h=stock_row.get('exchangeTrades24h'),buys24h=None,sells24h=None,tradesScope='exchange')
            asset_view['fieldTimes']={**asset_view.get('fieldTimes',{}),'txs24h':stock_row.get('quoteAt') if stock_row.get('exchangeTrades24h') is not None else None,'buys24h':None,'sells24h':None}
    asset_view['dataQuality']=evaluate_asset(asset_view,verified,now)

    body = await attach_market_detail({
        "asset": asset_view,
        "stock": stock_row,
        "relations": relations,
        "trades": trades,
        "events": events,
        "samples": samples,
        "sampleMetadata":sample_metadata(samples,asset_view),
        "pools": pools,
        "scan": scan,
        "activity": activity,
        "onchainActivity":activity,
        "tradesScope":"dex",
        "tradeBuckets": trade_buckets,
        "analysis": _analysis(asset.get("kind"), verified, None),
    }, s)
    return _canonical_overlay(body, snapshot, chain, address)
