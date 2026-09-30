"""Durable whole-product projection, driven by transactional change records.

The worker calls projection_tick once a second. It does no dashboard work when
nothing changed, except a bounded expiry sweep to publish stale/missing status.
JSON producers are bridged by file identity; no browser reads or polls files.
"""
import asyncio
import json
import time
import zlib
from contextvars import ContextVar
from pathlib import Path

from .db import retry_busy_write, store, transaction_view
from .dashboard_projection import compact_dashboard, market_dashboard, overview_dashboard, projection_key
from .realtime_schema import enqueue_event
from .source_snapshots import bind_snapshots
from .market_quotes import bind_previous_quotes
from .projection_facts import ProjectionFacts

SCHEMA = 1
COLLECTIONS = ('assets', 'stockTokens', 'relations', 'sectors')
SHARED_FILES = ('data/market-enrichment.json', 'data/robinhood.json', 'data/binance.json')
_tick_lock = asyncio.Lock()
_file_signatures = {}
_committed_projection = None
_serialized_projection = None
_parsed_tokens = None
_committed_facts = None
_building_facts = ContextVar('building_projection_facts', default=None)
_SNAPSHOT_MAGIC = b'CRP1\x00'
_FEED_CHAINS = ('196', '56', '4663')


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _encode_snapshot(body):
    # Keep the cursor and its complete body in the same SQLite transaction.
    # Level 1 shrinks repeated field metadata without changing JSON semantics.
    return _SNAPSHOT_MAGIC + zlib.compress(body.encode('utf-8'), level=1)


def _decode_snapshot(body):
    # Rolling upgrade: pre-compression databases contain TEXT in this column.
    if isinstance(body, str):
        return body
    if body.startswith(_SNAPSHOT_MAGIC):
        return zlib.decompress(body[len(_SNAPSHOT_MAGIC):]).decode('utf-8')
    return body.decode('utf-8')


async def bridge_shared_sources(paths=None):
    """Import atomic Node/worker snapshots into the same fact/outbox commit.

    Malformed snapshots keep last good data and publish a source error. A file
    removed after a successful import explicitly clears its previous overlay.
    The persisted signature also makes restart recovery idempotent.
    """
    scoped = await store('system')
    changed = 0
    for name in paths or SHARED_FILES:
        path = Path(name)
        try:
            info = path.stat()
            signature = f'{info.st_mtime_ns}:{info.st_size}'
        except OSError:
            signature = 'missing'
        if _file_signatures.get(name) == signature:
            continue
        saved = await scoped.get('shared-source', name)
        if saved and saved.get('signature') == signature:
            _file_signatures[name] = signature
            continue
        if signature == 'missing':
            if not saved:
                _file_signatures[name] = signature
                continue
            data = {'status': 'missing', 'error': 'snapshot-file-missing', 'tokens': []}
        else:
            try:
                data = json.loads(path.read_text())
                if not isinstance(data, dict):
                    raise ValueError('snapshot-not-object')
                # Do not import a mixed read if a producer replaced the file.
                after = path.stat()
                if signature != f'{after.st_mtime_ns}:{after.st_size}':
                    continue
            except (OSError, ValueError):
                data = {**((saved or {}).get('data') or {}),
                        'status': 'error', 'error': 'snapshot-unreadable'}
        await scoped.put('shared-source', name, {'signature': signature, 'data': data})
        _file_signatures[name] = signature
        changed += 1
    return changed


def projection_delta(previous, current):
    before, after = (previous or {}).get('unified', {}), current['unified']
    upserts, removes = {}, {}
    for name in COLLECTIONS:
        old = {row.get('projectionKey') or projection_key(name, row): row for row in before.get(name, [])}
        new = {row.get('projectionKey') or projection_key(name, row): row for row in after.get(name, [])}
        upserts[name] = [row for key, row in new.items() if old.get(key) != row]
        removes[name] = [key for key in old if key not in new]
    # Values are complete replacements. Null, zero and empty arrays are all
    # meaningful changes; omit only metadata that is actually unchanged.
    meta = {key: value for key, value in after.items() if key not in COLLECTIONS and before.get(key) != value}
    for key in before.keys() - after.keys() - set(COLLECTIONS):
        meta[key] = None
    return {'schema': SCHEMA, 'upserts': upserts, 'removes': removes, 'meta': meta}


def _invalidations(changes, previous, current):
    out = {}
    def add(kind, chain=None, token=None):
        row = {'kind': kind}
        if chain:
            row['chainId'] = str(chain)
        if token:
            row['token'] = str(token).lower()
        out[(kind, chain, token)] = row
    relations = {}
    for payload in (previous or {}, current):
        for row in payload.get('unified', {}).get('relations', []):
            relations[(str(row.get('chainId')), str(row.get('id')))] = row
    for change in changes:
        name, entity = change['kind'], change['entity']
        if ':' in name:
            chain, kind = name.split(':', 1)
        else:
            kind = name
            chain, _, _ = entity.partition(':')
        if kind == 'shared-source':
            add('detail')
            continue
        if kind.startswith('briefing'):
            add('briefing')
        elif kind.startswith('insight'):
            add('insight', chain, entity.split(':')[0])
        elif kind == 'relation':
            row = relations.get((chain, entity)) or {}
            add('detail', chain, row.get('token'))
            add('registry')
            add('feed')
        elif kind in ('feed', 'event'):
            add('feed')
            add('detail', chain, entity.partition(':')[2] or None)
        elif kind in ('asset', 'stock', 'comparison', 'pool-quote', 'market-quote', 'market-registry'):
            if kind == 'comparison':
                add('comparison', chain, entity)
            token = entity.rsplit(':', 1)[-1] if kind.startswith('market-') else entity
            add('detail', chain, token)
        elif kind == 'trade':
            if not any(marker in entity for marker in (':binance:', ':binance-alpha:', ':dex:')):
                add('feed')  # legacy bare-token writers recover through HTTP history
        elif kind == 'candle':
            pass  # official candle frames update subscribed charts directly
        elif kind in ('sample', 'sample-evidence', 'trade-coverage', 'trade-bucket', 'comparison-sample'):
            token = entity.partition(':')[2]
            # Market-qualified storage names end with the contract address.
            add('detail', chain, token.rsplit(':', 1)[-1] if ':' in token else token)
        elif kind.startswith('registry'):
            add('registry')
        elif kind not in ('basket', 'basket-view', 'basket-last'):
            # Unrecognized new evidence is explicitly invalidated rather than
            # silently excluded from the delivery protocol.
            add('detail', chain if chain in ('196', '56', '4663') else None)
    return list(out.values())


async def _build_payload(connection):
    from .state import DashboardData
    facts = _building_facts.get()
    if facts is None:
        facts = await ProjectionFacts.capture(connection)
    sources = {identity: body.get('data') or {} for identity, body in facts.all_kv('system', 'shared-source')}
    with bind_snapshots(sources):
        async with transaction_view(connection):
            data = DashboardData()
            await data.reload(facts=facts)
            feed = _feed_snapshot(data)
            return compact_dashboard(await data.payload(include_groups=False)), feed


def _feed_snapshot(data):
    """Retain only the inputs /feed needs from its consistent fact view.

    Selection happens before publication because the overview ranks by volume,
    while /feed ranks verified candidates by their latest trade. Keep thirty
    events per chain so filtering a quiet chain cannot lose its recent events.
    """
    from .state import BASE_QUOTE_SYMBOLS

    related = {(str(row.get('chainId') or '196'), row.get('token'))
               for row in data.relations if row.get('status') == 'verified'}
    by_chain = {chain: [] for chain in _FEED_CHAINS}
    for asset in data.assets:
        chain = str(asset.get('chainId') or '196')
        if (chain in by_chain and asset.get('tradeAt') and asset.get('kind') == 'candidate'
                and (chain, asset.get('token')) in related
                and str(asset.get('symbol') or '').upper() not in BASE_QUOTE_SYMBOLS):
            by_chain[chain].append(asset)
    assets = [{'chainId': chain, 'token': asset['token'], 'symbol': asset.get('symbol')}
              for chain in _FEED_CHAINS
              for asset in sorted(by_chain[chain], key=lambda row: -(row.get('tradeAt') or 0))[:10]]
    signals = []
    counts = {chain: 0 for chain in _FEED_CHAINS}
    for signal in data.signals:
        chain = str(signal.get('chainId'))
        if chain in counts and counts[chain] < 30:
            signals.append(signal)
            counts[chain] += 1
    tracked = [{'chainId': str(a.get('chainId') or '196'), 'token': a['token'], 'symbol': a.get('symbol')}
               for a in data.assets if a.get('token') and a.get('kind') == 'candidate'
               and str(a.get('symbol') or '').upper() not in BASE_QUOTE_SYMBOLS]
    return {'assets': assets, 'trackedAssets': tracked, 'signals': signals}


async def projection_tick(force=False, expiry_ms=30_000):
    # A separate collector/API process can briefly own SQLite's only WAL
    # writer. Rebuild from a fresh snapshot after contention; neither the
    # input cursor nor its outbox rows were committed on the failed attempt.
    return await retry_busy_write(lambda: _projection_tick_once(force, expiry_ms))


async def _projection_tick_once(force=False, expiry_ms=30_000):
    """Build from a consistent read snapshot, then publish with a short write.

    The snapshot cursor is captured WITH the fact view, before construction.
    Events written during construction remain replayable after that cursor.
    A later publication id must never replace it and hide intervening trades.
    """
    global _committed_projection, _committed_facts
    import aiosqlite
    async with _tick_lock:
        await bridge_shared_sources()
        scoped = await store('196')
        db = scoped.db
        columns = "SELECT revision,cursor,input_cursor,built_at FROM dashboard_projection WHERE name='full'"
        now = int(time.time()*1000)
        started = time.monotonic()
        async with aiosqlite.connect(scoped.path, timeout=15) as reader:
            reader.row_factory = aiosqlite.Row
            await reader.execute('PRAGMA query_only=ON')
            await reader.execute('BEGIN')
            old_rows = await reader.execute_fetchall(columns)
            old_row = old_rows[0] if old_rows else None
            old_input = old_row['input_cursor'] if old_row else 0
            bounds = (await reader.execute_fetchall('''SELECT
                (SELECT id FROM change_outbox ORDER BY id LIMIT 1),
                (SELECT id FROM change_outbox ORDER BY id DESC LIMIT 1)'''))[0]
            low, high = bounds[0], bounds[1] or 0
            contiguous = old_input <= high and (low is None or low <= old_input + 1)
            # Maintenance/recovery may have truncated unconsumed changes. A
            # lowered or discontinuous watermark must rebuild even when MAX(id)
            # is below our old cursor; it must not enter the idle fast path.
            if old_row and not force and contiguous and high <= old_input and now-old_row['built_at'] < expiry_ms:
                return {'changed': False, 'revision': old_row['revision'], 'cursor': old_row['cursor']}
            changes = await reader.execute_fetchall(
                'SELECT DISTINCT kind,entity,operation FROM change_outbox WHERE id>?', (old_input,))
            read_cursor = (await reader.execute_fetchall('SELECT COALESCE(MAX(id),0) FROM realtime_events'))[0][0]
            tape_only = bool(old_row and not force and contiguous and now-old_row['built_at'] < expiry_ms and changes and all(
                change['kind'] == 'candle' or (change['kind'] == 'trade' and
                any(marker in change['entity'] for marker in (':binance:', ':binance-alpha:', ':dex:')))
                for change in changes))
            if not tape_only:
                stamp = (scoped.path, old_row['revision'], old_row['cursor'], old_row['built_at']) if old_row else None
                if stamp and _committed_projection and _committed_projection[0] == stamp:
                    previous = _committed_projection[1]
                elif old_row:
                    body = (await reader.execute_fetchall("SELECT body FROM dashboard_projection WHERE name='full'"))[0]
                    previous = json.loads(_decode_snapshot(body['body']))
                else:
                    previous = None
                fact_stamp = (scoped.path, id(scoped.db), old_input)
                # Forced calibration reads every fact, including writes whose
                # outbox evidence was removed outside normal publication.
                prior_facts = _committed_facts[1] if (not force and contiguous and _committed_facts
                                                     and _committed_facts[0] == fact_stamp) else None
                facts = await ProjectionFacts.capture(reader, prior_facts, changes)
                binding = _building_facts.set(facts)
                try:
                    with bind_previous_quotes(previous):
                        payload, feed = await _build_payload(reader)
                finally:
                    _building_facts.reset(binding)
        # JSON comparison/encoding and full catalogue traversal never hold the
        # sole writer. Live collectors may commit throughout this work.
        if not tape_only:
            delta = projection_delta(previous, payload)
            invalidations = _invalidations(changes, previous, payload)
            if old_row and not contiguous:
                # Missing outbox evidence may concern detail-only fields,
                # briefings or history absent from compact list DTOs.
                invalidations = [{'kind': kind} for kind in
                                 ('detail', 'feed', 'registry', 'briefing', 'insight', 'comparison')]
            changed = bool(not previous or invalidations or delta['meta'] or
                           any(delta['upserts'].values()) or any(delta['removes'].values()))
            revision = (old_row['revision'] if old_row else 0) + changed
            delta.update(revision=revision, invalidations=invalidations, now=payload['now'])
            payload['realtime'] = {'schema': SCHEMA, 'cursor': read_cursor, 'revision': revision}
            feed['now'] = payload['now']
            feed['realtime'] = payload['realtime']
            bodies = {
                'full': _encode_snapshot(_dump(payload)),
                'overview': _encode_snapshot(_dump(overview_dashboard(payload))),
                'market': _encode_snapshot(_dump(market_dashboard(payload))),
                'feed': _encode_snapshot(_dump(feed)),
            }
            affected = _derived_dependencies(previous, payload, changes)
        async with scoped._guard_write():
            await db.execute('BEGIN IMMEDIATE')
            current = await scoped.fetchone(columns)
            # A bootstrap in another API process can publish during our read.
            # Retry without consuming input rather than overwrite a new base.
            if (tuple(current) if current else None) != (tuple(old_row) if old_row else None):
                await db.rollback()
                return {'changed': False, 'retry': True,
                        'revision': current['revision'] if current else 0,
                        'cursor': current['cursor'] if current else 0}
            if tape_only:
                await db.execute("UPDATE dashboard_projection SET input_cursor=? WHERE name IN ('full','overview','market','feed')", (high,))
                await db.execute('DELETE FROM change_outbox WHERE id<=?', (high-1000,))
                await db.commit()
                if _committed_facts and _committed_facts[0] == (scoped.path, id(scoped.db), old_input):
                    _committed_facts = ((scoped.path, id(scoped.db), high), _committed_facts[1])
                return {'changed': False, 'revision': old_row['revision'], 'cursor': old_row['cursor'],
                        'inputCursor': high, 'durationMs': round((time.monotonic()-started)*1000, 1)}
            sequence = await enqueue_event(db, 'projection.delta', delta) if changed else read_cursor
            await _queue_derived(db, affected, high)
            await db.executemany('''INSERT INTO dashboard_projection VALUES (?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,cursor=excluded.cursor,
                input_cursor=excluded.input_cursor,body=excluded.body,built_at=excluded.built_at''',
                [(name, revision, read_cursor, high, body, now) for name, body in bodies.items()])
            # Changes committed AFTER the read snapshot retain ids above high.
            await db.execute('DELETE FROM change_outbox WHERE id<=?', (high-1000,))
            await db.commit()
            _committed_projection = ((scoped.path, revision, read_cursor, now), payload)
            _committed_facts = ((scoped.path, id(scoped.db), high), facts)
        return {'changed': changed, 'revision': revision, 'cursor': read_cursor, 'eventCursor': sequence,
                'inputCursor': high, 'durationMs': round((time.monotonic()-started)*1000, 1)}

async def read_projection_json(view='full'):
    # A separate read connection never observes an uncommitted bootstrap.
    # Read the small named row directly. During a rolling upgrade, an older
    # worker may have published only 'full'; derive dashboard views from that
    # exact revision instead of serving an older named row.
    if view not in ('full', 'overview', 'market', 'feed'):
        raise ValueError(f'unsupported projection view: {view}')
    global _serialized_projection
    import aiosqlite
    scoped = await store('196')

    async def read():
        global _serialized_projection
        async with aiosqlite.connect(scoped.path, timeout=15) as reader:
            # The stamp and body must describe the same commit if a worker
            # publishes between these SELECTs. This is a read transaction.
            await reader.execute('BEGIN')
            full = await (await reader.execute("SELECT revision,cursor,input_cursor,built_at FROM dashboard_projection WHERE name='full'")).fetchone()
            if full is None:
                return None
            selected = full if view == 'full' else await (await reader.execute(
                'SELECT revision,cursor,input_cursor,built_at FROM dashboard_projection WHERE name=?',
                (view,))).fetchone()
            current = selected is not None and tuple(selected) == tuple(full)
            # Tape-only commits advance input_cursor without changing the
            # published body. They must not invalidate every large JSON cache.
            stamp = (scoped.path, id(scoped.db), view, full[0], full[1], full[3],
                     *((selected[0], selected[1], selected[3]) if selected else ()))
            cache = _serialized_projection if isinstance(_serialized_projection, dict) else {}
            if view in cache and cache[view][0] == stamp:
                return cache[view][1]
            if view == 'feed' and not current:
                return None
            source = view if current else 'full'
            row = await (await reader.execute('SELECT body FROM dashboard_projection WHERE name=?',
                                              (source,))).fetchone()
        body = _decode_snapshot(row[0])
        if not current:
            full_payload = json.loads(body)
            body = _dump(overview_dashboard(full_payload) if view == 'overview'
                         else market_dashboard(full_payload))
        if not isinstance(_serialized_projection, dict):
            _serialized_projection = {}
        _serialized_projection[view] = (stamp, body)
        return body

    body = await read()
    if body is None:
        # Only the projection worker may build. Concurrent first visitors must
        # never turn a missing snapshot into N full catalogue reconstructions.
        raise ProjectionUnavailable(f'projection view unavailable: {view}')
    return body


class ProjectionUnavailable(RuntimeError):
    pass


async def read_token_projection(chain, token):
    """Reuse one decoded/indexed canonical revision across all token pages."""
    global _parsed_tokens
    body = await read_projection_json('full')
    if not _parsed_tokens or _parsed_tokens[0] is not body:
        payload = json.loads(body)
        unified = payload.get('unified') or {}
        assets = {(str(r.get('chainId')), str(r.get('token') or '').lower()): r for r in unified.get('assets', [])}
        stocks = {(str(r.get('chainId')), str(r.get('tokenContractAddress') or '').lower()): r for r in unified.get('stockTokens', [])}
        relations = {}
        for row in unified.get('relations', []):
            for address in {row.get('token'), row.get('stock')} - {None}:
                relations.setdefault((str(row.get('chainId')), str(address).lower()), []).append(row)
        _parsed_tokens = (body, payload.get('now'), payload.get('realtime'), assets, stocks, relations)
    _, at, realtime, assets, stocks, relations = _parsed_tokens
    identity = (str(chain), token.lower())
    return {'asset': assets.get(identity), 'stock': stocks.get(identity), 'relations': relations.get(identity, []),
            'snapshotAt': at, 'realtime': realtime}


async def read_projection():
    return json.loads(await read_projection_json())


_DERIVED_FIELDS = ('price', 'priceCurrency', 'priceScope', 'quoteAt', 'quoteStatus', 'marketCap', 'liquidity',
                   'stockPrice', 'referenceAt', 'referenceCurrency', 'tokenToAssetRatio', 'ratioVersion')
_RELATION_INPUTS = ('token', 'stock', 'stockSide', 'pool', 'ticker', 'status', 'checkedAt', 'liquidityUsd', 'liquidityAt', 'liquidityStatus')


def _derived_dependencies(previous, current, changes):
    before, after = (previous or {}).get('unified', {}), current['unified']
    affected = set()
    for collection in ('assets', 'stockTokens', 'relations'):
        old = {projection_key(collection, r): r for r in before.get(collection, [])}
        new = {projection_key(collection, r): r for r in after.get(collection, [])}
        fields = _RELATION_INPUTS if collection == 'relations' else _DERIVED_FIELDS
        for key in old.keys() | new.keys():
            left, right = old.get(key, {}), new.get(key, {})
            if {k: left.get(k) for k in fields} == {k: right.get(k) for k in fields}:
                continue
            for row in (left, right):
                chain = str(row.get('chainId') or '196')
                for token in (row.get('token'), row.get('tokenContractAddress'), row.get('stock')):
                    if token:
                        affected.add((chain, str(token).lower()))
    for change in changes:
        name = change['kind']
        if ':' not in name:
            continue
        chain, kind = name.split(':', 1)
        if kind == 'fx-quote':
            affected.add((chain, '*'))
        elif kind == 'independent-quote':
            affected.add((chain, change['entity'].lower()))
        elif kind == 'pool-quote':
            for row in after.get('relations', []):
                if str(row.get('chainId')) == chain and str(row.get('pool')).lower() == change['entity'].lower():
                    affected.add((chain, str(row.get('stock') or '').lower()))
    return affected


async def _queue_derived(connection, affected, generation):
    await connection.executemany("""INSERT INTO projection_dirty VALUES (?,?,?)
        ON CONFLICT(chain,token) DO UPDATE SET generation=MAX(generation,excluded.generation)""",
        [(chain, token, generation) for chain, token in affected if token])


async def refresh_derived():
    """Consume durable dependencies in bounded, oldest-first worker turns.

    A large quote refresh can dirty thousands of tokens at once. Processing a
    bounded prefix lets newer projection and collector writes run between
    turns. Requeued tokens move to their newer generation; the conditional
    delete cannot erase work published while this turn was calculating.
    """
    from .state import DashboardData
    from .collectors.baskets import refresh_baskets
    from .comparison_service import refresh_comparisons
    scoped = await store('196')
    rows = await scoped.fetchall('''SELECT chain,token,generation FROM projection_dirty
                                  ORDER BY generation,chain,token LIMIT 200''')
    if not rows:
        return {'affected': 0, 'baskets': 0, 'comparisons': 0}
    affected = {(row['chain'], row['token']) for row in rows}
    data = DashboardData()
    await data.reload()
    baskets = await refresh_baskets(affected=affected, data=data)
    comparisons = await refresh_comparisons(affected=affected, data=data)
    async def acknowledge():
        async with scoped._guard_write():
            try:
                await scoped.db.execute('BEGIN IMMEDIATE')
                await scoped.db.executemany('DELETE FROM projection_dirty WHERE chain=? AND token=? AND generation<=?',
                                          [(r['chain'], r['token'], r['generation']) for r in rows])
                await scoped.db.commit()
            except BaseException:
                # A failed executemany may have already deleted some rows in
                # this transaction. A retry must begin from its original set.
                await scoped.db.rollback()
                raise
    await retry_busy_write(acknowledge)
    return {'affected': len(affected), 'baskets': baskets, 'comparisons': comparisons}
