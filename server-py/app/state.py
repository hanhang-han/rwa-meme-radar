"""In-memory snapshot assembled from the research store, mirroring the Node
xLayerState/dashboardState shapes so the frontend contract stays identical."""
import time
import asyncio
from decimal import Decimal
from typing import Any

from .db import store
from .data_quality import evaluate_asset, quality_summary
from .market_quotes import enrich_asset, enrich_relation

BASE_QUOTE_SYMBOLS = {
    "USDT", "USDC", "USDG", "DAI", "WOKB", "OKB", "WETH", "ETH", "WBTC", "BTC", "XBTC",
    "WBNB", "BTCB", "WTBC", "USD1", "XUSD",
}
CHAIN_NAMES = {"196": "X Layer", "56": "BNB Smart Chain", "4663": "Robinhood Chain"}


def fresh(at, age_ms=900_000) -> bool:
    return bool(at) and time.time() * 1000 - at < age_ms


def _sane_usd(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e10


def unquoted_market_status(row: dict, job: dict | None, now: float) -> dict:
    """Expose a repeatedly unquoted asset without erasing its discovery.

    A failed provider request is not evidence that a market is absent.  Only
    repeated, explicit missing-price responses can change a missing quote's
    presentation; any real price observation immediately restores its status.
    """
    if (row.get('price') is not None or row.get('quoteStatus') not in (None, 'missing')
            or any(m.get('price') is not None for m in row.get('exchangeMarkets') or [])):
        return row
    job = job or {}
    if (job.get('failureCount') or 0) < 5 or job.get('lastSuccessAt') or job.get('reason') != 'missing-price-row':
        return row
    liquidity = row.get('totalLiquidityUsd')
    liquidity_at = row.get('totalLiquidityAt')
    low_liquidity = (_sane_usd(liquidity) and liquidity < 1_000
                     and (row.get('totalLiquidityCoverage') or {}).get('scope') == 'token-aggregate'
                     and isinstance(liquidity_at, (int, float))
                     and 0 <= now - liquidity_at <= 1_800_000)
    return {**row, 'quoteStatus': 'no-verified-market',
            'quoteReason': 'insufficient-liquidity' if low_liquidity else 'source-returned-no-price'}


def _official_pools(relations: list[dict]) -> list[dict]:
    """One freshly assessed stock pair per chain+pool, independent of relation count."""
    selected = {}
    for row in relations:
        pool = str(row.get('pool') or '').lower()
        if row.get('status') != 'verified' or row.get('level') != 'A' or not pool:
            continue
        key = (str(row.get('chainId') or '196'), pool)
        old = selected.get(key)
        if old is None or (row.get('liquidityAt') or 0, row.get('checkedAt') or 0) > (old.get('liquidityAt') or 0, old.get('checkedAt') or 0):
            selected[key] = row
    return list(selected.values())


def _assessed_relations(relations: list[dict], now: float) -> list[dict]:
    """Re-evaluate identity and valuation at the projection cutoff.

    A persisted A grade is only historical evidence.  A revoked issuer entry,
    old pool valuation or a changed wrapper must take effect in every new
    dashboard version without waiting for the next pool scan.
    """
    from .stock_identity import assess_pool_relation
    return [{**row, **assess_pool_relation(row, int(now))} for row in relations]


def _pool_totals(pools: list[dict], now: float) -> dict:
    """One cutoff and one deduplicated pool set for KPI and chain rows."""
    valued = [r for r in pools if _sane_usd(r.get('liquidityUsd'))
              and r.get('liquidityAt') and 0 <= now - r['liquidityAt'] <= 900_000]
    # A pool's 24h volume only joins its liquidity when the same pool and
    # nearby observation time are explicit.  Asset-wide volume never enters.
    traded = [r for r in valued if isinstance(r.get('poolMarket'), dict)
              and _sane_usd(r['poolMarket'].get('volume24h'))
              and r['poolMarket'].get('scope') == 'pool:' + str(r.get('pool') or '').lower()
              and isinstance(r['poolMarket'].get('updatedAt'), (int, float))
              and 0 <= now - r['poolMarket']['updatedAt'] <= 900_000
              and abs(r['poolMarket']['updatedAt'] - r['liquidityAt']) <= 300_000]
    cents = lambda rows, key: float(sum((Decimal(str(key(r))) for r in rows), Decimal(0)).quantize(Decimal('0.01')))
    return {
        'pools': pools, 'valued': valued, 'traded': traded,
        'liquidityUsd': cents(valued, lambda r: r['liquidityUsd']) if valued else None,
        'volume24hUsd': cents(traded, lambda r: r['poolMarket']['volume24h']) if traded else None,
        'coverage': {'valued': len(valued), 'volumeKnown': len(traded), 'total': len(pools),
                     'complete': len(valued) == len(pools) and bool(pools), 'asOf': now,
                     'scope': 'approved-stock-pair-pools'},
    }


def _coverage(rows: list[dict], field: str, max_age_ms: int = 900_000) -> dict:
    known = sum(1 for r in rows if r.get(field) is not None)
    current = sum(1 for r in rows if r.get(field) is not None and fresh((r.get("fieldTimes") or {}).get(field), max_age_ms))
    return {"known": known, "fresh": current, "total": len(rows)}


def _active_meme_count(assets: list[dict], now: float, chain_id: str | None = None) -> int:
    """PRD active Meme: a fresh token aggregate, independent of pair grade.

    The pair-backed actionable count is a separate operational metric. A
    single-pool quote cannot qualify an asset for this total-liquidity KPI.
    """
    identities = set()
    for row in assets:
        chain = str(row.get('chainId') or '')
        token = str(row.get('token') or '').lower()
        total = row.get('totalLiquidityUsd')
        total_at = row.get('totalLiquidityAt')
        price_at = (row.get('fieldTimes') or {}).get('price')
        if (row.get('kind') != 'candidate' or not token or (chain_id and chain != chain_id)
                or not _sane_usd(total) or total < 1_000
                or row.get('totalLiquidityStatus') != 'current'
                or not isinstance(total_at, (int, float)) or not 0 <= now - total_at <= 1_800_000
                or row.get('price') is None or not isinstance(price_at, (int, float))
                or not 0 <= now - price_at <= 900_000):
            continue
        identities.add((chain, token))
    return len(identities)


def newly_discovered_assets(assets, now):
    """Discovery is first indexing, distinct from token or pool creation."""
    first = {}
    for row in assets:
        token = str(row.get('token') or '').lower()
        at = row.get('firstSeen')
        if token and isinstance(at, (int, float)) and not isinstance(at, bool) and 0 < at <= now:
            key = (str(row.get('chainId') or '196'), token)
            first[key] = min(at, first.get(key, at))
    by_chain = {cid: sum(1 for (chain, _), at in first.items() if chain == cid and now-86_400_000 <= at <= now)
                for cid in ('196', '56', '4663')}
    return {'value': sum(by_chain.values()), 'byChain': by_chain, 'window': '24h',
            'from': now-86_400_000, 'to': now, 'scope': 'first-indexed-assets',
            'knownFirstSeen': len(first), 'timeField': 'firstSeen'}


def _source_statuses(stock_tokens: list[dict], assets: list[dict]) -> list[dict]:
    """Per-source health, so the page can say realtime/scheduled/stale/market
    closed instead of silently showing frozen numbers."""
    from . import stock_quotes
    from .collectors import binance
    from .okx_client import USAGE

    USAGE.sync()
    limits = USAGE.limits()
    okx_updated = max(
        [
            (a.get("fieldTimes") or {}).get("price") or 0
            for a in assets
            if a.get("provider") == "OKX" or (a.get("fieldSources") or {}).get("price") == "OKX"
        ] or [0]
    ) or None
    if USAGE.daily >= limits["daily"]:
        okx_status = "budget-exhausted"
    elif USAGE.daily >= limits["interactiveCap"]:
        okx_status = "critical-only"
    elif USAGE.daily >= limits["backgroundCap"]:
        okx_status = "reserved"
    else:
        okx_status = "ready" if fresh(okx_updated) else "stale"
    binance_status = stock_quotes.binance_snapshot()
    out = [
        {
            "id": "okx:dex", "provider": "OKX", "chainId": "196",
            "chainName": CHAIN_NAMES["196"], "kind": "dex-quotes",
            "status": okx_status,
            "updatedAt": okx_updated,
            "budget": {
                "daily": USAGE.daily, "dailyLimit": limits["daily"], "day": USAGE.day,
                "backgroundCap": limits["backgroundCap"], "interactiveCap": limits["interactiveCap"],
                "remaining": max(0, limits["daily"] - USAGE.daily),
            },
        },
        {
            "id": "binance:bstocks:56", "provider": "Binance", "chainId": "56",
            "chainName": CHAIN_NAMES["56"], "kind": "exchange-token-quotes",
            "status": binance_status.get("status", "starting"),
            "updatedAt": binance_status.get("updatedAt"),
            "error": binance_status.get("error"),
        },
        stock_quotes.robinhood_status() | {"id": "robinhood:chain:4663",
                                            "chainId": "4663", "chainName": CHAIN_NAMES["4663"], "kind": "issuer-quotes"},
        stock_quotes.eodhd_status() | {"id": "eodhd:exchange", "chainId": "exchange",
                                        "chainName": "Global equities", "kind": "stock-references"},
    ]
    for provider, info in (stock_quotes._read_snapshot(stock_quotes.EODHD_FILE).get("providers") or {}).items():
        if provider != "EODHD":
            out.append({**info, "provider": provider, "id": provider.lower()+":enrichment", "chainId": "multi", "chainName": "Multiple networks", "kind": "supplemental-quotes"})
    return out


def self_updated(rows: list[dict], chain: str) -> int | None:
    ats = [r.get("quoteAt") or (r.get("fieldTimes") or {}).get("price") or 0 for r in rows if str(r.get("chainId")) == chain]
    return max(ats) if ats else None


def _capabilities(sources: list[dict]) -> list[dict]:
    return [
        {
            "provider": s.get("provider"), "chainName": s.get("chainName"),
            "catalogue": "not-applicable",
            "market": s.get("status"),
            "trades": "partial" if s.get("id") == "okx:dex" else "not-applicable",
            "relations": "partial" if s.get("id") == "okx:dex" else "not-applicable",
            "updatedAt": s.get("updatedAt"), "error": s.get("error"),
        }
        for s in sources
    ]


class DashboardData:
    """Reads the persisted facts and renders the unified payload."""

    def __init__(self):
        self.assets: list[dict] = []
        self.relations: list[dict] = []
        self.stock_tokens: list[dict] = []
        self.signals: list[dict] = []
        self.baskets: dict = {}
        self.basket_last: dict = {}
        self.basket_views: dict = {}
        self.updated_at: float | None = None
        self.comparisons: dict = {}
        self.market_quotes: list[dict] = []
        self.stream_states: list[dict] = []
        self.quote_jobs: dict[tuple[str, str], dict] = {}

    async def reload(self, facts=None) -> None:
        async def all_rows(scoped, kind):
            return facts.all(scoped.scope, kind) if facts is not None else await scoped.all(kind)

        async def keyed_rows(scoped, kind):
            return facts.all_kv(scoped.scope, kind) if facts is not None else await scoped.all_kv(kind)

        loaded_assets = []
        loaded_relations = []
        loaded_stock_tokens = []
        loaded_comparisons = {}
        loaded_signals = []
        loaded_market_quotes = []
        loaded_stream_states = []
        loaded_quote_jobs = {}
        updated = 0
        for chain_id in ("196", "56", "4663"):
            s = await store(chain_id)
            assets = await all_rows(s, "asset")
            # Only price-less assets need their durable quote checkpoint.
            # These reads use the same pinned SQLite snapshot as the assets.
            for asset in assets:
                token = str(asset.get('token') or '').lower()
                if asset.get('kind') in ('candidate', 'stock') and asset.get('price') is None and token:
                    job = await s.get('collector-job', 'quote:' + token)
                    if job:
                        loaded_quote_jobs[(chain_id, token)] = job
            relations = await all_rows(s, "relation")
            stocks = catalogue_rows(await all_rows(s, "stock"), chain_id)
            loaded_market_quotes.extend({**q, "chainId": chain_id} for q in await all_rows(s, "market-quote"))
            for kind in ("chain-stream", "market-stream", "market-stream-status"):
                for identity, q in await keyed_rows(s, kind):
                    loaded_stream_states.append({**q, "chainId": chain_id,
                        "id": q.get("id") or f"{kind}:{chain_id}:{identity}",
                        "provider": q.get("provider") or {"binance": "Binance", "binance-alpha": "Binance Alpha"}.get(identity, identity)})
            for packet in await all_rows(s, "comparison"):
                loaded_comparisons[(chain_id, str(packet.get("token", "")).lower())] = packet

            candidate_ids = {
                a["token"] for a in assets
                if a.get("kind") == "candidate"
                and str(a.get("symbol") or "").upper() not in BASE_QUOTE_SYMBOLS
            }
            relations = [r for r in relations if r.get("token") in candidate_ids]

            for a in assets:
                a["chainId"] = chain_id
                a["chain"] = chain_id
                a["chainName"] = CHAIN_NAMES.get(chain_id, chain_id)
                a.setdefault("fieldTimes", {})
                updated = max(updated, a.get("updatedAt") or 0)

            for r in relations:
                r["chainId"] = chain_id
                r.setdefault("liquidityUsd", None)
                updated = max(updated, r.get("checkedAt") or 0)

            relations = [enrich_relation(r) for r in relations]

            for st in stocks:
                st["chainId"] = chain_id
                st["chain"] = chain_id
                st.setdefault("tokenContractAddress", st.get("tokenContractAddress"))

            loaded_signals.extend(
                {**e, "asset": e.get("asset", "").split(":", 1)[-1]}
                for e in await s.events(None, 100)
                if e.get("asset", "").split(":", 1)[-1] in candidate_ids
            )
            loaded_assets.extend(assets)
            loaded_relations.extend(relations)
            loaded_stock_tokens.extend(stocks)

        # Keep each chain's bounded event page in the shared read model.  A
        # global top-30 cut here could erase every X Layer event whenever a
        # busier chain produced the latest 30; /feed applies its chain filter
        # before taking the requested 30 rows.
        loaded_signals = sorted(loaded_signals, key=lambda e: -(e.get("t") or 0))
        loaded_baskets = {}
        loaded_basket_last = {}
        loaded_basket_views = {}
        for chain_id in ("196", "56", "4663"):
            scoped = await store(chain_id)
            for name, body in await keyed_rows(scoped, "basket"):
                loaded_baskets[(chain_id, name)] = {**body, "chainId": chain_id}
            for name, body in await keyed_rows(scoped, "basket-last"):
                loaded_basket_last[(chain_id, name)] = body
            for name, body in await keyed_rows(scoped, "basket-view"):
                loaded_basket_views[(chain_id, name)] = body
        self.assets, self.relations, self.stock_tokens, self.signals = loaded_assets, loaded_relations, loaded_stock_tokens, loaded_signals
        self.baskets, self.basket_last = loaded_baskets, loaded_basket_last
        self.basket_views = loaded_basket_views
        self.comparisons = loaded_comparisons
        self.market_quotes = loaded_market_quotes
        self.stream_states = loaded_stream_states
        self.quote_jobs = loaded_quote_jobs
        self.updated_at = updated or None

    # -- unified payload pieces -------------------------------------------------

    def visible_assets(self, enriched=None, now=None, relations=None) -> list[dict]:
        from .stock_identity import match_name, classify_derivative
        out = []
        by_asset = {}
        now = time.time() * 1000 if now is None else now
        for relation in relations if relations is not None else _assessed_relations(self.relations, now):
            key = (str(relation.get("chainId") or "196"), str(relation.get("token") or "").lower())
            by_asset.setdefault(key, []).append(relation)
        markets_by_asset = {}
        for quote in self.market_quotes:
            if quote.get("venue") == "binance-alpha":
                key = (str(quote.get("chainId")), str(quote.get("token") or "").lower())
                markets_by_asset.setdefault(key, []).append(quote)
        for a in self.assets:
            if a.get("kind") != "candidate":
                continue
            if str(a.get("symbol", "")).upper() in BASE_QUOTE_SYMBOLS:
                continue
            key = (str(a.get("chainId")), str(a.get("token") or "").lower())
            row = dict(enriched[key]) if enriched is not None else enrich_asset(a)
            markets = exchange_market_views(markets_by_asset.get(key, []), a.get("chainId"), a.get("token"))
            if markets:
                row["exchangeMarkets"] = markets
            row = unquoted_market_status(row, self.quote_jobs.get(key), now)
            asset_relations = by_asset.get(key, [])
            approved = [r for r in asset_relations if r.get('status') == 'verified' and r.get('level') == 'A']
            # Old persisted substring matches included generic BTC-like terms.
            # Recompute the name clue from the pinned whole-term dictionary.
            row['derivative'] = classify_derivative(row.get('symbol'), row.get('name'))
            row['assetCategory'] = 'derivative' if row['derivative'] else 'meme'
            row['match'] = match_name(row.get('symbol'), row.get('name'))
            row['relationLevel'] = 'A' if approved else 'B' if row['match'] else None
            pool_totals = _pool_totals(_official_pools(approved), now)
            row['pairLiquidityUsd'] = pool_totals['liquidityUsd']
            row['pairLiquidityStatus'] = ('unknown' if not pool_totals['pools'] else
                                          'current' if pool_totals['coverage']['complete'] else 'partial')
            row['pairLiquidityCoverage'] = pool_totals['coverage']
            row["dataQuality"] = evaluate_asset(row, approved, now)
            out.append(row)
        return out

    def verified_relations(self, max_age_ms: int | None = None) -> list[dict]:
        now = time.time() * 1000
        return [
            r for r in self.relations
            if r.get("status") == "verified"
            and (max_age_ms is None or now - r.get("checkedAt", 0) < max_age_ms)
        ]

    def official_relations(self, max_age_ms: int | None = None) -> list[dict]:
        now = time.time() * 1000
        return [r for r in _assessed_relations(self.relations, now) if r.get('status') == 'verified' and r.get('level') == 'A'
                and (max_age_ms is None or 0 <= now - (r.get('checkedAt') or 0) < max_age_ms)]

    def groups(self, visible=None) -> list[dict]:
        by_symbol: dict[str, list[dict]] = {}
        for a in visible if visible is not None else self.visible_assets():
            by_symbol.setdefault(str(a.get("symbol", "?")).upper(), []).append(a)
        groups = []
        for symbol, members in by_symbol.items():
            members.sort(key=lambda m: (not (m.get("dataQuality") or {}).get("eligible", {}).get("marketRanking"), -(m.get("volume24h") or 0)))
            qualified = [m for m in members if (m.get("dataQuality") or {}).get("eligible", {}).get("marketRanking")]
            groups.append({
                "symbol": symbol,
                "members": [f"{m['chainId']}:{m['token']}" for m in members],
                "qualifiedMembers": len(qualified),
                "volume24h": sum(m.get("volume24h") or 0 for m in qualified) or None,
            })
        groups.sort(key=lambda g: -(g["volume24h"] or 0))
        return groups

    async def _sectors(self, enriched=None) -> list:
        from .collectors.baskets import CHAIN_LABELS, METHOD_VERSION

        out = []
        keys = set(self.basket_views) | set(self.baskets)
        for key in keys:
            if isinstance(key, tuple):
                chain_id, name = key
            else:  # Compatibility for older fixtures and saved snapshots.
                b = self.baskets.get(key) or {}
                chain_id, name = str(b.get("chainId") or "196"), key
            b = self.baskets.get((chain_id, name), self.baskets.get(name)) or {}
            projection = self.basket_views.get((chain_id, name), self.basket_views.get(name))
            if isinstance(projection, dict) and projection.get("projectionVersion") == METHOD_VERSION:
                view = {**projection, "sector": name, "chainId": chain_id,
                        "scopeLabel": CHAIN_LABELS.get(chain_id, chain_id)}
                at = view.get("at")
                now = time.time() * 1000
                if view.get("dataStatus") == "current" and not (
                    isinstance(at, (int, float)) and 0 <= now - at <= 1_800_000
                ):
                    view["value"] = None
                    view["at"] = None
                    view["dataStatus"] = "paused"
                    view["reason"] = "指数行情超过 30 分钟未更新，等待新数据"
                out.append(view)
                continue

            # A legacy basket can still supply history and constituent labels,
            # but its old index value must not be published as a live quote.
            scoped = await store(chain_id)
            current = {token: row for (cid, token), row in enriched.items() if cid == chain_id} if enriched is not None else {
                a.get("token"): enrich_asset(a) for a in self.assets if str(a.get("chainId")) == chain_id
            }
            members = b.get("members") or []
            quote_count = sum(1 for m in members if fresh((current.get(m.get("token"), {}).get("fieldTimes") or {}).get("price")))
            total_cap = sum(m.get("baseCap") or 0 for m in members) or 1
            history_rows = await scoped.samples(b.get("sampleKey") or "basket:" + name, 200)
            last = self.basket_last.get((chain_id, name), self.basket_last.get(name)) or {}
            out.append({
                "sector": name, "chainId": chain_id,
                "scopeLabel": CHAIN_LABELS.get(chain_id, chain_id), "baseAt": b.get("baseAt"),
                "basketVersion": b.get("version") or b.get("baseAt"),
                "members": len(members),
                "value": None,
                "lastValue": last.get("value"),
                "lastAt": last.get("at"),
                "dataStatus": "paused" if b else "unavailable",
                "reason": "等待按官方 A 级关系重建主题指数；旧版历史保留" if b else "尚无符合条件的成分",
                "quoteCoverage": {"fresh": quote_count, "total": len(members)},
                "history": [{"t": r["t"], "price": r["price"]} for r in history_rows],
                "components": [
                    {
                        "chainId": chain_id, "token": m.get("token"),
                        "symbol": current.get(m.get("token"), {}).get("symbol") or m.get("token", "")[:10],
                        "weight": (m.get("baseCap") or 0) / total_cap if m.get("baseCap") else None,
                    }
                    for m in members
                ],
            })
        return sorted(out, key=lambda row: (row["sector"], row["chainId"]))

    def metrics(self, now=None, pools=None) -> dict:
        now = time.time() * 1000 if now is None else now
        verified = _official_pools(_assessed_relations(self.relations, now)) if pools is None else pools
        totals = _pool_totals(verified, now)
        recently_checked = [r for r in verified if 0 <= now - (r.get('checkedAt') or 0) < 3_600_000]
        creation_known = [r for r in verified if isinstance(r.get('poolCreatedAt'), (int, float))
                          and 0 < r['poolCreatedAt'] <= now]
        return {
            "verifiedPools": len(verified),
            "freshVerifiedPools": len(recently_checked),
            "verifiedAssets": len({(str(r.get("chainId") or "196"), r["token"]) for r in verified}),
            "actionableAssets": len({
                (str(r.get("chainId") or "196"), r["token"]) for r in verified
                if _sane_usd(r.get('liquidityUsd')) and r['liquidityUsd'] >= 1000
                and r.get('liquidityAt') and 0 <= now - r['liquidityAt'] <= 900_000
            }),
            "pairedLiquidityUsd": totals['liquidityUsd'],
            "pairedVolume24hUsd": totals['volume24hUsd'],
            "liquidityCoverage": totals['coverage'],
            # Discovery firstSeen is a scan time, not pool creation. Only
            # publish a true 24h new-pool count with complete creation coverage.
            "newPair24h": (sum(1 for r in creation_known if r['poolCreatedAt'] >= now - 86_400_000)
                            if verified and len(creation_known) == len(verified) else None),
            "newPair24hCoverage": {"known": len(creation_known), "total": len(verified)},
            "newRelations24h": sum(1 for r in verified if (r.get("firstSeen") or 0) >= now - 86_400_000),
        }

    def stock_views(self, include_comparisons: bool = True, enriched=None) -> list[dict]:
        from . import stock_quotes
        from .stock_identity import token_identity

        facts = enriched if enriched is not None else {(str(a.get("chainId")), str(a.get("token") or "").lower()): enrich_asset(a) for a in self.assets}
        directory = []
        for st in self.stock_tokens:
            row = dict(st)
            row['issuerIdentity'] = token_identity(st.get('chainId'), st.get('tokenContractAddress'), st.get('tokenSymbol'))
            row['verificationStatus'] = row['issuerIdentity']['verificationStatus']
            fact = facts.get((str(st.get("chainId")), str(st.get("tokenContractAddress", "")).lower()))
            if fact:
                for field in ("price", "volume24h", "change24h", "fieldTimes", "fieldSources", "quoteAt", "provider", "priceProvenance", "fieldScopes", "fieldTimeKinds", "fieldStatus", "fieldObservations"):
                    if fact.get(field) is not None:
                        row[field] = fact[field]
            directory.append(row)
        rows = stock_quotes.apply_stock_overlays(directory, self.market_quotes) if self.market_quotes else stock_quotes.apply_stock_overlays(directory)
        now = time.time() * 1000
        rows = [unquoted_market_status(row, self.quote_jobs.get((str(row.get('chainId')),
                                                                str(row.get('tokenContractAddress') or '').lower())), now)
                for row in rows]
        if include_comparisons:
            from .comparison_service import public_metric
            from .comparisons import VERSION, independent_stock_row
            for row in rows:
                packet = self.comparisons.get((str(row.get("chainId")), str(row.get("tokenContractAddress", "")).lower()))
                if packet and packet.get("method") == VERSION:
                    metric = public_metric(packet.get("premium"), now)
                    inputs = metric.get("inputs") or {}
                    reference_row = independent_stock_row(row)
                    if metric.get("value") is not None and inputs.get("referenceAt") == reference_row.get("referenceAt") and inputs.get("ratioVersion") == reference_row.get("ratioVersion"):
                        row["premium"] = metric
        return rows

    async def payload(self, include_groups=True) -> dict:
        now = time.time() * 1000
        relations = _assessed_relations(self.relations, now)
        enriched = {(str(a.get("chainId")), str(a.get("token") or "").lower()): enrich_asset(a) for a in self.assets}
        stock_tokens = self.stock_views(enriched=enriched)
        assets = self.visible_assets(enriched=enriched, now=now, relations=relations)
        official_pools = _official_pools(relations)
        totals_by_chain = {cid: _pool_totals([r for r in official_pools if str(r.get('chainId')) == cid], now)
                           for cid in ('196', '56', '4663')}
        sources = _source_statuses(stock_tokens, assets)
        sources.extend({**row, "id": row.get("id") or f"{row.get('provider', 'chain')}:stream:{row['chainId']}",
                        "kind": row.get("kind") or "chain-stream",
                        "chainName": CHAIN_NAMES.get(row["chainId"], row["chainId"])} for row in self.stream_states)
        quality_summary_row = quality_summary(assets)
        quality = {
            "assets": {
                "price": _coverage(assets, "price"),
                "volume24h": _coverage(assets, "volume24h"),
            },
            "stocks": {
                "reference": _coverage(stock_tokens, "stockPrice"),
                "tokenPrice": _coverage(stock_tokens, "price"),
            },
            "stockReferences": sum(1 for s in stock_tokens if s.get("stockPrice") is not None),
            "summary": quality_summary_row,
        }
        metrics = self.metrics(now=now, pools=official_pools)
        metrics['activeMemeCount'] = _active_meme_count(assets, now)
        new_assets = newly_discovered_assets(assets, now)
        metrics['newAssets24h'] = new_assets['value']
        metrics_by_chain = {}
        for cid in ('196', '56', '4663'):
            chain_pools = [r for r in official_pools if str(r.get('chainId')) == cid]
            metrics_by_chain[cid] = self.metrics(now=now, pools=chain_pools)
            metrics_by_chain[cid]['activeMemeCount'] = _active_meme_count(assets, now, cid)
            metrics_by_chain[cid]['newAssets24h'] = new_assets['byChain'][cid]
        # Dashboard rows contain only list/sort fields. Risk profiles, trade
        # cursors and full field histories remain available from token detail;
        # excluding them here materially reduces every 20-second snapshot.
        asset_omit = {"risk", "profile", "detailAt", "oldestTradeAt", "gapDetected", "tradeCursor", "tradeGaps", "error", "source"}
        payload_assets = [{k: v for k, v in row.items() if k not in asset_omit} for row in assets]
        payload_stocks = []
        for stock in stock_tokens:
            row = {
                k: v for k, v in stock.items()
                if k not in {"logoUrl", "chainIndex", "chain", "updatedAt"}
            }
            row["fieldTimes"] = {
                k: v for k, v in (row.get("fieldTimes") or {}).items()
                if k in {"price", "stockPrice", "volume24h", "marketCap", "change24h"}
            }
            row["fieldSources"] = {
                k: v for k, v in (row.get("fieldSources") or {}).items()
                if k in {"price", "stockPrice"}
            }
            payload_stocks.append(row)
        by_asset = {(str(a.get("chainId")), str(a.get("token")).lower()): a for a in assets}
        signals = []
        for signal in self.signals[:30]:
            row = dict(signal)
            key = (str(row.get("chainId") or "196"), str(row.get("asset") or "").lower())
            row["dataQuality"] = (by_asset.get(key) or {}).get("dataQuality")
            row["qualified"] = bool(row.get("id") and row.get("t") and row.get("kind") in ("verified", "invalidated"))
            signals.append(row)
        from .comparison_service import public_packet
        comparison_pairs = {}
        for (cid, _), packet in self.comparisons.items():
            for pair in public_packet(packet, now).get("pairs", []):
                comparison_pairs[(cid, packet["token"], pair["pool"])] = {
                    "relative": pair.get("relative"), "spread": pair.get("spread"),
                    "calculatedAt": packet.get("calculatedAt"),
                }
        payload_relations = [{**r, "priceComparison": comparison_pairs.get((str(r.get("chainId")), r.get("stock"), r.get("pool")))} for r in relations]
        from .theme_map import build_theme_map, build_important_changes
        theme_map = build_theme_map(assets, relations, int(now), stock_tokens)
        important_changes = build_important_changes(self.signals, relations, assets, int(now))
        return {
            "now": now,
            "unified": {
                "version": 2,
                "sources": sources,
                "assets": payload_assets,
                "stockTokens": payload_stocks,
                "relations": payload_relations,
                "signals": signals,
                "themeMap": theme_map,
                "importantChanges": important_changes,
                "groups": self.groups(assets) if include_groups else [],
                "quality": quality,
                "metrics": metrics,
                "newAssets24h": new_assets,
                "metricsByChain": metrics_by_chain,
                "sectors": await self._sectors(enriched),

                "distribution": [
                    {
                        "chainId": cid, "name": CHAIN_NAMES.get(cid, cid),
                        "assets": len([a for a in assets if a.get("chainId") == cid]),
                        "volume": {
                            "value": totals_by_chain[cid]['volume24hUsd'],
                            "known": totals_by_chain[cid]['coverage']['volumeKnown'],
                            "fresh": totals_by_chain[cid]['coverage']['volumeKnown'],
                            "total": totals_by_chain[cid]['coverage']['total'],
                            "scope": 'approved-stock-pair-pools',
                            "asOf": now,
                        },
                        "liquidity": {
                            "value": totals_by_chain[cid]['liquidityUsd'],
                            "known": totals_by_chain[cid]['coverage']['valued'],
                            "total": totals_by_chain[cid]['coverage']['total'],
                            "scope": 'approved-stock-pair-pools',
                            "asOf": now,
                        },
                    }
                    for cid in ("196", "56", "4663")
                ],
                "capabilities": _capabilities(sources),
                "collection": {"updatedAt": self.updated_at},
            },
        }


DATA = DashboardData()
_loaded_at = 0.0
_reload_task = None
_shared_fact_cache = None
_invalidate_generation = 0


async def _reload_shared_snapshot() -> None:
    """Refresh the API view from one read snapshot and an outbox-bound cache."""
    global _shared_fact_cache
    import os
    import aiosqlite
    from .db import transaction_view
    from .projection_facts import ProjectionFacts
    from .source_snapshots import bind_snapshots

    scoped = await store('196')
    try:
        stat = os.stat(scoped.path)
        file_identity = (stat.st_dev, stat.st_ino)
    except OSError:
        file_identity = None
    identity = (scoped.path, scoped.db, file_identity, id(DATA))
    previous = _shared_fact_cache
    async with aiosqlite.connect(scoped.path, timeout=15) as reader:
        reader.row_factory = aiosqlite.Row
        await reader.execute('PRAGMA query_only=ON')
        await reader.execute('BEGIN')
        bounds = (await reader.execute_fetchall('''SELECT
            (SELECT id FROM change_outbox ORDER BY id LIMIT 1),
            (SELECT id FROM change_outbox ORDER BY id DESC LIMIT 1)'''))[0]
        low, high = bounds[0], bounds[1] or 0
        reusable = (previous is not None and previous[0] == identity
                    and previous[1] <= high
                    and (low is None or low <= previous[1] + 1))
        changes = await reader.execute_fetchall(
            'SELECT id,kind,entity,operation FROM change_outbox WHERE id>? AND id<=? ORDER BY id',
            (previous[1], high)) if reusable else ()
        facts = await ProjectionFacts.capture(reader, previous[2] if reusable else None, changes)
        sources = {key: body.get('data') or {} for key, body in facts.all_kv('system', 'shared-source')}
        with bind_snapshots(sources):
            async with transaction_view(reader):
                await DATA.reload(facts=facts)
    # Publishing only after all reads succeed preserves the last good cache
    # when a reload fails or is cancelled. Unchanged parsed bodies are shared.
    _shared_fact_cache = (identity, high, facts)


async def reload_data() -> None:
    global _reload_task
    async def run():
        global _loaded_at
        generation = _invalidate_generation
        await _reload_shared_snapshot()
        # An event may invalidate this API view while its older read snapshot
        # is being assembled. Do not mark that snapshot fresh over the event.
        _loaded_at = time.time() if generation == _invalidate_generation else 0.0
    if _reload_task is None or _reload_task.done():
        _reload_task = asyncio.create_task(run())
    await asyncio.shield(_reload_task)


async def reload_if_stale(max_age: float = 10.0) -> None:
    if time.time() - _loaded_at > max_age:
        await reload_data()


def invalidate() -> None:
    """Called once by the process event tailer before notifying detail readers."""
    global _loaded_at, _invalidate_generation
    _invalidate_generation += 1
    _loaded_at = 0.0


def exchange_market_views(quotes, chain, token):
    """Secondary venues stay independent of the row's DEX currency/counts."""
    return [{key: q.get(key) for key in (
        "marketId", "venue", "price", "priceCurrency", "volume24h", "volumeCurrency",
        "exchangeTrades24h", "change24h", "quoteAt", "provider", "sourceEventAt", "receivedAt")}
        for q in quotes if str(q.get("chainId")) == str(chain)
        and str(q.get("token") or "").lower() == str(token or "").lower()
        and q.get("venue") == "binance-alpha"]


def catalogue_rows(persisted, chain):
    """Shared issuer/exchange catalogues can add contracts between DB scans."""
    from . import stock_quotes
    snapshot = stock_quotes._read_snapshot(stock_quotes.ROBINHOOD_FILE) if str(chain) == '4663' else stock_quotes.binance_snapshot() if str(chain) == '56' else {}
    rows = {str(row.get('tokenContractAddress') or '').lower(): dict(row) for row in persisted}
    for item in snapshot.get('tokens') or []:
        if not isinstance(item, dict):
            continue
        address = str(item.get('tokenContractAddress') or '').lower()
        if not address:
            continue
        rows[address] = {**rows.get(address, {}), **item, 'tokenContractAddress': address, 'chainId': str(chain)}
    return list(rows.values())
