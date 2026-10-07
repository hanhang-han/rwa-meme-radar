"""Independent storage domain for the bounded, native live-market lane.

The live worker owns this domain; directory discovery and dashboard projection
keep the research domain. SQLite files and PostgreSQL namespaces use separate
connections and transaction locks.  Catalogue handles are read-only and never
run the research store's startup DDL.  There is deliberately no automatic
history/coverage migration: copying a bounded trade window together with an
old accumulator can silently undercount its next candle update.
"""
import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .storage_runtime import async_connect

from . import db as research_db
from .db import ResearchStore, WriterLock


DEFAULT_LIVE_MARKET_DB = 'data/live-market.sqlite'
LIVE_BUSY_TIMEOUT_MS = 250
NATIVE_BATCH_ROWS = 32
NATIVE_BATCH_BYTES = 256 * 1024
RAW_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_stream_logs (
    chain TEXT NOT NULL, id TEXT NOT NULL, pool TEXT NOT NULL,
    block INTEGER NOT NULL, hash TEXT NOT NULL, at INTEGER NOT NULL,
    body TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(chain,id));
CREATE INDEX IF NOT EXISTS chain_stream_logs_block ON chain_stream_logs(chain,block);
CREATE INDEX IF NOT EXISTS chain_stream_logs_retention ON chain_stream_logs(chain,at,processed);
CREATE INDEX IF NOT EXISTS candles_unconfirmed ON candles(confirmed,openTime);
"""

_market_stores = {}
_catalogue_stores = {}
_writer_locks = {}
_market_init_lock = asyncio.Lock()
_catalogue_init_lock = asyncio.Lock()


class _NativeBatchCursor:
    def __init__(self, cursor, rowcount):
        self._cursor, self.rowcount = cursor, rowcount
        self.lastrowid = cursor.lastrowid if cursor is not None else None

    async def close(self):
        if self._cursor is not None:
            await self._cursor.close()


class NativeMarketBatchConnection:
    """Coalesce only native trade writes inside their existing transaction.

    This deliberately uses the storage adapter's execute path, including its
    type conversion, writer lock, migration provenance and statement rollback.
    It never starts/commits a transaction or combines separate trades. Repeated
    UPSERT keys use the original sequential path because PostgreSQL forbids
    updating one key twice in a multi-VALUES statement.
    """

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    @staticmethod
    def _spec(statement):
        from .collectors.chain_stream import FACT_UPSERT, CANDLE_UPSERT
        clean = ' '.join(statement.split())
        for sql, width, key_width in (
            (FACT_UPSERT, 3, 2), (CANDLE_UPSERT, 10, 3),
            ('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)', 3, 0),
        ):
            if clean == ' '.join(sql.split()):
                match = re.search(r'\bVALUES\s+(\([?,]+\))', clean)
                return clean[:match.start(1)], clean[match.end(1):], width, key_width
        return None

    @staticmethod
    def payload_bytes(row):
        # Include a small per-bind wire overhead, and measure UTF-8 rather than
        # Python character counts. An oversized single row keeps the original
        # path; we never merge it with another row past this limit.
        return sum(4 + (len(value.encode('utf-8')) if isinstance(value, str)
                       else len(value) if isinstance(value, (bytes, bytearray, memoryview))
                       else 1 if value is None else 8) for value in row)

    async def executemany(self, statement, parameters):
        if (getattr(self.connection, 'backend', 'sqlite') != 'postgres'
                or not self.connection.in_transaction):
            return await self.connection.executemany(statement, parameters)
        spec = self._spec(statement)
        if spec is None:
            return await self.connection.executemany(statement, parameters)
        rows = [tuple(row) for row in parameters]
        prefix, suffix, width, key_width = spec
        if (any(len(row) != width for row in rows)
                or (key_width and len({row[:key_width] for row in rows}) != len(rows))):
            return await self.connection.executemany(statement, rows)
        placeholder = '(' + ','.join('?' for _ in range(width)) + ')'
        batch, size, changed, last = [], 0, 0, None

        async def flush():
            nonlocal batch, size, changed, last
            if batch:
                last = await self.connection.execute(
                    prefix + ','.join(placeholder for _ in batch) + suffix,
                    tuple(value for row in batch for value in row))
                changed += max(0, last.rowcount)
                batch, size = [], 0

        for row in rows:
            row_size = self.payload_bytes(row)
            if batch and (len(batch) >= NATIVE_BATCH_ROWS or size + row_size > NATIVE_BATCH_BYTES):
                await flush()
            if row_size > NATIVE_BATCH_BYTES:
                last = await self.connection.executemany(statement, [row])
                changed += max(0, last.rowcount)
            else:
                batch.append(row)
                size += row_size
        await flush()
        return _NativeBatchCursor(last, changed)


def _physical_path(value: str) -> str:
    if value == ':memory:' or value.startswith('file:'):
        raise ValueError('live market storage requires an ordinary persistent file path')
    return str(Path(value).expanduser().resolve())


def catalogue_db_path() -> str:
    """Use the configured research path, without creating it."""
    return _physical_path(research_db.DB_PATH)


def market_db_path() -> str:
    """Reject a path/symlink/hardlink that would undo physical isolation."""
    path = _physical_path(os.environ.get('LIVE_MARKET_DB') or DEFAULT_LIVE_MARKET_DB)
    original = catalogue_db_path()
    if path == original or (os.path.exists(path) and os.path.exists(original)
                            and os.path.samefile(path, original)):
        raise ValueError('LIVE_MARKET_DB must be physically separate from RESEARCH_DB')
    return path


def demand_lease_path() -> str:
    """All API and live-worker demand hints use the original shared sidecar.

    Call configure_demand_leases before using a live store with demand_leases;
    its existing path helper otherwise derives a different file from store.path.
    """
    override = os.environ.get('DEMAND_LEASE_DB')
    return _physical_path(override) if override else catalogue_db_path() + '.leases.sqlite'


def configure_demand_leases() -> str:
    """Pin this process to the same sidecar as the directory/API process."""
    path = demand_lease_path()
    os.environ['DEMAND_LEASE_DB'] = path
    return path


class LiveMarketStore(ResearchStore):
    """Research table protocol, with its own physical file and writer lock."""

    realtime_outbox = False

    async def connect(self):
        try:
            await super().connect()
            if getattr(self.db, 'backend', 'sqlite') == 'postgres':
                from .storage_schema import verify_tables
                await verify_tables(self.db, ('chain_stream_logs', 'live_market_identity'))
                return self
            async with self._guard_write():
                await self.db.executescript(RAW_LOG_SCHEMA)
                # This lane publishes explicit transaction-bound market
                # events. No dashboard projection consumes its change_outbox;
                # retaining the research triggers would grow unused evidence
                # on every trade/candle mutation. Only this isolated file is
                # changed, never the catalogue schema.
                triggers = await self.fetchall(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'realtime_%'")
                for row in triggers:
                    name = row[0].replace('"', '""')
                    await self.db.execute('DROP TRIGGER IF EXISTS "' + name + '"')
                await self.db.commit()
            return self
        except BaseException:
            await self.close()
            raise


class CatalogueReadStore(ResearchStore):
    """Existing catalogue reads only; no schema setup or write operations."""

    async def connect(self):
        connection = async_connect(Path(self.path).as_uri() + '?mode=ro',
                                      readonly=True, uri=True, timeout=LIVE_BUSY_TIMEOUT_MS / 1000)
        self.db = connection
        try:
            await connection
            connection.row_factory = aiosqlite.Row
            if getattr(connection, 'backend', 'sqlite') == 'postgres':
                from .storage_schema import verify_tables
                await verify_tables(connection, ('facts',))
            await connection.execute('PRAGMA query_only=ON')
            if getattr(connection, 'backend', 'sqlite') != 'postgres':
                await connection.execute(f'PRAGMA busy_timeout={LIVE_BUSY_TIMEOUT_MS}')
            return self
        except BaseException:
            await asyncio.shield(connection.close())
            self.db = None
            raise

    @asynccontextmanager
    async def _guard_write(self, connection=None):
        raise PermissionError('live-market catalogue access is read-only')
        yield  # Make this an async context manager, including the failure path.

    async def _checkpoint_connection(self):
        raise PermissionError('live-market catalogue access is read-only')


async def market_store(chain: str = '196') -> LiveMarketStore:
    """Single-flight scoped handle; chains share only this file's writer lock."""
    path = market_db_path()
    identity = (path, str(chain))
    scoped = _market_stores.get(identity)
    if scoped is not None and scoped.db is not None:
        return scoped
    async with _market_init_lock:
        scoped = _market_stores.get(identity)
        if scoped is None or scoped.db is None:
            lock = _writer_locks.setdefault(path, WriterLock())
            scoped = LiveMarketStore(path, str(chain), write_lock=lock,
                                     busy_timeout_ms=LIVE_BUSY_TIMEOUT_MS)
            await scoped.connect()
            _market_stores[identity] = scoped
        return scoped


async def catalogue_store(chain: str = '196') -> CatalogueReadStore:
    """Read the live catalogue; never call the normal writable store factory."""
    path = catalogue_db_path()
    identity = (path, str(chain))
    scoped = _catalogue_stores.get(identity)
    if scoped is not None and scoped.db is not None:
        return scoped
    async with _catalogue_init_lock:
        scoped = _catalogue_stores.get(identity)
        if scoped is None or scoped.db is None:
            scoped = CatalogueReadStore(path, str(chain), write_lock=WriterLock(),
                                        busy_timeout_ms=LIVE_BUSY_TIMEOUT_MS)
            await scoped.connect()
            _catalogue_stores[identity] = scoped
        return scoped


async def close_all() -> None:
    """After stopping users, close only this module's handles and reset caches."""
    global _market_init_lock, _catalogue_init_lock
    async with _market_init_lock:
        async with _catalogue_init_lock:
            handles = [*_market_stores.values(), *_catalogue_stores.values()]
            results = await asyncio.gather(*(scoped.close() for scoped in handles),
                                           return_exceptions=True)
            _market_stores.clear()
            _catalogue_stores.clear()
            _writer_locks.clear()
    _market_init_lock = asyncio.Lock()
    _catalogue_init_lock = asyncio.Lock()
    for result in results:
        if isinstance(result, BaseException):
            raise result
