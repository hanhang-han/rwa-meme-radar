"""Scheduled, versioned basket projections. Queries never establish a baseline."""
import math
import asyncio
import json

from ..db import store, ResearchStore
from ..market_quotes import enrich_asset, enrich_relation
from ..stock_identity import assess_pool_relation
from .assets import now_ms
from .queue import result

SECTORS = {
    '芯片': ['NVDA', 'AMD', 'INTC', 'TSM', 'SKHY', 'SNDK'],
    '交易所': ['COIN', 'HOOD'], '支付': ['V', 'MA', 'PYPL', 'CRCL'],
    '托管': ['BK', 'STT'], '加密国库': ['MSTR', 'BMNR'],
}
METHOD_VERSION = 'official-a-fixed-cap-35-v3'
MAX_WEIGHT = 0.35
MIN_MEMBERS = 3
MAX_MEMBERS = 8
BASE_SYMBOLS = frozenset('WBNB BTCB WBTC WETH ETH BTC USDT USDC USDG DAI WOKB OKB XBTC XUSD USD1'.split())
BASE_ADDRESSES = frozenset({
    # Public BSC WBNB and BTCB contracts appear in old baskets. They are
    # excluded by address as well as symbol so a metadata rename cannot keep
    # a base asset in a themed Meme index.
    '56:0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c',
    '56:0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c',
})
CHAIN_LABELS = {'196': 'X Layer', '56': 'BNB Smart Chain', '4663': 'Robinhood Chain'}


def fresh(at, now, age=1_800_000):
    return isinstance(at, (int, float)) and 0 <= now-at <= age


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _meme_asset(asset, chain):
    if asset.get('kind') != 'candidate':
        return False
    if asset.get('chainId') is not None and str(asset['chainId']) != chain:
        return False
    token = str(asset.get('token') or '').lower()
    symbol = str(asset.get('symbol') or '').upper()
    return (bool(token) and symbol not in BASE_SYMBOLS
            and f'{chain}:{token}' not in BASE_ADDRESSES)


def _capped_weights(caps):
    """Redistribute excess proportionally while enforcing a hard 35% cap."""
    if len(caps) < MIN_MEMBERS or not all(positive(cap) for cap in caps):
        raise ValueError('At least three positive market caps are required')
    if len(caps) * MAX_WEIGHT < 1:
        raise ValueError('Weight cap cannot be satisfied')
    weights = [0.0] * len(caps)
    remaining = set(range(len(caps)))
    budget = 1.0
    while remaining:
        total = sum(caps[i] for i in remaining)
        capped = {i for i in remaining if budget * caps[i] / total > MAX_WEIGHT}
        if not capped:
            for i in remaining:
                weights[i] = budget * caps[i] / total
            break
        for i in capped:
            weights[i] = MAX_WEIGHT
            budget -= MAX_WEIGHT
        remaining -= capped
    return weights


def _qualified_assets(name, chain, assets, relations, now):
    tickers = set(SECTORS[name])
    qualified = set()
    for relation in relations:
        if str(relation.get('chainId') or chain) != chain:
            continue
        assessment = assess_pool_relation(relation, now)
        identity = assessment.get('stockIdentity') or {}
        if assessment.get('level') == 'A' and identity.get('ticker') in tickers:
            qualified.add(str(relation.get('token') or '').lower())
    candidates = [asset for token, asset in assets.items()
                  if token.lower() in qualified and _meme_asset(asset, chain)]
    eligible = [asset for asset in candidates
                if positive(asset.get('price')) and positive(asset.get('marketCap'))
                and fresh((asset.get('fieldTimes') or {}).get('price'), now)
                and fresh((asset.get('fieldTimes') or {}).get('marketCap'), now, 3_600_000)]
    eligible.sort(key=lambda asset: (-asset['marketCap'], str(asset['token']).lower()))
    return qualified, candidates, eligible[:MAX_MEMBERS]


def _sample_key(name, base):
    return base.get('sampleKey') or f'basket:{name}'


def project_basket(name, chain, base, assets, relations, now):
    chain = str(chain)
    qualified, candidates, eligible = _qualified_assets(name, chain, assets, relations, now)
    new_method = not base or base.get('methodVersion') != METHOD_VERSION
    if new_method and len(eligible) >= MIN_MEMBERS:
        version = int((base or {}).get('version') or 0) + 1
        weights = _capped_weights([asset['marketCap'] for asset in eligible])
        base = {'baseAt': now, 'version': version, 'methodVersion': METHOD_VERSION,
                'method': 'fixed-base-market-cap-weight-with-cap',
                'ruleVersion': 'official-pool-a-b-v1', 'maxWeight': MAX_WEIGHT,
                'sampleKey': f'basket:{name}:v{version}',
                'members': [
                    {'token': asset['token'], 'basePrice': asset['price'],
                     'baseCap': asset['marketCap'], 'weight': weight}
                    for asset, weight in zip(eligible, weights)
                ]}
    if not base or base.get('methodVersion') != METHOD_VERSION:
        historical = base or {}
        return base, {'sector': name, 'chainId': chain, 'scopeLabel': CHAIN_LABELS.get(chain, chain),
                      'baseAt': historical.get('baseAt'), 'basketVersion': historical.get('version'),
                      'methodVersion': historical.get('methodVersion'), 'projectionVersion': METHOD_VERSION,
                      'value': None,
                      'members': len(historical.get('members') or []) if base else len(candidates),
                      'legacyMembers': len(historical.get('members') or []), 'components': [
                          {'token': asset['token'], 'symbol': asset.get('symbol'), 'weight': None}
                          for asset in candidates],
                      'dataStatus': 'paused' if base else 'unavailable',
                      'quoteCoverage': {'fresh': len(eligible), 'total': max(MIN_MEMBERS, len(candidates))},
                      'reason': f'仅有 {len(eligible)}/{MIN_MEMBERS} 个官方 A 级且行情有效的 Meme；旧版指数暂停，保留历史' if base
                                else f'仅有 {len(eligible)}/{MIN_MEMBERS} 个官方 A 级且行情有效的 Meme',
                      'updatedAt': now}
    members = base.get('members') or []
    valid = len(members) >= MIN_MEMBERS and all(
        positive(m.get('weight')) and m['weight'] <= MAX_WEIGHT + 1e-9 for m in members)
    fresh_count, invalid, value, ats = 0, [], 0, []
    for member in members:
        token = member.get('token')
        asset = assets.get(token) or {}
        at = (asset.get('fieldTimes') or {}).get('price')
        priced = positive(asset.get('price')) and positive(member.get('basePrice')) and fresh(at, now)
        fresh_count += int(priced)
        if token not in qualified or not _meme_asset(asset, chain):
            invalid.append(token)
        valid = valid and priced and token in qualified and _meme_asset(asset, chain)
        if priced:
            value += 100 * member['weight'] * asset['price'] / member['basePrice']
            ats.append(at)
    view = {'sector': name, 'chainId': chain, 'baseAt': base['baseAt'], 'basketVersion': base.get('version', 1),
            'scopeLabel': CHAIN_LABELS.get(chain, chain), 'methodVersion': METHOD_VERSION,
            'projectionVersion': METHOD_VERSION,
            'sampleKey': _sample_key(name, base), 'maxWeight': MAX_WEIGHT,
            'members': len(members), 'value': value if valid else None,
            'at': min(ats) if valid else None, 'dataStatus': 'current' if valid else 'paused',
            'quoteCoverage': {'fresh': fresh_count, 'total': len(members)}, 'invalidMembers': invalid,
            'reason': '官方 A 级成分 · 基期市值权重（单项上限 35%）' if valid else '成分证据或行情过期，指数暂停；保留原基准',
            'updatedAt': now, 'components': [
                {'token': m['token'], 'symbol': (assets.get(m['token']) or {}).get('symbol') or m['token'],
                 'weight': m.get('weight')} for m in members]}
    return base, view


_refresh_lock = asyncio.Lock()


async def refresh_baskets(affected=None, data=None):
    async with _refresh_lock:
        return await _refresh_baskets(affected=affected, data=data)


async def _refresh_baskets(affected=None, data=None):
    totals, now = result(), now_ms()
    for chain in ('196', '56', '4663'):
        dirty = {str(token).lower() for cid, token in affected if str(cid) == chain} if affected is not None else None
        if dirty is not None and not dirty:
            continue
        s = await store(chain)
        raw_assets = [a for a in data.assets if str(a.get('chainId')) == chain] if data else await s.all('asset')
        raw_relations = [r for r in data.relations if str(r.get('chainId')) == chain] if data else await s.all('relation')
        bases = dict(await s.all_kv('basket'))
        selected=[]
        for name in SECTORS:
            original = bases.get(name)
            if dirty is not None and '*' not in dirty:
                members = {str(m.get('token') or '').lower() for m in (original or {}).get('members', [])}
                related = {str(r.get(key) or '').lower() for r in raw_relations
                           if (assess_pool_relation(r, now).get('stockIdentity') or {}).get('ticker') in SECTORS[name]
                           or r.get('ticker') in SECTORS[name]
                           for key in ('token', 'stock', 'stockSide')}
                if not dirty.intersection(members | related):
                    continue
            selected.append(name)
        if not selected:continue
        # The provider's ticker is a claim, not the issuer identity. Feed the
        # authoritative matcher all relations on this chain; it selects only
        # official A pools for the theme.
        relevant=raw_relations
        needed={r.get('token') for r in relevant}
        needed.update(m.get('token') for name in selected for m in (bases.get(name) or {}).get('members',[]))
        assets={a['token']:enrich_asset({**a,'chainId':chain}) for a in raw_assets if a.get('token') in needed}
        # DashboardData.reload has already applied the exact same relation overlay.
        relations=relevant if data else [enrich_relation({**r,'chainId':chain}) for r in relevant]
        lasts=dict(await s.all_kv('basket-last'))
        previous_views=dict(await s.all_kv('basket-view'))
        updates=[];samples=[]
        for name in selected:
            original=bases.get(name)
            totals['requested'] += 1
            base, view = project_basket(name, chain, original, assets, relations, now)
            if base and base != original:
                if original:
                    archive_key=f"{name}:v{original.get('version',1)}:{original.get('baseAt') or 0}"
                    updates.append(('basket-archive',archive_key,{
                        'sector':name,'chainId':chain,'retiredAt':now,
                        'reason':'superseded-by-official-a-method',
                        'base':original,'last':lasts.get(name),
                        'view':previous_views.get(name),
                        'sampleKey':_sample_key(name,original),
                    }))
                updates.append(('basket',name,base))
            sample_key=_sample_key(name,base or {})
            history=await s.samples(sample_key,200) if base else []
            last={} if base != original else (lasts.get(name) or {})
            if base and last.get('basketVersion') not in (None,base.get('version')):
                last={}
            if view.get('value') is not None:
                current={'value':view['value'],'at':view['at'],'basketVersion':base.get('version',1)}
                if current!=last:
                    updates.append(('basket-last',name,current))
                    bucket=int(view['at']//300000*300000)
                    samples.append((sample_key,bucket,view['value'],None))
                    by_time={r['t']:r for r in history}
                    by_time[bucket]={**by_time.get(bucket,{'provenance':None}),'t':bucket,'price':view['value'],'cap':None}
                    history=[by_time[t] for t in sorted(by_time)[-200:]]
                last=current
            view.update(lastValue=last.get('value'), lastAt=last.get('at'),
                        history=history)
            previous = previous_views.get(name) or {}
            if {k: v for k, v in previous.items() if k != 'updatedAt'} != {k: v for k, v in view.items() if k != 'updatedAt'}:
                updates.append(('basket-view',name,view))
            totals['accepted'] += 1
            totals['updated'] += 1
        await _persist_projection(s,updates,samples)
    return totals


async def _persist_projection(s,updates,samples):
    if not updates and not samples:return
    if not isinstance(s,ResearchStore):
        for asset,at,price,cap in samples:await s.sample(asset,price,cap,at)
        for kind,ident,body in updates:await s.put(kind,ident,body)
        return
    facts=[(s.key(kind),ident,json.dumps(body,ensure_ascii=False,allow_nan=False)) for kind,ident,body in updates]
    rows=[(s.key(asset),at,price,cap)for asset,at,price,cap in samples]
    # At most five sectors / chain: one short commit publishes baseline,
    # sample and view together instead of taking the writer for every field.
    async with s._guard_write():
        await s.db.execute('BEGIN IMMEDIATE')
        if rows:await s.db.executemany('''INSERT INTO samples VALUES (?,?,?,?)
            ON CONFLICT(asset,t) DO UPDATE SET price=excluded.price,cap=excluded.cap
            WHERE samples.price IS NOT excluded.price OR samples.cap IS NOT excluded.cap''',rows)
        if facts:await s.db.executemany('''INSERT INTO facts VALUES (?,?,?)
            ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body WHERE facts.body IS NOT excluded.body''',facts)
        await s.db.commit()
