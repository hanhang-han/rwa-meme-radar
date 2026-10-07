"""One durable event journal and one tailer per API process.

Collectors can append in their fact transaction with enqueue_event. Legacy
publishers remain supported, but all readers now share the same committed cursor.
"""
import asyncio
import json
import os
import sqlite3
import time
from collections import deque

from .realtime_schema import REALTIME_SCHEMA
from .storage_runtime import is_postgres_path, sync_connect

_clients: set = set()
_latest_price: dict = {}
_latest_stock: dict = {}
_db = None
_tailer = None
_last_cursor = 0
_last_pruned = 0.0
_publisher = None
_pruner = None
_pending = deque()
JOURNAL_MAX_AGE_MS = 3_600_000
JOURNAL_MAX_EVENTS = 100_000
JOURNAL_PRUNE_BATCH = 20_000


def _ledger():
    global _db
    if _db is None:
        from .db import DB_PATH
        path = os.environ.get('STREAM_LEDGER_PATH') or DB_PATH
        if not is_postgres_path(path):
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        postgres = is_postgres_path(path)
        _db = sync_connect(path, timeout=15, readonly=postgres)
        if not postgres:
            _db.execute('PRAGMA journal_mode=WAL')
            _db.executescript(REALTIME_SCHEMA)
            _db.commit()
    return _db


def _frame(event, data, seq=None):
    return _serialized_frame(event, json.dumps(data, ensure_ascii=False, separators=(',', ':')), seq)


def _serialized_frame(event, body, seq=None):
    """Frame the journal's already serialized JSON without decoding it again."""
    # Current publishers write single-line JSON. Keep old pretty-printed rows
    # compatible with the single data line expected by scoped frame filtering.
    if '\n' in body or '\r' in body:
        body = json.dumps(json.loads(body), ensure_ascii=False, separators=(',', ':'))
    prefix = f'id: {seq}\n' if seq is not None else ''
    return (prefix + f'event: {event}\ndata: {body}\n\n').encode()


def cursor():
    return _ledger().execute('SELECT COALESCE(MAX(id),0) FROM realtime_events').fetchone()[0]


def _fanout(seq, frame):
    for q in list(_clients):
        try:
            q.put_nowait((seq, frame))
        except asyncio.QueueFull:
            _clients.discard(q)
            while not q.empty():
                q.get_nowait()
            q.put_nowait(None)


def _journal_path():
    from .db import DB_PATH
    return os.environ.get('STREAM_LEDGER_PATH') or DB_PATH


def _append_legacy_batch(batch):
    # This function runs on a thread when called from an event loop. External
    # SQLite writers may hold WAL's write lock; never wait for it on the loop.
    path = _journal_path()
    if not is_postgres_path(path):
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    connection = sync_connect(path, timeout=15)
    try:
        if not is_postgres_path(path):
            connection.executescript(REALTIME_SCHEMA)
            connection.commit()
        connection.execute('BEGIN IMMEDIATE')
        result = []
        for event, body, now, key in batch:
            if key is not None:
                connection.execute('INSERT INTO realtime_latest VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET event=excluded.event,body=excluded.body,at=excluded.at',
                                   (key, event, body, now))
            seq = connection.execute('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)', (event, body, now)).lastrowid
            result.append((seq, event, body))
        connection.commit()
        return result
    finally:
        connection.close()


async def _publish_pending():
    from .db import _global_write_lock
    while _pending:
        await asyncio.sleep(.01)  # batch synchronous legacy publishers
        batch = [_pending.popleft() for _ in range(min(len(_pending), 1000))]
        try:
            async with _global_write_lock:
                rows = await asyncio.to_thread(_append_legacy_batch, batch)
            if _tailer is None or _tailer.done():
                for seq, event, body in rows:
                    _fanout(seq, _serialized_frame(event, body, seq))
        except asyncio.CancelledError:
            _pending.extendleft(reversed(batch))
            raise
        except Exception as exc:
            _pending.extendleft(reversed(batch))
            print('[stream] legacy publisher retry:', type(exc).__name__, flush=True)
            await asyncio.sleep(1)


def broadcast(event, data):
    """Nonblocking compatibility publisher; collectors should enqueue in-tx."""
    global _publisher
    if event == 'heartbeat':
        _fanout(None, _frame(event, data))
        return
    stable_key = None
    if event in ('price', 'stock-quote'):
        key = (str(data.get('chainId')), str(data.get('token')), data.get('venue', 'dex'), data.get('quoteType', 'dex'))
        target = _latest_price if event == 'price' else _latest_stock
        target[key] = data
        if len(target) > 6000:
            target.pop(next(iter(target)))
        stable_key = '|'.join(str(x) for x in key)
    item = (event, json.dumps(data, ensure_ascii=False, separators=(',', ':')), int(time.time()*1000), stable_key)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        for seq, name, body in _append_legacy_batch([item]):
            _fanout(seq, _serialized_frame(name, body, seq))
        return
    _pending.append(item)
    if _publisher is None or _publisher.done():
        _publisher = loop.create_task(_publish_pending(), name='legacy-event-publisher')


async def flush_legacy():
    """Shutdown/test barrier. Business collectors need not wait for this."""
    if _publisher is not None:
        await asyncio.shield(_publisher)


def clients():
    return _clients


def hello(q, sequence=None):
    q.put_nowait((None, _frame('hello', {'schema': 1, 'at': int(time.time()*1000), 'cursor': cursor() if sequence is None else sequence})))


def replay_page(last_id, upper=None, limit=500):
    """Bounded page; a large valid backlog is paginated, never discarded."""
    db = _ledger()
    # Separate extrema use primary-key seeks. SQLite cannot apply that shortcut
    # to a combined MIN/MAX aggregate and would scan the entire retained tape
    # for every 500-event page. The scalar subqueries share one read snapshot.
    lo, hi = db.execute('''SELECT
        (SELECT id FROM realtime_events ORDER BY id LIMIT 1),
        (SELECT id FROM realtime_events ORDER BY id DESC LIMIT 1)''').fetchone()
    upper = min(hi or 0, upper) if upper is not None else (hi or 0)
    if last_id < 0 or last_id > (hi or 0) or (lo and last_id < lo-1):
        return [_frame('reset', {'schema': 1, 'cursor': hi or 0, 'reason': 'cursor-expired'})], upper, True
    rows = db.execute('SELECT id,event,body FROM realtime_events WHERE id>? AND id<=? ORDER BY id LIMIT ?',
                      (last_id, upper, limit)).fetchall()
    frames = [_serialized_frame(event, body, seq) for seq, event, body in rows]
    reached = rows[-1][0] if rows else upper
    return frames, reached, reached >= upper


def replay_batch(last_id, include_latest=True):
    # Kept for legacy callers/tests. HTTP replay uses pages, so it does not need
    # to allocate an entire disconnected session's backlog.
    upper, out = cursor(), []
    if last_id is not None:
        while True:
            page, last_id, done = replay_page(last_id, upper)
            out.extend(page)
            if done:
                break
    if include_latest:
        rows = _ledger().execute('SELECT event,body FROM realtime_latest ORDER BY at DESC LIMIT 6000').fetchall()
        out.extend(_serialized_frame(event, body) for event, body in rows)
    return out, upper


def replay_after(last_id):
    return replay_batch(last_id)[0]


def _invalidate_reads(event):
    if event != 'projection.delta':
        return
    # Token detail uses a small in-process cache; it must not serve pre-event
    # relationships after an invalidate+fetch initiated by the browser.
    from . import state, stock_quotes
    state.invalidate()
    stock_quotes._cache.clear()


def _prune_journal(sequence):
    connection = sync_connect(_journal_path(), timeout=15)
    try:
        # Select only expired ids through covering indexes before reserving a
        # writer. The former OR predicate scanned every retained JSON body
        # under a write transaction, blocking ingestion even if none expired.
        rows = connection.execute('''SELECT id FROM realtime_events
            INDEXED BY realtime_events_time WHERE at<? AND id<? LIMIT ?''',
            (int(time.time()*1000)-JOURNAL_MAX_AGE_MS, sequence, JOURNAL_PRUNE_BATCH)).fetchall()
        ids = {row[0] for row in rows}
        if len(ids) < JOURNAL_PRUNE_BATCH and sequence > JOURNAL_MAX_EVENTS:
            ids.update(row[0] for row in connection.execute(
                'SELECT id FROM realtime_events WHERE id<? ORDER BY id LIMIT ?',
                (sequence-JOURNAL_MAX_EVENTS, JOURNAL_PRUNE_BATCH-len(ids))).fetchall())
        # Bound each physical write transaction as well as the overall pass.
        ordered = sorted(ids)
        for offset in range(0, len(ordered), 250):
            connection.executemany('DELETE FROM realtime_events WHERE id=?',
                                   [(value,) for value in ordered[offset:offset+250]])
            connection.commit()
    finally:
        connection.close()


async def _prune(sequence):
    from .db import _global_write_lock
    try:
        async with _global_write_lock:
            await asyncio.to_thread(_prune_journal, sequence)
    except Exception as exc:
        print('[stream] prune retry next interval:', type(exc).__name__, flush=True)


def _tail_rows(after):
    return _ledger().execute(
        'SELECT id,event,body FROM realtime_events WHERE id>? ORDER BY id LIMIT 500', (after,)
    ).fetchall()


async def async_cursor():
    if is_postgres_path(_journal_path()):
        return await asyncio.to_thread(cursor)
    return cursor()


async def async_replay_page(last_id, upper=None, limit=500):
    if is_postgres_path(_journal_path()):
        return await asyncio.to_thread(replay_page, last_id, upper, limit)
    return replay_page(last_id, upper, limit)


async def async_replay_batch(last_id, include_latest=True):
    if is_postgres_path(_journal_path()):
        return await asyncio.to_thread(replay_batch, last_id, include_latest)
    return replay_batch(last_id, include_latest)


async def _tail():
    global _last_cursor, _last_pruned, _pruner
    while True:
        try:
            if is_postgres_path(_journal_path()):
                rows = await asyncio.to_thread(_tail_rows, _last_cursor)
            else:
                rows = _tail_rows(_last_cursor)
            for seq, event, body in rows:
                _invalidate_reads(event)
                _fanout(seq, _serialized_frame(event, body, seq))
                _last_cursor = seq
            if time.monotonic() - _last_pruned > 15 and (_pruner is None or _pruner.done()):
                _pruner = asyncio.create_task(_prune(_last_cursor), name='event-journal-retention')
                _last_pruned = time.monotonic()
            await asyncio.sleep(0 if len(rows) == 500 else .2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print('[stream] tailer retry:', type(exc).__name__, flush=True)
            await asyncio.sleep(1)


async def start_hub():
    global _tailer, _last_cursor
    if _tailer is None or _tailer.done():
        _last_cursor = await async_cursor()
        _tailer = asyncio.create_task(_tail(), name='realtime-event-tailer')


async def stop_hub():
    global _tailer, _db, _pruner, _publisher
    await flush_legacy()
    _publisher = None
    if _tailer:
        _tailer.cancel()
        await asyncio.gather(_tailer, return_exceptions=True)
        _tailer = None
    if _pruner:
        await asyncio.gather(_pruner, return_exceptions=True)
        _pruner = None
    for q in list(_clients):
        while not q.empty():
            q.get_nowait()
        q.put_nowait(None)
    _clients.clear()
    if _db:
        _db.close()
        _db = None
