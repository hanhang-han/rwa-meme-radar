"""Evidence-bound asset risk checks.

Each check is three-valued: missing, stale, or incompatible inputs never mean
that the asset passed the check.  These flags describe anomalies, not findings
of fraud.  In particular, a pool observation cannot be divided into an
unrelated token-wide volume figure.
"""
import math
import time


MARKET_MAX_AGE_MS = 30 * 60_000
HOLDER_MAX_AGE_MS = 24 * 60 * 60_000
SCAN_MAX_AGE_MS = 24 * 60 * 60_000
MAX_ASOF_SKEW_MS = 5 * 60_000
FLAGS = ('wash_suspect', 'thin_spike', 'contract_risk', 'concentrated', 'holder_anomaly')


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _fresh(at, now, max_age):
    return _number(at) and 0 <= now - at <= max_age


def _check(status='unknown', reason='missing-evidence', evidence=None):
    return {'status': status, 'reason': reason, 'evidence': evidence or {}}


def _same_asof(left, right):
    return _number(left) and _number(right) and abs(left - right) <= MAX_ASOF_SKEW_MS


def assess_risk(asset: dict, aggregate_market: dict | None = None, now: float | None = None) -> dict:
    """Return a public, replayable risk assessment for one chain+contract.

    aggregate_market is a *single source's* token-wide observation.  Its
    volume and liquidity must refer to that same source/coverage; the public
    row's most recent 24h volume may have a different scope and is not used.
    """
    now = time.time() * 1000 if now is None else now
    times = asset.get('fieldTimes') or {}
    scopes = asset.get('fieldScopes') or {}
    checks = {flag: _check() for flag in FLAGS}
    total = asset.get('totalLiquidityUsd')
    total_at = asset.get('totalLiquidityAt')
    total_current = (asset.get('totalLiquidityStatus') == 'current'
                     and _number(total) and total >= 0 and _fresh(total_at, now, MARKET_MAX_AGE_MS))

    # A token aggregate may be incomplete relative to the whole chain.  The
    # provider-indexed coverage is carried into any positive flag and never
    # described as a whole-chain finding.
    ratio_check = _check()
    if aggregate_market:
        volume, liquidity = aggregate_market.get('volume24h'), aggregate_market.get('liquidityUsd')
        volume_at, liquidity_at = aggregate_market.get('volumeAt'), aggregate_market.get('liquidityAt')
        coverage = aggregate_market.get('coverage')
        provider = aggregate_market.get('provider')
        scope = aggregate_market.get('scope')
        if (scope == 'token-aggregate' and provider and coverage == 'provider-indexed-pools'
                and _number(volume) and volume >= 0 and _number(liquidity) and liquidity > 0
                and _fresh(volume_at, now, MARKET_MAX_AGE_MS)
                and _fresh(liquidity_at, now, MARKET_MAX_AGE_MS)
                and _same_asof(volume_at, liquidity_at)):
            ratio = volume / liquidity
            ratio_check = _check('triggered' if ratio > 50 else 'clear', 'threshold-exceeded' if ratio > 50 else 'below-threshold', {
                'volumeLiquidityRatio': ratio, 'volume24hUsd': volume, 'totalLiquidityUsd': liquidity,
                'numeratorScope': scope, 'denominatorScope': scope, 'coverage': coverage,
                'provider': provider, 'volumeAt': volume_at, 'liquidityAt': liquidity_at,
                'threshold': 50,
            })

    activity_check = _check()
    transactions, holders = asset.get('txs24h'), asset.get('holders')
    activity_scope = scopes.get('txs24h')
    if (activity_scope in ('token', 'token-aggregate') and _number(transactions) and transactions >= 0
            and _number(holders) and holders > 0
            and _fresh(times.get('txs24h'), now, MARKET_MAX_AGE_MS)
            and _fresh(times.get('holders'), now, HOLDER_MAX_AGE_MS)):
        per_holder = transactions / holders
        activity_check = _check('triggered' if per_holder > 20 else 'clear', 'threshold-exceeded' if per_holder > 20 else 'below-threshold', {
            'transactionsPerHolder': per_holder, 'transactions24h': transactions,
            'holderAddresses': holders, 'numeratorScope': activity_scope,
            'denominatorScope': 'token-holder-addresses', 'coverage': 'observed-holders',
            'transactionsAt': times['txs24h'], 'holdersAt': times['holders'], 'threshold': 20,
        })
    if ratio_check['status'] == 'triggered':
        checks['wash_suspect'] = ratio_check
    elif activity_check['status'] == 'triggered':
        checks['wash_suspect'] = activity_check
    elif ratio_check['status'] == activity_check['status'] == 'clear':
        checks['wash_suspect'] = _check('clear', 'below-threshold', {
            'volumeLiquidityRatio': ratio_check['evidence']['volumeLiquidityRatio'],
            'transactionsPerHolder': activity_check['evidence']['transactionsPerHolder'],
            'coverage': ratio_check['evidence']['coverage'],
            'denominatorScope': ratio_check['evidence']['denominatorScope'],
        })
    else:
        checks['wash_suspect'] = _check('unknown', 'incomplete-or-incompatible-market-coverage', {
            'volumeLiquidity': ratio_check, 'transactionsPerHolder': activity_check,
        })

    change = asset.get('change24h')
    if (total_current and _number(change) and scopes.get('change24h') in ('token', 'token-aggregate')
            and _fresh(times.get('change24h'), now, MARKET_MAX_AGE_MS)
            and _same_asof(times['change24h'], total_at)):
        triggered = change > 1000 and total < 100_000
        checks['thin_spike'] = _check('triggered' if triggered else 'clear', 'threshold-exceeded' if triggered else 'below-threshold', {
            'change24hPct': change, 'totalLiquidityUsd': total,
            'changeScope': scopes['change24h'], 'liquidityScope': 'token-aggregate',
            'coverage': (asset.get('totalLiquidityCoverage') or {}).get('coverage'),
            'changeAt': times['change24h'], 'liquidityAt': total_at,
            'threshold': {'change24hPct': 1000, 'totalLiquidityUsd': 100_000},
        })

    # An old generic `risk.level` or raw top-ten percentage is not an
    # Onchain OS scan and says nothing about honeypot/tax/mint/pause checks.
    scan = asset.get('tokenScan') or {}
    if (scan.get('provider') in ('OKX Onchain OS', 'Onchain OS')
            and scan.get('status') == 'complete' and _fresh(scan.get('checkedAt'), now, SCAN_MAX_AGE_MS)):
        tax_buy, tax_sell = scan.get('buyTaxPct'), scan.get('sellTaxPct')
        fields = ('honeypot', 'mintable', 'pausable')
        if all(isinstance(scan.get(field), bool) for field in fields) and _number(tax_buy) and _number(tax_sell):
            triggers = [field for field in fields if scan[field]]
            if tax_buy > 10:
                triggers.append('buyTaxPct')
            if tax_sell > 10:
                triggers.append('sellTaxPct')
            checks['contract_risk'] = _check('triggered' if triggers else 'clear', 'scan-trigger' if triggers else 'scan-clear', {
                'provider': scan['provider'], 'checkedAt': scan['checkedAt'],
                'triggers': triggers, 'buyTaxPct': tax_buy, 'sellTaxPct': tax_sell,
            })

    # Most provider `top10` metrics include pool, burn and custodial wallets.
    # Concentration can only be decided after those exclusions are evidenced.
    distribution = asset.get('holderDistribution') or {}
    top10 = distribution.get('top10AdjustedPercent')
    if (distribution.get('excludedKnownAddresses') is True and distribution.get('provider')
            and _number(top10) and 0 <= top10 <= 100
            and _fresh(distribution.get('checkedAt'), now, HOLDER_MAX_AGE_MS)):
        triggered = top10 > 50
        checks['concentrated'] = _check('triggered' if triggered else 'clear', 'threshold-exceeded' if triggered else 'below-threshold', {
            'top10AdjustedPercent': top10, 'exclusions': 'pools-burn-known-exchanges',
            'provider': distribution['provider'], 'checkedAt': distribution['checkedAt'], 'threshold': 50,
        })

    if (total_current and _number(holders) and holders >= 0
            and _fresh(times.get('holders'), now, HOLDER_MAX_AGE_MS)):
        triggered = holders > 1_000_000 and total < 5_000_000
        checks['holder_anomaly'] = _check('triggered' if triggered else 'clear', 'threshold-exceeded' if triggered else 'below-threshold', {
            'holderAddresses': holders, 'totalLiquidityUsd': total,
            'holderScope': 'token-holder-addresses', 'liquidityScope': 'token-aggregate',
            'coverage': (asset.get('totalLiquidityCoverage') or {}).get('coverage'),
            'holdersAt': times['holders'], 'liquidityAt': total_at,
            'threshold': {'holderAddresses': 1_000_000, 'totalLiquidityUsd': 5_000_000},
        })

    flags = [flag for flag in FLAGS if checks[flag]['status'] == 'triggered']
    statuses = [check['status'] for check in checks.values()]
    status = ('flagged' if flags else 'clear' if all(item == 'clear' for item in statuses)
              else 'partial' if any(item == 'clear' for item in statuses) else 'unknown')
    return {'version': 1, 'status': status, 'flags': flags, 'checks': checks, 'checkedAt': now}
