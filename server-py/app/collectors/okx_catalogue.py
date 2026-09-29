"""Incrementally copy Node's atomic OKX directory snapshots into Python facts.

The directory is identity evidence, while its ``quoteAt`` is a producer
observation clock, not an exchange trade timestamp. Rows retained after a
short OKX page may have older (or no) quote time. Never turn the worker's read
time into a market observation.
"""

import asyncio
import json
import math
import re
import time
from pathlib import Path


SNAPSHOTS = {'196': 'okx.json', '56': 'okx-56.json', '4663': 'okx-4663.json'}
ADDRESS = re.compile(r'0x[0-9a-f]{40}\Z', re.I)
IDENTITY_FIELDS = ('tokenSymbol', 'tokenName', 'stockCode', 'issuer', 'logoUrl',
                   'reportedStockCode', 'stockCodeSource', 'stockCodeSourceUrl')
QUOTES = {'price': 'price', 'stockPrice': 'stockPrice', 'volume24h': 'volume24h',
          'marketCap': 'marketCap', 'change24h': 'change24h'}
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_TOKENS = 10_000
# Keep the catalogue write turn well below the five-minute quote cadence.
# The normal path is read/compare-only; a large recovery converges over later
# turns rather than monopolising SQLite while baseline prices are due.
PER_CHAIN_LIMIT = 100


class InvalidCatalogue(ValueError):
    pass


def _time(value, now):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0 or value > now + 60_000
            or int(value) != value):
        raise InvalidCatalogue('invalid catalogue time')
    return int(value)


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidCatalogue('invalid catalogue number')
    return value


def _invalid_json_constant(_value):
    raise InvalidCatalogue('non-finite catalogue JSON')


def read_catalogue(path: Path, chain: str, now: int) -> list[dict]:
    """Validate the entire snapshot before allowing any writes from it."""
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise InvalidCatalogue('catalogue snapshot too large')
    try:
        payload = json.loads(path.read_text(encoding='utf-8'),
                             parse_constant=_invalid_json_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidCatalogue('invalid catalogue JSON') from exc
    if not isinstance(payload, dict):
        raise InvalidCatalogue('invalid catalogue object')
    # A DB-restored directory can legitimately have no completed refresh yet.
    # Its rows still carry identities, and any row quote has its own clock.
    if payload.get('updatedAt') is not None:
        _time(payload['updatedAt'], now)
    tokens = payload.get('tokens')
    if not isinstance(tokens, list) or len(tokens) > MAX_TOKENS:
        raise InvalidCatalogue('invalid catalogue token list')
    result, seen = [], set()
    for token in tokens:
        if not isinstance(token, dict):
            raise InvalidCatalogue('invalid catalogue token')
        declared = token.get('chainIndex', token.get('chainId'))
        if str(declared) != chain or ('chainId' in token and str(token['chainId']) != chain):
            raise InvalidCatalogue('catalogue chain mismatch')
        address = token.get('tokenContractAddress')
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise InvalidCatalogue('invalid catalogue address')
        address = address.lower()
        if address in seen:
            raise InvalidCatalogue('duplicate catalogue address')
        seen.add(address)
        quote_at = token.get('quoteAt')
        if quote_at is not None:
            quote_at = _time(quote_at, now)
        row = {'chainIndex': chain, 'tokenContractAddress': address, 'quoteAt': quote_at}
        for field in IDENTITY_FIELDS:
            if field in token:
                value = token[field]
                if value is not None and not isinstance(value, str):
                    raise InvalidCatalogue('invalid catalogue identity')
                row[field] = value
        for field in QUOTES:
            if field in token:
                row[field] = _number(token[field])
        if 'tokenToAssetRatio' in token:
            row['tokenToAssetRatio'] = _number(token['tokenToAssetRatio'])
        result.append(row)
    return result


def _clock(row, field):
    value = (row.get('fieldTimes') or {}).get(field)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return value
    # Old facts predate per-field clocks. Their quoteAt still bounds the age
    # of any quote present; without either clock, a timed new quote may win.
    if row.get(field) is None:
        return None
    value = row.get('quoteAt')
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _observation(at):
    return {'provider': 'OKX', 'scope': 'token', 'venue': 'dex', 'currency': 'USD',
            'timeKind': 'received', 'marketAt': None, 'receivedAt': at}


def _stock_patch(old: dict, row: dict) -> dict:
    patch = {}
    address = row['tokenContractAddress']
    if not old:
        patch.update(chainIndex=row['chainIndex'], tokenContractAddress=address)
    prior_quote = _clock(old, 'price') or 0
    source_at = row['quoteAt'] or 0
    for field in IDENTITY_FIELDS:
        value = row.get(field)
        if value and (not old.get(field) or source_at > prior_quote):
            if value != old.get(field):
                patch[field] = value
    times = dict(old.get('fieldTimes') or {})
    sources = dict(old.get('fieldSources') or {})
    observations = dict(old.get('fieldObservations') or {})
    scopes = dict(old.get('fieldScopes') or {})
    kinds = dict(old.get('fieldTimeKinds') or {})
    accepted = False
    # A null ratio is absence of evidence. Never erase issuer-verified ratio
    # metadata or turn a provider-reported ratio into a verified one.
    ratio = row.get('tokenToAssetRatio')
    ratio_at = _clock(old, 'tokenToAssetRatio')
    if ratio is not None and ratio > 0 and not old.get('ratioVerified'):
        if old.get('tokenToAssetRatio') is None or (source_at and (ratio_at is None or source_at > ratio_at)):
            if ratio != old.get('tokenToAssetRatio'):
                patch['tokenToAssetRatio'] = ratio
            if source_at and source_at > (ratio_at or 0):
                times['tokenToAssetRatio'] = source_at
                sources['tokenToAssetRatio'] = 'OKX'
                observations['tokenToAssetRatio'] = _observation(source_at)
                scopes['tokenToAssetRatio'] = 'catalogue'
                kinds['tokenToAssetRatio'] = 'received'
                accepted = True
    for field in QUOTES:
        value = row.get(field)
        if value is None or (field == 'price' and value <= 0):
            continue
        previous_at = _clock(old, field)
        if source_at and (previous_at is None or source_at > previous_at):
            patch[field] = value
            times[field] = source_at
            sources[field] = 'OKX'
            observations[field] = _observation(source_at)
            scopes[field] = 'token' if field != 'stockPrice' else 'catalogue-reference'
            kinds[field] = 'received'
            accepted = True
        elif not old and not source_at:
            # Preserve historical catalogue numbers without a freshness claim.
            patch[field] = value
    if accepted:
        patch.update(fieldTimes=times, fieldSources=sources, fieldObservations=observations,
                     fieldScopes=scopes, fieldTimeKinds=kinds)
        if any(field in patch for field in QUOTES) and source_at > (old.get('quoteAt') or 0):
            patch['quoteAt'] = source_at
        patch['updatedAt'] = max(old.get('updatedAt') or 0, source_at)
    return patch


def _asset_plan(old: dict, row: dict, now: int):
    address = row['tokenContractAddress']
    base = {}
    if not old:
        base.update(token=address, chain=row['chainIndex'], chainId=row['chainIndex'],
                    kind='stock', firstSeen=now)
    elif old.get('kind') not in ('stock', 'wrapped_stock'):
        base['kind'] = 'stock'
        if old.get('kind') == 'candidate' and old.get('discoveryEventPending'):
            base['discoveryEventPending'] = False
    for field, source in (('symbol', 'tokenSymbol'), ('name', 'tokenName'), ('logoUrl', 'logoUrl')):
        value = row.get(source)
        if value and (not old.get(field) or old.get(field) in (address, address[:8])):
            base[field] = value
    timed = {}
    source_at = row['quoteAt']
    observations = {}
    for field in ('price', 'marketCap', 'volume24h', 'change24h'):
        value = row.get(field)
        if value is None or (field == 'price' and value <= 0) or not source_at:
            continue
        previous_at = _clock(old, field)
        if previous_at is None or source_at > previous_at:
            timed[field] = (value, source_at)
            observations[field] = _observation(source_at)
    if observations:
        base['fieldSources'] = {field: 'OKX' for field in observations}
        base['fieldObservations'] = observations
        base['fieldScopes'] = {field: 'token' for field in observations}
        base['fieldTimeKinds'] = {field: 'received' for field in observations}
    if 'price' in timed:
        base.update(provider='OKX', venue='dex', priceScope='dex', priceCurrency='USD',
                    quoteType='dex', quoteAt=source_at,
                    priceProvenance={'timeKind': 'received', 'venue': 'dex',
                                     'dependencies': [], 'dependenciesComplete': True})
    price = timed.get('price', (None, None))[0]
    cap = timed.get('marketCap', (old.get('marketCap'), None))[0]
    sample = (price, cap, source_at) if price is not None else None
    return base, timed, sample


async def sync_okx_catalogues(directory='data', *, per_chain_limit=PER_CHAIN_LIMIT,
                              now_ms=None, store_factory=None):
    """Copy at most ``per_chain_limit`` changed rows per chain each run.

    Reading complete fact indexes once keeps the common duplicate path free
    of SQLite writes. Each changed fact uses its own short transaction and
    yields after a small group so chain and quote writers can progress.
    """
    from ..db import retry_busy_write, store

    now = int(now_ms if now_ms is not None else time.time() * 1000)
    get_store = store_factory or store
    limit = max(1, min(int(per_chain_limit), 1000))
    outcome = {'requested': 0, 'accepted': 0, 'updated': 0, 'failed': 0,
               'unavailable': 0, 'skipped': 0}
    for chain, filename in SNAPSHOTS.items():
        path = Path(directory) / filename
        try:
            rows = read_catalogue(path, chain, now)
        except FileNotFoundError:
            outcome['unavailable'] += 1
            continue
        except (OSError, InvalidCatalogue):
            outcome['failed'] += 1
            continue
        outcome['requested'] += 1
        scoped = await get_store(chain)
        stocks = {address.lower(): value for address, value in await scoped.all_kv('stock')}
        assets = {address.lower(): value for address, value in await scoped.all_kv('asset')}
        pending = []
        for row in rows:
            address = row['tokenContractAddress']
            stock_patch = _stock_patch(stocks.get(address) or {}, row)
            asset_base, timed, sample = _asset_plan(assets.get(address) or {}, row, now)
            if stock_patch or asset_base or timed:
                pending.append((row, stock_patch, asset_base, timed, sample))
        for row, stock_patch, asset_base, timed, sample in pending[:limit]:
            address = row['tokenContractAddress']
            if stock_patch:
                await retry_busy_write(lambda: scoped.patch_fact('stock', address, stock_patch))
            if asset_base or timed:
                await scoped.merge_asset_observation(address, asset_base, timed, sample)
            outcome['updated'] += 1
            # SQLite has one writer across connections. Give the quote lane a
            # real scheduling gap after each short catalogue transaction.
            await asyncio.sleep(.01)
        outcome['accepted'] += min(len(pending), limit)
        outcome['skipped'] += max(0, len(pending) - limit)
    if outcome['requested'] and not outcome['updated'] and not outcome['failed']:
        outcome['noChange'] = True
    return outcome
