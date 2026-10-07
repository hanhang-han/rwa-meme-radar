"""Native charts read and stream the isolated market journal, never the catalogue."""
import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..storage_runtime import async_connect
from ..collectors.market_streams import Market
from ..demand_leases import publish_lease
from ..live_market_store import market_store, market_db_path, catalogue_db_path
from .candles import BARS, observation_rows, pool_freshness

router = APIRouter()
ADDRESS = re.compile(r'^0x[0-9a-f]{40}$')
CHAINS = ('196', '56', '4663')
QUEUE_LIMIT = 64
REPLAY_LIMIT = 10000
SNAPSHOT_CONCURRENCY = 8
SNAPSHOT_INFLIGHT_LIMIT = 128
JOURNAL_BOUNDS_SQL = '''SELECT
    COALESCE((SELECT id FROM realtime_events ORDER BY id ASC LIMIT 1),0),
    COALESCE((SELECT id FROM realtime_events ORDER BY id DESC LIMIT 1),0)'''


def _enabled():
    return os.environ.get('LIVE_MARKET_ENABLED', 'true').lower() not in ('false', '0', 'off')


def _validate(chain, token, pool=None, bar='5m'):
    if chain not in CHAINS or not ADDRESS.fullmatch(token) or bar not in BARS:
        raise HTTPException(400, 'invalid market identity')
    if pool is not None and not ADDRESS.fullmatch(pool):
        raise HTTPException(400, 'invalid pool')


_epochs = {}
_epoch_lock = asyncio.Lock()
_snapshot_inflight = {}
_snapshot_slots = asyncio.Semaphore(SNAPSHOT_CONCURRENCY)
_snapshot_closing = False


async def stream_epoch(scoped):
    # A durable identity also survives worker/API restarts, unlike a random
    # process nonce. Replacing the database invalidates its cached identity.
    if getattr(scoped.db, 'backend', 'sqlite') == 'postgres':
        physical = (scoped.path, 'postgres', scoped.db.schema)
    else:
        stat = os.stat(scoped.path)
        physical = (scoped.path, stat.st_dev, stat.st_ino)
    if physical not in _epochs:
        async with _epoch_lock:
            if physical not in _epochs:
                async with scoped._guard_write():
                    if getattr(scoped.db, 'backend', 'sqlite') != 'postgres':
                        await scoped.db.execute('CREATE TABLE IF NOT EXISTS live_market_identity (name TEXT PRIMARY KEY,value TEXT NOT NULL)')
                    await scoped.db.execute("INSERT OR IGNORE INTO live_market_identity VALUES ('epoch',?)", (uuid.uuid4().hex,))
                    await scoped.db.commit()
                row = await scoped.fetchone("SELECT value FROM live_market_identity WHERE name='epoch'")
                _epochs[physical] = row[0]
    return _epochs[physical]


async def _snapshot(chain, token=None, pool=None, bar='5m', limit=500):
    # Merge only requests already in flight: every subsequent request still
    # gets a new transaction snapshot, with its own matching rows and cursor.
    # A timeout/disconnect cancels its waiter, never another user's DB read.
    if _snapshot_closing:
        raise HTTPException(503, 'live market query shutting down')
    key = (chain, token, pool, bar, limit)
    task = _snapshot_inflight.get(key)
    if task is None:
        if len(_snapshot_inflight) >= SNAPSHOT_INFLIGHT_LIMIT:
            raise HTTPException(503, 'live market query busy', headers={'Retry-After': '1'})

        async def read():
            async with _snapshot_slots:
                return await _snapshot_database(chain, token, pool, bar, limit)

        task = asyncio.create_task(read(), name='live-market-snapshot')
        _snapshot_inflight[key] = task

        def release(done):
            if _snapshot_inflight.get(key) is done:
                _snapshot_inflight.pop(key, None)
            # Retrieve failures when every original waiter disconnected;
            # shielded tasks otherwise report an unhandled exception.
            if not done.cancelled():
                done.exception()

        task.add_done_callback(release)
    return await asyncio.shield(task)


async def _snapshot_database(chain, token=None, pool=None, bar='5m', limit=500):
    scoped = await asyncio.wait_for(market_store(chain), 3)
    epoch = await stream_epoch(scoped)
    async with async_connect(Path(scoped.path).as_uri() + '?mode=ro', readonly=True, uri=True, timeout=.25) as db:
        await db.execute('PRAGMA query_only=ON')
        await db.execute('BEGIN')
        facts = await db.execute_fetchall('SELECT id,body FROM facts WHERE kind=?', (scoped.key('market-registry'),))
        selected_rows = await db.execute_fetchall("SELECT body FROM facts WHERE kind=? AND id='pools'", (scoped.key('live-selection'),))
        selected = json.loads(selected_rows[0][0]) if selected_rows else None
        selected_pools = {str(value).lower() for value in selected.get('poolIds', [])} if selected is not None else None
        selected_tokens = {str(value).lower() for value in selected.get('tokenIds', [])} if selected is not None else None
        bases_by_pool = selected.get('basesByPool') if selected is not None else None
        markets = []
        known_markets = []
        for _, encoded in facts:
            record = json.loads(encoded)
            definition = record.get('definition') or {}
            if (str(definition.get('chain_id')) != chain or definition.get('venue') != 'dex'
                    or not definition.get('pool_id') or token and str(definition.get('token')).lower() != token):
                continue
            try:
                market = Market(**definition)
            except (TypeError, ValueError):
                continue
            if pool and market.pool_id.lower() != pool:
                continue
            # Previously observed definitions can request a bounded wakeup,
            # but are not evidence of current selection or live coverage.
            if (market.kind == 'pool' and market.quote_currency
                    and ADDRESS.fullmatch(market.pool_id.lower())):
                known_markets.append(market.frame())
            if selected_pools is not None and market.pool_id.lower() not in selected_pools:
                continue
            if isinstance(bases_by_pool, dict):
                bases = bases_by_pool.get(market.pool_id.lower(), [])
                if market.token.lower() not in {str(value).lower() for value in bases}:
                    continue
            elif selected_tokens is not None and market.token.lower() not in selected_tokens:
                continue
            markets.append((market, record))
        health_rows = await db.execute_fetchall("SELECT id,body FROM facts WHERE kind=? AND id IN ('pools')",
                                                (scoped.key('chain-stream'),))
        cursor_rows = await db.execute_fetchall("SELECT body FROM facts WHERE kind=? AND id='pools'",
                                                (scoped.key('chain-stream-cursor'),))
        health = json.loads(health_rows[0][1]) if health_rows else {}
        scan = json.loads(cursor_rows[0][0]) if cursor_rows else {}
        # Separate LIMIT 1 primary-key seeks avoid the full journal scan
        # SQLite needs for MIN and MAX combined in one aggregate projection.
        journal = await db.execute_fetchall(JOURNAL_BOUNDS_SQL)
        floor, cursor = journal[0]
        result = []
        now = int(time.time()*1000)
        for market, record in markets:
            if token is None:
                result.append(market.frame())
                continue
            rows = await db.execute_fetchall('SELECT body FROM facts WHERE kind=? AND id=?',
                                             (scoped.key('candle-meta'), market.candle_key(bar)))
            meta = json.loads(rows[0][0]) if rows else {}
            state = pool_freshness(health, scan, meta, now)
            item = {**record, **meta, **market.frame(), **state, 'liveMarket': True,
                    'status': 'current' if not state['stale'] else 'stale',
                    'source': meta.get('source', 'Chain RPC'), 'bar': bar}
            if pool:
                candles = await db.execute_fetchall('''SELECT openTime,open,high,low,close,volume,volumeUsd,confirmed
                    FROM candles WHERE asset=? AND bar=? ORDER BY openTime DESC LIMIT ?''',
                    (scoped.key(market.storage), bar, limit))
                history = [dict(zip(('t','o','h','l','c','v','vu','confirmed'), row)) for row in reversed(candles)]
                item.update(rows=observation_rows(history, meta), storage=market.storage,
                            lastCandleAt=history[-1]['t'] if history else None,
                            historyStatus='observed', coverage='observed', timeZone='UTC',
                            at=meta.get('lastSuccessfulAt', 0)/1000, refreshIntervalMs=1000)
                if not history:
                    item.update(status='collecting', error='awaiting-observed-trades')
            result.append(item)
        await db.rollback()
    return {'markets': result, 'knownMarkets': known_markets, 'health': health, 'scan': scan, 'cursor': cursor,
            'floor': floor, 'epoch': epoch}


@router.get('/live-market/status')
async def get_status():
    if not _enabled():
        return {'liveMarket': False, 'enabled': False, 'status': 'disabled', 'chains': []}
    chains = []
    for chain in CHAINS:
        snapshot = await _snapshot(chain)
        chains.append({'chainId': chain, **snapshot['health'], 'markets': len(snapshot['markets']),
                       'cursor': snapshot['cursor'], 'epoch': snapshot['epoch']})
    return {'liveMarket': True, 'enabled': True, 'chains': chains}


@router.get('/live-market/markets/{chain}/{token}')
async def get_markets(chain: str, token: str, pool: str | None = None, bar: str = '5m'):
    token = token.lower()
    pool = pool.lower() if pool else None
    _validate(chain, token, pool, bar)
    if not _enabled():
        return {'liveMarket': False, 'enabled': False, 'markets': []}
    snapshot = await _snapshot(chain, token, bar=bar)
    known = snapshot.get('knownMarkets', [])
    if pool:
        requested = next((m for m in known if str(m.get('poolId')).lower() == pool), None)
    else:
        # Ordinary asset links have no pool. Prefer a currently selected
        # market, otherwise wake at most one previously observed real pool.
        selected = sorted(snapshot['markets'], key=lambda m: (-int(m.get('lastTradeAt') or 0), str(m.get('poolId'))))
        requested = selected[0] if selected else next(iter(sorted(known, key=lambda m: str(m.get('poolId')))), None)
    watch_pool = requested.get('poolId') if requested else None
    if not watch_pool and pool and await catalogue_pool_hint(chain, token, pool):
        watch_pool = pool
    if watch_pool:
        publish_market_watch(chain, token, watch_pool, bar)
    selected = any(str(m.get('poolId')).lower() == str(watch_pool).lower() for m in snapshot['markets']) if watch_pool else False
    return {'liveMarket': bool(snapshot['markets']), 'enabled': True, 'markets': snapshot['markets'],
            'watchRequested': bool(watch_pool), 'watchPool': watch_pool,
            'selectionStatus': 'selected' if selected else 'pending' if watch_pool else 'unregistered',
            'cursor': snapshot['cursor'], 'epoch': snapshot['epoch']}


async def catalogue_pool_hint(chain, token, pool):
    """Wake one observed pool/base before its first realtime registration.

    This primary-key read is a demand hint. Selection and live coverage
    still come exclusively from the isolated market collector.
    """
    try:
        async with async_connect(Path(catalogue_db_path()).as_uri()+'?mode=ro', readonly=True, uri=True, timeout=.25) as db:
            await db.execute('PRAGMA query_only=ON')
            rows = await db.execute_fetchall('SELECT body FROM facts WHERE kind=? AND id=?', (chain+':pool', pool))
    except (aiosqlite.Error, OSError):
        return False
    if not rows:
        return False
    try:
        row = json.loads(rows[0][0])
    except (ValueError, TypeError):
        return False
    if not isinstance(row, dict):
        return False
    sides = {str(row.get(k) or '').lower() for k in ('token0', 'token1')}
    return (str(row.get('pool') or '').lower() == pool and token in sides and len(sides) == 2
            and all(ADDRESS.fullmatch(side) for side in sides)
            and row.get('creationStatus') != 'orphaned' and row.get('verificationStatus') != 'reorged'
            and bool(row.get('checkedAt')) and ('v2' in str(row.get('protocol')).lower() or 'v3' in str(row.get('protocol')).lower()))


@router.get('/live-market/candles/{chain}/{token}')
async def get_candles(chain: str, token: str, pool: str = Query(...), bar: str = '5m',
                      limit: int = Query(500, ge=1, le=1000)):
    token, pool = token.lower(), pool.lower()
    _validate(chain, token, pool, bar)
    if not _enabled():
        return {'liveMarket': False, 'enabled': False, 'rows': [], 'status': 'disabled'}
    snapshot = await _snapshot(chain, token, pool, bar, limit)
    if not snapshot['markets']:
        return {'liveMarket': False, 'enabled': True, 'rows': [], 'status': 'unregistered'}
    item = snapshot['markets'][0]
    publish_market_watch(chain, token, pool, bar)
    return {**item, 'cursor': snapshot['cursor'], 'epoch': snapshot['epoch']}


def publish_market_watch(chain, token, pool, bar):
    # Non-blocking hints use the original sidecar, not the research file.
    # An open live subscription renews these too: a busy chart may safely
    # avoid another full candle snapshot for longer than the lease lifetime.
    descriptor = SimpleNamespace(path=catalogue_db_path(), key=lambda kind: f'{chain}:{kind}')
    key = f'dex:{chain}:{token}:{bar}:{pool}'
    publish_lease(descriptor, 'candle-watch', key, {'chain': chain, 'address': token,
                  'bar': bar, 'venue': 'dex', 'market': pool, 'pool': pool,
                  'expiresAt': int(time.time()*1000)+120000})


def _frame(event, data, ident=None):
    prefix = f'id: {ident}\n' if ident is not None else ''
    return (prefix + f'event: {event}\ndata: ' + json.dumps(data, separators=(',', ':')) + '\n\n').encode()


@dataclass(eq=False)
class Subscriber:
    chain: str
    token: str
    pool: str
    bar: str
    currency: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_LIMIT))
    closed: bool = False

    def matches(self, event, packet):
        return (event in ('candle', 'candle-reset') and str(packet.get('chainId')) == self.chain
                and str(packet.get('token', '')).lower() == self.token and packet.get('venue') == 'dex'
                and str(packet.get('poolId') or packet.get('pool') or '').lower() == self.pool
                and packet.get('bar') == self.bar and packet.get('priceCurrency') == self.currency)


class MarketHub:
    """One journal reader for all clients; filter before their bounded queues."""
    def __init__(self, path):
        self.path = path
        self.clients = set()
        self.task = None
        self.cursor = 0
        self.ready = asyncio.Event()

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name='isolated-market-fanout')

    async def run(self):
        async with async_connect(Path(self.path).as_uri() + '?mode=ro', readonly=True, uri=True, timeout=.25) as db:
            await db.execute('PRAGMA query_only=ON')
            row = await db.execute_fetchall('SELECT COALESCE(MAX(id),0) FROM realtime_events')
            self.cursor = row[0][0]
            self.ready.set()
            while True:
                rows = await db.execute_fetchall('SELECT id,event,body FROM realtime_events WHERE id>? ORDER BY id LIMIT 256', (self.cursor,))
                for seq, event, encoded in rows:
                    packet = json.loads(encoded)
                    self.cursor = seq
                    for subscriber in tuple(self.clients):
                        if subscriber.closed or not subscriber.matches(event, packet):
                            continue
                        frame = (seq, event, {**packet, 'liveMarket': True})
                        if subscriber.queue.full():
                            subscriber.closed = True
                            while not subscriber.queue.empty():
                                subscriber.queue.get_nowait()
                            subscriber.queue.put_nowait((seq, 'reset', {'reason': 'slow-consumer', 'cursor': seq, 'liveMarket': True}))
                        else:
                            subscriber.queue.put_nowait(frame)
                await asyncio.sleep(0 if len(rows) == 256 else .1)

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.clients.clear()


_hubs = {}


async def stop_market_hubs():
    global _epoch_lock, _snapshot_slots, _snapshot_closing
    _snapshot_closing = True
    await asyncio.gather(*(hub.stop() for hub in _hubs.values()))
    pending = list(_snapshot_inflight.values())
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    _snapshot_inflight.clear()
    _snapshot_slots = asyncio.Semaphore(SNAPSHOT_CONCURRENCY)
    _hubs.clear()
    _epochs.clear()
    _epoch_lock = asyncio.Lock()
    _snapshot_closing = False


async def market_frames(subscriber, after=None, epoch=None):
    scoped = await market_store(subscriber.chain)
    path = scoped.path
    current_epoch = await stream_epoch(scoped)
    hub = _hubs.setdefault(path, MarketHub(path))
    hub.start()
    await asyncio.wait_for(hub.ready.wait(), 3)
    hub.clients.add(subscriber)
    publish_market_watch(subscriber.chain, subscriber.token, subscriber.pool, subscriber.bar)
    renew_demand_at = time.monotonic() + 15
    try:
        async with async_connect(Path(path).as_uri() + '?mode=ro', readonly=True, uri=True, timeout=.25) as db:
            bounds = (await db.execute_fetchall(JOURNAL_BOUNDS_SQL))[0]
            floor, high = bounds
            last = high if after is None else after
            reason = ('epoch-changed' if epoch and epoch != current_epoch else
                      'cursor-expired' if floor and last < floor-1 else
                      'cursor-ahead' if last > high else None)
            yield _frame('hello', {'liveMarket': True, 'epoch': current_epoch, 'cursor': last,
                                  'chainId': subscriber.chain, 'token': subscriber.token,
                                  'poolId': subscriber.pool, 'bar': subscriber.bar}, last)
            if reason:
                yield _frame('reset', {'liveMarket': True, 'reason': reason, 'cursor': high, 'epoch': current_epoch}, high)
                return
            replay = await db.execute_fetchall('SELECT id,event,body FROM realtime_events WHERE id>? AND id<=? ORDER BY id LIMIT ?',
                                               (last, high, REPLAY_LIMIT+1))
            if len(replay) > REPLAY_LIMIT:
                yield _frame('reset', {'liveMarket': True, 'reason': 'replay-limit', 'cursor': high, 'epoch': current_epoch}, high)
                return
            for seq, event, encoded in replay:
                packet = json.loads(encoded)
                if subscriber.matches(event, packet):
                    yield _frame('market.reset' if event == 'candle-reset' else 'market.candle',
                                 {**packet, 'liveMarket': True}, seq)
            last = high
        while True:
            if time.monotonic() >= renew_demand_at:
                publish_market_watch(subscriber.chain, subscriber.token, subscriber.pool, subscriber.bar)
                renew_demand_at = time.monotonic() + 15
            try:
                seq, event, packet = await asyncio.wait_for(subscriber.queue.get(), 15)
            except asyncio.TimeoutError:
                if hub.task.done():
                    yield _frame('reset', {'liveMarket': True, 'reason': 'journal-unavailable', 'cursor': last}, last)
                    return
                last = max(last, hub.cursor)
                yield _frame('heartbeat', {'liveMarket': True, 'cursor': last, 'epoch': current_epoch}, last)
                continue
            if event == 'reset':
                yield _frame('reset', {**packet, 'epoch': current_epoch}, seq)
                return
            if seq <= last:
                continue
            last = seq
            yield _frame('market.reset' if event == 'candle-reset' else 'market.candle', packet, seq)
    finally:
        hub.clients.discard(subscriber)


@router.get('/live-market/stream')
async def live_stream(chain: str, token: str, pool: str, bar: str = '5m', after: int | None = None,
                      epoch: str | None = None, last_event_id: str | None = Header(None)):
    token, pool = token.lower(), pool.lower()
    _validate(chain, token, pool, bar)
    if not _enabled():
        raise HTTPException(404, 'live market disabled')
    # Never accept a bare Last-Event-ID from the research journal. A reconnect
    # must identify this independent realm, supplied by its hello/snapshot.
    if last_event_id and after is None and epoch:
        try:
            after = int(last_event_id)
        except ValueError:
            raise HTTPException(400, 'invalid cursor')
    if after is not None and after < 0:
        raise HTTPException(400, 'invalid cursor')
    snapshot = await _snapshot(chain, token, pool, bar, 1)
    if not snapshot['markets']:
        raise HTTPException(404, 'unregistered market')
    selected = snapshot['markets'][0]
    subscriber = Subscriber(chain, token, pool, bar, selected['priceCurrency'])
    return StreamingResponse(market_frames(subscriber, after, epoch), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
