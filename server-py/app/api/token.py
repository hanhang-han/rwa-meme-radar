"""Asset detail endpoint: persisted asset, relations, samples, recent trades,
events and activity aggregates, matching the Node response shape.

Opening a detail page only writes a short-lived watch lease. The collector
worker batches those leases and remains the sole owner of upstream calls."""
import time

from fastapi import APIRouter, HTTPException

from ..db import store
from ..data_quality import evaluate_asset
from ..state import _assessed_relations, _official_pools, _pool_totals, unquoted_market_status
from ..scoped_reads import candidate_relations, stock_view, token_pools
from .. import stock_quotes
from ..market_quotes import enrich_asset
from ..market_history import attach_market_detail
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


@router.get("/token/{chain}/{address}")
async def get_token(chain: str, address: str):
    address = address.lower()
    if chain not in ("196", "56", "4663") or not (address.startswith("0x") and len(address) == 42):
        raise HTTPException(status_code=400, detail="unsupported token")
    s = await store(chain)
    asset = await s.get("asset", address)
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
                return await attach_market_detail({
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
        if chain == "4663":
            token = stock_quotes.robinhood_token(address)
            if token:
                t = _stock_view(token, chain)
                now = token.get("quoteAt") or time.time() * 1000
                return await attach_market_detail({
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
        raise HTTPException(status_code=404, detail="该地址尚未进入追踪索引")

    now = time.time() * 1000
    relations = _assessed_relations(await candidate_relations(s, chain, address), now)
    samples = await s.samples(address, 288)
    trades = await s.recent_trades(address, 50)
    events = [e for e in await s.events(address, 50)][:50]
    pools = await token_pools(s, address)
    scan = await s.get("scan", address)
    day_ago = time.time() * 1000 - 86_400_000
    activity = await s.activity(address, day_ago)
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

    return await attach_market_detail({
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
