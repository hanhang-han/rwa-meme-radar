"""Cross-process daily request budget, shared with Node's durable ledger."""
import json
import os
import sqlite3
import time

from .storage_runtime import is_postgres_path, sync_connect

def _legacy_seed(day):
    try:
        with open('data/okx-usage.json') as f:
            old = json.load(f)
        return max(0, int(old.get('daily', 0))) if old.get('day') == day else 0
    except (OSError, ValueError, TypeError):
        return 0


def shared_usage(day: str, limit: int | None = None, lane: str | None = None, lane_limit: int | None = None) -> int:
    path = os.environ.get('OKX_LEDGER_PATH', 'data/okx-budget.sqlite')
    postgres = is_postgres_path(path)
    if not postgres:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    db = sync_connect(path, timeout=5)
    try:
        if limit is None:
            # Status rendering must not reserve a SQLite writer or wait for
            # another process's quota charge. Existing WAL rows are readable
            # while the writer commits. A new day still uses the seed path.
            try:
                row = db.execute('SELECT used FROM budget WHERE day=?', (day,)).fetchone()
                if row is not None:
                    return row[0]
                if postgres:
                    return _legacy_seed(day)
            except sqlite3.OperationalError as exc:
                if 'no such table' not in str(exc):
                    raise
        if not postgres:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS budget(day TEXT PRIMARY KEY, used INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS lane_budget(day TEXT, lane TEXT, used INTEGER NOT NULL, PRIMARY KEY(day,lane))')
            db.commit()
        db.execute('BEGIN IMMEDIATE')
        seed = _legacy_seed(day)
        db.execute('INSERT OR IGNORE INTO budget(day,used) VALUES (?,?)', (day, seed))
        used = db.execute('SELECT used FROM budget WHERE day=?', (day,)).fetchone()[0]
        if limit is not None:
            if used >= limit:
                raise RuntimeError('OKX local request budget exhausted')
            if lane and lane_limit is not None:
                db.execute('INSERT OR IGNORE INTO lane_budget VALUES (?,?,0)', (day,lane))
                lane_used = db.execute('SELECT used FROM lane_budget WHERE day=? AND lane=?', (day,lane)).fetchone()[0]
                if lane_used >= lane_limit:
                    raise RuntimeError(f'OKX {lane} daily allowance exhausted')
                db.execute('UPDATE lane_budget SET used=used+1 WHERE day=? AND lane=?', (day,lane))
            db.execute('UPDATE budget SET used=used+1 WHERE day=?', (day,))
            used += 1
        db.commit()
        return used
    finally:
        db.close()


def reserve_request_slot(interval_ms: int = 500, now_ms: int | None = None) -> float:
    """Reserve a global start slot shared with Node; return seconds to wait.

    The SQLite transaction ends before sleeping/network I/O. Cancelled slots
    waste a small interval but cannot create an uncharged upstream request.
    """
    path = os.environ.get('OKX_LEDGER_PATH', 'data/okx-budget.sqlite')
    postgres = is_postgres_path(path)
    if not postgres:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    now = int(time.time()*1000) if now_ms is None else now_ms
    db = sync_connect(path, timeout=5)
    try:
        if not postgres:
            db.execute('CREATE TABLE IF NOT EXISTS request_rate(provider TEXT PRIMARY KEY,next_at INTEGER NOT NULL)')
            db.commit()
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT next_at FROM request_rate WHERE provider='okx'").fetchone()
        start = max(now, int(row[0]) if row else now)
        db.execute("INSERT INTO request_rate VALUES ('okx',?) ON CONFLICT(provider) DO UPDATE SET next_at=excluded.next_at", (start+max(400,interval_ms),))
        db.commit()
        return max(0,start-now)/1000
    finally:
        db.close()
