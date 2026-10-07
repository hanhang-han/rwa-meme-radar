"""Short-lived page demand lives outside the market research database.

API reads publish coalesced hints to a small, persistent sidecar. Collectors
read that sidecar across processes. During a rolling deploy they also accept
the unexpired leases left in the research database for one lease lifetime.
"""
import asyncio
import json
import os
import tempfile
import time
import uuid
import weakref
from pathlib import Path

import aiosqlite

from .storage_runtime import async_connect, is_postgres_path


LEGACY_WINDOW_MS = 120_000
PRUNE_INTERVAL_MS = 60_000
SCHEMA = """
CREATE TABLE IF NOT EXISTS leases (
    kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
    expiresAt INTEGER NOT NULL, PRIMARY KEY(kind,id)
);
CREATE INDEX IF NOT EXISTS leases_expiry ON leases(expiresAt);
CREATE TABLE IF NOT EXISTS lease_meta (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""

_pending = {}
_task = None
_last_pruned = {}
_initialized = {}
_memory_paths = weakref.WeakKeyDictionary()


def lease_path(store) -> str:
    """Derive one shared sidecar from the research DB path in every process."""
    override = os.environ.get('DEMAND_LEASE_DB')
    if override:
        return override
    if store.path == ':memory:':
        # Memory-backed tests have no cross-process market database. Keep the
        # companion file in the OS temp directory, never in the repo cwd.
        if store not in _memory_paths:
            _memory_paths[store] = os.path.join(
                tempfile.gettempdir(), 'cliperx-lease-' + uuid.uuid4().hex + '.sqlite')
        return _memory_paths[store]
    return store.path + '.leases.sqlite'


def publish_lease(store, kind, key, body):
    global _task
    identity = (lease_path(store), store.key(kind), key)
    if identity not in _pending and len(_pending) >= 2000:
        return
    _pending[identity] = json.dumps(body, ensure_ascii=False)
    if _task is None or _task.done():
        _task = asyncio.create_task(_flush(), name='api-demand-leases')


async def _flush():
    while _pending:
        await asyncio.sleep(.1)
        batch = list(_pending.items())[:100]
        paths = {key[0] for key, _ in batch}
        for path in paths:
            selected = [(key, body) for key, body in batch if key[0] == path]
            try:
                directory = os.path.dirname(path)
                postgres = is_postgres_path(path)
                if directory and not postgres:
                    os.makedirs(directory, exist_ok=True)
                async with async_connect(path, timeout=.25) as connection:
                    stat = None if postgres else os.stat(path)
                    identity = ('postgres', str(Path(path).resolve())) if postgres else (stat.st_dev, stat.st_ino)
                    now = int(time.time() * 1000)
                    if _initialized.get(path) != identity:
                        if not postgres:
                            mode = (await connection.execute_fetchall('PRAGMA journal_mode'))[0][0]
                            if mode.lower() != 'wal':
                                await connection.execute('PRAGMA journal_mode=WAL')
                        await connection.executescript(SCHEMA)
                        await connection.execute(
                            "INSERT OR IGNORE INTO lease_meta VALUES ('createdAt', ?)", (now,))
                        await connection.commit()
                        _initialized[path] = identity
                    await connection.execute('BEGIN IMMEDIATE')
                    await connection.executemany(
                        'INSERT INTO leases VALUES (?,?,?,?) '
                        'ON CONFLICT(kind,id) DO UPDATE SET '
                        'body=excluded.body,expiresAt=excluded.expiresAt',
                        [(identity[1], identity[2], body,
                          int(json.loads(body).get('expiresAt') or 0))
                         for identity, body in selected],
                    )
                    prune = now - _last_pruned.get(path, 0) >= PRUNE_INTERVAL_MS
                    if prune:
                        await connection.execute('DELETE FROM leases WHERE expiresAt<?',
                                                 (now - LEGACY_WINDOW_MS,))
                    await connection.commit()
                if prune:
                    _last_pruned[path] = now
                for identity, body in selected:
                    if _pending.get(identity) == body:
                        _pending.pop(identity, None)
            except aiosqlite.OperationalError:
                # A hint may wait for another API process without stalling the
                # market response; the newest value remains queued.
                await asyncio.sleep(.5)


async def all_lease_kv(store, kind) -> list[tuple[str, dict]]:
    """Read sidecar leases, with a bounded rolling-deploy legacy overlap."""
    path = lease_path(store)
    sidecar = {}
    created_at = None
    if is_postgres_path(path) or os.path.isfile(path):
        try:
            async with async_connect(Path(path).absolute().as_uri() + '?mode=ro',
                                         uri=True, timeout=.25) as connection:
                rows = await connection.execute_fetchall(
                    'SELECT id,body FROM leases WHERE kind=?', (store.key(kind),))
                marker = await connection.execute_fetchall(
                    "SELECT value FROM lease_meta WHERE name='createdAt'")
            sidecar = {str(ident): json.loads(body) for ident, body in rows}
            created_at = int(marker[0][0]) if marker else None
        except (aiosqlite.OperationalError, OSError):
            # The writer may still be creating the schema, or an old worker
            # may be running during the cutover. Its leases remain readable.
            pass
    if created_at is None or int(time.time() * 1000) <= created_at + LEGACY_WINDOW_MS:
        legacy = await store.all_kv(kind)
        for ident, body in legacy:
            newer = sidecar.get(ident)
            if newer is None or (body.get('expiresAt') or 0) > (newer.get('expiresAt') or 0):
                sidecar[ident] = body
    return list(sidecar.items())


async def all_leases(store, kind) -> list[dict]:
    return [body for _, body in await all_lease_kv(store, kind)]


async def stop_lease_writer():
    global _task
    if _task:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
        _task = None
    _pending.clear()


async def flush_lease_writer():
    """Test/maintenance barrier; query handlers intentionally never wait."""
    if _task:
        await asyncio.shield(_task)
