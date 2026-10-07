"""Small evidence-bound product metrics. No request-time provider calls.

The same pure functions serve lists, asset details, themes and wallet readouts.
A missing or stale observation remains null; a provider's indexed-pool sum is
not described as the total of every pool on a chain.
"""
from __future__ import annotations

import math
import time
from statistics import median

MAX_AGE_MS = 30 * 60_000
THEME_VOLUME_MAX_AGE_MS = 15 * 60_000
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
# These fixed fee schedules apply only after independent factory proof.
V2_FACTORY_FEES = {
    '0xdf38f24fe153761634be942f9d859f3dba857e95': (30, 'https://docs.uniswap.org/contracts/v2/concepts/advanced-topics/fees'),
    '0xca143ce32fe78f1f7019d7d551a6402fc5350c73': (25, 'https://docs.pancakeswap.finance/trade/pancakeswap-exchange/trade'),
}


def number(value, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value if minimum is None or value >= minimum else None


def fresh(at, now, age=MAX_AGE_MS):
    return number(at, 1) is not None and 0 <= now-at <= age


def qualified_asset(asset, now=None):
    """One eligibility rule shared by home KPI and its list destination."""
    now = time.time()*1000 if now is None else now
    price = number(asset.get('price'), 0)
    return bool(asset.get('kind') == 'candidate' and price is not None and price > 0
                and fresh((asset.get('fieldTimes') or {}).get('price'), now, 900_000)
                and number(asset.get('totalLiquidityUsd'), 1000) is not None
                and asset.get('totalLiquidityStatus') == 'current'
                and fresh(asset.get('totalLiquidityAt'), now)
                and (asset.get('totalLiquidityCoverage') or {}).get('scope') == 'token-aggregate')


def _metric(value=None, *, at=None, source=None, scope=None, status='unknown', reason=None, **extra):
    return {'value': value, 'at': at, 'source': source, 'scope': scope,
            'status': status, 'reason': reason, **extra}


def field_availability(asset, now=None):
    now = time.time()*1000 if now is None else now
    result = {}
    for field in ('price', 'volume24h', 'marketCap', 'buys24h', 'sells24h', 'txs24h', 'holders', 'liquidity', 'change24h'):
        at = (asset.get('fieldTimes') or {}).get(field)
        value = number(asset.get(field))
        current = value is not None and fresh(at, now, DAY_MS if field == 'holders' else MAX_AGE_MS)
        result[field] = _metric(value if current else None, historicalValue=value if not current else None,
            at=at, source=(asset.get('fieldSources') or {}).get(field), scope=(asset.get('fieldScopes') or {}).get(field),
            status='current' if current else 'missing' if value is None else 'stale' if at else 'unknown',
            reason=None if current else 'observation-expired' if at else 'observation-time-unavailable')
    value, at = number(asset.get('totalLiquidityUsd'), 0), asset.get('totalLiquidityAt')
    coverage = asset.get('totalLiquidityCoverage') or {}
    # A zero from incomplete indexed pools is not proof of zero liquidity.
    usable = value is not None and fresh(at, now) and asset.get('totalLiquidityStatus') == 'current' and (value > 0 or coverage.get('complete') is True)
    result['totalLiquidityUsd'] = _metric(value if usable else None, historicalValue=value if not usable else None,
        at=at, source=coverage.get('provider'), scope=coverage.get('scope'), coverage=coverage,
        status='current' if usable else 'partial' if fresh(at, now) and value == 0 else 'stale' if at else 'unknown',
        reason=None if usable else 'indexed-coverage-incomplete' if value == 0 and not coverage.get('complete') else 'observation-expired')
    return result


def _pool_market(relation):
    pool = str(relation.get('pool') or '').lower()
    market = relation.get('poolMarket') or {}
    return market if pool and market.get('scope') == 'pool:'+pool else {}


def v2_exit_impact(relation, sell_usd, now=None):
    """Balanced V2 reserve estimate, including pool fee, excluding token tax.

    This is average proceeds versus the pre-swap quote, not the change in the
    ending marginal price. Provider liquidity is a USD two-sided reserve
    valuation; this estimate is not an executable quote.
    """
    now = time.time()*1000 if now is None else now
    pool = str(relation.get('pool') or '').lower()
    base = {'valuePercent': None, 'curveImpactPercent': None, 'outputUsd': None,
            'status': 'unknown', 'method': 'v2-balanced-reserves-estimate', 'pool': pool,
            'sellUsd': sell_usd, 'excludesTokenTax': True, 'executable': False}
    protocol = str(relation.get('dex') or relation.get('protocol') or '').lower()
    v2 = relation.get('poolType') in ('v2', 'uniswap_v2') or 'v2' in protocol or relation.get('liquidityMethod') in ('reserves_valuation', 'streamed-v2-two-sided-reserves', 'erc4626_reserves_valuation')
    if not v2:
        return {**base, 'status': 'unsupported', 'missing': ['confirmed-v2-pool']}
    if relation.get('status') not in ('verified', 'confirmed'):
        return {**base, 'missing': ['pool-identity']}
    fee = number(relation.get('feeBps'), 0)
    source = relation.get('feeSourceUrl')
    factory = str(relation.get('factory') or '').lower()
    pinned = V2_FACTORY_FEES.get(factory) if relation.get('factoryVerified') is True else None
    if fee is None and pinned:
        fee, source = pinned
    if fee is None or fee >= 10000 or not source:
        return {**base, 'missing': ['verified-fee-schedule']}
    liquidity, at = number(relation.get('liquidityUsd'), 0), relation.get('liquidityAt')
    if liquidity is None or liquidity <= 0 or liquidity >= 1e10 or not fresh(at, now):
        return {**base, 'status': 'stale' if at else 'unknown', 'missing': ['current-pool-liquidity']}
    amount = number(sell_usd, 0)
    if amount is None or amount <= 0:
        return {**base, 'missing': ['positive-sell-amount']}
    effective = amount*(1-fee/10000)
    output = effective/(1+effective/(liquidity/2))
    return {**base, 'valuePercent': 100*(1-output/amount),
            'curveImpactPercent': 100*(1-output/effective), 'outputUsd': output,
            'status': 'current', 'missing': [], 'at': at, 'feeBps': fee,
            'feeSourceUrl': source, 'liquidityUsd': liquidity,
            'source': relation.get('liquidityProvider') or relation.get('provider')}


def asset_product_metrics(asset, relations=None, poolMarkets=None, now=None):
    now = time.time()*1000 if now is None else now
    relations = list(relations or [])
    by_pool = {str(p.get('pool') or '').lower(): p for p in poolMarkets or []}
    pools = []
    for row in relations:
        if str(row.get('token') or '').lower() != str(asset.get('token') or '').lower() or str(row.get('chainId')) != str(asset.get('chainId')):
            continue
        pools.append({**by_pool.get(str(row.get('pool') or '').lower(), {}), **row})
    pools.sort(key=lambda p: (not fresh(p.get('liquidityAt'), now), -(number(p.get('liquidityUsd'), 0) or 0)))
    main = pools[0] if pools else {}
    available = field_availability(asset, now)
    buys, sells = (available[f]['value'] for f in ('buys24h', 'sells24h'))
    comparable = bool(asset.get('activityComparable')) and buys is not None and sells is not None
    buy_share = _metric(100*buys/(buys+sells) if comparable and buys+sells > 0 else None,
        at=available['buys24h']['at'], source=available['buys24h']['source'], scope=available['buys24h']['scope'],
        status='current' if comparable and buys+sells > 0 else 'unknown', unit='percent', reason=None if comparable else 'activity-coverage-incompatible')
    aggregate = asset.get('aggregateMarket') or {}
    volume, liquidity = number(aggregate.get('volume24h'), 0), available['totalLiquidityUsd']['value']
    volume_at, liquidity_at = aggregate.get('volumeAt'), available['totalLiquidityUsd']['at']
    comparable = bool(volume is not None and liquidity is not None and liquidity > 0
        and aggregate.get('scope') == 'token-aggregate' and aggregate.get('provider') == available['totalLiquidityUsd']['source']
        and fresh(volume_at, now) and fresh(liquidity_at, now) and abs(volume_at-liquidity_at) <= 300_000)
    ratio = _metric(volume/liquidity if comparable else None, at=volume_at, source=aggregate.get('provider'),
        scope='token-aggregate', status='current' if comparable else 'unknown', coverage=aggregate.get('coverage'),
        reason=None if comparable else 'same-source-volume-liquidity-unavailable')
    fdv = number(asset.get('fdv'), 0)
    fdv_at = (asset.get('fieldTimes') or {}).get('fdv')
    fdv_source = (asset.get('fieldSources') or {}).get('fdv')
    fdv_scope = (asset.get('fieldScopes') or {}).get('fdv') or 'token'
    main_market = _pool_market(main)
    if fdv is None and str(main_market.get('baseToken') or '').lower() == str(asset.get('token') or '').lower():
        fdv, fdv_at = number(main_market.get('fdv'), 0), main_market.get('updatedAt')
        fdv_source = main_market.get('provider')
        fdv_scope = 'token'
    supply = number(asset.get('totalSupply'), 0)
    price = available['price']['value']
    if fdv is None and supply is not None and price is not None and asset.get('supplyVerified') is True:
        fdv, fdv_at = supply*price, available['price']['at']
        fdv_source = available['price']['source']
        fdv_scope = 'token'
    age_at = main.get('poolCreatedAt')
    age_known = number(age_at, 1) is not None and age_at <= now and (main.get('creationConfirmed') is True or main.get('confirmationStatus') == 'confirmed' or main.get('creationStatus') == 'confirmed')
    changes = {}
    market = _pool_market(main)
    for window in ('m5', 'h1', 'h6', 'h24'):
        raw = number((market.get('priceChange') or {}).get(window)) if not market.get('baseToken') or str(market['baseToken']).lower() == str(asset.get('token') or '').lower() else None
        at = market.get('updatedAt')
        changes[window] = _metric(raw if raw is not None and fresh(at, now) else None,
            at=at, source=market.get('provider'), scope=market.get('scope'), status='current' if raw is not None and fresh(at, now) else 'unknown')
    impacts = {str(amount): v2_exit_impact(main, amount, now) for amount in (100, 1000, 10000)}
    return {'version': 1, 'at': now, 'qualified': qualified_asset(asset, now),
            'buyShare24h': buy_share, 'volumeLiquidityRatio': ratio, 'changes': changes,
            'fdvUsd': _metric(fdv if fdv is not None and fresh(fdv_at, now) else None, at=fdv_at,
                source=fdv_source, scope=fdv_scope,
                status='current' if fdv is not None and fresh(fdv_at, now) else 'unknown'),
            'poolAgeHours': _metric((now-age_at)/HOUR_MS if age_known else None, at=age_at,
                source='confirmed-creation-event' if age_known else None, scope='pool:'+str(main.get('pool') or '').lower(), status='current' if age_known else 'unknown'),
            'mainPool': main.get('pool'), 'exitImpacts': impacts, 'exitImpact1k': impacts['1000']}


def _usd_volume_currency(market):
    currencies = [market.get(field) for field in ('volumeCurrency', 'currency') if market.get(field)]
    # DexScreener's documented volume is USD. An explicit incompatible unit
    # still takes precedence over that provider-specific default.
    return all(value == 'USD' for value in currencies) if currencies else market.get('provider') == 'DexScreener'


def _current_theme_pool_volume(row, now):
    market = _pool_market(row)
    value = number(market.get('volume24h'), 0)
    return value if (value is not None and market.get('provider')
        and _usd_volume_currency(market)
        and fresh(market.get('updatedAt'), now, THEME_VOLUME_MAX_AGE_MS)) else None


def _theme_pool_observation_order(row):
    return (number(row.get('liquidityAt'), 0) or 0,
            number(row.get('checkedAt'), 0) or 0,
            number(_pool_market(row).get('updatedAt'), 0) or 0)


def _theme_paired_volume_ratio(selected, contributions, asset_map, now, volume_count):
    if not selected:
        return None, 'no-eligible-pools'
    if volume_count != len(selected):
        return None, 'pool-volume-incomplete'
    markets_by_asset = {}
    for (chain, _), row in selected.items():
        markets_by_asset.setdefault((chain, str(row.get('token') or '').lower()), []).append(_pool_market(row))
    total = paired = 0.0
    for key, direct_volume in contributions.items():
        aggregate = (asset_map.get(key) or {}).get('aggregateMarket') or {}
        value = number(aggregate.get('volume24h'), 0)
        if value is None or not fresh(aggregate.get('volumeAt'), now, THEME_VOLUME_MAX_AGE_MS):
            return None, 'asset-volume-unavailable'
        if aggregate.get('scope') != 'token-aggregate':
            return None, 'asset-volume-scope-incompatible'
        if not _usd_volume_currency(aggregate):
            return None, 'asset-volume-currency-incompatible'
        markets = markets_by_asset[key]
        if any(market.get('provider') != aggregate.get('provider') for market in markets):
            return None, 'volume-provider-mismatch'
        if any(abs(aggregate['volumeAt']-market['updatedAt']) > 300_000 for market in markets):
            return None, 'observation-time-mismatch'
        # Validate the sum of every direct pool for this asset. Checking each
        # pool separately allowed two 80 USD pools / a 100 USD asset to be 160%.
        if direct_volume > value and not math.isclose(direct_volume, value, rel_tol=1e-9, abs_tol=1e-9):
            return None, 'paired-volume-exceeds-asset-total'
        paired += direct_volume
        total += value
    return (min(1.0, paired/total), None) if total > 0 else (None, 'asset-volume-denominator-zero')


def build_product_theme_metrics(ticker, assets, relations, now=None, snapshots=None):
    now = time.time()*1000 if now is None else now
    asset_map = {(str(a.get('chainId')), str(a.get('token') or '').lower()): a for a in assets}
    observed = {}
    for row in relations:
        identity = row.get('stockIdentity') or {}
        if str(identity.get('ticker') or row.get('ticker') or '').upper() != str(ticker).upper():
            continue
        pool = str(row.get('pool') or '').lower()
        if not pool:
            continue
        key = (str(row.get('chainId')), pool)
        previous = observed.get(key)
        if previous is None or _theme_pool_observation_order(row) > _theme_pool_observation_order(previous):
            observed[key] = row
    selected = {key: row for key, row in observed.items()
        if row.get('status') == 'verified' and row.get('level') == 'A'
        and number(row.get('liquidityUsd'), 1000) is not None
        and fresh(row.get('liquidityAt'), now, 900_000)
        and (asset_map.get((key[0], str(row.get('token') or '').lower())) or {}).get('assetCategory') != 'derivative'}
    contributions = {}
    volume_count = created_count = new_count = 0
    for (chain, pool), row in selected.items():
        key = (chain, str(row.get('token') or '').lower())
        volume = _current_theme_pool_volume(row, now)
        if volume is not None:
            volume_count += 1
            contributions[key] = contributions.get(key, 0)+volume
        created = row.get('poolCreatedAt')
        confirmed = row.get('creationConfirmed') is True or row.get('confirmationStatus') == 'confirmed' or row.get('creationStatus') == 'confirmed'
        if confirmed and number(created, 1) is not None and created <= now:
            created_count += 1
            new_count += int(created >= now-DAY_MS)
    volume_sum = sum(contributions.values()) if contributions else None
    paired_ratio, ratio_reason = _theme_paired_volume_ratio(selected, contributions, asset_map, now, volume_count)
    ranked = sorted(contributions.items(), key=lambda item: -item[1])
    top_two = sum(value for _, value in ranked[:2])/volume_sum if volume_sum and volume_count == len(selected) else None
    history = list(snapshots or [])
    ratios = seven_day_volume_ratio(selected, history, now)
    return {'memeCount': len({(chain, str(row.get('token') or '').lower()) for (chain, _), row in selected.items()}),
        'volume24hUsd': volume_sum, 'mainPoolVolumeRatio': paired_ratio, 'mainPoolVolumeRatioReason': ratio_reason,
        'topTwoShare': top_two, 'newPairs24h': new_count if selected and created_count == len(selected) else None,
        'volumeRatio7d': ratios['value'], 'volumeRatio7dEvidence': ratios, 'at': now,
        'coverage': {'poolCount': len(selected), 'volumeKnown': volume_count, 'creationKnown': created_count,
            'complete': bool(selected) and volume_count == len(selected), 'scope': 'approved-stock-pair-pools'},
        'contributions': [{'chainId': key[0], 'token': key[1], 'volume24hUsd': value,
            'symbol': (asset_map.get(key) or {}).get('symbol'), 'share': value/volume_sum if volume_sum else None} for key, value in ranked]}


def seven_day_volume_ratio(selected, snapshots, now):
    """Current pool h24 / median of the same UTC hour on the seven prior days.

    Every historical day must cover exactly the current pool cohort and the
    same provider. New pools, missing hours or provider changes withhold the
    ratio rather than creating a smaller denominator.
    """
    if not selected:
        return {'value': None, 'status': 'no-eligible-pools', 'days': 0, 'requiredDays': 7}
    hour = int(now//HOUR_MS)*HOUR_MS
    pool_keys = set(selected)
    current = {}
    for key, row in selected.items():
        market = _pool_market(row)
        value = _current_theme_pool_volume(row, now)
        if value is None:
            return {'value': None, 'status': 'current-volume-incomplete', 'days': 0}
        current[key] = (value, market['provider'])
    buckets = {}
    for row in snapshots:
        key = (str(row.get('chainId')), str(row.get('pool') or '').lower())
        at = row.get('hour')
        value = number(row.get('volume24hUsd'), 0)
        if key in pool_keys and at in {hour-day*DAY_MS for day in range(1, 8)} and value is not None and row.get('provider') == current[key][1] and row.get('scope') == 'pool:'+key[1]:
            buckets.setdefault(at, {})[key] = value
    totals = [sum(buckets[hour-day*DAY_MS].values()) for day in range(1, 8) if set(buckets.get(hour-day*DAY_MS, {})) == pool_keys]
    if not pool_keys or len(totals) != 7:
        return {'value': None, 'status': 'history-insufficient', 'days': len(totals), 'requiredDays': 7, 'sameHourUtc': hour%DAY_MS//HOUR_MS}
    reference = median(totals)
    current_total = sum(value for value, _ in current.values())
    return {'value': current_total/reference if reference > 0 else None,
        'status': 'current' if reference > 0 else 'history-denominator-zero', 'days': 7,
        'currentVolume24hUsd': current_total, 'medianVolume24hUsd': reference, 'sameHourUtc': hour%DAY_MS//HOUR_MS,
        'scope': 'same-provider-current-pool-cohort', 'at': now}
