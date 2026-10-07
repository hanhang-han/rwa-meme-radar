"""Account watchlists, isolated from market ingestion and quoted data."""
from __future__ import annotations

import os
import math
import re
import threading
import time
from . import developer_access as access

_lock = threading.Lock()
_initialized: set[str] = set()
KEY = re.compile(r"^(?:stock:[A-Z0-9.^-]{1,24}|[0-9]{1,10}:0x[a-f0-9]{40})$")
MAX_WATCHES = 200


def init():
    access.init()
    path = access._storage_key()
    with _lock:
        if path in _initialized:
            return
        with access.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS user_watches (
              user_id TEXT NOT NULL REFERENCES users(id), item_key TEXT NOT NULL,
              created_at INTEGER NOT NULL, PRIMARY KEY(user_id,item_key)
            );
            CREATE TABLE IF NOT EXISTS user_alert_preferences (
              user_id TEXT PRIMARY KEY REFERENCES users(id), new_pool INTEGER NOT NULL DEFAULT 1,
              large_trade INTEGER NOT NULL DEFAULT 0, risk_change INTEGER NOT NULL DEFAULT 1,
              trade_usd REAL NOT NULL DEFAULT 10000, updated_at INTEGER NOT NULL
            );
            """)
        _initialized.add(path)


def normalize_key(key):
    if not isinstance(key, str):
        raise access.AccessError('invalid-watch-key')
    key = key.strip()
    key = 'stock:' + key[6:].upper() if key.lower().startswith('stock:') else key.lower()
    if not KEY.fullmatch(key):
        raise access.AccessError('invalid-watch-key')
    if not key.startswith('stock:'):
        chain, address = key.split(':', 1)
        if int(chain) <= 0:
            raise access.AccessError('invalid-watch-key')
        key = str(int(chain)) + ':' + address
    return key


def list_watches(user_id):
    init()
    with access.connection() as db:
        return [row['item_key'] for row in db.execute(
            'SELECT item_key FROM user_watches WHERE user_id=? ORDER BY created_at,item_key', (user_id,))]


def change_watches(user_id, add, remove):
    # Incremental, idempotent changes preserve concurrent edits on other devices.
    added = {normalize_key(key) for key in add}
    removed = {normalize_key(key) for key in remove}
    if added & removed:
        raise access.AccessError('conflicting-watch-change')
    init()
    with access.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            for key in removed:
                db.execute('DELETE FROM user_watches WHERE user_id=? AND item_key=?', (user_id, key))
            now = int(time.time() * 1000)
            for key in added:
                db.execute('INSERT OR IGNORE INTO user_watches VALUES (?,?,?)', (user_id, key, now))
            total = db.execute('SELECT count(*) FROM user_watches WHERE user_id=?', (user_id,)).fetchone()[0]
            if total > MAX_WATCHES:
                raise access.AccessError('watch-limit', 409)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return list_watches(user_id)


def alert_preferences(user_id):
    init()
    with access.connection() as db:
        row = db.execute('SELECT * FROM user_alert_preferences WHERE user_id=?', (user_id,)).fetchone()
    # Import lazily: the bot stores its bindings alongside these preferences.
    # Configuration and an opt-in binding describe readiness, never delivery.
    from .telegram_alerts import status as telegram_status
    telegram = telegram_status(user_id)
    delivery = {key: telegram[key] for key in
                ('available', 'linked', 'reason', 'capabilities', 'deliverySemantics')}
    return {'newPool': bool(row['new_pool']) if row else True,
            'largeTrade': bool(row['large_trade']) if row else False,
            'riskChange': bool(row['risk_change']) if row else True,
            'tradeUsd': row['trade_usd'] if row else 10000,
            'delivery': delivery,
            'cooldownMinutes': 15, 'dailyLimit': 20}


def save_alert_preferences(user_id, values):
    init()
    threshold = values.get('tradeUsd', 10000)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 100 <= threshold <= 1e9:
        raise access.AccessError('invalid-trade-threshold')
    if any(not isinstance(values.get(key), bool) for key in ('newPool', 'largeTrade', 'riskChange')):
        raise access.AccessError('invalid-alert-type')
    with access.connection() as db:
        db.execute('''INSERT INTO user_alert_preferences VALUES (?,?,?,?,?,?)
          ON CONFLICT(user_id) DO UPDATE SET new_pool=excluded.new_pool,
          large_trade=excluded.large_trade,risk_change=excluded.risk_change,
          trade_usd=excluded.trade_usd,updated_at=excluded.updated_at''',
          (user_id, bool(values.get('newPool')), bool(values.get('largeTrade')),
           bool(values.get('riskChange')), threshold, int(time.time() * 1000)))
    return alert_preferences(user_id)


def is_operator(user):
    # Registration does not prove mailbox ownership. An email alone must
    # never grant operations access: pin the existing account's immutable ID.
    ids = {v.strip() for v in os.environ.get('RADAR_OPERATOR_USER_IDS', '').split(',') if v.strip()}
    emails = {v.strip().casefold() for v in os.environ.get('RADAR_OPERATOR_EMAILS', '').split(',') if v.strip()}
    return str(user.get('id', '')) in ids and (not emails or str(user.get('email', '')).casefold() in emails)
