"""Durable whole-product projection, driven by transactional change records.

The worker calls projection_tick once a second. It does no dashboard work when
nothing changed, except a bounded expiry sweep to publish stale/missing status.
JSON producers are bridged by file identity; no browser reads or polls files.
"""
import asyncio
import copy
import gc
import json
import os
import time
import zlib
import threading
from collections import OrderedDict
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from .storage_runtime import async_connect
from .db import retry_busy_write, store, transaction_view
from .dashboard_projection import compact_dashboard, market_dashboard, overview_dashboard, projection_key
from .realtime_schema import enqueue_event
from .source_snapshots import bind_snapshots
from .market_quotes import bind_previous_quotes
from .projection_facts import ProjectionFacts
from .config import bounded_env_int
from .projection_query_cache import QueryBusy, QueryWork, SnapshotBody, SnapshotPayload

SCHEMA = 1
COLLECTIONS = ('assets', 'stockTokens', 'relations', 'sectors')
SHARED_FILES = ('data/market-enrichment.json', 'data/robinhood.json', 'data/binance.json', 'data/equity-market.json', 'data/pool-market.json')
_tick_lock = asyncio.Lock()
_file_signatures = {}
_committed_projection = None
_committed_page_views = None
_serialized_projection = None
_committed_facts = None
_building_facts = ContextVar('building_projection_facts', default=None)
_building_timings = ContextVar('building_projection_timings', default=None)
_SNAPSHOT_MAGIC = b'CRP1\x00'
_FEED_CHAINS = ('196', '56', '4663')
QUERY_DECODED_LIMIT = bounded_env_int('PROJECTION_QUERY_MAX_MB', 256, 8, 512) * 1024 * 1024
QUERY_CACHE_BYTES = bounded_env_int('PROJECTION_QUERY_CACHE_MB', 256, 16, 2048) * 1024 * 1024
_query_work = None
_shared_refreshes = {}
_snapshot_parse_lock = threading.Lock()


def _load_query_json(body):
    """Fast public-snapshot parsing; producers retain their existing encoder."""
    wire = body.wire if isinstance(body, SnapshotBody) else body
    try:
        import msgspec
    except ImportError:
        return json.loads(wire)
    def decode():
        try:
            return msgspec.json.decode(wire)
        except msgspec.DecodeError:
            # Rolling snapshots may use JSON accepted by the original decoder.
            return json.loads(wire)
    if len(wire) < 2*1024*1024:
        return decode()
    # Large, acyclic JSON graphs otherwise trigger repeated GC scans while
    # being constructed. Coalesce those scans into one young-generation pass,
    # serialize changes to process-wide GC state, and always restore it.
    with _snapshot_parse_lock:
        enabled = gc.isenabled()
        if enabled:
            gc.disable()
        try:
            return decode()
        finally:
            if enabled:
                try:
                    gc.collect(0)
                finally:
                    gc.enable()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _encode_snapshot(body):
    # Keep the cursor and its complete body in the same SQLite transaction.
    # Level 1 shrinks repeated field metadata without changing JSON semantics.
    return _SNAPSHOT_MAGIC + zlib.compress(body.encode('utf-8'), level=1)


def _decode_snapshot(body, limit=None):
    # Rolling upgrade: pre-compression databases contain TEXT in this column.
    if isinstance(body, str):
        return body
    if body.startswith(_SNAPSHOT_MAGIC):
        if limit is not None:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(body[len(_SNAPSHOT_MAGIC):], limit + 1)
            if len(raw) > limit or decoder.unconsumed_tail:
                raise ProjectionUnavailable('projection-snapshot-too-large')
            if not decoder.eof:
                raise ProjectionUnavailable('projection-snapshot-invalid')
            return raw.decode('utf-8')
        return zlib.decompress(body[len(_SNAPSHOT_MAGIC):]).decode('utf-8')
    if limit is not None and len(body) > limit:
        raise ProjectionUnavailable('projection-snapshot-too-large')
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


def field_operations(before, after, path=()):
    """Lossless compact field edits: [path,value] sets, [path] deletes.

    Null is a value, never a deletion. Arrays of equal length are edited by
    position (including rankings); a length change replaces that array. The
    receiver checks baseRevision before applying any edits.
    """
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        edits = [[[*path, key]] for key in before if key not in after]
        for key, value in after.items():
            edits.extend(field_operations(before[key], value, (*path, key))
                         if key in before else [[[*path, key], value]])
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        edits = [edit for index, value in enumerate(after)
                 for edit in field_operations(before[index], value, (*path, index))]
    else:
        return [[list(path), after]]
    replacement = [[list(path), after]]
    # Replacing a changed small object can be cheaper than several paths, but
    # large stable evidence remains on the browser instead of crossing again.
    return replacement if len(_dump(replacement)) < len(_dump(edits)) else edits


def view_delta(previous, current, scope, base_revision, revision):
    """Publish page edits in the SAME journal record as the legacy delta.

    No HTTP handler rebuilds a page or invents a replay delta from today's
    snapshot. Keyed rows preserve object identity; explicit order changes keep
    additions/removals and rank changes identical to the corresponding HTTP DTO.
    """
    before, after = (previous or {}).get('unified', {}), current['unified']
    out = {'schema': 2, 'scope': scope, 'baseRevision': base_revision,
           'revision': revision, 'now': current['now'], 'upserts': {},
           'patches': {}, 'removes': {}, 'orders': {}}
    for name in COLLECTIONS:
        old = {row.get('projectionKey') or projection_key(name, row): row for row in before.get(name, [])}
        new = {row.get('projectionKey') or projection_key(name, row): row for row in after.get(name, [])}
        added = [row for key, row in new.items() if key not in old]
        patches = [[key, field_operations(old[key], row)] for key, row in new.items()
                   if key in old and old[key] != row]
        removed = [key for key in old if key not in new]
        if added:
            out['upserts'][name] = added
        if patches:
            out['patches'][name] = patches
        if removed:
            out['removes'][name] = removed
        if list(old) != list(new):
            out['orders'][name] = list(new)
    out['metaOps'] = field_operations({k: v for k, v in before.items() if k not in COLLECTIONS},
                                      {k: v for k, v in after.items() if k not in COLLECTIONS})
    return out


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
            timings = _building_timings.get()
            began = time.monotonic()
            await data.reload(facts=facts)
            if timings is not None:
                timings['reloadMs'] = round((time.monotonic()-began)*1000, 1)
            feed = _feed_snapshot(data)
            began = time.monotonic()
            payload = await data.payload(include_groups=False)
            if timings is not None:
                timings['payloadMs'] = round((time.monotonic()-began)*1000, 1)
            began = time.monotonic()
            payload = compact_dashboard(payload)
            if timings is not None:
                timings['compactMs'] = round((time.monotonic()-began)*1000, 1)
            return payload, feed


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


async def prune_consumed_outbox(batch_rows=64):
    """Remove a bounded batch already consumed by a committed publication.

    Each removed row also generates recovery CDC work on PostgreSQL. Keep
    that work outside publication and retain the original 1000-id overlap.
    No unconsumed change or market history is removed by this maintenance.
    """
    scoped = await store('196')
    limit = max(1, min(128, int(batch_rows)))

    async def prune():
        # Maintenance has its own short deadline and connection, so it cannot
        # change the publication/collector connection's timeout or transaction.
        async with async_connect(scoped.path, timeout=.25) as connection:
            async with scoped._guard_write(connection):
                await connection.execute('BEGIN IMMEDIATE')
                rows = await connection.execute_fetchall(
                    "SELECT input_cursor FROM dashboard_projection WHERE name='full'")
                watermark = int(rows[0][0] or 0) if rows else 0
                cutoff = watermark - 1000
                candidates = await connection.execute_fetchall(
                    'SELECT id FROM change_outbox WHERE id<=? ORDER BY id LIMIT ?',
                    (cutoff, limit)) if cutoff > 0 else []
                ids = [row[0] for row in candidates]
                if ids:
                    placeholders = ','.join('?' for _ in ids)
                    await connection.execute(
                        f'DELETE FROM change_outbox WHERE id IN ({placeholders})', ids)
                await connection.commit()
                return {'accepted': len(ids), 'updated': len(ids),
                        'skipped': int(not ids), 'inputCursor': watermark}

    return await retry_busy_write(prune)


async def _projection_tick_once(force=False, expiry_ms=30_000):
    """Build from a consistent read snapshot, then publish with a short write.

    The snapshot cursor is captured WITH the fact view, before construction.
    Events written during construction remain replayable after that cursor.
    A later publication id must never replace it and hide intervening trades.
    """
    global _committed_projection, _committed_facts, _committed_page_views
    import aiosqlite
    async with _tick_lock:
        started = time.monotonic()
        stages = {}
        await bridge_shared_sources()
        stages['bridgeMs'] = round((time.monotonic()-started)*1000, 1)
        scoped = await store('196')
        db = scoped.db
        columns = "SELECT revision,cursor,input_cursor,built_at FROM dashboard_projection WHERE name='full'"
        now = int(time.time()*1000)
        read_started = time.monotonic()
        async with async_connect(scoped.path, readonly=True, timeout=15) as reader:
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
                stages['snapshotReadMs'] = round((time.monotonic()-read_started)*1000, 1)
                return {'changed': False, 'revision': old_row['revision'], 'cursor': old_row['cursor'],
                        'inputCursor': old_input, 'durationMs': round((time.monotonic()-started)*1000, 1),
                        'stagesMs': stages}
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
                if stamp and _committed_page_views and _committed_page_views[0] == stamp:
                    previous_views = _committed_page_views[1]
                else:
                    previous_views = {}
                    if old_row:
                        named = await reader.execute_fetchall("SELECT name,revision,cursor,built_at,body FROM dashboard_projection WHERE name IN ('overview','market')")
                        for row in named:
                            if (row['revision'], row['cursor'], row['built_at']) == (old_row['revision'], old_row['cursor'], old_row['built_at']):
                                previous_views[row['name']] = json.loads(_decode_snapshot(row['body']))
                    # Rolling upgrade from full-only storage matches the HTTP
                    # read fallback. Otherwise use the ACTUAL previous DTO,
                    # even if a deployment has changed its formatting rules.
                    if previous:
                        if 'overview' not in previous_views:
                            previous_views['overview'] = overview_dashboard(previous)
                        if 'market' not in previous_views:
                            previous_views['market'] = market_dashboard(previous)
                fact_stamp = (scoped.path, id(scoped.db), old_input)
                # Forced calibration reads every fact, including writes whose
                # outbox evidence was removed outside normal publication.
                prior_facts = _committed_facts[1] if (not force and contiguous and _committed_facts
                                                     and _committed_facts[0] == fact_stamp) else None
                stages['snapshotReadMs'] = round((time.monotonic()-read_started)*1000, 1)
                fact_started = time.monotonic()
                facts = await ProjectionFacts.capture(reader, prior_facts, changes)
                stages['factReadMs'] = round((time.monotonic()-fact_started)*1000, 1)
                binding = _building_facts.set(facts)
                timing_binding = _building_timings.set(stages)
                try:
                    with bind_previous_quotes(previous):
                        payload, feed = await _build_payload(reader)
                finally:
                    _building_facts.reset(binding)
                    _building_timings.reset(timing_binding)
            else:
                stages['snapshotReadMs'] = round((time.monotonic()-read_started)*1000, 1)
        # JSON comparison/encoding and full catalogue traversal never hold the
        # sole writer. Live collectors may commit throughout this work.
        if not tape_only:
            began = time.monotonic()
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
            stages['deltaMs'] = round((time.monotonic()-began)*1000, 1)
            began = time.monotonic()
            overview, market = overview_dashboard(payload), market_dashboard(payload)
            if changed:
                base_revision = old_row['revision'] if old_row else 0
                delta['views'] = {
                    'overview': view_delta(previous_views.get('overview'),
                                           overview, 'overview', base_revision, revision),
                    'market': view_delta(previous_views.get('market'),
                                         market, 'market', base_revision, revision),
                }
            stages['pageViewsMs'] = round((time.monotonic()-began)*1000, 1)
            began = time.monotonic()
            bodies = {
                'full': _encode_snapshot(_dump(payload)),
                'overview': _encode_snapshot(_dump(overview)),
                'market': _encode_snapshot(_dump(market)),
                'feed': _encode_snapshot(_dump(feed)),
            }
            affected = _derived_dependencies(previous, payload, changes)
            stages['encodeMs'] = round((time.monotonic()-began)*1000, 1)
        write_started = time.monotonic()
        async with scoped._guard_write():
            stages['writerWaitMs'] = round((time.monotonic()-write_started)*1000, 1)
            commit_started = time.monotonic()
            await db.execute('BEGIN IMMEDIATE')
            current = await scoped.fetchone(columns)
            # A bootstrap in another API process can publish during our read.
            # Retry without consuming input rather than overwrite a new base.
            if (tuple(current) if current else None) != (tuple(old_row) if old_row else None):
                await db.rollback()
                return {'changed': False, 'retry': True,
                        'revision': current['revision'] if current else 0,
                        'cursor': current['cursor'] if current else 0,
                        'durationMs': round((time.monotonic()-started)*1000, 1), 'stagesMs': stages}
            if tape_only:
                await db.execute("UPDATE dashboard_projection SET input_cursor=? WHERE name IN ('full','overview','market','feed')", (high,))
                await db.commit()
                if _committed_facts and _committed_facts[0] == (scoped.path, id(scoped.db), old_input):
                    _committed_facts = ((scoped.path, id(scoped.db), high), _committed_facts[1])
                stages['commitMs'] = round((time.monotonic()-commit_started)*1000, 1)
                return {'changed': False, 'revision': old_row['revision'], 'cursor': old_row['cursor'],
                        'inputCursor': high, 'durationMs': round((time.monotonic()-started)*1000, 1),
                        'stagesMs': stages}
            sequence = await enqueue_event(db, 'projection.delta', delta) if changed else read_cursor
            await _queue_derived(db, affected, high)
            await db.executemany('''INSERT INTO dashboard_projection VALUES (?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,cursor=excluded.cursor,
                input_cursor=excluded.input_cursor,body=excluded.body,built_at=excluded.built_at''',
                [(name, revision, read_cursor, high, body, now) for name, body in bodies.items()])
            # Maintenance reads this committed input watermark separately;
            # an old backlog must never hold the publication's writer gate.
            await db.commit()
            stages['commitMs'] = round((time.monotonic()-commit_started)*1000, 1)
            _committed_projection = ((scoped.path, revision, read_cursor, now), payload)
            _committed_page_views = ((scoped.path, revision, read_cursor, now), {'overview': overview, 'market': market})
            _committed_facts = ((scoped.path, id(scoped.db), high), facts)
        return {'changed': changed, 'revision': revision, 'cursor': read_cursor, 'eventCursor': sequence,
                'inputCursor': high, 'durationMs': round((time.monotonic()-started)*1000, 1),
                'stagesMs': stages}

class ProjectionUnavailable(RuntimeError):
    pass


def _query_coordinator():
    global _query_work
    loop = asyncio.get_running_loop()
    if _query_work is None or _query_work.loop is not loop:
        _query_work = QueryWork()
    return _query_work


def _bounded_query(function):
    @wraps(function)
    async def call(*args, **kwargs):
        try:
            async with _query_coordinator().admit():
                return await function(*args, **kwargs)
        except QueryBusy as exc:
            raise ProjectionUnavailable(str(exc)) from exc
    return call


async def run_projection_query(function, *args, lane='default'):
    """Limit threaded decode/parse/format jobs, including disconnected viewers."""
    try:
        return await _query_coordinator().cpu(function, *args, lane=lane)
    except QueryBusy as exc:
        raise ProjectionUnavailable(str(exc)) from exc


async def run_projection_read(action, *args, **kwargs):
    """Bound a complete async read, including waiting for query-specific locks."""
    return await _bounded_query(action)(*args, **kwargs)


def _retain_snapshot(view, body):
    global _serialized_projection
    if body.serialized_bytes > QUERY_CACHE_BYTES:
        raise ProjectionUnavailable('projection-cache-budget-exceeded')
    if not isinstance(_serialized_projection, OrderedDict):
        _serialized_projection = OrderedDict(_serialized_projection or {})
    _serialized_projection.pop(view, None)
    while _serialized_projection and sum(value[1].serialized_bytes for value in _serialized_projection.values()) + body.serialized_bytes > QUERY_CACHE_BYTES:
        _serialized_projection.popitem(last=False)
        _query_coordinator().stats['evicted'] += 1
    _serialized_projection[view] = (body.stamp, body)


def _prepare_query_snapshot(stored, stamp, view, current):
    body = _decode_snapshot(stored, QUERY_DECODED_LIMIT)
    if not current:
        payload = json.loads(body)
        body = _dump(overview_dashboard(payload) if view == 'overview' else market_dashboard(payload))
    result = SnapshotBody(body, stamp)
    if len(result.wire) > QUERY_DECODED_LIMIT:
        raise ProjectionUnavailable('projection-snapshot-too-large')
    return result


async def _read_query_snapshot(scoped, identity, view):
    import aiosqlite
    coordinator = _query_coordinator()
    async with coordinator.read_slots:
        coordinator.stats['activeConnections'] += 1
        coordinator.stats['peakConnections'] = max(coordinator.stats['peakConnections'], coordinator.stats['activeConnections'])
        try:
            async with async_connect(scoped.path, readonly=True, timeout=15) as reader:
                await reader.execute('PRAGMA query_only=ON')
                await reader.execute('BEGIN')
                full = await (await reader.execute("SELECT revision,cursor,input_cursor,built_at FROM dashboard_projection WHERE name='full'")).fetchone()
                if full is None:
                    raise ProjectionUnavailable(f'projection view unavailable: {view}')
                selected = full if view == 'full' else await (await reader.execute(
                    'SELECT revision,cursor,input_cursor,built_at FROM dashboard_projection WHERE name=?', (view,))).fetchone()
                current = selected is not None and tuple(selected) == tuple(full)
                if view == 'feed' and not current:
                    raise ProjectionUnavailable('projection view unavailable: feed')
                # input_cursor alone is tape bookkeeping. Body publication is
                # identified by revision/cursor/built_at and the chosen source.
                stamp = (*identity, view, full[0], full[1], full[3], current,
                         *((selected[0], selected[1], selected[3]) if selected else ()))
                cache = _serialized_projection or {}
                cached = cache.get(view)
                if cached and cached[0] == stamp:
                    if isinstance(cache, OrderedDict):
                        cache.move_to_end(view)
                    coordinator.stats['cacheHits'] += 1
                    return cached[1]
                source = view if current else 'full'
                row = await (await reader.execute('SELECT body FROM dashboard_projection WHERE name=?', (source,))).fetchone()
                if row is None:
                    raise ProjectionUnavailable(f'projection view unavailable: {view}')
        except aiosqlite.DatabaseError as exc:
            code = getattr(exc, 'sqlite_errorcode', 0) & 255
            reason = 'projection-database-busy' if code in (5, 6) else 'projection-database-unavailable'
            raise ProjectionUnavailable(reason) from exc
        finally:
            coordinator.stats['activeConnections'] -= 1
    # Release the SQLite snapshot before decompressing/parsing large JSON.
    try:
        body = await run_projection_query(_prepare_query_snapshot, row[0], stamp, view, current,
                                          lane='feed' if view == 'feed' else 'default')
    except (ValueError, UnicodeError, zlib.error) as exc:
        raise ProjectionUnavailable('projection-snapshot-invalid') from exc
    coordinator.stats['decoded'] += 1
    _retain_snapshot(view, body)
    return body


@_bounded_query
async def read_projection_json(view='full'):
    """Verify a committed publication, merging only overlapping view reads."""
    if view not in ('full', 'overview', 'market', 'feed'):
        raise ValueError(f'unsupported projection view: {view}')
    from .read_model_store import settings, shared_manifest, shared_body, publication_stamp
    shared_config = settings()
    if shared_config.get('enabled') and shared_config.get('dsn'):
        async def shared_read():
            # Merge only overlapping validations against these settings. Once
            # complete, the next request reads the current manifest again.
            manifest = await _query_coordinator().read(
                ('shared-read-model-manifest', id(shared_config)),
                lambda: run_projection_query(shared_manifest, shared_config, lane='manifest'))
            if not manifest:
                return None
            stamp = publication_stamp(manifest, view)
            cached = (_serialized_projection or {}).get(view)
            if cached and cached[0] == stamp:
                cached[1].shared_verified_at = manifest['checkedAtMs']
                _query_coordinator().stats['cacheHits'] += 1
                return cached[1]
            async def refresh():
                lane = 'feed' if view == 'feed' else 'default'
                raw = await run_projection_query(shared_body, shared_config, manifest, view, lane=lane)
                if raw is None:
                    return None
                body = await run_projection_query(_prepare_query_snapshot, raw, stamp, view, True, lane=lane)
                _query_coordinator().stats['decoded'] += 1
                body.shared_verified_at = manifest['checkedAtMs']
                # Prepare the graph before replacing a view already used by
                # paged endpoints. No request inherits a half-prepared graph.
                if cached and cached[1].payload is not None:
                    await _parsed_body(body, view)
                from .read_model_store import valid_manifest
                if not valid_manifest(shared_config, manifest):
                    return None
                _retain_snapshot(view, body)
                return body
            task = _shared_refreshes.get(view)
            if task is None or task.done():
                task = _query_coordinator().track(asyncio.create_task(refresh(), name='shared-publication-refresh'))
                task.refresh_started = time.monotonic()
                _shared_refreshes[view] = task
                def finished(done):
                    if _shared_refreshes.get(view) is done:
                        _shared_refreshes.pop(view, None)
                task.add_done_callback(finished)
            previous_stamp = cached[0] if cached else ()
            verified = getattr(cached[1], 'shared_verified_at', 0) if cached else 0
            now_ms = int(time.time()*1000)
            # A recently verified old publication can finish serving while the
            # next one is prepared. Preserve its own source times and cursor.
            # No grace across source resets, regressions, expired verification
            # or more than three seconds of preparation. At most four jobs.
            can_finish_previous = (len(previous_stamp) == 8
                and previous_stamp[0] == 'shared-read-model'
                and previous_stamp[1] == stamp[1]
                and all(a >= b for a,b in zip(stamp[4:7], previous_stamp[4:7]))
                and 0 <= now_ms-verified <= shared_config.get('maxVerifiedAgeMs', 6000)
                and time.monotonic()-task.refresh_started < 3)
            if can_finish_previous:
                stats = _query_coordinator().stats
                stats['previousPublicationsServed'] = stats.get('previousPublicationsServed', 0)+1
                return cached[1]
            return await asyncio.shield(task)
        shared = await _query_coordinator().read(('shared-read-model', view), shared_read)
        if shared is not None:
            return shared
    scoped = await store('196')
    try:
        stat = os.stat(scoped.path)
        identity = (scoped.path, id(scoped.db), stat.st_dev, stat.st_ino)
    except OSError as exc:
        raise ProjectionUnavailable('projection-database-unavailable') from exc
    try:
        return await _query_coordinator().read((*identity, view),
            lambda: _read_query_snapshot(scoped, identity, view))
    except QueryBusy as exc:
        raise ProjectionUnavailable(str(exc)) from exc


@_bounded_query
async def projection_response_body(body, view='full'):
    # Plain strings remain supported for old in-process callers and rolling
    # integrations. Production reads return stamped JSON bytes.
    if isinstance(body, SnapshotBody):
        return body
    prepared = await run_projection_query(SnapshotBody, body, ('legacy', view),
                                         lane='feed' if view == 'feed' else 'default')
    if len(prepared.wire) > QUERY_DECODED_LIMIT:
        raise ProjectionUnavailable('projection-snapshot-too-large')
    cached = (_serialized_projection or {}).get(view)
    if cached and cached[0] == ('legacy', view, prepared.etag):
        return cached[1]
    prepared.stamp = ('legacy', view, prepared.etag)
    _retain_snapshot(view, prepared)
    return prepared


async def _parsed_body(body, view='full'):
    if body.payload is None:
        if body.parse_task is None:
            async def parse():
                try:
                    payload = await run_projection_query(_load_query_json, body,
                                                         lane='feed' if view == 'feed' else 'default')
                    if not isinstance(payload, dict):
                        raise ProjectionUnavailable('projection-snapshot-invalid')
                    body.payload = SnapshotPayload(payload, body.stamp)
                    _query_coordinator().stats['parsed'] += 1
                    return body.payload
                except (ValueError, UnicodeError) as exc:
                    raise ProjectionUnavailable('projection-snapshot-invalid') from exc
                finally:
                    body.parse_task = None
            body.parse_task = _query_coordinator().track(asyncio.create_task(parse(), name='projection-query-parse'))
        return await asyncio.shield(body.parse_task)
    return body.payload


@_bounded_query
async def read_projection_payload(view='full', *, json_reader=None):
    """Shared read-only graph; response builders must copy fields they change."""
    body = await (json_reader or read_projection_json)(view)
    return await _parsed_body(await projection_response_body(body, view), view)


def _token_indexes(payload):
    unified = payload.get('unified') or {}
    assets = {(str(r.get('chainId')), str(r.get('token') or '').lower()): r for r in unified.get('assets', [])}
    stocks = {(str(r.get('chainId')), str(r.get('tokenContractAddress') or '').lower()): r for r in unified.get('stockTokens', [])}
    relations = {}
    for row in unified.get('relations', []):
        for address in {row.get('token'), row.get('stock')} - {None}:
            relations.setdefault((str(row.get('chainId')), str(address).lower()), []).append(row)
    return assets, stocks, relations


@_bounded_query
async def read_token_projection(chain, token):
    body = await projection_response_body(await read_projection_json('full'), 'full')
    payload = await _parsed_body(body)
    if body.token_index is None:
        if body.index_task is None:
            async def index():
                try:
                    body.token_index = await run_projection_query(_token_indexes, payload)
                    _query_coordinator().stats['indexed'] += 1
                    return body.token_index
                finally:
                    body.index_task = None
            body.index_task = _query_coordinator().track(asyncio.create_task(index(), name='projection-query-index'))
        indexes = await asyncio.shield(body.index_task)
    else:
        indexes = body.token_index
    assets, stocks, relations = indexes
    identity = (str(chain), token.lower())
    return {'asset': assets.get(identity), 'stock': stocks.get(identity), 'relations': relations.get(identity, []),
            'snapshotAt': payload.get('now'), 'realtime': payload.get('realtime')}


@_bounded_query
async def read_projection():
    # Legacy producers may annotate their input. Preserve their private object
    # contract while product_v2/token consumers reuse the read-only graph.
    return await run_projection_query(copy.deepcopy, await read_projection_payload())


def projection_query_health():
    from .read_model_store import read_model_health
    coordinator = _query_work
    cache = _serialized_projection or {}
    return {'limits': {'connections': 2, 'workers': 2,
                       'workerLanes': dict(coordinator.worker_limits) if coordinator else {'default': 2, 'manifest': 1, 'feed': 1},
                       'totalWorkers': sum(coordinator.worker_limits.values()) if coordinator else 4,
                       'readers': 64, 'jobs': 32,
                       'decodedBytes': QUERY_DECODED_LIMIT, 'serializedCacheBytes': QUERY_CACHE_BYTES},
            'stats': dict(coordinator.stats) if coordinator else {},
            'sharedReadModel': read_model_health(),
            'waiters': coordinator.waiters if coordinator else 0,
            'inflightReads': len(coordinator.inflight) if coordinator else 0,
            'queuedOrRunningWork': len(coordinator.jobs) if coordinator else 0,
            'serializedBytes': sum(body.serialized_bytes for _, body in cache.values()),
            'views': {view: {'wireBytes': len(body.wire), 'serializedBytes': body.serialized_bytes,
                             'parsed': body.payload is not None, 'indexed': body.token_index is not None}
                      for view, (_, body) in cache.items()}}


async def stop_projection_queries():
    global _query_work, _serialized_projection
    if _query_work is not None:
        await _query_work.close()
    _query_work = None
    _serialized_projection = None
    _shared_refreshes.clear()
    from .read_model_store import close_cache
    await asyncio.to_thread(close_cache)


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
