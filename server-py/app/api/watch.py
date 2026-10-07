"""Public read-only, bounded reads for a device/account's saved identities.

Saved lists remain owned by the account API. These routes neither save a list
nor collect quotes; source timestamps and committed projection cursors survive
unchanged. Event indexes are prepared by deployment, never by an HTTP request.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..storage_runtime import connect, is_postgres_path
from .. import db
from ..dashboard_projection import asset_summary, stock_summary
from ..services.stock_catalog_search import COMPANY_ALIASES, normalize_stock_ticker

router = APIRouter(prefix='/watch', tags=['watch-reads'])
CHAINS = ('196', '56', '4663', '5042')
ADDRESS = re.compile(r'0x[0-9a-f]{40}', re.I)
TICKER = re.compile(r'[A-Z0-9.^_-]{1,24}', re.I)
CHAIN_ID = re.compile(r'[1-9][0-9]{0,9}')
MAX_KEYS = 200
MAX_EVENT_BODY = 64 * 1024
READ_SECONDS = .75
READ_OPS = 2_000_000
_event_slots = asyncio.Semaphore(2)


def key_identity(value: str):
    if value.startswith('stock:') and TICKER.fullmatch(value[6:]):
        return 'theme', normalize_stock_ticker(value[6:])
    parts = value.split(':')
    if len(parts) == 2 and CHAIN_ID.fullmatch(parts[0]) and ADDRESS.fullmatch(parts[1]):
        return 'asset', (parts[0], parts[1].lower())
    raise ValueError('invalid-watch-key')


class WatchKeys(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    keys: list[str] = Field(default_factory=list, max_length=MAX_KEYS)
    chain: Literal['all', '196', '56', '4663', '5042'] = 'all'
    limit: int = Field(default=20, ge=1, le=20)

    @field_validator('keys')
    @classmethod
    def valid_keys(cls, values):
        result = []
        for value in values:
            if len(value) > 80:
                raise ValueError('invalid-watch-key')
            key_identity(value)
            if value not in result:
                result.append(value)
        return result


class WatchSummaryBody(WatchKeys):
    offset: int = Field(default=0, ge=0, le=100_000)
    q: str = Field(default='', max_length=80)
    kind: Literal['all', 'theme', 'asset'] = 'all'
    sort: Literal['followed', 'name', 'change24h'] = 'followed'
    quoteFilter: Literal['all', 'quote', 'risk'] = 'all'


class WatchEventsBody(WatchKeys):
    cursor: str | None = Field(default=None, max_length=1024)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _in_scope(chain, requested):
    return requested == 'all' or str(chain) == requested


def _names(stock, ticker):
    identity = (stock or {}).get('stockIdentity') or {}
    aliases = COMPANY_ALIASES.get(ticker, ())
    return (identity.get('nameZh') or (aliases[0] if aliases else None),
            identity.get('nameEn') or (aliases[-1] if aliases else None))


def _query_matches(item, query):
    term = query.strip().casefold()
    if not term:
        return True
    asset = item.get('asset') or {}
    theme = item.get('theme') or {}
    if term.startswith('0x'):
        return bool(ADDRESS.fullmatch(term)) and term in {
            str(item.get('token') or '').lower(),
            str(asset.get('token') or asset.get('tokenContractAddress') or '').lower(),
            str(theme.get('stockToken') or '').lower()}
    return any(term in str(value or '').casefold() for value in (
        item.get('ticker'), item.get('nameZh'), item.get('nameEn'), item.get('symbol'),
        item['key'], asset.get('symbol'), asset.get('name'), asset.get('tokenSymbol'),
        asset.get('tokenName'), *((asset.get('stockIdentity') or {}).get(k) for k in ('nameZh', 'nameEn'))))


def _quote_missing(asset, now):
    if not asset or _number(asset.get('price')) is None or _number(asset.get('price')) <= 0:
        return True
    at = _number((asset.get('fieldTimes') or {}).get('price') or asset.get('quoteAt'))
    return (at is None or not 0 <= now-at <= 900_000 or
            asset.get('quoteStatus') in ('missing', 'no-verified-market', 'unsupported',
                                        'entitlement-required', 'identity-unverified',
                                        'quota-exhausted', 'budget-exhausted'))


def summary_rows(payload, body, evaluated_at):
    """Select saved identities before paging, without a separate full cache."""
    from .product_v2 import directory_theme, stock_ticker
    source = payload.get('unified') or {}
    identities = {key: key_identity(key) for key in body.keys}
    tickers = {identity for kind, identity in identities.values() if kind == 'theme'}
    exact = {identity for kind, identity in identities.values() if kind == 'asset'}
    versions, stocks, assets = {}, {}, {}
    for row in source.get('stockTokens', []):
        if str(row.get('chainId')) not in CHAINS or not _in_scope(row.get('chainId'), body.chain):
            continue
        identity = (str(row.get('chainId')), str(row.get('tokenContractAddress') or '').lower())
        ticker = normalize_stock_ticker(stock_ticker(row))
        if ticker in tickers:
            versions.setdefault(ticker, []).append(row)
        if identity in exact:
            stocks[identity] = row
    for row in source.get('assets', []):
        identity = (str(row.get('chainId')), str(row.get('token') or '').lower())
        if identity in exact and identity[0] in CHAINS and _in_scope(identity[0], body.chain):
            assets[identity] = row
    theme_source = (source.get('stockThemes') or []) if body.chain == 'all' else (
        source.get('stockThemesByChain') or {}).get(body.chain, [])
    themes = {normalize_stock_ticker(row.get('ticker')): row for row in theme_source
              if normalize_stock_ticker(row.get('ticker')) in tickers}
    rows = []
    for key, (kind, identity) in identities.items():
        if kind == 'theme':
            ticker, candidates = identity, versions.get(identity, [])
            original = themes.get(ticker)
            small = directory_theme(original, candidates) if original else None
            selected = small.get('stock') if small else None
            preferred = next((row for row in candidates if selected and
                              row.get('tokenContractAddress') == selected.get('tokenContractAddress') and
                              str(row.get('chainId')) == str(selected.get('chainId'))), None)
            quote = stock_summary(preferred) if preferred else None
            zh, en = _names(preferred, ticker) if original or preferred else (None, None)
            rows.append({'key': key, 'kind': 'theme', 'ticker': ticker, 'nameZh': zh, 'nameEn': en,
                         'symbol': ticker, 'available': bool(original or preferred),
                         'status': 'available' if original or preferred else 'not-indexed',
                         'asset': quote, 'theme': small, 'quoteAssets': [quote] if quote else [],
                         'poolVolumeScope': 'direct-stock-pair-pools', 'poolVolumeCurrency': 'USD'})
        else:
            chain, token = identity
            if not _in_scope(chain, body.chain):
                continue
            stock, asset = stocks.get(identity), assets.get(identity)
            quote = stock_summary(stock) if stock else asset_summary(asset) if asset else None
            ticker = normalize_stock_ticker(stock_ticker(stock)) if stock else None
            zh, en = _names(stock, ticker) if stock else (None, None)
            rows.append({'key': key, 'kind': 'stock-token' if stock else 'asset', 'chainId': chain,
                         'token': token, 'ticker': ticker, 'nameZh': zh, 'nameEn': en,
                         'symbol': (stock or asset or {}).get('tokenSymbol') or (asset or {}).get('symbol'),
                         'available': quote is not None, 'status': 'unsupported-chain' if chain not in CHAINS else 'available' if quote else 'not-indexed',
                         'asset': quote, 'theme': None, 'quoteAssets': [quote] if quote else []})
    total_scope = len(rows)
    rows = [row for row in rows if (body.kind == 'all' or (row['kind'] == 'theme') == (body.kind == 'theme'))
            and _query_matches(row, body.q) and (body.quoteFilter == 'all' or
            body.quoteFilter == 'quote' and _quote_missing(row['asset'], evaluated_at) or
            body.quoteFilter == 'risk' and bool((row['asset'] or {}).get('riskFlags')))]
    if body.sort == 'name':
        rows.sort(key=lambda row: (str(row.get('nameZh') or row.get('nameEn') or row.get('symbol') or row['key']).casefold(), row['key']))
    elif body.sort == 'change24h':
        def change(row):
            asset = row['asset'] or {}
            at = _number((asset.get('fieldTimes') or {}).get('change24h'))
            value = _number(asset.get('change24h'))
            return value if value is not None and at is not None and 0 <= evaluated_at-at <= 900_000 else None
        rows.sort(key=lambda row: (change(row) is None, -(change(row) or 0), row['key']))
    return {'now': payload.get('now'), 'snapshotAt': payload.get('now'), 'evaluatedAt': evaluated_at,
            'realtime': dict(payload.get('realtime') or {}), 'total': len(rows), 'totalInScope': total_scope,
            'directory': {'total': len(rows), 'totalInScope': total_scope, 'limit': body.limit, 'offset': body.offset},
            'items': rows[body.offset:body.offset+body.limit]}


@router.post('/summary')
async def watch_summary(body: WatchSummaryBody):
    if not body.keys:
        return {'now': None, 'snapshotAt': None, 'realtime': {}, 'total': 0, 'totalInScope': 0,
                'directory': {'total': 0, 'totalInScope': 0, 'limit': body.limit, 'offset': body.offset}, 'items': []}
    from .product_v2 import published, query_work
    payload = await published()
    return await query_work(summary_rows, payload, body, int(time.time()*1000))


# The expressions below are deliberately identical between indexes/queries.
# Guard historical invalid JSON; it must not make index creation fail.
SCOPE_SQL = "substr(asset,1,instr(asset,':')-1)"
def _json_sql(*paths):
    return "CASE WHEN json_valid(body) THEN coalesce(" + ','.join(
        f"nullif(json_extract(body,'{path}'),'')" for path in paths) + ",'') ELSE '' END"

_TICKER_RAW = "upper(" + _json_sql('$.ticker', '$.relation.ticker', '$.match.ticker') + ')'
TICKER_SQL = (f"CASE WHEN {_TICKER_RAW}<>'' AND {_TICKER_RAW} NOT GLOB '*[^0-9]*' "
              f"THEN coalesce(nullif(ltrim({_TICKER_RAW},'0'),''),'0') ELSE {_TICKER_RAW} END")
POOL_SQL = "lower(" + _json_sql('$.pool', '$.relation.pool') + ')'
# Python's canonical relation event ID is chain:chain:pool:meme:stock:kind:t.
# All three address segments have 42 characters. No name/ticker inference.
NATIVE_STOCK_SQL = (f"CASE WHEN substr(id,1,2*instr(asset,':'))={SCOPE_SQL}||':'||{SCOPE_SQL}||':' "
                    f"AND lower(substr(id,2*instr(asset,':')+1,2))='0x' "
                    f"AND lower(substr(id,2*instr(asset,':')+3,40)) NOT GLOB '*[^0-9a-f]*' "
                    f"AND lower(substr(id,2*instr(asset,':')+44,2))='0x' "
                    f"AND lower(substr(id,2*instr(asset,':')+46,40)) NOT GLOB '*[^0-9a-f]*' "
                    f"AND substr(id,2*instr(asset,':')+43,1)=':' "
                    f"AND substr(id,2*instr(asset,':')+86,1)=':' "
                    f"AND substr(id,2*instr(asset,':')+129,1)=':' "
                    f"THEN lower(substr(id,2*instr(asset,':')+87,42)) ELSE '' END")
WATCH_EVENT_INDEX_SQL = (
    'CREATE INDEX IF NOT EXISTS watch_events_asset_time ON events(asset,t DESC,id DESC)',
    f'CREATE INDEX IF NOT EXISTS watch_events_ticker_time ON events({SCOPE_SQL},({TICKER_SQL}),t DESC,id DESC)',
    f'CREATE INDEX IF NOT EXISTS watch_events_native_stock_time ON events({SCOPE_SQL},({NATIVE_STOCK_SQL}),t DESC,id DESC)',
    f'CREATE INDEX IF NOT EXISTS watch_events_pool_time ON events({SCOPE_SQL},({POOL_SQL}),asset,t DESC,id DESC)',
)


def _fingerprint(keys, chain):
    canonical = sorted({str(key_identity(key)) for key in keys})
    return hashlib.sha256(json.dumps([canonical, chain], separators=(',', ':')).encode()).hexdigest()[:24]


def _cursor_encode(row, fingerprint):
    value = {'v': 1, 't': row['t'], 'id': row['id'], 'q': fingerprint}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode().rstrip('=')


def _cursor_decode(value, fingerprint):
    if not value:
        return None
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        cursor = json.loads(raw.decode())
        if (not isinstance(cursor, dict) or cursor.get('v') != 1 or cursor.get('q') != fingerprint or
                type(cursor.get('t')) is not int or not 0 < cursor['t'] < 10**16 or
                not isinstance(cursor.get('id'), str) or not 0 < len(cursor['id']) <= 512):
            raise ValueError('invalid cursor')
        return {'t': cursor['t'], 'id': cursor['id']}
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(400, 'invalid-watch-cursor') from exc


def _native_stock(event_id, chain):
    parts = event_id.split(':')
    if (len(parts) >= 7 and parts[:2] == [chain, chain] and
            all(ADDRESS.fullmatch(address) for address in parts[2:5])):
        return parts[4].lower()
    return None


def _address(value, chain):
    if isinstance(value, dict):
        value = value.get('token') or value.get('tokenContractAddress')
    value = str(value or '').lower()
    while value.startswith(chain+':'):
        value = value[len(chain)+1:]
    return value if ADDRESS.fullmatch(value) else None


def _event_matches(item, keys, pool_sides):
    chain = item['chainId']
    relation = item.get('relation') or {}
    ticker = normalize_stock_ticker(item.get('ticker') or relation.get('ticker') or (item.get('match') or {}).get('ticker'))
    native = _native_stock(item['id'], chain)
    addresses = {_address(item.get(field), chain) for field in ('asset', 'token', 'stock', 'stockSide')}
    addresses.update(_address(relation.get(field), chain) for field in ('token', 'stock', 'stockSide'))
    addresses.add(native)
    pool = str(item.get('pool') or relation.get('pool') or '').lower()
    token = _address(item.get('asset') or relation.get('token'), chain)
    # Old event bodies omit relation.stockSide. Match only the immutable pool
    # identities when the canonical event ID also agrees with native stock.
    for side in pool_sides.get((chain, pool, token, native), ()):
        addresses.add(side)
    return [key for key in keys if ((identity := key_identity(key))[0] == 'theme' and identity[1] == ticker)
            or (identity[0] == 'asset' and identity[1][0] == chain and identity[1][1] in addresses)]


def _branches(keys, chain, pool_sides):
    chains = CHAINS if chain == 'all' else (chain,)
    tickers = sorted({identity for kind, identity in map(key_identity, keys) if kind == 'theme'})
    exact = sorted({identity for kind, identity in map(key_identity, keys) if kind == 'asset' and identity[0] in chains})
    branches = []
    if exact:
        values = [cid+':'+token for cid, token in exact]
        branches.append((f"asset IN ({','.join('?' for _ in values)})", values))
    for cid in chains:
        if tickers:
            branches.append((f"{SCOPE_SQL}=? AND ({TICKER_SQL}) IN ({','.join('?' for _ in tickers)})", [cid, *tickers]))
        tokens = [token for chain_id, token in exact if chain_id == cid]
        if tokens:
            branches.append((f"{SCOPE_SQL}=? AND ({NATIVE_STOCK_SQL}) IN ({','.join('?' for _ in tokens)})", [cid, *tokens]))
            # Newer/external durable events can include explicit stock sides.
            expressions = ['lower(' + _json_sql(path) + ')' for path in
                           ('$.stockSide', '$.relation.stockSide', '$.stock', '$.relation.stock',
                            '$.token', '$.relation.token')]
            predicates = [f"({expression}) IN ({','.join('?' for _ in tokens)})" for expression in expressions]
            branches.append((f"{SCOPE_SQL}=? AND ("+' OR '.join(predicates)+')', [cid, *(tokens*len(expressions))]))
        # A pool alone is too broad: unrelated counterparties must never use
        # a page slot before the exact stock-side identity has been matched.
        for (chain_id, pool, token, native), sides in pool_sides.items():
            if chain_id == cid and sides:
                branches.append((f"{SCOPE_SQL}=? AND ({POOL_SQL})=? AND asset=? AND ({NATIVE_STOCK_SQL})=?",
                                 [cid, pool, cid+':'+token, native]))
    return branches


def _event_query(keys, chain, limit, cursor, pool_sides):
    """Only identity/time metadata enters intermediate union/sort tables."""
    sqls, params = [], []
    for predicate, values in _branches(keys, chain, pool_sides):
        before = ' AND (t < ? OR (t = ? AND id < ?))' if cursor else ''
        sqls.append(f'SELECT id,asset,t FROM (SELECT id,asset,t FROM events WHERE ({predicate}){before} ORDER BY t DESC,id DESC LIMIT ?)')
        params.extend(values)
        if cursor:
            params.extend((cursor['t'], cursor['t'], cursor['id']))
        params.append(limit+1)
    if not sqls:
        return None, []
    selected = 'SELECT id,asset,t FROM ('+' UNION '.join(sqls)+') ORDER BY t DESC,id DESC LIMIT ?'
    params.append(limit+1)
    # Fetch at most 20+1 bodies AFTER the cross-chain match/dedup/page. A
    # wrapper follow can expand into many pools, without multiplying bodies
    # retained by SQLite's temporary union and sort structures.
    query = ('SELECT source.id,source.asset,source.t,source.body FROM events AS source '
             'JOIN ('+selected+') AS matched ON source.id=matched.id '
             'ORDER BY source.t DESC,source.id DESC')
    return query, params


def _read_events(path, keys, chain, limit, cursor, pool_sides):
    """A private read connection cannot queue behind/hold a writer transaction."""
    deadline = time.monotonic()+READ_SECONDS
    calls = 0
    def progress():
        nonlocal calls
        calls += 1
        return time.monotonic() > deadline or calls * 1000 > READ_OPS
    try:
        with closing(connect(Path(path).resolve().as_uri()+'?mode=ro', readonly=True, uri=True, timeout=READ_SECONDS if is_postgres_path(path) else .25)) as connection:
            connection.execute('PRAGMA query_only=ON')
            connection.set_progress_handler(progress, 1000)
            query, params = _event_query(keys, chain, limit, cursor, pool_sides)
            if query is None:
                return []
            if time.monotonic() > deadline:
                raise HTTPException(503, 'watch-events-busy', headers={'Retry-After': '3'})
            rows = connection.execute(query, params).fetchall()
            if time.monotonic() > deadline:
                raise HTTPException(503, 'watch-events-busy', headers={'Retry-After': '3'})
            result = []
            for event_id, stored_asset, at, raw in rows:
                if len(raw) > MAX_EVENT_BODY:
                    raise HTTPException(503, 'watch-event-too-large', headers={'Retry-After': '3'})
                try:
                    item = json.loads(raw)
                    if (not isinstance(item, dict) or any(item.get(name) is not None and
                        not isinstance(item[name], dict) for name in ('relation', 'match')) or
                        not isinstance(event_id, str) or not 0 < len(event_id) <= 512 or
                        not isinstance(stored_asset, str) or type(at) is not int or not 0 < at < 10**16):
                        raise ValueError()
                except (ValueError, TypeError, RecursionError) as exc:
                    raise HTTPException(503, 'watch-event-invalid', headers={'Retry-After': '3'}) from exc
                cid = stored_asset.split(':', 1)[0]
                if cid not in CHAINS or not _address(stored_asset, cid):
                    raise HTTPException(503, 'watch-event-identity-invalid', headers={'Retry-After': '3'})
                item.update(id=event_id, chainId=cid, t=at, asset=_address(stored_asset, cid))
                item['token'] = item['asset']
                item['matchedKeys'] = _event_matches(item, keys, pool_sides)
                if not item['matchedKeys']:
                    # A mismatch indicates malformed persisted identity, not
                    # permission to return an incomplete paginated result.
                    raise HTTPException(503, 'watch-event-identity-invalid', headers={'Retry-After': '3'})
                result.append(item)
            return result
    except sqlite3.OperationalError as exc:
        reason = 'watch-events-busy' if 'locked' in str(exc).lower() or 'interrupt' in str(exc).lower() else 'watch-events-unavailable'
        raise HTTPException(503, reason, headers={'Retry-After': '3'}) from exc


@router.post('/events')
async def watch_events(body: WatchEventsBody):
    fingerprint = _fingerprint(body.keys, body.chain)
    cursor = _cursor_decode(body.cursor, fingerprint)
    unsupported = [key for key in body.keys if (identity := key_identity(key))[0] == 'asset' and identity[1][0] not in CHAINS]
    active_keys = [key for key in body.keys if key not in unsupported]
    if not active_keys:
        return {'items': [], 'nextCursor': None, 'hasMore': False, 'asOf': int(time.time()*1000), 'unsupportedKeys': unsupported}
    pool_sides = {}
    exact = {identity for kind, identity in map(key_identity, active_keys) if kind == 'asset'}
    if exact:
        from .product_v2 import published
        payload = await published()
        for relation in (payload.get('unified') or {}).get('relations', []):
            cid = str(relation.get('chainId'))
            side, native = (_address(relation.get(field), cid) for field in ('stockSide', 'stock'))
            if (cid, side) not in exact or not _in_scope(cid, body.chain):
                continue
            pool, token = str(relation.get('pool') or '').lower(), _address(relation.get('token'), cid)
            if ADDRESS.fullmatch(pool) and token and native:
                pool_sides.setdefault((cid, pool, token, native), set()).add(side)
                if len(pool_sides) > 200:
                    raise HTTPException(503, 'watch-identity-expansion-too-large', headers={'Retry-After': '3'})
    try:
        await asyncio.wait_for(_event_slots.acquire(), timeout=.03)
    except TimeoutError as exc:
        raise HTTPException(503, 'watch-events-busy', headers={'Retry-After': '1'}) from exc
    task = asyncio.create_task(asyncio.to_thread(_read_events, db.DB_PATH, body.keys, body.chain, body.limit, cursor, pool_sides))
    # Cancellation cannot terminate a SQLite thread. Keep its read slot until
    # it really finishes, rather than admitting unlimited abandoned readers.
    task.add_done_callback(lambda done: (_event_slots.release(), done.exception() if not done.cancelled() else None))
    rows = await asyncio.shield(task)
    more = len(rows) > body.limit
    items = rows[:body.limit]
    return {'items': items, 'nextCursor': _cursor_encode(items[-1], fingerprint) if more else None,
            'hasMore': more, 'asOf': int(time.time()*1000), 'unsupportedKeys': unsupported}
