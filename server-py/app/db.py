"""aiosqlite port of the Node research-store: the same tables (facts, samples,
trades, events, candles) and the same scope-prefixed keys, so the existing
research.sqlite is read in place with no migration."""
import json
import os
import time

import aiosqlite

DB_PATH = os.environ.get("RESEARCH_DB", "data/research.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS samples (asset TEXT NOT NULL, t INTEGER NOT NULL, price REAL NOT NULL, cap REAL, PRIMARY KEY(asset,t));
CREATE TABLE IF NOT EXISTS trades (asset TEXT NOT NULL, id TEXT NOT NULL, t INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(asset,id));
CREATE INDEX IF NOT EXISTS trades_time ON trades(asset,t);
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, asset TEXT NOT NULL, t INTEGER NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_time ON events(t);
CREATE TABLE IF NOT EXISTS candles (asset TEXT NOT NULL, bar TEXT NOT NULL, openTime INTEGER NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume REAL, volumeUsd REAL, confirmed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(asset,bar,openTime));
"""


class ResearchStore:
    def __init__(self, path: str = DB_PATH, scope: str = "196"):
        self.path = path
        self.scope = scope
        self.db: aiosqlite.Connection | None = None

    def key(self, value: str) -> str:
        return f"{self.scope}:{value}"

    async def connect(self) -> "ResearchStore":
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.executescript(SCHEMA)
        await self.db.commit()
        return self

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    async def get(self, kind: str, id: str):
        cur = await self.db.execute(
            "SELECT body FROM facts WHERE kind=? AND id=?", (self.key(kind), id)
        )
        row = await cur.fetchone()
        return json.loads(row[0]) if row else None

    async def all(self, kind: str) -> list:
        cur = await self.db.execute("SELECT body FROM facts WHERE kind=?", (self.key(kind),))
        rows = await cur.fetchall()
        return [json.loads(r[0]) for r in rows]

    async def put(self, kind: str, id: str, value) -> None:
        await self.db.execute(
            "INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
            (self.key(kind), id, json.dumps(value, ensure_ascii=False)),
        )
        await self.db.commit()

    async def samples(self, asset: str, limit: int = 288) -> list:
        cur = await self.db.execute(
            "SELECT t,price,cap FROM samples WHERE asset=? ORDER BY t DESC LIMIT ?",
            (self.key(asset), limit),
        )
        rows = await cur.fetchall()
        return [{"t": r[0], "price": r[1], "cap": r[2]} for r in reversed(rows)]

    async def sample(self, asset: str, price: float, cap, now=None) -> None:
        if not (price > 0):
            return
        bucket = (now or time.time() * 1000) // 300000 * 300000
        await self.db.execute(
            "INSERT INTO samples VALUES (?,?,?) ON CONFLICT(asset,t) DO UPDATE SET price=excluded.price,cap=excluded.cap",
            (self.key(asset), int(bucket), price, cap),
        )
        await self.db.commit()

    async def recent_trades(self, asset: str, limit: int = 30) -> list:
        cur = await self.db.execute(
            "SELECT body FROM trades WHERE asset=? ORDER BY t DESC LIMIT ?",
            (self.key(asset), limit),
        )
        return [json.loads(r[0]) for r in await cur.fetchall()]

    async def has_trade(self, asset: str, id: str) -> bool:
        cur = await self.db.execute(
            'SELECT 1 FROM trades WHERE asset=? AND id=?', (self.key(asset), id)
        )
        return await cur.fetchone() is not None

    async def put_trades(self, asset: str, rows: list) -> None:
        await self.db.executemany(
            "INSERT OR IGNORE INTO trades VALUES (?,?,?,?)",
            [(self.key(asset), r["id"], r["t"], json.dumps(r, ensure_ascii=False)) for r in rows],
        )
        await self.db.commit()

    async def events(self, asset: str | None = None, limit: int = 100) -> list:
        if asset:
            cur = await self.db.execute(
                "SELECT body FROM events WHERE asset=? ORDER BY t DESC LIMIT ?",
                (self.key(asset), limit),
            )
        else:
            cur = await self.db.execute(
                "SELECT body FROM events WHERE asset LIKE ? ORDER BY t DESC LIMIT ?",
                (f"{self.scope}:%", limit),
            )
        return [json.loads(r[0]) for r in await cur.fetchall()]

    async def put_event(self, id: str, asset: str, value: dict, now=None) -> None:
        body = {**value, "id": self.key(id), "asset": self.key(asset),
                "chainId": self.scope, "t": now or time.time() * 1000}
        await self.db.execute(
            "INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
            (self.key(id), self.key(asset), int(body["t"]), json.dumps(body, ensure_ascii=False)),
        )
        await self.db.commit()

    async def activity(self, asset: str, since_ms: int) -> dict:
        cur = await self.db.execute(
            "SELECT body FROM trades WHERE asset=? AND t>=?", (self.key(asset), since_ms)
        )
        rows = [json.loads(r[0]) for r in await cur.fetchall()]
        return {
            "buys": sum(1 for r in rows if r.get("type") == "buy"),
            "sells": sum(1 for r in rows if r.get("type") == "sell"),
            "volume": sum(r["volume"] for r in rows if r.get("volume") is not None) or None,
            "count": len(rows),
            "traders": len({r.get("user") for r in rows if r.get("user")}),
        }

    async def put_candles(self, asset: str, bar: str, rows: list) -> None:
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
        cur = await self.db.execute(
            """SELECT openTime t,open o,high h,low l,close c,volume v,volumeUsd vu,confirmed
               FROM candles WHERE asset=? AND bar=? ORDER BY openTime DESC LIMIT ?""",
            (self.key(asset), bar, limit),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for r in reversed(rows):
            r["confirmed"] = bool(r["confirmed"])
        return rows[::-1]


# One store per chain scope, mirroring the Node side's per-scope map.
_stores: dict[str, ResearchStore] = {}


async def store(chain: str = "196") -> ResearchStore:
    if chain not in _stores:
        _stores[chain] = await ResearchStore(DB_PATH, chain).connect()
    return _stores[chain]


async def close_all() -> None:
    for s in _stores.values():
        await s.close()
    _stores.clear()
