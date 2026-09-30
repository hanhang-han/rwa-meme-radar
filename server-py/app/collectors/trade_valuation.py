"""Value a native pool swap only with dated, independent USD evidence.

The quote token's symbol is never evidence of a dollar peg. A replayed swap
must use a quote observed at or before its block timestamp; a current asset
fact can therefore be too new even while an older sample remains usable.
"""
import json
import math
from decimal import Decimal, InvalidOperation

from .chainlink_anchor import (CHAIN_ID as BNB_CHAIN_ID, FEEDS as BNB_USD_FEEDS,
                               PROVIDER as ORACLE_PROVIDER, dated_quote_rate)


MAX_QUOTE_AGE_MS = 15 * 60_000
SAMPLE_BUCKET_MS = 5 * 60_000


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _millis(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return int(number) if math.isfinite(number) and number > 0 and number.is_integer() else None


def _candidate(price, evidence, trade_at, *, origin, bucket=None, fact_at=None):
    if not isinstance(evidence, dict):
        return None
    quote_at = _millis(evidence.get('marketAt'))
    provider = evidence.get('provider')
    if (evidence.get('timeKind') != 'market' or evidence.get('currency') != 'USD'
            or not isinstance(provider, str) or not provider.strip()
            or provider.lower() == 'chain rpc' or evidence.get('independent') is not True
            or evidence.get('scope') not in ('token', 'exchange')
            or quote_at is None or not 0 <= trade_at - quote_at <= MAX_QUOTE_AGE_MS):
        return None
    # sample_evidence currently normalizes an absent priceCurrency to USD.
    # Its currency alone therefore cannot prove an arbitrary provider's unit.
    # In this codebase only OKX price-info samples have a documented
    # token-USD ingestion contract; other sample sources need their own proof.
    if origin == 'asset-sample' and provider != 'OKX':
        return None
    # Bind the field value to precisely the dated observation we are using.
    if (fact_at is not None and _millis(fact_at) != quote_at) or (
            bucket is not None and quote_at // SAMPLE_BUCKET_MS * SAMPLE_BUCKET_MS != bucket):
        return None
    rate = _positive(price)
    return (quote_at, rate, provider, origin) if rate is not None else None


def _usd_fields(scoped, quote_token, quantity, quote_at, rate, provider, origin, *, feed=None):
    try:
        usd = float(quantity * rate)
        rate_float = float(rate)
    except (OverflowError, ValueError):
        return {}
    if not math.isfinite(usd) or usd <= 0 or not math.isfinite(rate_float):
        return {}
    provenance = {
        'method': 'native-quote-quantity-times-dated-usd-price',
        'chainId': str(scoped.scope), 'quoteToken': quote_token,
        'quotePriceUsd': rate_float, 'quoteAt': quote_at,
        'timeKind': 'market', 'provider': provider, 'evidence': origin,
    }
    if feed is not None:
        provenance['feedAddress'] = feed
    return {'volume': usd, 'volumeCurrency': 'USD', 'quoteAt': quote_at,
            'volumeProvenance': provenance}


async def dated_usd_volume(db, scoped, market, trade):
    """Return USD fields for one pool trade, or an empty patch if unknown.

    Reads are index seeks on the chain-scoped quote token; no RPC or network
    request is made in the live log path. A 15-minute window spans at most
    four five-minute sample buckets, so the fallback read is bounded.
    """
    quote_token = market.quote_token.lower() if market.quote_token else None
    trade_at = _millis(trade.get('t'))
    quantity = _positive(trade.get('quoteQuantity'))
    if not quote_token or trade_at is None or quantity is None or str(market.chain_id) != str(scoped.scope):
        return {}
    # Three reviewed BNB Chain quote contracts have independent USD feeds.
    # Never fall back to a token headline or inferred stablecoin peg when a
    # feed is unavailable, stale, or mismatched.
    if str(scoped.scope) == BNB_CHAIN_ID and quote_token in BNB_USD_FEEDS:
        dated = await dated_quote_rate(db, scoped, quote_token, trade_at)
        if dated is None:
            return {}
        quote_at, rate = dated
        return _usd_fields(scoped, quote_token, quantity, quote_at, rate,
                           ORACLE_PROVIDER, 'oracle-feed',
                           feed=BNB_USD_FEEDS[quote_token]['feed'])
    asset_key = scoped.key(quote_token)
    fact_rows = await db.execute_fetchall(
        'SELECT body FROM facts WHERE kind=? AND id=?', (scoped.key('asset'), quote_token))
    if not fact_rows:
        return {}
    asset = json.loads(fact_rows[0][0])
    if (str(asset.get('token') or '').lower() != quote_token
            or str(asset.get('chainId') or asset.get('chain') or scoped.scope) != str(scoped.scope)):
        return {}
    observation = (asset.get('fieldObservations') or {}).get('price')
    candidate = _candidate(
        asset.get('price'), observation, trade_at, origin='asset-fact',
        fact_at=(asset.get('fieldTimes') or {}).get('price'))
    candidates = [candidate] if candidate else []
    # Historical sample currency was not preserved independently of the
    # current asset fact. Require its same-provider USD unit as a second
    # check before consulting that archive.
    sample_unit_verified = (isinstance(observation, dict)
                            and observation.get('provider') == 'OKX'
                            and observation.get('currency') == 'USD'
                            and observation.get('independent') is True)
    if not candidates and sample_unit_verified:
        rows = await db.execute_fetchall(
            'SELECT s.t,s.price,e.body FROM samples s JOIN sample_evidence e '
            'ON e.asset=s.asset AND e.t=s.t WHERE s.asset=? AND s.t BETWEEN ? AND ? '
            'ORDER BY s.t DESC LIMIT 5',
            (asset_key, (trade_at - MAX_QUOTE_AGE_MS) // SAMPLE_BUCKET_MS * SAMPLE_BUCKET_MS,
             trade_at // SAMPLE_BUCKET_MS * SAMPLE_BUCKET_MS))
        for bucket, price, body in rows:
            try:
                evidence = json.loads(body)
            except (TypeError, ValueError):
                continue
            candidate = _candidate(price, evidence, trade_at, origin='asset-sample', bucket=bucket)
            if candidate:
                candidates.append(candidate)
    if not candidates:
        return {}
    quote_at, rate, provider, origin = max(candidates, key=lambda value: value[0])
    return _usd_fields(scoped, quote_token, quantity, quote_at, rate, provider, origin)
