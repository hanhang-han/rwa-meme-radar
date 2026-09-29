"""Read field observations from the shared enrichment snapshot.

An object write time is not a field observation time. Old snapshots without
field times remain usable as historical values, never as fresh observations.
"""
import math
import time
from .stock_quotes import _read_snapshot, EODHD_FILE
from .risk_assessment import assess_risk

FIELDS = ('price', 'marketCap', 'volume24h', 'buys24h', 'sells24h', 'txs24h', 'holders', 'liquidity', 'change24h')


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def field_at(q: dict, field: str):
    at = (q.get('fieldTimes') or {}).get(field)
    # Blockscout snapshots are single-purpose holder records, not merged market objects.
    if not at and field == 'holders' and q.get('provider') == 'Blockscout':
        at = q.get('updatedAt')
    return at if _number(at) and at > 0 else None


def _aggregate_market(asset: dict, observations: list[dict], now: float):
    """Select one explicitly token-wide liquidity quote, never a top pool.

    CoinGecko's total_reserve_in_usd covers its indexed pools.  This is a
    source-reported aggregate, not proof that every on-chain pool was indexed.
    The volume/liquidity comparison below retains that exact source scope.
    """
    candidates = [q for q in observations
                  if (q.get('fieldScopes') or {}).get('liquidity') == 'token-aggregate'
                  and _number(q.get('liquidity')) and 0 <= q['liquidity'] < 1e10
                  and field_at(q, 'liquidity')]
    own_scope = (asset.get('fieldScopes') or {}).get('liquidity')
    own_at = (asset.get('fieldTimes') or {}).get('liquidity')
    if (own_scope == 'token-aggregate' and _number(asset.get('liquidity'))
            and 0 <= asset['liquidity'] < 1e10 and _number(own_at) and own_at > 0):
        candidates.append({'provider': (asset.get('fieldSources') or {}).get('liquidity'),
                           'liquidity': asset['liquidity'], 'fieldTimes': {'liquidity': own_at},
                           'fieldScopes': {'liquidity': 'token-aggregate'}})
    chosen = max(candidates, key=lambda q: field_at(q, 'liquidity') or 0, default=None)
    if chosen is None:
        return None
    liquidity_at = field_at(chosen, 'liquidity')
    volume_scope = (chosen.get('fieldScopes') or {}).get('volume24h')
    volume = chosen.get('volume24h') if volume_scope == 'token-aggregate' else None
    volume_at = field_at(chosen, 'volume24h') if volume is not None else None
    return {'provider': chosen.get('provider'), 'scope': 'token-aggregate',
            'coverage': 'provider-indexed-pools', 'complete': False,
            'liquidityUsd': chosen['liquidity'], 'liquidityAt': liquidity_at,
            'volume24h': volume if _number(volume) and volume >= 0 else None,
            'volumeAt': volume_at,
            'status': 'current' if 0 <= now - liquidity_at <= 1_800_000 else 'stale'}


def enrich_asset(asset: dict) -> dict:
    row = {**asset, 'fieldTimes': dict(asset.get('fieldTimes') or {}),
           'fieldSources': dict(asset.get('fieldSources') or {}),
           'fieldScopes': dict(asset.get('fieldScopes') or {}),
           'fieldTimeKinds': dict(asset.get('fieldTimeKinds') or {}),
           'fieldObservations': dict(asset.get('fieldObservations') or {}),
           'fieldStatus': dict(asset.get('fieldStatus') or {})}
    snapshot = _read_snapshot(EODHD_FILE)
    key = f"{row.get('chainId', '196')}:{str(row.get('token', '')).lower()}"
    observations = list((snapshot.get('assets', {}).get(key) or {}).values())
    holder = snapshot.get('holders', {}).get(key)
    if holder:
        observations.append(holder)
    now = time.time() * 1000
    aggregate = _aggregate_market(asset, observations, now)
    prior_scope = row['fieldScopes'].get('liquidity') or ''
    if prior_scope.startswith('pool:'):
        # Preserve the single-pool observation under its actual scope, then
        # clear the generic slot before token-wide candidates are considered.
        row['singlePoolLiquidityUsd'] = row.get('liquidity') if _number(row.get('liquidity')) else None
        row['singlePoolLiquidityScope'] = prior_scope
        row['singlePoolLiquidityAt'] = row['fieldTimes'].get('liquidity')
        row['liquidity'] = None
        row['fieldTimes']['liquidity'] = None
        row['fieldSources']['liquidity'] = None
        row['fieldScopes']['liquidity'] = 'unknown'
        row['fieldTimeKinds']['liquidity'] = 'unknown'
        row['fieldObservations'].pop('liquidity', None)
    for field in FIELDS:
        available = [q for q in observations if _number(q.get(field))
                     and (field != 'liquidity' or (q.get('fieldScopes') or {}).get(field) == 'token-aggregate')]
        if available:
            q = max(available, key=lambda q: field_at(q, field) or 0)
            at = field_at(q, field)
            if row.get(field) is None or (at and at > (row['fieldTimes'].get(field) or 0)):
                row[field] = q[field]
                row['fieldTimes'][field] = at
                row['fieldSources'][field] = q.get('provider')
                row['fieldScopes'][field] = (q.get('fieldScopes') or {}).get(field, 'unknown')
                row['fieldTimeKinds'][field] = (q.get('fieldTimeKinds') or {}).get(field, 'observed') if at else 'unknown'
                row['fieldObservations'][field] = {'provider': q.get('provider'), 'scope': row['fieldScopes'][field],
                    'timeKind': row['fieldTimeKinds'][field], 'marketAt': at if row['fieldTimeKinds'][field] == 'market' else None,
                    'receivedAt': q.get('updatedAt'), 'observedAt': at, 'currency': 'USD' if field in ('price', 'marketCap', 'volume24h', 'liquidity') else None}
                if field == 'price':
                    row['priceProvenance'] = q.get('priceProvenance')
        at = row['fieldTimes'].get(field)
        missing = row.get(field) is None
        age_limit = 86_400_000 if field == 'holders' else 1_800_000
        status = 'missing' if missing else 'unknown' if not at else 'scheduled' if 0 <= now-at <= age_limit else 'stale'
        row['fieldStatus'][field] = {'status': status, 'reason': 'not-observed' if missing else 'field-time-unavailable' if not at else None}
    distribution = [q for q in observations if _number(q.get('holderTop10')) and 0 <= q['holderTop10'] <= 100]
    if distribution:
        q = max(distribution, key=lambda q: field_at(q, 'holderTop10') or field_at(q, 'holders') or 0)
        at = field_at(q, 'holderTop10') or field_at(q, 'holders')
        old = row.get('risk') or {}
        if old.get('top10') is None or (at and at > (old.get('checkedAt') or 0)):
            row['risk'] = {**old, 'level': old.get('level', 'unknown'), 'tags': old.get('tags', []),
                           'top10': q['holderTop10'], 'checkedAt': at, 'provider': q.get('provider')}
    row['totalLiquidityUsd'] = aggregate['liquidityUsd'] if aggregate else None
    row['totalLiquidityAt'] = aggregate['liquidityAt'] if aggregate else None
    row['totalLiquidityStatus'] = aggregate['status'] if aggregate else 'unknown'
    row['totalLiquidityCoverage'] = ({k: aggregate[k] for k in ('provider', 'scope', 'coverage', 'complete')}
                                     if aggregate else {'scope': 'unknown', 'coverage': 'unverified', 'complete': False})
    assessment = assess_risk(row, aggregate, now)
    row['riskFlags'] = assessment['flags']
    row['riskStatus'] = assessment['status']
    row['riskAssessment'] = assessment
    row['quoteType'] = 'dex'
    row['venue'] = 'dex'
    row['priceScope'] = 'dex'
    row['provider'] = row['fieldSources'].get('price') or row.get('provider') or 'OKX'
    row['priceCurrency'] = 'USD' if row['provider'] in {'OKX', 'CoinGecko', 'DexScreener'} else asset.get('priceCurrency')
    row['quoteAt'] = row['fieldTimes'].get('price')
    row['quoteStatus'] = row['fieldStatus']['price']['status']
    row['quoteReason'] = row['fieldStatus']['price']['reason']
    row['activityScope'] = row['fieldScopes'].get('buys24h', 'unknown')
    row['activityComparable'] = all(row.get(f) is not None for f in ('buys24h', 'sells24h')) and row['activityScope'] != 'unknown' and row['activityScope'] == row['fieldScopes'].get('sells24h') and row['fieldTimes'].get('buys24h') == row['fieldTimes'].get('sells24h')
    return row


def enrich_relation(relation: dict) -> dict:
    """Single pool projection shared by dashboard, detail and comparison inputs."""
    row = dict(relation)
    key = f"{row.get('chainId', '196')}:{str(row.get('pool', '')).lower()}"
    records = (_read_snapshot(EODHD_FILE).get('pools') or {}).get(key) or {}
    candidates = [q for q in records.values() if _number(q.get('liquidityUsd')) and 0 <= q['liquidityUsd'] < 1e10]
    q = max(candidates, key=lambda q: q.get('updatedAt') or 0, default=None)
    own = row.get('liquidityUsd')
    own_valid = _number(own) and 0 <= own < 1e10
    if not own_valid:
        row['liquidityUsd'] = None
    # Pool observations are replaced atomically by the producer, not merged
    # across holder-only updates, so their observedAt is legitimate.
    if q and (not own_valid or (q.get('updatedAt') or 0) > (row.get('liquidityAt') or 0)):
        row.update(liquidityUsd=q['liquidityUsd'], liquidityAt=q.get('updatedAt'),
                   liquidityProvider=q.get('provider'), liquidityTimeKind='observed')
        row['poolMarket'] = {f: q.get(f) for f in ('volume24h', 'buys24h', 'sells24h', 'txs24h', 'provider', 'updatedAt')}
        row['poolMarket']['scope'] = 'pool:' + str(row.get('pool', '')).lower()
    at = row.get('liquidityAt')
    row['liquidityStatus'] = 'missing' if row.get('liquidityUsd') is None else 'unknown' if not at else 'scheduled' if 0 <= time.time()*1000-at <= 1_800_000 else 'stale'
    row['liquidityScope'] = 'pool:' + str(row.get('pool') or '').lower() if row.get('pool') else 'unknown'
    return row
