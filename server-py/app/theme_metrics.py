"""Reproducible stock-pair flow and heat from audited Swap evidence.

These pure calculators are intentionally disconnected from public rankings
until a confirmed on-chain Swap cursor supplies the required coverage. The
current time-observation trade buckets do not establish 95% event coverage.

Input pools have an A-grade relation and `swapCoverage24h` with
{method:'confirmed-onchain-swap-cursor', ratio, from, to}. Each confirmed trade
identifies its pool, direction relative to the Meme, stockAmount and an OKX
USD stock-token quote observed at the trade. Historical daily theme totals
must carry the same audited coverage method and unique tx.from count.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


DAY_MS = 86_400_000
MAX_QUOTE_SKEW_MS = 60_000
MAX_LIQUIDITY_AGE_MS = 15 * 60_000
MAX_CONFIRMED_TAIL_LAG_MS = 60_000
COVERAGE_MIN = Decimal('0.95')
METHOD = 'confirmed-onchain-swap-cursor'


def _decimal(value, minimum=None):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        return None
    if not result.is_finite() or (minimum is not None and result < minimum):
        return None
    return result


def _time(value, now):
    number = _decimal(value, Decimal(1))
    return int(number) if number is not None and number <= now else None


def _eligible_pools(ticker, pools, now, exclude_abnormal):
    selected = {}
    excluded = []
    for row in pools:
        pool = str(row.get('pool') or '').lower()
        relation = row.get('relation') or row
        identity = relation.get('stockIdentity') or {}
        row_ticker = identity.get('ticker') or relation.get('ticker')
        liquidity = _decimal(relation.get('liquidityUsd'), Decimal(0))
        liquidity_at = _time(relation.get('liquidityAt'), now)
        if (not pool or relation.get('level') != 'A' or relation.get('status') != 'verified'
                or str(row_ticker or '').upper() != str(ticker).upper()
                or liquidity is None or liquidity < 1000
                or liquidity_at is None or now-liquidity_at > MAX_LIQUIDITY_AGE_MS):
            continue
        if exclude_abnormal and set(row.get('riskFlags') or ()) & {'wash_suspect', 'thin_spike'}:
            excluded.append(pool)
            continue
        prior = selected.get(pool)
        if prior is None or relation.get('liquidityAt', 0) > (prior.get('relation') or prior).get('liquidityAt', 0):
            selected[pool] = row
    return selected, sorted(set(excluded))


def _coverage(row, now):
    evidence = row.get('swapCoverage24h') or {}
    ratio = _decimal(evidence.get('ratio'), Decimal(0))
    start = _time(evidence.get('from'), now)
    end = _time(evidence.get('to'), now)
    if (evidence.get('method') != METHOD or ratio is None or ratio > 1
            or start is None or end is None or now-end > MAX_CONFIRMED_TAIL_LAG_MS
            or start > end-DAY_MS):
        return None
    return {'ratio': ratio, 'from': start, 'to': end}


def _common_coverage(selected, now):
    evidence = {pool: _coverage(row, now) for pool, row in selected.items()}
    if any(value is None for value in evidence.values()):
        return None
    cutoff = min(value['to'] for value in evidence.values())
    if any(value['from'] > cutoff-DAY_MS for value in evidence.values()):
        return None
    # When pools' confirmed heads differ, exclude the unshared tail from the
    # displayed window and conservatively reduce the reported coverage ratio.
    ratio = min(max(Decimal(0), value['ratio'] -
                    Decimal(value['to']-cutoff)/Decimal(DAY_MS))
                for value in evidence.values())
    return {'to': cutoff, 'ratio': ratio, 'trailingLagMs': now-cutoff}


def _trade_value(trade, pool, start, end):
    if (str(trade.get('pool') or '').lower() != pool
            or trade.get('direction') not in ('buy_meme', 'sell_meme')
            or trade.get('finality') != 'confirmed'):
        return None
    t = _time(trade.get('t'), end)
    quote = trade.get('stockUsdQuote') or {}
    quote_at = _time(quote.get('at'), end)
    amount = _decimal(trade.get('stockAmount'), Decimal(0))
    price = _decimal(quote.get('value'), Decimal(0))
    if (t is None or not start <= t <= end or amount is None or price is None
            or quote.get('currency') != 'USD' or quote.get('provider') != 'OKX'
            or quote_at is None or abs(quote_at-t) > MAX_QUOTE_SKEW_MS):
        return None
    return amount * price


def stock_pool_flow(ticker, pools, now, *, exclude_abnormal=True, history=None):
    """Compute 24h buy pressure; only audited ≥95% samples may rank.

    A lower audited ratio returns an observed value with sample-incomplete.
    Missing audit or any unpriced trade returns no aggregate at all.
    """
    selected, excluded = _eligible_pools(ticker, pools, now, exclude_abnormal)
    if not selected:
        return {'valueUsd': None, 'status': 'no-eligible-pools', 'rankEligible': False,
                'poolCount': 0, 'excludedPools': excluded}
    coverage = _common_coverage(selected, now)
    if coverage is None:
        return {'valueUsd': None, 'status': 'coverage-unknown', 'rankEligible': False,
                'poolCount': len(selected), 'excludedPools': excluded,
                'coverageRatio': None}
    cutoff = coverage['to']
    start = cutoff-DAY_MS
    buys = sells = Decimal(0)
    ids = set()
    refs = []
    for pool, row in selected.items():
        for trade in row.get('trades') or ():
            t = _time(trade.get('t'), now)
            if t is None or t < start:
                continue
            if t > cutoff:
                continue
            identity = (pool, trade.get('id'))
            if not trade.get('id') or identity in ids:
                continue
            ids.add(identity)
            value = _trade_value(trade, pool, start, cutoff)
            if value is None:
                return {'valueUsd': None, 'status': 'unpriced-or-unconfirmed-swap',
                        'rankEligible': False, 'poolCount': len(selected),
                        'excludedPools': excluded, 'coverageRatio': float(coverage['ratio']),
                        'trailingLagMs': coverage['trailingLagMs']}
            if trade['direction'] == 'buy_meme':
                buys += value
            else:
                sells += value
            refs.append({'pool': pool, 'tradeId': str(trade['id']),
                         'signedValueUsd': float(value if trade['direction'] == 'buy_meme' else -value)})
    net = buys-sells
    min_coverage = coverage['ratio']
    status = 'current' if min_coverage >= COVERAGE_MIN else 'sample-incomplete'
    mean = None
    prior_windows = {(cutoff-(day+2)*DAY_MS, cutoff-(day+1)*DAY_MS)
                     for day in range(7)}
    if history and len(history) == 7 and {
            (item.get('from'), item.get('to')) for item in history} == prior_windows and all(
            item.get('method') == METHOD and _decimal(item.get('coverageRatio'), Decimal(0)) is not None
            and _decimal(item.get('coverageRatio'), Decimal(0)) >= COVERAGE_MIN
            and _decimal(item.get('netUsd')) is not None for item in history):
        mean = sum((_decimal(item['netUsd']) for item in history), Decimal(0))/7
    multiple = float(net/mean) if mean is not None and mean > 0 else None
    total = buys+sells
    return {'valueUsd': float(net), 'buyUsd': float(buys), 'sellUsd': float(sells),
            'buyShare': float(buys/total) if total > 0 else None,
            'vsSevenDayMean': multiple, 'historyMeanUsd': float(mean) if mean is not None else None,
            'status': status, 'rankEligible': status == 'current',
            'coverageRatio': float(min_coverage), 'poolCount': len(selected),
            'excludedPools': excluded, 'window': {'from': start, 'to': cutoff},
            'trailingLagMs': coverage['trailingLagMs'],
            'method': METHOD, 'trades': refs}


def _heat_components(ticker, pools, history_days, now):
    selected, excluded = _eligible_pools(ticker, pools, now, True)
    if len(selected) < 3:
        return {'status': 'sample-too-small', 'poolCount': len(selected),
                'excludedPools': excluded}
    coverage = _common_coverage(selected, now)
    if coverage is None or coverage['ratio'] < COVERAGE_MIN:
        return {'status': 'coverage-insufficient', 'poolCount': len(selected),
                'excludedPools': excluded}
    if any(_time((row.get('relation') or row).get('poolCreatedAt'), now) is None
           or (row.get('relation') or row).get('creationConfirmed') is not True
           for row in selected.values()):
        return {'status': 'pool-creation-unknown', 'poolCount': len(selected),
                'excludedPools': excluded}
    cutoff = coverage['to']
    valid_days = {}
    for day in history_days:
        start = _time(day.get('dayStart'), now)
        volume = _decimal(day.get('volumeUsd'), Decimal(0))
        traders = _decimal(day.get('uniqueTraderAddresses'), Decimal(0))
        daily_coverage = _decimal(day.get('coverageRatio'), Decimal(0))
        if (start is not None and cutoff-8*DAY_MS <= start < cutoff-DAY_MS
                and day.get('method') == METHOD and daily_coverage is not None
                and daily_coverage >= COVERAGE_MIN and volume is not None
                and traders is not None and day.get('complete') is True):
            valid_days[start] = (volume, traders)
    if len(valid_days) < 3:
        return {'status': 'history-insufficient', 'poolCount': len(selected),
                'excludedPools': excluded}
    unique, volume = set(), Decimal(0)
    seen = set()
    for pool, row in selected.items():
        for trade in row.get('trades') or ():
            t = _time(trade.get('t'), now)
            if t is None or t < cutoff-DAY_MS or t > cutoff:
                continue
            identity = (pool, trade.get('id'))
            if not trade.get('id') or identity in seen:
                continue
            seen.add(identity)
            tx_from = str(trade.get('txFrom') or '').lower()
            if not (tx_from.startswith('0x') and len(tx_from) == 42):
                return {'status': 'trader-address-unknown', 'poolCount': len(selected),
                        'excludedPools': excluded}
            value = _trade_value(trade, pool, cutoff-DAY_MS, cutoff)
            if value is None:
                return {'status': 'unpriced-or-unconfirmed-swap', 'poolCount': len(selected),
                        'excludedPools': excluded}
            unique.add(tx_from)
            volume += value
    n = Decimal(len(valid_days))
    average_volume = sum((item[0] for item in valid_days.values()), Decimal(0))/n
    average_traders = sum((item[1] for item in valid_days.values()), Decimal(0))/n
    if average_volume <= 0 or average_traders <= 0:
        return {'status': 'history-denominator-zero', 'poolCount': len(selected),
                'excludedPools': excluded}
    new_pools = sum(cutoff-DAY_MS <= _time((row.get('relation') or row)['poolCreatedAt'], now) <= cutoff
                    for row in selected.values())
    return {'status': 'current', 'poolCount': len(selected), 'excludedPools': excluded,
            'components': {'volumeRatio': float(volume/average_volume),
                           'newPools24h': new_pools,
                           'traderRatio': float(Decimal(len(unique))/average_traders),
                           'volume24hUsd': float(volume), 'uniqueTraders24h': len(unique),
                           'historyDays': len(valid_days)},
            'coverageRatio': float(coverage['ratio']), 'trailingLagMs': coverage['trailingLagMs'],
            'method': METHOD}


def rank_theme_heat(themes, now):
    """Equal-weight midrank percentiles, with all three raw parts retained."""
    results = {str(theme['ticker']).upper(): _heat_components(theme['ticker'],
               theme.get('pools') or [], theme.get('historyDays') or [], now)
               for theme in themes}
    eligible = [row for row in results.values() if row['status'] == 'current']
    for component in ('volumeRatio', 'newPools24h', 'traderRatio'):
        values = [row['components'][component] for row in eligible]
        for row in eligible:
            value = row['components'][component]
            less = sum(other < value for other in values)
            equal = sum(other == value for other in values)
            row.setdefault('percentiles', {})[component] = 100*(less+equal/2)/len(values)
    for row in eligible:
        row['heat'] = sum(row['percentiles'].values())/3
        row['rankEligible'] = True
    for row in results.values():
        row.setdefault('heat', None)
        row.setdefault('rankEligible', False)
    return results
