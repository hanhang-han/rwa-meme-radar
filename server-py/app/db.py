"""aiosqlite port of the Node research-store: the same tables (facts, samples,
trades, events, candles) and the same scope-prefixed keys, so the existing
research.sqlite is read in place with no migration."""
import asyncio
import hashlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar

from .realtime_schema import REALTIME_SCHEMA, trigger_schema

import aiosqlite

DB_PATH = os.environ.get("RESEARCH_DB", "data/research.sqlite")
WAL_JOURNAL_SIZE_LIMIT_BYTES = 256 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS samples (asset TEXT NOT NULL, t INTEGER NOT NULL, price REAL NOT NULL, cap REAL, PRIMARY KEY(asset,t));
CREATE TABLE IF NOT EXISTS sample_evidence (asset TEXT NOT NULL,t INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(asset,t));
CREATE TABLE IF NOT EXISTS trades (asset TEXT NOT NULL, id TEXT NOT NULL, t INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(asset,id));
CREATE INDEX IF NOT EXISTS trades_time ON trades(asset,t);
CREATE INDEX IF NOT EXISTS trades_global_time ON trades(t);
CREATE INDEX IF NOT EXISTS trades_scope_time ON trades(substr(asset,1,instr(asset,':')-1),t);
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, asset TEXT NOT NULL, t INTEGER NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_time ON events(t);
CREATE TABLE IF NOT EXISTS candles (asset TEXT NOT NULL, bar TEXT NOT NULL, openTime INTEGER NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume REAL, volumeUsd REAL, confirmed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(asset,bar,openTime));
CREATE TABLE IF NOT EXISTS trade_coverage (asset TEXT NOT NULL, startTime INTEGER NOT NULL, endTime INTEGER NOT NULL,
  PRIMARY KEY(asset,startTime,endTime));
CREATE INDEX IF NOT EXISTS trade_coverage_time ON trade_coverage(asset,endTime);
CREATE TABLE IF NOT EXISTS trade_buckets (asset TEXT NOT NULL, bar TEXT NOT NULL, openTime INTEGER NOT NULL,
  buyCount INTEGER NOT NULL, sellCount INTEGER NOT NULL, tradeCount INTEGER NOT NULL,
  buyVolumeUsd REAL, sellVolumeUsd REAL, volumeUsd REAL, traders INTEGER NOT NULL,
  coverageRatio REAL NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0, updatedAt INTEGER NOT NULL,
  PRIMARY KEY(asset,bar,openTime));
CREATE INDEX IF NOT EXISTS trade_buckets_time ON trade_buckets(asset,bar,openTime);
CREATE TABLE IF NOT EXISTS comparison_samples (
  scope TEXT NOT NULL, subject TEXT NOT NULL, fingerprint TEXT NOT NULL,
  t INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(scope,subject,fingerprint));
CREATE INDEX IF NOT EXISTS comparison_samples_time ON comparison_samples(scope,subject,t);
"""

# All chain scopes share one SQLite file. A per-connection lock still allows
# 196/56/4663 writers in this process to collide with each other, especially
# when every scheduled task starts at boot. Serialize writes process-wide;
# WAL continues to allow concurrent readers and external processes use the
# SQLite busy timeout.
class WriterLock(asyncio.Lock):
    """An asyncio lock with bounded diagnostics, without database/body data."""

    def __init__(self):
        super().__init__()
        self.owner = None
        self.acquired_at = None

    async def acquire(self):
        acquired = await super().acquire()
        self.owner = asyncio.current_task()
        self.acquired_at = time.monotonic()
        return acquired

    def release(self):
        super().release()
        self.owner = None
        self.acquired_at = None

    def snapshot(self):
        task = self.owner
        stack = []
        coroutine = task.get_coro() if task else None
        seen = set()
        while coroutine is not None and id(coroutine) not in seen and len(stack) < 12:
            seen.add(id(coroutine))
            frame = getattr(coroutine, 'cr_frame', None) or getattr(coroutine, 'gi_frame', None)
            if frame is not None:
                stack.append({'file': frame.f_code.co_filename,
                              'function': frame.f_code.co_name, 'line': frame.f_lineno})
            coroutine = getattr(coroutine, 'cr_await', None) or getattr(coroutine, 'gi_yieldfrom', None)
        return {
            'locked': self.locked(),
            'owner': task.get_name() if task else None,
            'heldMs': round((time.monotonic()-self.acquired_at)*1000) if self.acquired_at else 0,
            'stack': stack,
        }


_global_write_lock = WriterLock()


def _is_transient_write_busy(exc: BaseException) -> bool:
    """Retry only contention with another writer, never a stale read snapshot."""
    if not isinstance(exc, aiosqlite.OperationalError):
        return False
    code = getattr(exc, 'sqlite_errorcode', None)
    if code is not None:
        return code == sqlite3.SQLITE_BUSY
    return 'database is locked' in str(exc).lower()


async def retry_busy_write(operation, attempts: int = 3):
    """Replay an idempotent write after its failed transaction was rolled back.

    The operation must acquire/release _guard_write itself. Backoff happens
    outside that process-wide lock so another local writer can make progress.
    """
    for attempt in range(attempts):
        try:
            return await operation()
        except aiosqlite.OperationalError as exc:
            if not _is_transient_write_busy(exc) or attempt == attempts - 1:
                raise
            await asyncio.sleep(0.05 * (attempt + 1))


def write_lock_snapshot():
    """Read-only diagnostics suitable for worker health (no keys or locals)."""
    result = _global_write_lock.snapshot()
    result['connections'] = {
        scope: {'inTransaction': bool(scoped.db.in_transaction)}
        for scope, scoped in _stores.items() if scoped.db is not None
    }
    return result


class ResearchStore:
    def __init__(self, path: str = DB_PATH, scope: str = "196"):
        self.path = path
        self.scope = scope
        self.db: aiosqlite.Connection | None = None
        self._write_lock = _global_write_lock

    @asynccontextmanager
    async def _guard_write(self):
        async with self._write_lock:
            try:
                yield
            except BaseException as exc:
                if isinstance(exc, aiosqlite.OperationalError) and 'locked' in str(exc).lower():
                    print('[research-db] locked ' + json.dumps({**write_lock_snapshot(),
                        'sqliteErrorCode': getattr(exc, 'sqlite_errorcode', None),
                        'sqliteErrorName': getattr(exc, 'sqlite_errorname', None)}), flush=True)
                # Cancellation during an implicit SQLite transaction must not
                # leave the connection holding a write lock during shutdown.
                if self.db:
                    await asyncio.shield(self.db.rollback())
                raise

    def key(self, value: str) -> str:
        return f"{self.scope}:{value}"

    async def connect(self) -> "ResearchStore":
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        try:
            # DDL is a writer too. It must participate in the exact same
            # process lock as collector transactions; otherwise a concurrent
            # connect can wait on a transaction whose task is waiting for DDL.
            async with self._guard_write():
                await self.db.execute("PRAGMA busy_timeout=15000")
                await self.db.execute("PRAGMA journal_mode=WAL")
                if self.path != ':memory:':
                    # Cap the reusable WAL file after SQLite's normal reset;
                    # this does not force a checkpoint or interrupt readers.
                    await self.fetchone(f"PRAGMA journal_size_limit={WAL_JOURNAL_SIZE_LIMIT_BYTES}")
                await self.db.executescript(SCHEMA + REALTIME_SCHEMA)
                version = hashlib.sha256(trigger_schema().encode()).hexdigest()
                existing = await self.fetchone("SELECT value FROM realtime_schema_version WHERE name='triggers'")
                if existing is None or existing[0] != version:
                    # No external writer can slip through a DROP/CREATE gap.
                    await self.db.executescript('BEGIN IMMEDIATE;\n' + trigger_schema(replace=True))
                    await self.db.execute("INSERT INTO realtime_schema_version VALUES ('triggers',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (version,))
                await self.db.commit()
        except BaseException:
            await self.db.close()
            self.db = None
            raise
        return self

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    async def get(self, kind: str, id: str):
        row = await self.fetchone(
            "SELECT body FROM facts WHERE kind=? AND id=?", (self.key(kind), id)
        )
        return json.loads(row[0]) if row else None

    async def fetchone(self, query, parameters=()):
        # Execute and drain in one aiosqlite thread job: no other coroutine
        # can upgrade a write on this connection while SELECT pins a snapshot.
        rows = await self.db.execute_fetchall(query, parameters)
        return rows[0] if rows else None

    async def fetchall(self, query, parameters=()):
        return await self.db.execute_fetchall(query, parameters)

    async def all(self, kind: str) -> list:
        rows = await self.fetchall("SELECT body FROM facts WHERE kind=?", (self.key(kind),))
        return [json.loads(r[0]) for r in rows]

    async def all_kv(self, kind: str) -> list[tuple[str, dict]]:
        rows = await self.fetchall("SELECT id,body FROM facts WHERE kind=?", (self.key(kind),))
        return [(str(r[0]), json.loads(r[1])) for r in rows]

    async def put(self, kind: str, id: str, value) -> None:
        row = (self.key(kind), id, json.dumps(value, ensure_ascii=False))
        async def write():
            async with self._guard_write():
                await self.db.execute(
                    "INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body", row
                )
                await self.db.commit()
        await retry_busy_write(write)

    async def patch_fact(self, kind: str, id: str, patch: dict) -> dict:
        """Merge selected fields atomically without rewriting unrelated data.

        Collectors often wait on the network between reading and writing.  A
        full-object put after that wait can restore an old price over a newer
        quote.  Patch writes re-read under a write transaction instead.
        """
        async with self._guard_write():
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                row = await self.fetchone(
                    "SELECT body FROM facts WHERE kind=? AND id=?", (self.key(kind), id)
                )
                value = json.loads(row[0]) if row else {}
                value.update(patch)
                await self.db.execute(
                    "INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
                    (self.key(kind), id, json.dumps(value, ensure_ascii=False)),
                )
                await self.db.commit()
                return value
            except BaseException:
                await self.db.rollback()
                raise

    async def merge_asset_observation(
        self, id: str, base_patch: dict, timed_fields: dict[str, tuple[object, float]],
        sample: tuple[float, float | None, float] | None = None,
    ) -> dict:
        """Atomically merge a quote and its optional five-minute sample.

        Each field is monotonic by source observation time, so a delayed
        response cannot overwrite a newer value from another collector.
        """
        async with self._guard_write():
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                row = await self.fetchone(
                    "SELECT body FROM facts WHERE kind=? AND id=?", (self.key("asset"), id)
                )
                value = json.loads(row[0]) if row else {}
                base_patch = dict(base_patch)
                previous_updated_at = float(value.get("updatedAt") or 0)
                price_metadata = {key: base_patch.pop(key) for key in (
                    "priceProvenance", "priceCurrency", "priceScope", "quoteAt", "quoteType",
                    "quoteStatus", "quoteReason", "venue", "provider") if key in base_patch}
                field_sources = base_patch.pop("fieldSources", {})
                maps = {key:base_patch.pop(key,{}) for key in ('fieldObservations','fieldScopes','fieldTimeKinds','fieldStatus')}
                # Per-field source clocks are authoritative; a convenience
                # patch must never reset them before the monotonic check.
                base_patch.pop("fieldTimes", None)
                value.update(base_patch)
                times = dict(value.get("fieldTimes") or {})
                sources = dict(value.get("fieldSources") or {})
                merged_maps = {key:dict(value.get(key) or {}) for key in maps}
                accepted = set()
                for field, (field_value, observed) in timed_fields.items():
                    if observed >= float(times.get(field) or 0):
                        value[field] = field_value
                        times[field] = observed
                        accepted.add(field)
                        if field in field_sources:
                            sources[field] = field_sources[field]
                        else:
                            sources.pop(field,None)
                        for key,fields in maps.items():
                            if field in fields:merged_maps[key][field]=fields[field]
                            else:merged_maps[key].pop(field,None)
                if "price" in accepted:
                    value.update(price_metadata)
                value["fieldTimes"] = times
                value["fieldSources"] = sources
                value.update(merged_maps)
                value["updatedAt"] = max(previous_updated_at, float(value.get("updatedAt") or 0), *(x[1] for x in timed_fields.values()), 0)
                await self.db.execute(
                    "INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
                    (self.key("asset"), id, json.dumps(value, ensure_ascii=False)),
                )
                if sample and "price" in accepted:
                    price, cap, observed = sample
                    bucket = int(observed // 300000 * 300000)
                    await self.db.execute(
                        """INSERT INTO samples(asset,t,price,cap) VALUES (?,?,?,?)
                           ON CONFLICT(asset,t) DO UPDATE SET price=excluded.price,cap=excluded.cap""",
                        (self.key(id), bucket, price, cap),
                    )
                    evidence = merged_maps['fieldObservations'].get('price')
                    if isinstance(evidence,dict):
                        await self.db.execute('INSERT INTO sample_evidence VALUES (?,?,?) ON CONFLICT(asset,t) DO UPDATE SET body=excluded.body',
                                              (self.key(id),bucket,json.dumps({**evidence,'currency':value.get('priceCurrency') or 'USD'})))
                    else:
                        await self.db.execute('DELETE FROM sample_evidence WHERE asset=? AND t=?',(self.key(id),bucket))
                await self.db.commit()
                return value
            except BaseException:
                await self.db.rollback()
                raise

    async def samples(self, asset: str, limit: int = 288) -> list:
        rows = await self.fetchall(
            "SELECT s.t,s.price,s.cap,e.body FROM samples s LEFT JOIN sample_evidence e ON e.asset=s.asset AND e.t=s.t WHERE s.asset=? ORDER BY s.t DESC LIMIT ?",
            (self.key(asset), limit),
        )
        return [{"t": r[0], "price": r[1], "cap": r[2],"provenance":json.loads(r[3]) if r[3] else None} for r in reversed(rows)]

    async def comparison_sample(self, subject: str, fingerprint: str, at: int, body: dict) -> None:
        await self.comparison_samples_batch([(subject, fingerprint, at, body)])

    async def comparison_samples_batch(self, rows: list) -> int:
        if not rows:
            return 0
        encoded = [(self.scope, subject, fingerprint, at, json.dumps(body, ensure_ascii=False, allow_nan=False))
                   for subject, fingerprint, at, body in rows]
        accepted = 0
        # Each INSERT is idempotent. Small committed chunks bound the time this
        # process owns the sole SQLite writer; an interrupted run can replay
        # already committed chunks without duplicating observations.
        for offset in range(0, len(encoded), 250):
            batch = encoded[offset:offset + 250]
            async def write():
                async with self._guard_write():
                    cursor = await self.db.executemany(
                        "INSERT OR IGNORE INTO comparison_samples VALUES (?,?,?,?,?)", batch)
                    await self.db.commit()
                    return max(0, cursor.rowcount)
            accepted += await retry_busy_write(write)
            if offset + 250 < len(encoded):
                await asyncio.sleep(0)
        return accepted

    async def comparison_baselines(self, subject: str, at: int, tolerance_ms: int = 300000) -> list:
        """Fetch bounded neighborhoods for 1h/24h baselines, not a whole day."""
        rows = []
        for width in (3600000,86400000):
            target = at-width
            observations = await self.fetchall(
                "SELECT body FROM comparison_samples WHERE scope=? AND subject=? AND t BETWEEN ? AND ? ORDER BY ABS(t-?) LIMIT 8",
                (self.scope,subject,target-tolerance_ms,target+tolerance_ms,target))
            rows.extend(json.loads(row[0]) for row in observations)
        return sorted(rows,key=lambda row:row.get('at') or row.get('t') or 0)

    async def comparison_observations_at(self, subject: str, at: int, tolerance_ms: int = 60000, limit: int = 8) -> list:
        rows = await self.fetchall(
            "SELECT body FROM comparison_samples WHERE scope=? AND subject=? AND t BETWEEN ? AND ? ORDER BY ABS(t-?) LIMIT ?",
            (self.scope,subject,at-tolerance_ms,at+tolerance_ms,at,max(1,min(limit,100))))
        return [json.loads(row[0]) for row in rows]

    async def prune_comparisons(self, before: int, batch: int = 5000) -> int:
        """Bound each maintenance transaction so collection is not starved."""
        async with self._guard_write():
            cur = await self.db.execute(
                "DELETE FROM comparison_samples WHERE rowid IN (SELECT rowid FROM comparison_samples WHERE scope=? AND t<? LIMIT ?)",
                (self.scope,before,batch))
            await self.db.commit()
            return cur.rowcount

    async def prune_market_trades(self, prefix: str, before: int, limit: int = 5000) -> int:
        """Find expired market rows using the covering asset/time index.

        LIKE with OR can choose a full table scan; doing that inside DELETE
        reserves the sole writer even when nothing has expired. Drain the
        indexed read first, then delete in small independent transactions.
        """
        lower = self.key(prefix)
        upper = lower[:-1] + chr(ord(lower[-1]) + 1)
        rows = await self.fetchall('''SELECT rowid FROM trades INDEXED BY trades_time
            WHERE asset>=? AND asset<? AND t<? LIMIT ?''',
            (lower, upper, before, max(1, min(limit, 10000))))
        for offset in range(0, len(rows), 250):
            async with self._guard_write():
                # Recheck time/range if a concurrent writer replaced a row
                # after selection. Newly observed trades cannot be removed.
                await self.db.executemany('''DELETE FROM trades
                    WHERE rowid=? AND asset>=? AND asset<? AND t<?''',
                    [(row[0], lower, upper, before) for row in rows[offset:offset+250]])
                await self.db.commit()
            await asyncio.sleep(0)
        return len(rows)

    async def comparison_history(self, subject: str, since: int = 0, limit: int = 12000) -> list:
        rows = await self.fetchall(
            "SELECT body FROM comparison_samples WHERE scope=? AND subject=? AND t>=? ORDER BY t DESC,rowid DESC LIMIT ?",
            (self.scope, subject, since, limit))
        return [json.loads(row[0]) for row in reversed(rows)]

    async def sample(self, asset: str, price: float, cap, now=None, metadata=None) -> None:
        if not (price > 0):
            return
        bucket = (now or time.time() * 1000) // 300000 * 300000
        async with self._guard_write():
            await self.db.execute(
                """INSERT INTO samples(asset,t,price,cap) VALUES (?,?,?,?)
                   ON CONFLICT(asset,t) DO UPDATE SET price=excluded.price,cap=excluded.cap""",
                (self.key(asset), int(bucket), price, cap),
            )
            if metadata:
                await self.db.execute('INSERT INTO sample_evidence VALUES (?,?,?) ON CONFLICT(asset,t) DO UPDATE SET body=excluded.body',
                                      (self.key(asset),int(bucket),json.dumps(metadata)))
            else:
                await self.db.execute('DELETE FROM sample_evidence WHERE asset=? AND t=?',(self.key(asset),int(bucket)))
            await self.db.commit()

    async def recent_trades(self, asset: str, limit: int = 30) -> list:
        rows = await self.fetchall(
            "SELECT body FROM trades WHERE asset=? ORDER BY t DESC LIMIT ?",
            (self.key(asset), limit),
        )
        return [json.loads(r[0]) for r in rows]

    async def has_trade(self, asset: str, id: str) -> bool:
        row = await self.fetchone(
            'SELECT 1 FROM trades WHERE asset=? AND id=?', (self.key(asset), id)
        )
        return row is not None

    async def put_trades(self, asset: str, rows: list) -> None:
        async with self._guard_write():
            await self.db.executemany(
                "INSERT OR IGNORE INTO trades VALUES (?,?,?,?)",
                [(self.key(asset), r["id"], r["t"], json.dumps(r, ensure_ascii=False)) for r in rows],
            )
            await self.db.commit()

    async def record_trade_observation(self, asset: str, start_ms: int, end_ms: int) -> None:
        """Persist a provider interval known to be caught up without a gap."""
        start_ms, end_ms = int(start_ms), int(end_ms)
        if start_ms <= 0 or end_ms <= start_ms:
            return
        # A very old checkpoint after downtime is useful only when the bounded
        # backfill actually reached it. Keep seven days of interval evidence.
        start_ms = max(start_ms, end_ms - 7 * 86_400_000)
        async with self._guard_write():
            await self.db.execute(
                "INSERT OR IGNORE INTO trade_coverage VALUES (?,?,?)",
                (self.key(asset), start_ms, end_ms),
            )
            await self.db.execute(
                "DELETE FROM trade_coverage WHERE asset=? AND endTime<?",
                (self.key(asset), end_ms - 7 * 86_400_000),
            )
            await self.db.commit()

    async def rebuild_trade_buckets(self, asset: str, now_ms: int | None = None) -> dict:
        """Build honest 1m/5m/1h bars from deduplicated trades.

        Empty bars are written only where successful catch-up intervals prove
        that the provider was observed. coverageRatio distinguishes a closed,
        fully observed zero from an unobserved gap.
        """
        now_ms = int(now_ms or time.time() * 1000)
        specs = {"1m": (60_000, 6 * 3_600_000), "5m": (300_000, 86_400_000), "1h": (3_600_000, 7 * 86_400_000)}
        oldest = now_ms - max(horizon for _, horizon in specs.values())
        observations = await self.fetchall(
            "SELECT t,body FROM trades WHERE asset=? AND t>=? ORDER BY t",
            (self.key(asset), oldest),
        )
        trades = []
        for row in observations:
            try:
                trades.append((int(row[0]), json.loads(row[1])))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        observations = await self.fetchall(
            "SELECT startTime,endTime FROM trade_coverage WHERE asset=? AND endTime>=? ORDER BY startTime",
            (self.key(asset), oldest),
        )
        intervals = [(max(oldest, int(r[0])), min(now_ms, int(r[1]))) for r in observations if int(r[1]) > int(r[0])]
        merged = []
        for start, end in intervals:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)

        def covered(start: int, end: int) -> int:
            return sum(max(0, min(end, b) - max(start, a)) for a, b in merged)

        latest: dict[str, dict] = {}
        async with self._guard_write():
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                for bar, (width, horizon) in specs.items():
                    since = now_ms - horizon
                    first_points = [t for t, _ in trades if t >= since]
                    first_points.extend(a for a, b in merged if b >= since)
                    await self.db.execute(
                        "DELETE FROM trade_buckets WHERE asset=? AND bar=? AND openTime>=?",
                        (self.key(asset), bar, since // width * width),
                    )
                    if not first_points:
                        continue
                    first = max(since, min(first_points)) // width * width
                    last = now_ms // width * width
                    grouped: dict[int, list[dict]] = {}
                    for t, body in trades:
                        if t >= since:
                            grouped.setdefault(t // width * width, []).append(body)
                    rows = []
                    for opened in range(first, last + 1, width):
                        bucket_trades = grouped.get(opened, [])
                        ratio = min(1.0, covered(opened, opened + width) / width)
                        if not bucket_trades and ratio <= 0:
                            continue
                        buys = [r for r in bucket_trades if r.get("type") == "buy"]
                        sells = [r for r in bucket_trades if r.get("type") == "sell"]
                        buy_volume = sum(float(r["volume"]) for r in buys if r.get("volume") is not None)
                        sell_volume = sum(float(r["volume"]) for r in sells if r.get("volume") is not None)
                        known_volume = any(r.get("volume") is not None for r in bucket_trades)
                        complete = opened + width <= now_ms and ratio >= 0.98
                        row = {
                            "bar": bar, "openTime": opened, "closeTime": opened + width,
                            "buyCount": len(buys), "sellCount": len(sells), "tradeCount": len(bucket_trades),
                            "buyVolumeUsd": buy_volume if buys and any(r.get("volume") is not None for r in buys) else (0.0 if complete else None),
                            "sellVolumeUsd": sell_volume if sells and any(r.get("volume") is not None for r in sells) else (0.0 if complete else None),
                            "volumeUsd": buy_volume + sell_volume if known_volume else (0.0 if complete else None),
                            "traders": len({r.get("user") for r in bucket_trades if r.get("user")}),
                            "coverageRatio": round(ratio, 4), "complete": complete, "updatedAt": now_ms,
                        }
                        rows.append((self.key(asset), bar, opened, row["buyCount"], row["sellCount"], row["tradeCount"],
                                     row["buyVolumeUsd"], row["sellVolumeUsd"], row["volumeUsd"], row["traders"],
                                     row["coverageRatio"], 1 if complete else 0, now_ms))
                        if opened + width <= now_ms:
                            latest[bar] = row
                    if rows:
                        await self.db.executemany(
                            """INSERT INTO trade_buckets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(asset,bar,openTime) DO UPDATE SET
                               buyCount=excluded.buyCount,sellCount=excluded.sellCount,tradeCount=excluded.tradeCount,
                               buyVolumeUsd=excluded.buyVolumeUsd,sellVolumeUsd=excluded.sellVolumeUsd,
                               volumeUsd=excluded.volumeUsd,traders=excluded.traders,
                               coverageRatio=excluded.coverageRatio,complete=excluded.complete,updatedAt=excluded.updatedAt""",
                            rows,
                        )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        return latest

    async def trade_buckets(self, asset: str, bar: str, limit: int = 120) -> list:
        observations = await self.fetchall(
            """SELECT bar,openTime,buyCount,sellCount,tradeCount,buyVolumeUsd,sellVolumeUsd,
                      volumeUsd,traders,coverageRatio,complete,updatedAt
               FROM trade_buckets WHERE asset=? AND bar=? ORDER BY openTime DESC LIMIT ?""",
            (self.key(asset), bar, limit),
        )
        rows = [dict(r) for r in observations]
        width = {"1m": 60_000, "5m": 300_000, "1h": 3_600_000}.get(bar, 0)
        for row in rows:
            row["complete"] = bool(row["complete"])
            row["closeTime"] = row["openTime"] + width
        return rows[::-1]

    async def events(self, asset: str | None = None, limit: int = 100) -> list:
        if asset:
            rows = await self.fetchall(
                "SELECT body FROM events WHERE asset=? ORDER BY t DESC LIMIT ?",
                (self.key(asset), limit),
            )
        else:
            rows = await self.fetchall(
                "SELECT body FROM events WHERE asset LIKE ? ORDER BY t DESC LIMIT ?",
                (f"{self.scope}:%", limit),
            )
        return [json.loads(r[0]) for r in rows]

    async def events_page(self, before: dict | None = None, limit: int = 50) -> list:
        if before:
            cursor_id = str(before["id"])
            prefix = f"{self.scope}:"
            if cursor_id.startswith(prefix):
                cursor_id = cursor_id[len(prefix):]
            rows = await self.fetchall(
                """SELECT body FROM events WHERE asset LIKE ?
                   AND (t < ? OR (t = ? AND id < ?)) ORDER BY t DESC, id DESC LIMIT ?""",
                (f"{self.scope}:%", before["t"], before["t"], self.key(cursor_id), limit),
            )
        else:
            rows = await self.fetchall(
                "SELECT body FROM events WHERE asset LIKE ? ORDER BY t DESC, id DESC LIMIT ?",
                (f"{self.scope}:%", limit),
            )
        return [json.loads(r[0]) for r in rows]

    async def put_event(self, id: str, asset: str, value: dict, now=None) -> None:
        body = {**value, "id": self.key(id), "asset": self.key(asset),
                "chainId": self.scope, "t": now or time.time() * 1000}
        async with self._guard_write():
            await self.db.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                (self.key(id), self.key(asset), int(body["t"]), json.dumps(body, ensure_ascii=False)),
            )
            await self.db.commit()

    async def activity(self, asset: str, since_ms: int) -> dict:
        observations = await self.fetchall(
            "SELECT body FROM trades WHERE asset=? AND t>=?", (self.key(asset), since_ms)
        )
        rows = [json.loads(r[0]) for r in observations]
        return {
            "buys": sum(1 for r in rows if r.get("type") == "buy"),
            "sells": sum(1 for r in rows if r.get("type") == "sell"),
            "volume": sum(r["volume"] for r in rows if r.get("volume") is not None) or None,
            "count": len(rows),
            "traders": len({r.get("user") for r in rows if r.get("user")}),
        }

    async def put_candles(self, asset: str, bar: str, rows: list) -> None:
        async with self._guard_write():
            await self.db.executemany(
                """INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(asset,bar,openTime) DO UPDATE SET
                   open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                   volume=excluded.volume,volumeUsd=excluded.volumeUsd,confirmed=excluded.confirmed""",
                [
                    (self.key(asset), bar, r["t"], r["o"], r["h"], r["l"], r["c"],
                     r.get("v"), r.get("vu"), 1 if r.get("confirmed") else 0)
                    for r in rows
                ],
            )
            await self.db.commit()

    async def candle_range(self, asset: str, bar: str, limit: int = 300) -> list:
        observations = await self.fetchall(
            """SELECT openTime t,open o,high h,low l,close c,volume v,volumeUsd vu,confirmed
               FROM candles WHERE asset=? AND bar=? ORDER BY openTime DESC LIMIT ?""",
            (self.key(asset), bar, limit),
        )
        rows = [dict(r) for r in observations]
        for r in reversed(rows):
            r["confirmed"] = bool(r["confirmed"])
        return rows[::-1]


# One store per chain scope, mirroring the Node side's per-scope map.
_stores: dict[str, ResearchStore] = {}
_store_init_lock = asyncio.Lock()
_read_connection: ContextVar = ContextVar("research_read_connection", default=None)


async def store(chain: str = "196") -> ResearchStore:
    pinned = _read_connection.get()
    if pinned is not None:
        facade = ResearchStore(scope=chain)
        facade.db = pinned
        return facade
    if chain not in _stores:
        # Single flight before publishing the connection. All known scopes
        # are initialized at service startup, before collector write locks.
        async with _store_init_lock:
            if chain not in _stores:
                _stores[chain] = await ResearchStore(DB_PATH, chain).connect()
    return _stores[chain]


async def close_all() -> None:
    global _store_init_lock
    for s in _stores.values():
        await s.close()
    _stores.clear()
    _store_init_lock = asyncio.Lock()


@asynccontextmanager
async def transaction_view(connection):
    """Bind every scoped read in this task to one SQLite transaction."""
    token = _read_connection.set(connection)
    try:
        yield
    finally:
        _read_connection.reset(token)
