"""Small product reads over the worker's committed canonical snapshot.

These endpoints never ask a price provider to refresh. Theme scope is local to
the response; it must not replace a client's full-market projection cursor.
"""
import asyncio
import copy
import json
import re
import time
from collections import OrderedDict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..db import store
from ..asset_logo_metadata import asset_logos, parse_ids
from ..dashboard_projection import asset_summary, stock_summary, pick, _market_stock
from ..product_metrics import build_product_theme_metrics, number
from ..realtime_projection import (read_projection_json, read_projection_payload,
                                  run_projection_query, run_projection_read, ProjectionUnavailable)
from ..collectors.pool_volume_snapshots import read_theme_pool_history
from ..product_history import direct_pool_volumes
from ..meme_directory import (MEME_SORTS, normalize_query, select_memes, selection_valid_until,
                              meme_page, meme_members, meme_chart, pool_directory)
from ..services.stock_catalog_search import normalize_stock_ticker, stock_catalog_matches

router = APIRouter()
CHAINS = {'196', '56', '4663', '5042'}
_quote_reads = {}
_meme_query_lock = asyncio.Lock()
_meme_queries = OrderedDict()


@router.get('/asset-logos')
async def logos(response: Response, ids: str = ''):
    try:
        identities = parse_ids(ids)
    except ValueError as exc:
        raise HTTPException(400, 'invalid-logo-identities') from exc
    response.headers['Cache-Control'] = 'public, max-age=60'
    return await asset_logos(identities)


async def published():
    try:
        return await read_projection_payload('full', json_reader=read_projection_json)
    except ProjectionUnavailable as exc:
        raise HTTPException(503, str(exc) if str(exc).startswith('projection-') else 'snapshot-not-ready', headers={'Retry-After': '3'}) from exc


async def published_market():
    """Reuse the exact market DTO already consumed by the original Meme page."""
    try:
        return await read_projection_payload('market', json_reader=read_projection_json)
    except ProjectionUnavailable as exc:
        raise HTTPException(503, str(exc) if str(exc).startswith('projection-') else 'snapshot-not-ready', headers={'Retry-After': '3'}) from exc


async def query_work(function, *args):
    try:
        return await run_projection_query(function, *args)
    except ProjectionUnavailable as exc:
        raise HTTPException(503, str(exc), headers={'Retry-After': '1'}) from exc


async def selected_memes(request, revision=None):
    try:
        return await run_projection_read(_selected_memes, request, revision)
    except ProjectionUnavailable as exc:
        raise HTTPException(503, str(exc), headers={'Retry-After': '1'}) from exc


async def _selected_memes(request, revision=None):
    query = normalize_query(dict(request.query_params))
    if query['sort'] not in MEME_SORTS or len(query['q']) > 200 or any(len(value) > 256 for value in query.values()):
        raise HTTPException(400, 'invalid-meme-query')
    payload = await published_market()
    now = int(time.time()*1000)
    current_revision = (payload.get('realtime') or {}).get('revision')
    publication = getattr(payload, 'publication_stamp', None)
    epoch = publication[:4] if publication and publication[0] != 'legacy' else publication[:2] if publication else None
    requested_revision = current_revision if revision is None else revision
    key = (tuple(sorted(query.items())), requested_revision, epoch)
    async with _meme_query_lock:
        for stale_key, entry in list(_meme_queries.items()):
            if entry[0] <= now or entry[2] <= time.monotonic() or stale_key[2] != epoch:
                del _meme_queries[stale_key]
        cached = _meme_queries.get(key)
        cached_publication = getattr(cached[1]['payload'], 'publication_stamp', None) if cached else None
        same_publication = publication == cached_publication if publication else cached and cached[1]['payload'] is payload
        if cached and cached[0] > now and (revision is not None or same_publication):
            _meme_queries.move_to_end(key)
            return {**cached[1], 'evaluatedAt': now}
        if revision is not None and revision != current_revision:
            raise HTTPException(409, 'directory-revision-changed')
        selection = await query_work(select_memes, payload, query, now)
        until = await query_work(selection_valid_until, payload, now)
        _meme_queries[key] = (until, selection, time.monotonic()+2)
        _meme_queries.move_to_end(key)
        while len(_meme_queries) > 16:
            _meme_queries.popitem(last=False)
        # Two brief chart generations are sufficient for a paged response's
        # follow-up chart request; do not pin sixteen complete market revisions.
        generation = lambda entry: ((entry[1]['payload'].get('realtime') or {}).get('revision'), entry[1]['payload'].get('now'))
        current_generation = (current_revision, payload.get('now'))
        older = {generation(entry) for entry in _meme_queries.values()} - {current_generation}
        generations = {current_generation}
        if older:
            generations.add(max(older, key=lambda value: (value[0] or 0, value[1] or 0)))
        for old_key, entry in list(_meme_queries.items()):
            if generation(entry) not in generations:
                del _meme_queries[old_key]
        return selection


def page_bounds(limit, offset, maximum=100):
    if not 1 <= limit <= maximum or not 0 <= offset <= 100_000:
        raise HTTPException(400, 'invalid-directory-page')


@router.get('/memes')
async def memes(request: Request, limit: int = 20, offset: int = 0):
    page_bounds(limit, offset, 50)
    selection = await selected_memes(request)
    return await query_work(meme_page, selection, limit, offset)


@router.get('/memes/group')
async def meme_group(request: Request, group: str, focus: str = '', limit: int = 50,
                     offset: int = 0, revision: int | None = None):
    page_bounds(limit, offset)
    if len(group) > 256 or len(focus) > 256:
        raise HTTPException(400, 'invalid-meme-group')
    selection = await selected_memes(request, revision)
    return await query_work(meme_members, selection, group, limit, offset, focus)


@router.get('/memes/chart')
async def memes_chart(request: Request, revision: int | None = None):
    selection = await selected_memes(request, revision)
    return await query_work(meme_chart, selection)


@router.get('/memes/pools')
async def memes_pools(request: Request, limit: int = 15, offset: int = 0):
    page_bounds(limit, offset, 50)
    raw = dict(request.query_params)
    query = normalize_query(raw)
    if len(query['q']) > 200:
        raise HTTPException(400, 'invalid-pool-query')
    payload = await published_market()
    now = int(time.time()*1000)
    from functools import partial
    return await query_work(partial(pool_directory, payload, chain=query['chain'], q=query['q'],
                                  sort=raw.get('sort', 'liquidity'), qualified=raw.get('qualified') == '1',
                                  limit=limit, offset=offset, now=now))


def chain_scope(chain):
    if chain != 'all' and chain not in CHAINS:
        raise HTTPException(400, 'unsupported-chain')
    return chain


def stock_ticker(row):
    return str((row.get('stockIdentity') or {}).get('code') or row.get('stockCode') or '').upper()


def relation_ticker(row):
    return str((row.get('stockIdentity') or {}).get('ticker') or row.get('ticker') or '').upper()


def theme_rows(payload, ticker, chain='all'):
    ticker = str(ticker).upper()
    if not re.fullmatch(r'[A-Z0-9.^_-]{1,24}', ticker):
        raise HTTPException(400, 'invalid-ticker')
    chain_scope(chain)
    unified = payload.get('unified') or {}
    selected = lambda row: chain == 'all' or str(row.get('chainId')) == chain
    stocks = [row for row in unified.get('stockTokens', []) if selected(row) and stock_ticker(row) == ticker]
    relations = [row for row in unified.get('relations', []) if selected(row) and relation_ticker(row) == ticker]
    identities = {(str(row.get('chainId')), str(row.get('token') or '').lower()) for row in relations}
    assets = [row for row in unified.get('assets', []) if selected(row) and
              ((str(row.get('chainId')), str(row.get('token') or '').lower()) in identities or
               str((row.get('match') or {}).get('ticker') or '').upper() == ticker)]
    return stocks, assets, relations


def market_status(stocks, now):
    # Exchange token trading and its underlying equity's session differ.
    # Only expose a source's explicit equity session, never infer holidays.
    latest = max(stocks, key=lambda r: r.get('referenceAt') or 0, default={})
    session = str(latest.get('marketSession') or 'unknown').lower()
    at = latest.get('marketSessionAt') or latest.get('referenceAt')
    current = number(at, 1) is not None and 0 <= now-at <= 30*60_000
    aliases = {'regular': 'open', 'trading': 'open', 'closed': 'closed', 'halted': 'halted',
               'pre-market': 'premarket', 'post-market': 'afterhours', 'open': 'open'}
    status = aliases.get(session, 'unknown') if current else 'unknown'
    return {'status': status, 'asOf': at, 'nextOpenAt': latest.get('nextOpenAt') if current else None,
            'source': latest.get('marketSessionSource') or latest.get('referenceProvider'), 'scope': 'underlying-equity',
            'reason': None if status != 'unknown' else 'session-not-observed'}


@router.get('/home')
async def home(request: Request):
    from .dashboard import get_dashboard
    return await get_dashboard(request, view='overview')


def directory_stock(row):
    out = pick(row, {'chainId', 'tokenContractAddress', 'tokenSymbol', 'tokenName', 'stockCode',
                     'price', 'priceCurrency', 'change24h', 'quoteAt', 'provider', 'priceScope', 'quoteStatus',
                     'venue', 'quoteType', 'marketId', 'poolId', 'pool', 'quoteToken', 'priceProvenance'})
    out['fieldTimes'] = pick(row.get('fieldTimes') or {}, {'price', 'change24h'})
    out['fieldSources'] = pick(row.get('fieldSources') or {}, {'price', 'change24h'})
    out['fieldScopes'] = pick(row.get('fieldScopes') or {}, {'price', 'change24h'})
    out['fieldTimeKinds'] = pick(row.get('fieldTimeKinds') or {}, {'price', 'change24h'})
    out['stockIdentity'] = pick(row.get('stockIdentity') or {}, {'id', 'code', 'market', 'marketCode', 'currency', 'nameZh', 'nameEn'})
    return out


def directory_equity(versions):
    """Independent equity observations, never borrowed from token quotes."""
    rows = [r for r in versions if number(r.get('stockPrice'), 1e-12) is not None
            and number(r.get('referenceAt'), 1) is not None
            and r.get('referenceCurrency') and r.get('referenceProvider')
            and r.get('referenceCurrency') == (r.get('stockIdentity') or {}).get('currency')
            and r.get('referenceScope') == 'equity-exchange'
            and r.get('referenceIdentityVerified') is True]
    if not rows:
        return None
    row = max(rows, key=lambda r: r['referenceAt'])
    return pick(row, {'stockPrice', 'referenceAt', 'referenceObservedAt', 'referenceProvider',
                      'referenceCurrency', 'referenceChange24h', 'referenceStatus', 'referenceRealtime',
                      'referenceDelayMs', 'referenceDelayStatus', 'referenceSourceUrl', 'marketSession',
                      'marketSessionAt', 'marketSessionSource'})


def directory_theme(row, versions):
    preferred = next((r for r in versions if r.get('tokenContractAddress') == row.get('stockToken')
                     and str(r.get('chainId')) == str(row.get('stockTokenChain'))), versions[0] if versions else None)
    out = pick(row, {'ticker', 'name', 'stockCode', 'stockToken', 'stockTokenChain',
                     'volumeRatio7d', 'volumeRatio7dEvidence', 'sparklineCoverage'})
    points = row.get('sparkline') or []
    # Retain actual endpoints and evenly selected observations, never invent values.
    out['sparkline'] = points if len(points) <= 64 else [points[round(i*(len(points)-1)/63)] for i in range(64)]
    out['theme'] = copy.deepcopy(row.get('theme') or {'pairedCount': 0, 'nameCount': 0,
                                'volume': {'value': None, 'known': 0, 'total': 0}})
    out['stock'] = directory_stock(preferred) if preferred else None
    out['equity'] = directory_equity(versions)
    return out


def theme_asset(row):
    """Keep the displayed quote and its observation; evidence is detail-only."""
    out = pick(row, {'chainId', 'token', 'kind', 'symbol', 'name', 'assetCategory', 'relationLevel',
                     'match', 'volume24h', 'volumeCurrency', 'volumeScope', 'change1h', 'priceCurrency',
                     'price', 'provider', 'quoteAt', 'quoteStatus', 'quoteReason', 'riskFlags', 'riskStatus'})
    out['fieldTimes'] = pick(row.get('fieldTimes') or {}, {'price', 'volume24h', 'change1h'})
    out['fieldSources'] = pick(row.get('fieldSources') or {}, {'price', 'volume24h', 'change1h'})
    out['fieldScopes'] = pick(row.get('fieldScopes') or {}, {'price', 'volume24h'})
    if row.get('match'):
        out['match'] = pick(row['match'], {'ticker', 'level', 'evidenceStatus'})
    change = ((row.get('productMetrics') or {}).get('changes') or {}).get('h1') or {}
    if change.get('value') is not None:
        out['change1h'] = change['value']
        out['fieldTimes']['change1h'] = change.get('at')
        out['fieldSources']['change1h'] = change.get('source')
    assessment = row.get('riskAssessment') or {}
    out['riskAssessment'] = {'safety': {name: pick(check, {'status', 'severity', 'checkedAt', 'provider'})
        for name, check in (assessment.get('safety') or {}).items()}, 'checks': {}}
    for flag in row.get('riskFlags') or []:
        check = (assessment.get('checks') or {}).get(flag) or {}
        out['riskAssessment']['checks'][flag] = {'evidence': pick(check.get('evidence') or {}, {'volumeLiquidityRatio'})}
    return out


def theme_relation(row):
    out = pick(row, {'id', 'chainId', 'token', 'stock', 'ticker', 'pool', 'protocol', 'dex',
                     'status', 'level', 'evidenceStatus', 'liquidityUsd', 'liquidityAt', 'checkedAt',
                     'poolCreatedAt', 'creationConfirmed', 'confirmationStatus', 'creationStatus', 'discoveredAt'})
    if row.get('poolMarket'):
        out['poolMarket'] = pick(row['poolMarket'], {'volume24h', 'provider', 'updatedAt', 'scope', 'currency', 'volumeCurrency'})
    return out


@router.get('/stocks')
async def stocks(chain: str = 'all', q: str = '', sort: str = 'volume24h', catalog: str = 'paired',
                 limit: int = 20, offset: int = 0):
    chain_scope(chain)
    if len(q) > 80 or sort not in ('volume24h', 'related', 'ticker', 'change24h') or catalog not in ('paired', 'all') or not 1 <= limit <= 100 or not 0 <= offset <= 100_000:
        raise HTTPException(400, 'invalid-directory-query')
    payload = await published()
    return await query_work(_stock_directory, payload, chain, q, sort, catalog, limit, offset)


def _stock_directory(payload, chain, q, sort, catalog, limit, offset):
    source = payload.get('unified') or {}
    selected = lambda row: chain == 'all' or str(row.get('chainId')) == chain
    by_ticker = {}
    for token in source.get('stockTokens', []):
        if not selected(token):
            continue
        by_ticker.setdefault(stock_ticker(token), []).append(token)
    themes = (source.get('stockThemesByChain') or {}).get(chain, []) if chain != 'all' else source.get('stockThemes', [])
    rows = []
    known_tickers = {normalize_stock_ticker(row.get('ticker')) for row in themes}
    for row in themes:
        versions = by_ticker.get(row.get('ticker'), [])
        if not stock_catalog_matches(row, versions, q, known_tickers):
            continue
        if catalog == 'paired' and not (row.get('theme') or {}).get('pairedCount'):
            continue
        rows.append((row, versions))
    def volume(row):
        return number(((row.get('theme') or {}).get('volume') or {}).get('value'), 0)
    def change(row, versions):
        stock = directory_theme(row, versions)['stock'] or {}
        at = (stock.get('fieldTimes') or {}).get('change24h')
        return number(stock.get('change24h')) if number(at, 1) is not None and 0 <= payload['now']-at <= 900_000 else None
    if sort == 'ticker':
        rows.sort(key=lambda item: item[0]['ticker'])
    elif sort == 'related':
        def pool_order(item):
            theme = item[0].get('theme') or {}
            coverage = theme.get('poolCoverage') or {}
            recorded = coverage.get('recorded', 0)
            official = max(0, recorded-coverage.get('identityPending', 0)-coverage.get('other', 0))
            return (-theme.get('pairedCount', 0), -bool(official), -official, -recorded, item[0]['ticker'])
        rows.sort(key=pool_order)
    elif sort == 'change24h':
        rows.sort(key=lambda item: (change(*item) is None, -(change(*item) or 0), item[0]['ticker']))
    else:
        rows.sort(key=lambda item: (volume(item[0]) is None, -(volume(item[0]) or 0), item[0]['ticker']))
    ranked = sorted((item for item in rows if (item[0].get('theme') or {}).get('pairedCount') and volume(item[0]) is not None),
                    key=lambda item: (-(volume(item[0]) or 0), item[0]['ticker']))
    sectors = []
    for sector in source.get('sectors', []):
        if not selected(sector):
            continue
        small = pick(sector, {'sector', 'chainId', 'basketVersion', 'value', 'dataStatus', 'baseAt', 'members', 'lastAt', 'scopeLabel', 'at'})
        small['quoteCoverage'] = pick(sector.get('quoteCoverage') or {}, {'fresh', 'total'})
        small['components'] = [pick(part, {'token', 'symbol', 'weight'}) for part in sector.get('components', [])]
        sectors.append(small)
    # Only this page's cards carry quotes. Neither all deployments nor duplicate
    # quote/evidence trees are transferred alongside the paged directory.
    return {'now': payload.get('now'), 'realtime': dict(payload.get('realtime') or {}),
        'directory': {'total': len(rows), 'totalThemes': len(themes),
            'limit': limit, 'offset': offset,
            'pairedThemeCount': sum(bool((row.get('theme') or {}).get('pairedCount')) for row, _ in rows),
            'maxKnownVolume': max((volume(row) or 0 for row, _ in rows), default=0),
            'topThemes': [directory_theme(*item) for item in ranked[:8]]},
        'unified': {'snapshotScope': 'stocks', 'stockTokens': [],
            'stockThemes': [directory_theme(*item) for item in rows[offset:offset+limit]],
            'sectors': sectors, 'assets': [], 'relations': []}}


@router.get('/stocks/{ticker}')
async def theme(ticker: str, chain: str = 'all'):
    payload = await published()
    stocks, assets, relations = theme_rows(payload, ticker, chain)
    now = int(time.time()*1000)
    history = await read_theme_pool_history(ticker, relations, now)
    metrics = build_product_theme_metrics(ticker, assets, relations, now, history['snapshots'])
    return {'now': now, 'snapshotAt': payload.get('now'), 'unified': {
        'snapshotScope': 'theme', 'stockTokens': [_market_stock(r) for r in stocks],
        'assets': [theme_asset(r) for r in assets], 'relations': [theme_relation(r) for r in relations],
        'stockThemes': [r for r in ((payload.get('unified') or {}).get('stockThemes', []) if chain == 'all' else ((payload.get('unified') or {}).get('stockThemesByChain') or {}).get(chain, [])) if r.get('ticker') == ticker.upper()]},
        'themeMetrics': metrics, 'marketStatus': market_status(stocks, now),
        'tradingMarkets': stock_trading_markets(relations)}


def stock_trading_markets(relations):
    """Observed stock sides are candidates, never a claim of live coverage."""
    from ..stock_identity import token_identity
    rows = {}
    for r in relations:
        chain = str(r.get('chainId'))
        token = str(r.get('stockSide') or r.get('stock') or '').lower()
        pool = str(r.get('pool') or '').lower()
        identity = token_identity(chain, token)
        native_identity = token_identity(chain, r.get('stock'))
        if (r.get('status') != 'verified' or not identity.get('eligibleForPair')
                or not native_identity.get('eligibleForPair') or identity.get('underlyingId') != native_identity.get('underlyingId')
                or token not in {str(r.get(k) or '').lower() for k in ('token0', 'token1')}
                or r.get('confirmationStatus') == 'orphaned' or r.get('verificationStatus') == 'reorged'):
            continue
        key = (chain, token)
        item = {'chainId': chain, 'token': token, 'poolId': pool,
                'nativeToken': r.get('stock'), 'tokenKind': identity.get('tokenKind'),
                'symbol': ('w' if identity.get('tokenKind') == 'wrapper-current' else '')+str(identity.get('tokenSymbol') or token[:8]),
                'liquidityUsd': r.get('liquidityUsd'), 'liquidityAt': r.get('liquidityAt')}
        previous = rows.get(key)
        if previous is None or (number(item['liquidityUsd'], 0) or 0) > (number(previous['liquidityUsd'], 0) or 0):
            rows[key] = item
    return sorted(rows.values(), key=lambda r: (-(number(r.get('liquidityUsd'), 0) or 0), r['chainId'], r['token']))[:8]


@router.get('/stocks/{ticker}/equity-chart')
async def equity_chart(ticker: str, chain: str = 'all', range: str = '6m'):
    if range not in ('1m', '3m', '6m'):
        raise HTTPException(400, 'unsupported-equity-window')
    payload = await published()
    stocks, _, _ = theme_rows(payload, ticker, chain)
    stock = max(stocks, key=lambda r: r.get('referenceAt') or 0, default={})
    identity = stock.get('stockIdentity') or {}
    from ..stock_quotes import _read_snapshot, EQUITY_FILE
    history = (_read_snapshot(EQUITY_FILE).get('history') or {}).get(identity.get('id')) or {}
    if (history.get('id') != identity.get('id') or history.get('currency') != identity.get('currency')
            or history.get('symbol') != str(identity.get('code') or '').zfill(4)+'.HK'):
        history = {}
    now = int(time.time()*1000)
    since = now-{'1m': 31, '3m': 93, '6m': 186}[range]*86_400_000
    rows = [r for r in history.get('rows', []) if since <= (r.get('t') or 0) <= now]
    return {**{k: history.get(k) for k in ('id', 'symbol', 'currency', 'provider', 'bar', 'adjustment', 'observedAt', 'sourceUrl')},
            'rows': rows, 'range': range, 'realtime': False, 'now': now,
            'status': 'available' if rows else 'unavailable',
            'reason': None if rows else 'equity-history-not-covered', 'scope': 'underlying-equity'}


def eligible_price_samples(samples, stock, since, now):
    result = {}
    for row in samples:
        evidence = row.get('provenance') or {}
        at, value = row.get('t'), number(row.get('price'), 0)
        source_at = evidence.get('marketAt') if evidence.get('timeKind') == 'market' else evidence.get('observedAt', evidence.get('receivedAt'))
        venue = evidence.get('venue') or evidence.get('scope')
        venue = 'exchange' if venue == 'binance' else venue
        if (number(at, since) is None or at > now or value is None or not evidence.get('provider') or
                evidence.get('currency') != 'USD' or venue != stock.get('priceScope', 'dex') or
                evidence.get('timeKind') not in ('market', 'received', 'observed') or
                number(source_at, at) is None or source_at > now or source_at >= at+300_000):
            continue
        result[at] = {'t': at, 'value': value, 'source': evidence['provider']}
    return [result[t] for t in sorted(result)]


@router.get('/stocks/{ticker}/chart')
async def theme_chart(ticker: str, chain: str = 'all', tf: str = '1h', range: str = '24h',
                      stock: str | None = None, stockChain: str | None = None):
    if tf not in ('1h', '5m') or range not in ('24h', '7d'):
        raise HTTPException(400, 'unsupported-chart-window')
    payload = await published()
    stocks, assets, relations = theme_rows(payload, ticker, chain)
    chosen = next((r for r in stocks if str(r.get('tokenContractAddress')).lower() == str(stock).lower()
                   and str(r.get('chainId')) == str(stockChain)), None) if stock else max(stocks, key=lambda r: r.get('quoteAt') or 0, default=None)
    if stock and chosen is None:
        raise HTTPException(400, 'stock-not-in-theme')
    now = int(time.time()*1000)
    window = 86_400_000 if range == '24h' else 7*86_400_000
    since = now-window
    samples = await (await store(str(chosen['chainId']))).samples(chosen['tokenContractAddress'], 2100) if chosen else []
    prices = eligible_price_samples(samples, chosen or {}, since, now)
    # Display direct pool observations only. Rolling h24 snapshots do not
    # become hourly bars; until covered USD swaps exist the columns stay blank.
    history = await read_theme_pool_history(ticker, relations, now, hours=window//3_600_000)
    width = 3_600_000 if tf == '1h' else 300_000
    start = int(since//width)*width
    volumes = await direct_pool_volumes(relations, start, now//width*width, width)
    known_volume_hours = sum(row['volumeUsd'] is not None for row in volumes)
    events = []
    seen = set()
    for row in relations:
        created = row.get('poolCreatedAt')
        confirmed = row.get('creationConfirmed') is True or row.get('confirmationStatus') == 'confirmed' or row.get('creationStatus') == 'confirmed'
        identity = (str(row.get('chainId')), row.get('pool'))
        if confirmed and number(created, since) is not None and created <= now and row.get('pool') and identity not in seen:
            seen.add(identity)
            asset = next((a for a in assets if str(a.get('chainId')) == identity[0] and a.get('token') == row.get('token')), {})
            events.append({'t': created, 'chainId': identity[0], 'pool': row['pool'], 'ticker': ticker.upper(),
                           'symbol': asset.get('symbol'), 'dex': row.get('dex') or row.get('protocol'), 'source': 'confirmed-creation-event'})
    return {'ticker': ticker.upper(), 'prices': prices, 'volumes': volumes, 'events': sorted(events, key=lambda r: r['t']),
        'at': now, 'from': since, 'to': now, 'currency': 'USD', 'timeZone': 'UTC',
        'stock': {'chainId': chosen.get('chainId'), 'token': chosen.get('tokenContractAddress')} if chosen else None,
        'coverage': {'prices': {'known': len(prices), 'source': prices[-1]['source'] if prices else None},
                     'volume': {'status': 'current' if known_volume_hours == len(volumes) else 'partial' if known_volume_hours else 'unavailable',
                                'known': known_volume_hours, 'total': len(volumes),
                                'reason': None if known_volume_hours else 'covered-hourly-usd-swaps-unavailable',
                                'scope': 'direct-stock-pair-pools'}, 'rollingVolume': history['coverage']}}


@router.get('/search')
async def search(q: str, limit: int = 20, chain: str = 'all'):
    chain_scope(chain)
    if not 1 <= limit <= 50 or not 1 <= len(q.strip()) <= 80:
        raise HTTPException(400, 'invalid-search')
    payload = await published()
    source = payload.get('unified') or {}
    term = q.strip().lower()
    items = []
    selected = {'stockTokens': [], 'assets': []}
    for kind, rows in (('stock', source.get('stockTokens', [])), ('asset', source.get('assets', []))):
        for row in rows:
            if chain != 'all' and str(row.get('chainId')) != chain:
                continue
            address = row.get('tokenContractAddress') if kind == 'stock' else row.get('token')
            searchable = [row.get(field) for field in ('token', 'tokenContractAddress', 'symbol', 'name', 'tokenSymbol', 'tokenName', 'stockCode')]
            searchable += [(row.get(identity) or {}).get(field) for identity in ('stockIdentity', 'issuerIdentity') for field in ('nameZh', 'nameEn')]
            if not any(term in str(value or '').lower() for value in searchable):
                continue
            items.append({'kind': kind, 'chainId': row.get('chainId'), 'address': address,
                          'name': row.get('name') or row.get('tokenName'), 'symbol': row.get('symbol') or row.get('tokenSymbol'),
                          'path': f"/asset/{row.get('chainId')}/{address}"})
            selected['stockTokens' if kind == 'stock' else 'assets'].append(row)
    items.sort(key=lambda row: (str(row['address']).lower() != term, str(row.get('symbol') or '').lower() != term))
    visible = {(r['kind'], str(r['chainId']), r['address']) for r in items[:limit]}
    return {'items': items[:limit], 'total': len(items), 'snapshotAt': payload.get('now'), 'unified': {
        'stockTokens': [stock_summary(r) for r in selected['stockTokens'] if ('stock', str(r.get('chainId')), r.get('tokenContractAddress')) in visible],
        'assets': [asset_summary(r) for r in selected['assets'] if ('asset', str(r.get('chainId')), r.get('token')) in visible]}}


@router.get('/assets/{chain}/{address}')
async def asset(chain: str, address: str, section: str = 'summary', limit: int = 50, offset: int = 0):
    from .token import get_token
    return await get_token(chain, address, section, limit, offset)


@router.get('/assets/{chain}/{address}/exit-impact')
async def exit_impact(request: Request, chain: str, address: str, pool: str, sellUsd: int = 1000):
    from .. import product_quotes
    chain_scope(chain)
    if chain == 'all' or not re.fullmatch(r'0x[0-9a-fA-F]{40}', address) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', pool) or sellUsd not in (100, 1000, 10000):
        raise HTTPException(400, 'invalid-quote-request')
    client = request.client.host if request.client else 'unknown'
    clock = time.monotonic()
    if clock-_quote_reads.get(client, -10) < 3:
        raise HTTPException(429, 'quote-rate-limit', headers={'Retry-After': '3'})
    if len(_quote_reads) > 2000:
        for old in [key for key, at in _quote_reads.items() if clock-at > 60]:
            _quote_reads.pop(old, None)
    _quote_reads[client] = clock
    payload = await published()
    source = payload.get('unified') or {}
    return await product_quotes.quote(chain, address.lower(), pool.lower(), sellUsd,
        source.get('assets', [])+[dict(r, token=r.get('tokenContractAddress')) for r in source.get('stockTokens', [])], source.get('relations', []))


# Saved identities have bounded reads separate from the full market directory.
from .watch import router as watch_router
router.include_router(watch_router)
