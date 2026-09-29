"""Coalesced demand hints never block read-only market responses.

Uses a separate, short-timeout SQLite connection: waiting for the collector's
write lock must not block the query process's shared read connection.
"""
import asyncio
import json
import aiosqlite

_pending = {}
_task = None


def publish_lease(store, kind, key, body):
    global _task
    identity = (store.path, store.key(kind), key)
    if identity not in _pending and len(_pending) >= 2000:
        return
    _pending[identity] = json.dumps(body, ensure_ascii=False)
    if _task is None or _task.done():
        _task = asyncio.create_task(_flush(), name="api-demand-leases")


async def _flush():
    while _pending:
        await asyncio.sleep(.1)
        batch = list(_pending.items())[:100]
        paths = {key[0] for key, _ in batch}
        for path in paths:
            selected = [(key, body) for key, body in batch if key[0] == path]
            try:
                async with aiosqlite.connect(path, timeout=.25) as connection:
                    await connection.execute("BEGIN IMMEDIATE")
                    await connection.executemany("INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
                        [(key[1], key[2], body) for key, body in selected])
                    await connection.commit()
                for key, body in selected:
                    if _pending.get(key) == body:
                        _pending.pop(key, None)
            except aiosqlite.OperationalError:
                # A hint can be delayed, while the persisted market remains
                # readable. The next attempt retains the latest lease value.
                await asyncio.sleep(.5)


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
