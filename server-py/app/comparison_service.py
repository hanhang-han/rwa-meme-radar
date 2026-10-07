"""Worker-owned persistent comparisons. API reads never fabricate observations."""
import hashlib
import asyncio
import json
import time
import copy
from pathlib import Path

import aiosqlite

from .storage_runtime import async_connect
from .comparisons import (VERSION, MAX_AGE, MAX_SKEW, independent_quote, pool_spread,
                          relative_point, relative_return, stock_premium, unavailable, independent_stock_row)
from .db import store, ResearchStore, retry_busy_write
from .market_quotes import enrich_asset
from .state import DATA, DashboardData, reload_data
from .stream_hub import broadcast
from .comparison_alerts import check_alert


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def subject(token, pool=None):
    return f"{VERSION}:{token.lower()}" + (f":{pool.lower()}" if pool else "")


def source_quote(asset, chain, token):
    if not asset or asset.get("price") is None:
        return None
    evidence = asset.get("priceProvenance") or {}
    return {"chainId": chain, "token": token, "price": asset["price"],
            "at": asset.get("quoteAt"), "currency": asset.get("priceCurrency"),
            "provider": asset.get("provider"), "scope": asset.get("priceScope"),
            "derived": asset.get("priceScope") == "issuer-derived",
            "dependencies": evidence.get("dependencies"),
            "dependenciesComplete": evidence.get("dependenciesComplete") is True,
            "timeKind": evidence.get("timeKind", "unknown")}


def quote_options(packet, fallback):
    rows = (packet.get("quotes") or [packet]) if packet else []
    return [q for q in [*rows, fallback] if q]


def select_independent_quotes(relation, meme, stock, now):
    pool, chain = relation["pool"], str(relation["chainId"])
    side = relation.get("stockSide") or relation["stock"]
    left = [q for q in meme if not independent_quote(q, pool, relation["token"], chain)
            and 0 <= now - (q.get("at") or 0) <= MAX_AGE]
    right = [q for q in stock if not independent_quote(q, pool, side, chain)
             and 0 <= now - (q.get("at") or 0) <= MAX_AGE]
    pairs = [(a, b) for a in left for b in right if a["currency"] == b["currency"]
             and abs(a["at"] - b["at"]) <= MAX_SKEW]
    if pairs:
        return max(pairs, key=lambda p: min(p[0]["at"], p[1]["at"]))
    # Preserve original evidence so the public reason identifies circular paths,
    # incompatible units or expired observations instead of concealing them.
    return (left or meme or [None])[0], (right or stock or [None])[0]


def public_metric(metric, now):
    if metric and metric.get("value") is not None:
        if now > (metric.get("validUntil") or 0):
            return {**metric, "value": None, "status": "unavailable", "reason": "stale"}
        if metric.get("status") == "realtime" and now > (metric.get("realtimeUntil") or 0):
            return {**metric, "status": "snapshot"}
    return metric or unavailable("pending")


def public_packet(packet, now):
    return {**packet, "premium": public_metric(packet.get("premium"), now), "pairs": [
        {**p, "spread": public_metric(p.get("spread"), now),
         "relative": {w: public_metric(m, now) for w, m in p.get("relative", {}).items()}}
        for p in packet.get("pairs", [])]}


def history_row(key, body):
    at = body.get("at")
    if not at:
        return None
    identity = {k: v for k, v in body.items() if k not in ("status", "validUntil", "realtimeUntil")}
    return key, digest(identity), int(at), body


async def record(s, key, body):
    row = history_row(key, body)
    if row:
        await s.comparison_samples_batch([row])


def token_observation(asset):
    """Keep units/provenance; legacy bucket samples cannot prove them."""
    at = asset.get("quoteAt") or (asset.get("fieldTimes") or {}).get("price")
    if not at or asset.get("price") is None or not asset.get("priceCurrency"):
        return None
    return {**{k: asset.get(k) for k in ("price", "priceCurrency", "priceScope", "provider", "priceProvenance", "quoteFx")},
            "quoteAt": at, "at": at}


async def at_reference(s, asset, reference):
    """Delayed stock data use a stored token observation within 60 seconds."""
    at = reference.get("referenceAt")
    current = token_observation(asset)
    if not at or (current and abs(current["at"] - at) <= MAX_SKEW):
        return asset
    if not reference.get("referenceDelayMs"):
        return asset
    token = asset.get("token") or asset.get("tokenContractAddress")
    if not token:
        return asset
    history = await s.comparison_observations_at(subject(token) + ":token", at, MAX_SKEW)
    compatible = [q for q in history if q.get("provider") == asset.get("provider")
                  and q.get("priceScope") == asset.get("priceScope")
                  and q.get("priceCurrency") == asset.get("priceCurrency")]
    if compatible:
        return {**asset, **min(compatible, key=lambda q: abs(q["at"] - at)), "comparisonAsOf": at}
    return {**asset, "price": None, "alignmentReason": "missing-aligned-history"}


_refresh_lock = asyncio.Lock()


async def fact_index(s, kind, identities):
    """Read only this batch's dependencies through the facts primary key."""
    identities=sorted({ident for ident in identities if isinstance(ident,str) and ident})
    values={}
    for offset in range(0,len(identities),400):
        ids=identities[offset:offset+400]
        if isinstance(s,ResearchStore):
            rows=await s.fetchall('SELECT id,body FROM facts WHERE kind=? AND id IN ('+
                ','.join('?' for _ in ids)+')',(s.key(kind),*ids))
            values.update((row[0],json.loads(row[1])) for row in rows)
        else:
            for ident in ids:
                value=await s.get(kind,ident)
                if value is not None:values[ident]=value
    return values


class ComparisonBatch:
    """Batch dependency reads and commit each stock's state/events together.

    Arithmetic and JSON encoding happen before taking the shared writer.
    A bounded commit contains whole subjects so alerts cannot survive without
    their corresponding comparison packet after a partial batch failure.
    """
    def __init__(self,s,cached):
        self.store,self.cached=s,cached
        self.pending=[]
        self.groups=[]
        self.history={}

    def __getattr__(self,name):
        return getattr(self.store,name)

    async def get(self,kind,ident):
        if kind in self.cached:return self.cached[kind].get(ident)
        return await self.store.get(kind,ident)

    async def put(self,kind,ident,value):
        self.cached.setdefault(kind,{})[ident]=value
        self.pending.append(('fact',(kind,ident,value)))

    async def put_event(self,ident,asset,value,now=None):
        self.pending.append(('event',(ident,asset,value,now)))

    async def comparison_observations_at(self,key,at,tolerance_ms=60000,limit=8):
        identity=(key,at,tolerance_ms,limit)
        if identity not in self.history:
            self.history[identity]=await self.store.comparison_observations_at(key,at,tolerance_ms,limit)
        return self.history[identity]

    def finish_subject(self,packet=None):
        if self.pending or packet:
            self.groups.append((self.pending,packet))
        self.pending=[]

    async def flush(self):
        groups=[];size=0
        for group in self.groups:
            if groups and size+len(group[0])>200:
                await self._commit(groups);groups=[];size=0
            groups.append(group);size+=len(group[0])
        if groups:await self._commit(groups)
        self.groups=[]

    async def _commit(self,groups):
        s=self.store
        if not isinstance(s,ResearchStore):
            for operations,_ in groups:
                for kind,args in operations:
                    await (s.put(*args) if kind=='fact' else s.put_event(*args))
        else:
            facts=[];events=[]
            for operations,_ in groups:
                for kind,args in operations:
                    if kind=='fact':
                        name,ident,value=args
                        facts.append((s.key(name),ident,json.dumps(value,ensure_ascii=False,allow_nan=False)))
                    else:
                        ident,asset,value,at=args
                        at=at or int(time.time()*1000)
                        body={**value,'id':s.key(ident),'asset':s.key(asset),'chainId':s.scope,'t':at}
                        events.append((s.key(ident),s.key(asset),int(at),json.dumps(body,ensure_ascii=False,allow_nan=False)))
            async def write():
                async with s._guard_write():
                    await s.db.execute('BEGIN IMMEDIATE')
                    if facts:
                        await s.db.executemany('INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body WHERE facts.body IS NOT excluded.body',facts)
                    if events:await s.db.executemany('INSERT OR IGNORE INTO events VALUES (?,?,?,?)',events)
                    await s.db.commit()
            # Retry only a rolled-back SQLITE_BUSY transaction. A subject's
            # comparison and alert state still commit atomically, and events
            # are published only after the successful commit.
            await retry_busy_write(write)
        for _,packet in groups:
            if packet:broadcast('comparison',packet)
        await asyncio.sleep(0)


async def new_history_rows(s, rows):
    """Avoid acquiring SQLite's writer for observations already persisted.

    The primary key remains the final arbiter if another process inserts a
    sample after this read. A covering-index lookup keeps the read bounded to
    the candidate fingerprints instead of loading a subject's full history.
    """
    unique = {}
    for row in rows:
        if row:
            unique.setdefault((row[0], row[1]), row)
    if not unique:
        return []
    base = s.store if isinstance(s, ComparisonBatch) else s
    if not isinstance(base, ResearchStore):
        return list(unique.values())
    if base.path == ':memory:' or base.path.startswith('file::memory:'):
        # An independent reader cannot see this connection's in-memory DB.
        # Keep the original idempotent insert path for this rare store type.
        return list(unique.values())
    keys = list(unique)
    existing = set()
    # A separate read-only connection sees only committed rows. Reading the
    # shared store connection could see another task's row before rollback.
    uri = Path(base.path).resolve().as_uri() + '?mode=ro'
    async with async_connect(uri, readonly=True, uri=True,
                                 timeout=base.busy_timeout_ms / 1000) as reader:
        # 400 pairs plus the scope fit SQLite's older 999-variable limit.
        for offset in range(0, len(keys), 400):
            chunk = keys[offset:offset + 400]
            query = ('SELECT subject,fingerprint FROM comparison_samples WHERE scope=? '
                     'AND (subject,fingerprint) IN (VALUES ' + ','.join('(?,?)' for _ in chunk) + ')')
            params = (base.scope, *(value for key in chunk for value in key))
            found = await reader.execute_fetchall(query, params)
            existing.update((row[0], row[1]) for row in found)
    return [row for key, row in unique.items() if key not in existing]


async def refresh_comparisons(affected=None, data=None):
    # Full calibration and event-driven subsets share one writer; an older
    # full run cannot overwrite a more recent incremental result.
    async with _refresh_lock:
        return await _refresh_comparisons(affected=affected, data=data)


async def _refresh_comparisons(affected=None, data=None):
    if data is None:
        await reload_data()
        data = DATA
    relations = {}
    for r in data.relations:
        relations.setdefault((str(r.get("chainId")), str(r.get("stock")).lower()), []).append(r)
    targets=None
    if affected is not None:
        targets = set(affected)
        for relation in data.relations:
            chain = str(relation.get("chainId"))
            dependencies = {str(relation.get(key) or "").lower() for key in ("token", "stock", "stockSide")}
            if (chain, '*') in affected or any((chain, token) in affected for token in dependencies):
                targets.add((chain, str(relation.get("stock") or "").lower()))
    def selected(row):
        chain=str(row.get('chainId'))
        return targets is None or (chain,'*') in targets or (chain,str(row.get('tokenContractAddress') or '').lower()) in targets
    if isinstance(data,DashboardData):
        # Copy the view, never truncate the shared dashboard or its directory.
        view=copy.copy(data)
        view.stock_tokens=[row for row in data.stock_tokens if selected(row)]
        stock_ids={(str(row.get('chainId')),str(row.get('tokenContractAddress') or '').lower()) for row in view.stock_tokens}
    else:
        stocks=[row for row in data.stock_views(include_comparisons=False) if selected(row)]
        stock_ids={(str(row.get('chainId')),str(row.get('tokenContractAddress') or '').lower()) for row in stocks}
    relations={key:value for key,value in relations.items() if key in stock_ids}
    needed=set(stock_ids)
    for (chain,_),group in relations.items():
        needed.update((chain,str(r.get(key) or '').lower()) for r in group for key in ('token','stock','stockSide'))
    assets={(str(a.get('chainId')),str(a.get('token')).lower()):enrich_asset(a)
            for a in data.assets if (str(a.get('chainId')),str(a.get('token')).lower()) in needed}
    if isinstance(data,DashboardData):
        stocks=view.stock_views(include_comparisons=False,enriched=assets)
    now = int(time.time()*1000)
    stores, previous, batches, fx = {}, {}, {}, {}
    processed = changed = unavailable_count = skipped = 0
    for chain in {str(st.get("chainId")) for st in stocks}:
        base=await store(chain)
        tokens={token for cid,token in stock_ids if cid==chain}
        related=[r for (cid,_),group in relations.items() if cid==chain for r in group]
        previous[chain]=await fact_index(base,'comparison',tokens)
        cached={'comparison':previous[chain],
                'pool-quote':await fact_index(base,'pool-quote',(r.get('pool','') for r in related)),
                'independent-quote':await fact_index(base,'independent-quote',
                    (r.get(key) or r.get('stock') for r in related for key in ('token','stockSide'))),
                'comparison-alert-state':await fact_index(base,'comparison-alert-state',
                    [token+':premium' for token in tokens]+[str(r.get('stock'))+':'+str(r.get('pool')) for r in related])}
        s=stores[chain]=ComparisonBatch(base,cached)
        batches[chain] = []
        fx[chain] = {q.get("fromCurrency"): q for q in await s.all("fx-quote")}
        batches[chain].extend(history_row("fx-v1:" + currency, q) for currency, q in fx[chain].items() if currency and q.get("at"))
    # Save each new market observation once, in one transaction per chain.
    history_assets = {(str(st.get("chainId")), str(st.get("tokenContractAddress")).lower()): st
                      for st in stocks if st.get("stockPrice") is not None or (str(st.get("chainId")), str(st.get("tokenContractAddress")).lower()) in relations}
    related_memes = {(str(r.get("chainId")), r.get("token", "").lower()) for group in relations.values() for r in group}
    for chain_token, a in assets.items():
        if chain_token in related_memes:
            history_assets[chain_token] = a
    for (chain, token), a in history_assets.items():
        if chain not in stores:
            continue
        a = {**a, "quoteFx": fx[chain].get(a.get("priceCurrency"))}
        q = token_observation(a)
        row = history_row(subject(token) + ":token", q) if q else None
        if row:
            batches[chain].append(row)
    accepted_observations = 0
    for chain, s in stores.items():
        new_rows = await new_history_rows(s, batches[chain])
        if new_rows:
            accepted_observations += await s.comparison_samples_batch(new_rows) or 0
        batches[chain] = []
    for stock in stocks:
        chain, token = str(stock.get("chainId")), str(stock.get("tokenContractAddress") or "").lower()
        if not token:
            skipped += 1
            continue
        processed += 1
        s = stores[chain]
        stock = {**stock, "quoteFx": fx[chain].get(stock.get("priceCurrency")),
                 "referenceFx": stock.get("referenceFx") or fx[chain].get(stock.get("referenceCurrency"))}
        if stock.get("referenceDelayMs") and stock.get("referenceFx") and stock.get("referenceAt"):
            target = stock["referenceAt"]
            if abs(stock["referenceFx"]["at"] - target) > MAX_SKEW:
                observations = await s.comparison_observations_at("fx-v1:" + stock["referenceCurrency"], target, MAX_SKEW)
                if observations:
                    stock["referenceFx"] = min(observations, key=lambda q: abs(q["at"] - target))
        premium_stock = independent_stock_row(stock)
        premium_stock["referenceFx"] = stock.get("referenceFx") if premium_stock.get("referenceCurrency") == stock.get("referenceCurrency") else fx[chain].get(premium_stock.get("referenceCurrency"))
        aligned_stock = await at_reference(s, premium_stock, premium_stock)
        premium = stock_premium(aligned_stock, now)
        if premium.get("value") is None:
            unavailable_count += 1
        if aligned_stock.get("alignmentReason") and premium.get("reason") == "missing-price":
            premium["reason"] = aligned_stock["alignmentReason"]
        # Existing projections and alerts always see invalidation. Only entirely
        # unseen, unpaired unavailable directory entries can skip persistence.
        prior = previous[chain].get(token)
        if (chain, token) not in relations and premium.get("value") is None and not prior:
            continue
        key = subject(token)
        row = history_row(key, premium) if premium.get("value") is not None else None
        if row:
            batches[chain].append(row)
        await check_alert(s, token, premium, now)
        pairs = []
        for rel in relations.get((chain, token), []):
            pool = rel.get("pool", "")
            pair_key = subject(token, pool)
            pool_quote = await s.get("pool-quote", pool)
            side = rel.get("stockSide") or token
            meme_asset = assets.get((chain, rel["token"].lower()), {})
            meme_quotes = quote_options(await s.get("independent-quote", rel["token"]), source_quote(meme_asset, chain, rel["token"]))
            stock_quotes = quote_options(await s.get("independent-quote", side), source_quote(assets.get((chain, side.lower())), chain, side))
            meme_quote, side_quote = select_independent_quotes(rel, meme_quotes, stock_quotes, now)
            spread = pool_spread(rel, pool_quote, meme_quote, side_quote, now)
            await check_alert(s, token, spread, now, pool=pool, liquidity=rel.get("liquidityUsd"), liquidity_at=rel.get("liquidityAt"))
            meme_asset = {**meme_asset, "quoteFx": fx[chain].get(meme_asset.get("priceCurrency"))}
            aligned_meme = await at_reference(s, meme_asset, stock)
            point = relative_point(stock, aligned_meme, now)
            if aligned_meme.get("alignmentReason") and point.get("reason") == "missing-price":
                point["reason"] = aligned_meme["alignmentReason"]
            if not point.get("reason"):
                batches[chain].append(history_row(pair_key + ":relative", point))
                history = await s.comparison_baselines(pair_key + ":relative", point["at"], MAX_SKEW)
            else:
                history = []
            if spread.get("value") is not None:
                batches[chain].append(history_row(pair_key, spread))
            pairs.append({"pool": pool, "token": rel["token"], "stockSide": side,
                          "spread": spread, "poolQuote": pool_quote,
                          "relativePoint": point if not point.get("reason") else None,
                          "relative": {"1h": relative_return(point, history, 3_600_000),
                                       "24h": relative_return(point, history, 86_400_000)}})
        packet = {"chainId": chain, "token": token, "method": VERSION, "premium": premium,
                  "pairs": pairs, "calculatedAt": now}
        if prior and digest({k: v for k, v in prior.items() if k != "calculatedAt"}) == digest({k: v for k, v in packet.items() if k != "calculatedAt"}):
            s.finish_subject()
            continue
        await s.put("comparison", token, packet)
        changed += 1
        s.finish_subject(packet)
    for chain, s in stores.items():
        await s.flush()
        new_rows = await new_history_rows(s, batches[chain])
        if new_rows:
            accepted_observations += await s.comparison_samples_batch(new_rows) or 0
    accepted = accepted_observations + changed
    return {"requested": len(stocks), "processed": processed, "changed": changed,
            "unavailable": unavailable_count, "skipped": skipped, "accepted": accepted,
            "observations": accepted_observations, "updated": changed,
            "failed": 0, "noChange": bool(processed) and accepted == 0}


async def read_comparison(chain, token, pool=None):
    s = await store(chain)
    now = int(time.time()*1000)
    packet = await s.get("comparison", token)
    if not packet or packet.get("method") != VERSION:
        from .scoped_reads import stock_view
        row = await stock_view(s, chain, token, include_comparison=False)
        packet = {"chainId": chain, "token": token, "method": VERSION,
                  "premium": row["premium"] if row else unavailable("pending"), "pairs": [], "calculatedAt": now}
    key = subject(token, pool)
    history = await s.comparison_history(key, now - 86_400_000)
    relative = await s.comparison_history(key + ":relative", now - 86_400_000) if pool else []
    alerts = [e for e in await s.events(token, 100) if e.get("kind") in ("spread-expanded", "spread-recovered") and (not pool or e.get("pool") in (None, pool))]
    return {"current": public_packet(packet, now), "alerts": alerts[:20], "history": [
        {"at": h["at"], "value": h.get("value"), "status": h.get("status"),
         "version": (h.get("inputs") or {}).get("ratioVersion"), "method": h.get("method")} for h in history],
        "relativeHistory": [{"at": h["at"], "stock": h["stock"], "meme": h["meme"],
                             **{k: h.get("inputs", {}).get(k) for k in ("stockSource", "memeSource", "referenceSymbol", "adjustmentVersion", "memeCurrency", "memeScope")}} for h in relative],
        "serverAt": now}
