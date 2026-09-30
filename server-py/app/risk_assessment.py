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
FLAGS = ('wash_suspect', 'thin_spike', 'contract_risk', 'concentrated', 'holder_anomaly', 'liquidity_unlock')
SCAN_PROVIDERS = ('OKX Onchain OS', 'Onchain OS', 'GoPlus')


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _fresh(at, now, max_age):
    return _number(at) and 0 <= now - at <= max_age


def _check(status='unknown', reason='missing-evidence', evidence=None):
    return {'status': status, 'reason': reason, 'evidence': evidence or {}}


def _same_asof(left, right):
    return _number(left) and _number(right) and abs(left - right) <= MAX_ASOF_SKEW_MS


def safety_checks(asset, now):
    """Independent checks: unknown inputs can neither pass nor mask a finding.

    ``clear`` only describes the fields tested here, never overall safety.
    Provider observation time is not a claim about the scanner's block time.
    """
    scan = asset.get('tokenScan') or {}
    observation = asset.get('securityObservation') or {}
    result = {key: _check() for key in ('tax', 'permissions', 'concentration', 'liquidityLock')}
    valid_scan = (scan.get('provider') in SCAN_PROVIDERS and scan.get('status') in ('partial', 'complete')
                  and _fresh(scan.get('checkedAt'), now, SCAN_MAX_AGE_MS))
    if valid_scan:
        provenance = {key: scan.get(key) for key in ('provider', 'checkedAt', 'timeKind', 'sourceUrl')}
        buy, sell, honeypot = scan.get('buyTaxPct'), scan.get('sellTaxPct'), scan.get('honeypot')
        buy = buy if _number(buy) and 0 <= buy <= 100 else None
        sell = sell if _number(sell) and 0 <= sell <= 100 else None
        honeypot = honeypot if isinstance(honeypot, bool) else None
        triggers = (['honeypot'] if honeypot else []) + (['buyTaxPct'] if buy is not None and buy > 10 else []) + (['sellTaxPct'] if sell is not None and sell > 10 else [])
        known = buy is not None and sell is not None and honeypot is not None
        result['tax'] = {**_check('triggered' if triggers else 'clear' if known else 'unknown',
            'scan-trigger' if triggers else 'scan-clear' if known else 'incomplete-scan',
            {'buyTaxPct': buy, 'sellTaxPct': sell, 'honeypot': honeypot,
             'thresholdPct': 10, 'triggers': triggers}), **provenance}
        fields = ('mintable', 'pausable', 'blacklist', 'ownerChangeBalance', 'canTakeBackOwnership', 'hiddenOwner', 'selfDestruct')
        evidence = {key: scan.get(key) if isinstance(scan.get(key), bool) else None for key in fields}
        triggers = [key for key, value in evidence.items() if value is True]
        # Legacy Onchain OS payloads do not include blacklist. Preserve their
        # narrower test scope explicitly, rather than manufacturing false.
        required = ('mintable', 'pausable', 'blacklist') if scan['provider'] == 'GoPlus' else ('mintable', 'pausable')
        known = all(evidence[key] is not None for key in required) and scan.get('openSource') is not False
        evidence.update({key: scan.get(key) for key in ('proxy', 'openSource', 'ownerAddress', 'creatorAddress')})
        evidence.update(triggers=triggers, testedFields=list(required))
        result['permissions'] = {**_check('triggered' if triggers else 'clear' if known else 'unknown',
            'permission-detected' if triggers else 'scan-clear' if known else 'incomplete-scan', evidence), **provenance}
    elif scan.get('checkedAt'):
        for key in ('tax', 'permissions'):
            result[key] = {**_check(reason='stale-scan'), 'provider': scan.get('provider'), 'checkedAt': scan.get('checkedAt')}

    distribution = asset.get('holderDistribution') or {}
    top10 = distribution.get('top10AdjustedPercent')
    if (distribution.get('excludedKnownAddresses') is True and distribution.get('provider')
            and _number(top10) and 0 <= top10 <= 100
            and _fresh(distribution.get('checkedAt'), now, HOLDER_MAX_AGE_MS)):
        result['concentration'] = {**_check('triggered' if top10 > 50 else 'clear', 'threshold-exceeded' if top10 > 50 else 'below-threshold',
            {'top10AdjustedPercent': top10, 'thresholdPct': 50, 'exclusionsApplied': True}),
            'provider': distribution['provider'], 'checkedAt': distribution['checkedAt']}
    elif (observation.get('provider') == 'GoPlus'
          and _fresh(observation.get('checkedAt'), now, HOLDER_MAX_AGE_MS)):
        result['concentration'] = {**_check(reason='address-exclusions-unavailable', evidence=observation.get('holders') or {}),
            'provider': observation['provider'], 'checkedAt': observation['checkedAt']}

    if (observation.get('provider') == 'GoPlus'
            and _fresh(observation.get('checkedAt'), now, SCAN_MAX_AGE_MS)):
        lp = observation.get('liquidityLock') or {}
        locked, burned, unlocked = (lp.get(key) for key in ('lockedPercentMin', 'burnedPercent', 'unlockedPercentMin'))
        ends = lp.get('nextUnlockAt')
        if (lp.get('identified') is True and all(_number(value) and 0 <= value <= 100 for value in (locked, burned, unlocked))
                and locked + burned + unlocked <= 100.000001 and (ends is None or (_number(ends) and now < ends))):
            # Bounds are enough: 96% demonstrably locked passes even if the
            # remaining 4% are unknown; >5% demonstrably unlocked is notable.
            status = 'clear' if locked + burned >= 95 else 'triggered' if unlocked > 5 else 'unknown'
            reason = 'lp-lock-observed' if status == 'clear' else 'lp-unlocked-observed' if status == 'triggered' else 'lp-coverage-incomplete'
        else:
            status, reason = 'unknown', 'lp-lock-expired' if _number(ends) and now >= ends else lp.get('reason', 'lp-evidence-unavailable')
        result['liquidityLock'] = {**_check(status, reason, {**lp, 'minimumLockedPercent': 95}),
            'provider': observation['provider'], 'checkedAt': observation['checkedAt']}
    return result


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
    safety = safety_checks(asset, now)
    checks['liquidity_unlock'] = safety['liquidityLock']
    tax, permissions = safety['tax'], safety['permissions']
    triggers = tax['evidence'].get('triggers', []) + permissions['evidence'].get('triggers', [])
    contract_status = 'triggered' if triggers else 'clear' if tax['status'] == permissions['status'] == 'clear' else 'unknown'
    checks['contract_risk'] = _check(contract_status,
        'scan-trigger' if triggers else 'scan-clear' if contract_status == 'clear' else 'incomplete-scan', {
            'provider': tax.get('provider'), 'checkedAt': tax.get('checkedAt'),
            'triggers': triggers, 'buyTaxPct': tax['evidence'].get('buyTaxPct'),
            'sellTaxPct': tax['evidence'].get('sellTaxPct'),
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
    return {'version': 2, 'status': status, 'flags': flags, 'checks': checks, 'safety': safety, 'checkedAt': now}
