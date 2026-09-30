"""Opt-in Telegram alerts, isolated from the research database's write path.

Only /start with a one-use account code establishes a private recipient.
Telegram sendMessage has no idempotency key. Explicit rejected sends (429,
structured 5xx, connect failure) retry at most three times. Ambiguous delivery
(read/write timeout, unknown response, crash while sending) is terminal rather
than risking duplicate messages. Event intake and queue insertion are atomic.

Protocol references: https://core.telegram.org/bots/api#getupdates,
https://core.telegram.org/bots/api#sendmessage,
https://core.telegram.org/bots/features#deep-linking.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

import httpx

from . import developer_access as access, user_features

CODE_LIFETIME_MS = 600_000
COOLDOWN_MS = 900_000
DAILY_LIMIT = 20
MAX_ATTEMPTS = 3
MAX_EVENT_AGE_MS = 900_000
QUEUE_LIFETIME_MS = 3_600_000
ADDRESS = re.compile(r'^0x[a-f0-9]{40}$')
CODE = re.compile(r'^cx_[A-Za-z0-9_-]{32}$')
USERNAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]{4,31}bot$', re.I)
TOKEN = re.compile(r'^\d{5,}:[A-Za-z0-9_-]{20,}$')
CHAINS = {'196', '56', '4663'}
RISK_LABELS = {'wash_suspect': '成交结构异常', 'thin_spike': '低流动性大幅波动',
               'holder_anomaly': '持币地址数据异常', 'contract_risk': '合约风险',
               'concentrated': '持仓集中', 'liquidity_unlock': '流动性锁定风险'}
_lock = threading.Lock()
_initialized = set()
_tick_lock = asyncio.Lock()
_verified_bot = None


def now_ms():
    return int(time.time()*1000)


def settings():
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    username = os.environ.get('TELEGRAM_BOT_USERNAME', '').strip().lstrip('@')
    return {'available': bool(TOKEN.fullmatch(token) and USERNAME.fullmatch(username)),
            'token': token, 'username': username}


def configured():
    return settings()['available']


def _offset_key():
    # Token rotation keeps a bot's cursor, switching bots does not inherit it.
    return 'update_offset:'+settings()['username'].casefold()


def capabilities():
    # The existing chain swap writer records provisional swaps only. Closed
    # candle bars are not transaction finality and must not enable this class.
    return {'newPool': {'available': True}, 'riskChange': {'available': True},
            'largeTrade': {'available': False, 'reason': 'confirmed-trade-feed-unavailable'}}


def init():
    user_features.init()
    path = str(access._path().resolve())
    with _lock:
        if path in _initialized:
            return
        with access.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS telegram_bindings (
              user_id TEXT PRIMARY KEY REFERENCES users(id), binding_id TEXT NOT NULL UNIQUE,
              chat_id TEXT NOT NULL UNIQUE, bot_username TEXT NOT NULL,
              enabled INTEGER NOT NULL, bound_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_bind_codes (
              code_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
              created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, consumed_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS telegram_codes_user ON telegram_bind_codes(user_id,created_at);
            CREATE TABLE IF NOT EXISTS telegram_runtime (
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_asset_state (
              asset_key TEXT PRIMARY KEY, flags TEXT NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_outbox (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
              binding_id TEXT NOT NULL, event_key TEXT NOT NULL,
              category TEXT NOT NULL, asset_key TEXT NOT NULL, payload TEXT NOT NULL,
              created_at INTEGER NOT NULL, next_attempt_at INTEGER NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
              started_at INTEGER, finished_at INTEGER, message_id TEXT,
              budget_day TEXT, error_code TEXT,
              UNIQUE(user_id,binding_id,event_key)
            );
            CREATE INDEX IF NOT EXISTS telegram_queue_due ON telegram_outbox(status,next_attempt_at);
            CREATE INDEX IF NOT EXISTS telegram_queue_cooldown ON telegram_outbox(user_id,category,asset_key,created_at);
            CREATE TABLE IF NOT EXISTS telegram_daily_budget (
              user_id TEXT NOT NULL REFERENCES users(id), day TEXT NOT NULL,
              used INTEGER NOT NULL, PRIMARY KEY(user_id,day)
            );
            ''')
        _initialized.add(path)


def _runtime(db, key, default=None):
    row = db.execute('SELECT value FROM telegram_runtime WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def _save_runtime(db, key, value, now):
    db.execute('INSERT INTO telegram_runtime VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
               (key, json.dumps(value, separators=(',', ':')), now))


def status(user_id):
    init()
    config = settings()
    with access.connection() as db:
        binding = db.execute('SELECT * FROM telegram_bindings WHERE user_id=?', (user_id,)).fetchone()
        worker = _runtime(db, 'health', {})
    linked = bool(binding and binding['enabled'] and config['available']
                  and binding['bot_username'].casefold() == config['username'].casefold())
    return {'available': config['available'], 'linked': linked,
            'botUsername': config['username'] if config['available'] else None,
            'linkedAt': binding['bound_at'] if linked else None,
            'reason': None if config['available'] else 'telegram-not-configured',
            'worker': worker, 'capabilities': capabilities(),
            'cooldownMinutes': 15, 'dailyLimit': DAILY_LIMIT,
            'deliverySemantics': 'at-most-once-attempt-on-ambiguous-send'}


def create_bind_code(user_id):
    init()
    config = settings()
    if not config['available']:
        raise access.AccessError('telegram-not-configured', 503)
    now = now_ms()
    code = 'cx_' + secrets.token_urlsafe(24)
    digest = hashlib.sha256(code.encode()).hexdigest()
    with access.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        user = db.execute('SELECT id FROM users WHERE id=? AND disabled_at IS NULL', (user_id,)).fetchone()
        if not user:
            raise access.AccessError('login-required', 401)
        count = db.execute('SELECT count(*) FROM telegram_bind_codes WHERE user_id=? AND created_at>?', (user_id, now-3_600_000)).fetchone()[0]
        if count >= 5:
            raise access.AccessError('telegram-bind-rate-limit', 429, 3600)
        db.execute('UPDATE telegram_bind_codes SET consumed_at=? WHERE user_id=? AND consumed_at IS NULL', (now, user_id))
        db.execute('INSERT INTO telegram_bind_codes VALUES (?,?,?,?,NULL)', (digest, user_id, now, now+CODE_LIFETIME_MS))
        db.commit()
    return {'startUrl': f"https://t.me/{config['username']}?start={code}", 'expiresAt': now+CODE_LIFETIME_MS}


def _disable(db, user_id, now):
    db.execute('UPDATE telegram_bindings SET enabled=0 WHERE user_id=?', (user_id,))
    db.execute("UPDATE telegram_outbox SET status='cancelled',finished_at=?,error_code='unlinked' WHERE user_id=? AND status IN ('pending','sending')", (now, user_id))
    db.execute('UPDATE telegram_bind_codes SET consumed_at=? WHERE user_id=? AND consumed_at IS NULL', (now, user_id))


def unlink(user_id):
    init()
    with access.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        _disable(db, user_id, now_ms())
        db.commit()
    return {'ok': True, **status(user_id)}


def consume_update(update):
    """Accept only a private message authored by that chat's human owner."""
    init()
    config = settings()
    if not config['available']:
        return False
    update_id = update.get('update_id')
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        return False
    now = now_ms()
    message = update.get('message') or {}
    chat, sender = message.get('chat') or {}, message.get('from') or {}
    chat_id = chat.get('id')
    private = (chat.get('type') == 'private' and isinstance(chat_id, int) and not isinstance(chat_id, bool)
               and 0 < chat_id < 2**53 and sender.get('id') == chat_id and sender.get('is_bot') is False)
    text = message.get('text') or ''
    parts = text.split() if isinstance(text, str) else []
    command = parts[0].split('@', 1)[0] if parts else ''
    with access.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        offset = _runtime(db, _offset_key(), 0)
        if update_id < offset:
            return False
        accepted = False
        if private and command == '/stop':
            row = db.execute('SELECT user_id FROM telegram_bindings WHERE chat_id=? AND bot_username=?',
                             (str(chat_id), config['username'].casefold())).fetchone()
            if row:
                _disable(db, row['user_id'], now)
                accepted = True
        elif private and command == '/start' and len(parts) == 2 and CODE.fullmatch(parts[1]):
            digest = hashlib.sha256(parts[1].encode()).hexdigest()
            row = db.execute('''SELECT c.* FROM telegram_bind_codes c JOIN users u ON u.id=c.user_id
                WHERE code_hash=? AND consumed_at IS NULL AND expires_at>? AND u.disabled_at IS NULL''', (digest, now)).fetchone()
            message_at = message.get('date')
            valid_time = (isinstance(message_at, int) and not isinstance(message_at, bool)
                          and message_at*1000 <= now+60_000 and row and message_at*1000 >= row['created_at']-10_000)
            collision = db.execute('SELECT user_id,enabled FROM telegram_bindings WHERE chat_id=?', (str(chat_id),)).fetchone()
            if row and valid_time and (not collision or collision['user_id'] == row['user_id'] or not collision['enabled']):
                if collision and collision['user_id'] != row['user_id']:
                    db.execute('DELETE FROM telegram_bindings WHERE chat_id=? AND enabled=0', (str(chat_id),))
                _disable(db, row['user_id'], now)
                db.execute('''INSERT INTO telegram_bindings VALUES (?,?,?,?,1,?)
                    ON CONFLICT(user_id) DO UPDATE SET binding_id=excluded.binding_id,chat_id=excluded.chat_id,
                    bot_username=excluded.bot_username,enabled=1,bound_at=excluded.bound_at''',
                    (row['user_id'], secrets.token_hex(16), str(chat_id), config['username'].casefold(), now))
                accepted = True
        _save_runtime(db, _offset_key(), update_id+1, now)
        db.commit()
    return accepted


def _identity(chain, token):
    chain, token = str(chain or ''), str(token or '').lower()
    return f'{chain}:{token}' if chain in CHAINS and ADDRESS.fullmatch(token) else None


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _label(value, maximum=60):
    return ' '.join(str(value or '').split())[:maximum]


def _template(event):
    chain, token = event['assetKey'].split(':', 1)
    asset = _label(event.get('symbol')) or token[:8]+'…'+token[-6:]
    when = datetime.fromtimestamp(event['at']/1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    if event['category'] == 'newPool':
        description = f"新建配对池：{asset} ↔ {_label(event.get('ticker'))}\n池地址：{event['pool']}"
    elif event['category'] == 'riskChange':
        description = f"{asset} 风险检查新增触发：" + '、'.join(RISK_LABELS[flag] for flag in event['flags'])
    else:
        description = f"{asset} 已确认链上成交：{event['usd']:,.2f} USD"
    return f"CliperX\n{description}\n链：{chain}\n时间：{when}\nhttps://cliperx.com/dashboard/#/asset/{chain}/{token}"


def _enqueue(db, event, now):
    """Target matching, preference gates, cooldown and dedupe share one tx."""
    if event['category'] == 'largeTrade' and not capabilities()['largeTrade']['available']:
        return 0
    if not _finite(event.get('at')) or not 0 <= now-event['at'] <= MAX_EVENT_AGE_MS:
        return 0
    watch_keys = [event['assetKey']]
    for ticker in event.get('tickers') or ([event['ticker']] if event.get('ticker') else []):
        if re.fullmatch(r'[A-Z0-9.^-]{1,24}', str(ticker).upper()):
            watch_keys.append('stock:'+str(ticker).upper())
    placeholders = ','.join('?' for _ in watch_keys)
    targets = db.execute(f'''SELECT DISTINCT b.*,COALESCE(p.new_pool,1) new_pool,
        COALESCE(p.risk_change,1) risk_change,COALESCE(p.large_trade,0) large_trade,
        COALESCE(p.trade_usd,10000) trade_usd
        FROM telegram_bindings b JOIN users u ON u.id=b.user_id
        JOIN user_watches w ON w.user_id=b.user_id
        LEFT JOIN user_alert_preferences p ON p.user_id=b.user_id
        WHERE b.enabled=1 AND u.disabled_at IS NULL AND b.bot_username=?
          AND w.item_key IN ({placeholders}) AND w.created_at<=? AND b.bound_at<?''',
        [settings()['username'].casefold(), *watch_keys, event['at'], event['at']]).fetchall()
    category_column = {'newPool': 'new_pool', 'riskChange': 'risk_change', 'largeTrade': 'large_trade'}[event['category']]
    accepted = 0
    for target in targets:
        if not target[category_column] or (event['category'] == 'largeTrade' and event['usd'] < target['trade_usd']):
            continue
        recent = db.execute('''SELECT 1 FROM telegram_outbox WHERE user_id=? AND category=? AND asset_key=?
            AND created_at>? AND status NOT IN ('cancelled','failed') LIMIT 1''',
            (target['user_id'], event['category'], event['assetKey'], now-COOLDOWN_MS)).fetchone()
        if recent:
            continue
        event_key = event['eventKey']
        identity = hashlib.sha256(f"{target['user_id']}:{target['binding_id']}:{event_key}".encode()).hexdigest()
        payload = {**event, 'text': _template(event), 'watchKeys': watch_keys}
        result = db.execute('''INSERT OR IGNORE INTO telegram_outbox
            (id,user_id,binding_id,event_key,category,asset_key,payload,created_at,next_attempt_at)
            VALUES (?,?,?,?,?,?,?,?,?)''', (identity, target['user_id'], target['binding_id'], event_key,
                event['category'], event['assetKey'], json.dumps(payload, ensure_ascii=False), now, now))
        accepted += result.rowcount
    return accepted


def _risk_flags(asset):
    assessment = asset.get('riskAssessment') or {}
    checks = assessment.get('checks') or {}
    return sorted(flag for flag in asset.get('riskFlags') or []
                  if flag in RISK_LABELS and (checks.get(flag) or {}).get('status') == 'triggered')


def _confirmed_pool(relation, event):
    return bool(relation and relation.get('status') == 'verified'
        and _identity(relation.get('chainId'), relation.get('token')) == event['assetKey']
        and str(relation.get('pool') or '').lower() == event['pool']
        and relation.get('factoryEventId') == event.get('factoryEventId')
        and relation.get('confirmationStatus') == 'confirmed'
        and (relation.get('stockIdentity') or {}).get('verificationStatus') == 'official')


async def _current_pool(event):
    """Re-check durable facts after queueing, including unpublished reorgs."""
    from .db import store
    s = await store(event['assetKey'].split(':', 1)[0])
    relation = await s.get('relation', event.get('relationId') or '')
    if not _confirmed_pool(relation, event):
        return False
    factory = await s.fetchone('SELECT status FROM factory_discovery_events WHERE id=?', (event['factoryEventId'],))
    return bool(factory and factory[0] == 'confirmed')


def _seed(db, payload, cursor, now):
    db.execute('DELETE FROM telegram_asset_state')
    for asset in (payload.get('unified') or {}).get('assets') or []:
        key = _identity(asset.get('chainId'), asset.get('token'))
        if key:
            db.execute('INSERT OR REPLACE INTO telegram_asset_state VALUES (?,?,?)', (key, json.dumps(_risk_flags(asset)), now))
    _save_runtime(db, 'event_cursor', cursor, now)


def process_events(rows, payload, high, low):
    init()
    now = now_ms()
    unified = payload.get('unified') or {}
    assets = {key: a for a in unified.get('assets') or [] if (key := _identity(a.get('chainId'), a.get('token')))}
    tickers = {}
    for relation in unified.get('relations') or []:
        key = _identity(relation.get('chainId'), relation.get('token'))
        ticker = (relation.get('stockIdentity') or {}).get('ticker') or relation.get('ticker')
        if key and ticker and relation.get('level') == 'A' and relation.get('status') == 'verified':
            tickers.setdefault(key, set()).add(ticker)
    accepted = 0
    with access.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        cursor = _runtime(db, 'event_cursor')
        if cursor is None or cursor > high or (low and cursor < low-1):
            _seed(db, payload, high, now)
            db.commit()
            return 0
        for event in rows:
            seq = event['id']
            if seq <= cursor:
                continue
            try:
                body = json.loads(event['body'])
            except (TypeError, ValueError):
                cursor = seq
                continue
            if event['event'] == 'relationship':
                relation = body.get('relation') or {}
                key = _identity(relation.get('chainId') or body.get('chainId'), relation.get('token') or body.get('asset'))
                at, pool = relation.get('poolCreatedAt'), str(relation.get('pool') or '').lower()
                official = (relation.get('stockIdentity') or {}).get('verificationStatus') == 'official'
                if (key in assets and official and relation.get('confirmationStatus') == 'confirmed'
                        and relation.get('factoryEventId') and ADDRESS.fullmatch(pool) and _finite(at)):
                    candidate = {'category': 'newPool', 'assetKey': key,
                        'eventKey': 'new-pool:'+key.split(':')[0]+':'+pool,
                        'symbol': (assets[key] or {}).get('symbol'), 'ticker': relation.get('ticker'),
                        'pool': pool, 'at': at, 'relationId': relation.get('id'),
                        'factoryEventId': relation['factoryEventId']}
                    if candidate['relationId'] and any(_confirmed_pool(current, candidate) for current in unified.get('relations') or []):
                        accepted += _enqueue(db, candidate, now)
            elif event['event'] == 'projection.delta':
                for asset in (body.get('upserts') or {}).get('assets') or []:
                    key = _identity(asset.get('chainId'), asset.get('token'))
                    if not key:
                        continue
                    old = db.execute('SELECT flags FROM telegram_asset_state WHERE asset_key=?', (key,)).fetchone()
                    flags = _risk_flags(asset)
                    additions = sorted(set(flags)-set(json.loads(old['flags']))) if old else []
                    db.execute('INSERT OR REPLACE INTO telegram_asset_state VALUES (?,?,?)', (key, json.dumps(flags), now))
                    if additions and key in assets:
                        accepted += _enqueue(db, {'category': 'riskChange', 'assetKey': key,
                            'eventKey': f'risk:{seq}:{key}:'+','.join(additions),
                            'symbol': asset.get('symbol'), 'at': event['at'], 'flags': additions,
                            'tickers': sorted(tickers.get(key, []))}, now)
                for key in (body.get('removes') or {}).get('assets') or []:
                    db.execute('DELETE FROM telegram_asset_state WHERE asset_key=?', (str(key),))
            cursor = seq
        # A short page consumed every relevant event through this read's high
        # watermark; unrelated quote/candle/trade ids need not be revisited.
        if len(rows) < 100:
            cursor = high
        _save_runtime(db, 'event_cursor', cursor, now)
        db.commit()
    return accepted


async def scan_events():
    from .db import store
    from .realtime_projection import read_projection_json, ProjectionUnavailable
    init()
    with access.connection() as db:
        cursor = _runtime(db, 'event_cursor', 0)
    s = await store('196')
    bounds = await s.fetchone('SELECT (SELECT id FROM realtime_events ORDER BY id LIMIT 1),(SELECT id FROM realtime_events ORDER BY id DESC LIMIT 1)')
    low, high = bounds[0], bounds[1] or 0
    if high == cursor:
        return 0
    try:
        payload = json.loads(await read_projection_json('full'))
    except ProjectionUnavailable:
        return 0
    # Do not consume a relationship before its candidate has reached the
    # canonical projection. Publication can lag the collector's journal write.
    published = (payload.get('realtime') or {}).get('cursor')
    if not isinstance(published, int):
        return 0
    high = min(high, published)
    if high <= cursor:
        return 0
    rows = await s.fetchall("SELECT id,event,body,at FROM realtime_events WHERE id>? AND id<=? AND event IN ('relationship','projection.delta') ORDER BY id LIMIT 100", (cursor, high))
    return await asyncio.to_thread(process_events, [dict(row) for row in rows], payload, high, low)


def _claim():
    init()
    now = now_ms()
    day = datetime.fromtimestamp(now/1000, timezone.utc).strftime('%Y-%m-%d')
    with access.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        # A previous process may have delivered just before it crashed. Do not
        # re-send such an ambiguous attempt after restart.
        db.execute("UPDATE telegram_outbox SET status='uncertain',finished_at=?,error_code='interrupted-send' WHERE status='sending' AND started_at<?", (now, now-60_000))
        db.execute("UPDATE telegram_outbox SET status='failed',finished_at=?,error_code='expired' WHERE status='pending' AND created_at<?", (now, now-QUEUE_LIFETIME_MS))
        if _runtime(db, 'send_not_before', 0) > now:
            db.commit()
            return None
        candidates = db.execute("SELECT * FROM telegram_outbox WHERE status='pending' AND next_attempt_at<=? ORDER BY created_at,id LIMIT 50", (now,)).fetchall()
        selected = None
        for row in candidates:
            binding = db.execute('''SELECT b.* FROM telegram_bindings b JOIN users u ON u.id=b.user_id
                WHERE b.user_id=? AND b.binding_id=? AND b.enabled=1 AND u.disabled_at IS NULL AND b.bot_username=?''',
                (row['user_id'], row['binding_id'], settings()['username'].casefold())).fetchone()
            payload = json.loads(row['payload'])
            preferences = db.execute('SELECT * FROM user_alert_preferences WHERE user_id=?', (row['user_id'],)).fetchone()
            column = {'newPool': 'new_pool', 'riskChange': 'risk_change', 'largeTrade': 'large_trade'}[row['category']]
            enabled = bool(preferences[column]) if preferences else column != 'large_trade'
            keys = payload.get('watchKeys') or [row['asset_key']]
            watched = db.execute('SELECT 1 FROM user_watches WHERE user_id=? AND item_key IN ('+','.join('?' for _ in keys)+') LIMIT 1', [row['user_id'], *keys]).fetchone()
            if not binding or not watched or not enabled or (row['category'] == 'largeTrade' and not capabilities()['largeTrade']['available']):
                db.execute("UPDATE telegram_outbox SET status='cancelled',finished_at=?,error_code='recipient-disabled' WHERE id=?", (now, row['id']))
                continue
            previous = db.execute('''SELECT max(COALESCE(finished_at,started_at)) FROM telegram_outbox
                WHERE user_id=? AND category=? AND asset_key=? AND id<>? AND status IN ('sent','uncertain')''',
                (row['user_id'], row['category'], row['asset_key'], row['id'])).fetchone()[0]
            if previous is not None and previous+COOLDOWN_MS > now:
                db.execute('UPDATE telegram_outbox SET next_attempt_at=? WHERE id=?', (previous+COOLDOWN_MS, row['id']))
                continue
            budget = db.execute('SELECT used FROM telegram_daily_budget WHERE user_id=? AND day=?', (row['user_id'], day)).fetchone()
            if row['budget_day'] != day:
                if budget and budget['used'] >= DAILY_LIMIT:
                    db.execute("UPDATE telegram_outbox SET status='cancelled',finished_at=?,error_code='daily-limit' WHERE id=?", (now, row['id']))
                    continue
                db.execute('INSERT INTO telegram_daily_budget VALUES (?,?,1) ON CONFLICT(user_id,day) DO UPDATE SET used=used+1', (row['user_id'], day))
            db.execute("UPDATE telegram_outbox SET status='sending',started_at=?,attempts=attempts+1,budget_day=? WHERE id=?", (now, day, row['id']))
            selected = {**dict(row), 'attempts': row['attempts']+1, 'chat_id': binding['chat_id'], 'payload': payload}
            break
        db.commit()
    return selected


def _authorize_send(row):
    """Last local check after async source reads, without holding locks on HTTP."""
    with access.connection() as db:
        return bool(db.execute('''SELECT 1 FROM telegram_outbox q
            JOIN telegram_bindings b ON b.user_id=q.user_id AND b.binding_id=q.binding_id
            JOIN users u ON u.id=b.user_id
            WHERE q.id=? AND q.status='sending' AND b.enabled=1 AND u.disabled_at IS NULL
              AND b.chat_id=? AND b.bot_username=?''',
            (row['id'], row['chat_id'], settings()['username'].casefold())).fetchone())


def _cancel(row, reason):
    with access.connection() as db:
        db.execute("UPDATE telegram_outbox SET status='cancelled',finished_at=?,error_code=? WHERE id=? AND status='sending'",
                   (now_ms(), reason, row['id']))


class BotFailure(Exception):
    def __init__(self, code, *, retry_after=None, retryable=False, ambiguous=False):
        self.code, self.retry_after, self.retryable, self.ambiguous = code, retry_after, retryable, ambiguous
        super().__init__(code)  # Never include an httpx exception URL/token.


async def _api(client, method, params):
    token = settings()['token']
    try:
        response = await client.post(f'https://api.telegram.org/bot{token}/{method}', json=params)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise BotFailure('connect-failed', retryable=True) from None
    except httpx.HTTPError:
        raise BotFailure('delivery-unknown', ambiguous=True) from None
    try:
        packet = response.json()
    except (ValueError, TypeError):
        raise BotFailure('invalid-response', ambiguous=True) from None
    if not isinstance(packet, dict):
        raise BotFailure('invalid-response', ambiguous=True)
    if packet.get('ok') is True:
        return packet.get('result')
    code = packet.get('error_code')
    code = code if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599 else response.status_code
    if code == 429:
        retry = (packet.get('parameters') or {}).get('retry_after')
        retry = max(1, min(int(retry), 3600)) if _finite(retry) else 60
        raise BotFailure('rate-limited', retry_after=retry, retryable=True)
    if isinstance(code, int) and 500 <= code < 600 and packet.get('ok') is False:
        raise BotFailure('server-rejected', retryable=True)
    raise BotFailure('bot-rejected-'+(str(code) if isinstance(code, int) else 'unknown'))


def _finish(row, result=None, failure=None):
    now = now_ms()
    with access.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if failure is None:
            state, next_at, error = 'sent', now, None
        elif failure.ambiguous:
            state, next_at, error = 'uncertain', now, failure.code
        elif failure.retryable and row['attempts'] < MAX_ATTEMPTS:
            wait = failure.retry_after or min(300, 5 * 2**row['attempts'])
            state, next_at, error = 'pending', now+wait*1000, failure.code
            if failure.retry_after:
                _save_runtime(db, 'send_not_before', next_at, now)
        else:
            state, next_at, error = 'failed', now, failure.code
        db.execute('''UPDATE telegram_outbox SET status=?,next_attempt_at=?,finished_at=?,message_id=?,error_code=?
            WHERE id=? AND status='sending' ''', (state, next_at, None if state == 'pending' else now,
                str(result.get('message_id')) if isinstance(result, dict) and result.get('message_id') is not None else None, error, row['id']))
        db.commit()


async def dispatch(client, maximum=5):
    if not configured():
        return {'accepted': 0, 'skipped': 1}
    sent = failed = 0
    for _ in range(maximum):
        row = await asyncio.to_thread(_claim)
        if not row:
            break
        try:
            if row['category'] == 'newPool':
                try:
                    current = await _current_pool(row['payload'])
                except Exception:
                    raise BotFailure('source-unavailable', retryable=True) from None
                if not current:
                    _cancel(row, 'pool-no-longer-confirmed')
                    continue
            if not _authorize_send(row):
                _cancel(row, 'recipient-disabled')
                continue
            result = await _api(client, 'sendMessage', {'chat_id': row['chat_id'], 'text': row['payload']['text'],
                'link_preview_options': {'is_disabled': True}, 'allow_paid_broadcast': False})
            if not isinstance(result, dict) or not isinstance(result.get('message_id'), int):
                raise BotFailure('delivery-unknown', ambiguous=True)
        except BotFailure as exc:
            await asyncio.to_thread(_finish, row, failure=exc)
            failed += 1
            if exc.retry_after:
                break
        else:
            await asyncio.to_thread(_finish, row, result=result)
            sent += 1
    return {'accepted': sent, 'failed': failed}


async def tick():
    """One independently scheduled worker turn; no network when unconfigured."""
    global _verified_bot
    if not configured():
        return {'accepted': 0, 'skipped': 1}
    async with _tick_lock:
        await asyncio.to_thread(init)
        now = now_ms()
        with access.connection() as db:
            pending = db.execute('SELECT 1 FROM telegram_bind_codes WHERE consumed_at IS NULL AND expires_at>? LIMIT 1', (now,)).fetchone()
            linked = db.execute('SELECT 1 FROM telegram_bindings WHERE enabled=1 AND bot_username=? LIMIT 1', (settings()['username'].casefold(),)).fetchone()
            offset = _runtime(db, _offset_key(), 0)
            not_before = _runtime(db, 'poll_not_before', 0)
        if not pending and not linked:
            return {'accepted': 0, 'skipped': 1}
        if not_before > now:
            return {'accepted': 0, 'skipped': 1}
        config = settings()
        bot_key = (hashlib.sha256(config['token'].encode()).hexdigest(), config['username'].casefold())
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5), follow_redirects=False) as client:
                if _verified_bot != bot_key:
                    me = await _api(client, 'getMe', {})
                    if not isinstance(me, dict) or me.get('is_bot') is not True or str(me.get('username') or '').casefold() != config['username'].casefold():
                        raise BotFailure('bot-username-mismatch')
                    _verified_bot = bot_key
                updates = await _api(client, 'getUpdates', {'offset': offset, 'limit': 100, 'timeout': 20, 'allowed_updates': ['message']})
                if not isinstance(updates, list):
                    raise BotFailure('invalid-update-response')
                bound = 0
                for update in updates:
                    if isinstance(update, dict):
                        bound += int(await asyncio.to_thread(consume_update, update))
                queued = await scan_events()
                outcome = await dispatch(client)
                with access.connection() as db:
                    _save_runtime(db, 'health', {'status': 'ready', 'updatedAt': now_ms()}, now_ms())
                    db.execute('DELETE FROM telegram_bind_codes WHERE expires_at<?', (now_ms()-86_400_000,))
                    db.execute("DELETE FROM telegram_outbox WHERE status NOT IN ('pending','sending') AND created_at<?", (now_ms()-30*86_400_000,))
                    db.execute('DELETE FROM telegram_daily_budget WHERE day<?', (datetime.fromtimestamp((now_ms()-30*86_400_000)/1000, timezone.utc).strftime('%Y-%m-%d'),))
                return {**outcome, 'updated': bound+queued, 'requested': 1}
        except BotFailure as exc:
            with access.connection() as db:
                _save_runtime(db, 'health', {'status': 'unavailable', 'reason': exc.code, 'updatedAt': now_ms()}, now_ms())
                _save_runtime(db, 'poll_not_before', now_ms()+1000*(exc.retry_after or 30), now_ms())
            return {'accepted': 0, 'failed': 1}
