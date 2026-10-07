"""Allowlisted X mentions and reviewed aliases; no third-party scraping."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import re
import statistics
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit
import httpx
from . import developer_access as access, user_features
from .db import store
from .product_filters import structured_model
from .realtime_projection import read_projection_json

POST_URL = re.compile(r'^https://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/status/(\d{5,25})/?(?:\?[^#]*)?$')
ADDRESS = re.compile(r'0x[a-fA-F0-9]{40}')
HANDLE = re.compile(r'^[A-Za-z0-9_]{1,15}$')
_initialized = set()
_read_lock = asyncio.Lock()
_alias_cache = (0, [])


def now_ms():
    return int(time.time()*1000)


def explicit_x_id(value):
    return bool(re.fullmatch(r'[1-9][0-9]{0,24}', str(value or '')))


def init():
    user_features.init()
    path = access._storage_key()
    if path in _initialized:
        return
    with access.connection() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS product_kol_accounts(handle TEXT PRIMARY KEY, x_id TEXT, created_at INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS product_mentions(post_id TEXT NOT NULL,handle TEXT NOT NULL,asset_key TEXT NOT NULL,posted_at INTEGER NOT NULL,body TEXT NOT NULL,updated_at INTEGER NOT NULL,PRIMARY KEY(post_id,asset_key));
        CREATE TABLE IF NOT EXISTS product_x_reads(post_id TEXT PRIMARY KEY,handle TEXT NOT NULL,checked_at INTEGER NOT NULL,state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS product_x_budget(day TEXT PRIMARY KEY,used INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS product_x_cursors(handle TEXT PRIMARY KEY,x_id TEXT NOT NULL,since_id TEXT,page_token TEXT,pending_newest TEXT,bootstrap_at INTEGER NOT NULL,next_at INTEGER NOT NULL DEFAULT 0,lease_until INTEGER NOT NULL DEFAULT 0,lease_id TEXT);
        CREATE TABLE IF NOT EXISTS product_x_delete_checks(post_id TEXT PRIMARY KEY,next_at INTEGER NOT NULL DEFAULT 0,lease_until INTEGER NOT NULL DEFAULT 0,lease_id TEXT);
        CREATE TABLE IF NOT EXISTS product_x_poll_control(id TEXT PRIMARY KEY,until_at INTEGER NOT NULL,reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS product_alias_candidates(id TEXT PRIMARY KEY,ticker TEXT NOT NULL,alias TEXT NOT NULL,evidence TEXT NOT NULL,source_text TEXT NOT NULL,confidence TEXT NOT NULL,source TEXT NOT NULL,status TEXT NOT NULL,created_at INTEGER NOT NULL,reviewed_at INTEGER);
        CREATE TABLE IF NOT EXISTS product_wallet_saved(user_id TEXT NOT NULL REFERENCES users(id),address TEXT NOT NULL,label TEXT NOT NULL,created_at INTEGER NOT NULL,PRIMARY KEY(user_id,address));
        ''')
        try:
            configured = json.loads(os.environ.get('PRODUCT_KOL_ACCOUNTS_JSON', '[]'))
        except ValueError:
            configured = []
        if isinstance(configured, list):
            for account in configured[:200]:
                handle = account.get('handle') if isinstance(account, dict) else account
                if isinstance(handle, str) and HANDLE.fullmatch(handle.lstrip('@')):
                    ident = str(account.get('id') or '') if isinstance(account, dict) else ''
                    ident = ident if explicit_x_id(ident) else None
                    db.execute('INSERT INTO product_kol_accounts VALUES (?,?,?,1) ON CONFLICT(handle) DO UPDATE SET x_id=COALESCE(excluded.x_id,product_kol_accounts.x_id)',
                               (handle.lstrip('@').casefold(), ident, now_ms()))
    _initialized.add(path)


def x_ready():
    return bool(os.environ.get('X_BEARER_TOKEN')) and os.environ.get('PRODUCT_X_READ_ENABLED') == '1'


def daily_limit():
    from .config import bounded_env_int
    return bounded_env_int('PRODUCT_X_DAILY_READ_LIMIT', 20, 1, 10_000)


def autopoll_reason():
    if os.environ.get('PRODUCT_X_AUTOPOLL_ENABLED') != '1':
        return 'autopoll-disabled'
    if not x_ready():
        return 'x-not-configured'
    # An operator must verify the provider's actual metering before enabling.
    if os.environ.get('PRODUCT_X_BILLING_UNIT') != 'post-resource':
        return 'x-billing-unit-unconfirmed'
    return None


def accounts():
    init()
    with access.connection() as db:
        values = [dict(r) for r in db.execute('SELECT handle,x_id FROM product_kol_accounts WHERE active=1 ORDER BY handle')]
        budget = db.execute('SELECT used FROM product_x_budget WHERE day=?', (datetime.now(timezone.utc).strftime('%Y-%m-%d'),)).fetchone()
    return {'available': x_ready(), 'items': [{'handle': r['handle'], 'url': 'https://x.com/'+r['handle']} for r in values],
            'reason': None if x_ready() else 'x-not-configured', 'dailyReadLimit': daily_limit(),
            'readsToday': budget[0] if budget else 0, 'minimumRankingSamples': 10, 'scope': 'operator-allowlist-only',
            'automatic': {'available': autopoll_reason() is None and any(explicit_x_id(r['x_id']) for r in values),
                          'reason': autopoll_reason() or (None if any(explicit_x_id(r['x_id']) for r in values) else 'explicit-account-ids-required'),
                          'dailyReadLimit': min(20, daily_limit()), 'maxPostsPerPage': 5}}


def save_account(handle, active=True):
    init()
    if not isinstance(handle, str) or not HANDLE.fullmatch(handle.lstrip('@')) or not isinstance(active, bool):
        raise access.AccessError('invalid-kol-account')
    with access.connection() as db:
        db.execute('INSERT INTO product_kol_accounts VALUES (?,NULL,?,?) ON CONFLICT(handle) DO UPDATE SET active=excluded.active', (handle.lstrip('@').casefold(), now_ms(), active))
    return accounts()


def extract_mentions(text, payload):
    unified = payload.get('unified') or payload
    assets = unified.get('assets') or []
    addresses = {m.lower() for m in ADDRESS.findall(text)}
    symbols = {m.upper() for m in re.findall(r'\$([A-Za-z0-9_]{1,24})(?![A-Za-z0-9_])', text)}
    index = {}
    for asset in assets:
        index.setdefault(str(asset.get('symbol') or '').upper(), []).append(asset)
    result, pending = {}, []
    for asset in assets:
        key = f"{asset.get('chainId')}:{str(asset.get('token')).lower()}"
        if str(asset.get('token')).lower() in addresses:
            result[key] = {'assetKey': key, 'symbol': asset.get('symbol'), 'confidence': 'contract', 'evidence': str(asset.get('token')).lower()}
    stocks = {str(s.get('stockCode') or s.get('ticker') or str(s.get('tokenSymbol') or '').removesuffix('x')).upper() for s in unified.get('stockTokens') or []}
    narratives = []
    for symbol in symbols:
        if symbol in stocks:
            narratives.append(symbol); continue
        matches = index.get(symbol) or []
        if len(matches) == 1:
            asset = matches[0]
            key = f"{asset.get('chainId')}:{str(asset.get('token')).lower()}"
            result.setdefault(key, {'assetKey': key, 'symbol': asset.get('symbol'), 'confidence': 'unique-symbol', 'evidence': '$'+symbol})
        elif matches:
            pending.append({'symbol': symbol, 'reason': 'ambiguous-symbol', 'candidateCount': len(matches)})
    return {'mentions': list(result.values()), 'pending': pending, 'themes': sorted(narratives)}


async def _prices(asset_key, posted, now):
    chain, token = asset_key.split(':', 1)
    s = await store(chain)
    records = await s.fetchall('SELECT s.t,s.price,e.body FROM samples s JOIN sample_evidence e ON e.asset=s.asset AND e.t=s.t WHERE s.asset=? AND s.t BETWEEN ? AND ? ORDER BY s.t',
                              (s.key(token), posted-300_000, min(now, posted+7*86_400_000)+300_000))
    samples = []
    for row in records:
        try:
            evidence = json.loads(row[2])
        except (TypeError, ValueError):
            continue
        if evidence.get('currency') == 'USD' and isinstance(row[1], (int, float)) and row[1] > 0:
            at = evidence.get('marketAt') or evidence.get('at') or evidence.get('observedAt') or evidence.get('receivedAt')
            if not isinstance(at, (int, float)) or not 0 < at <= now:
                continue
            samples.append((int(at), row[1]))
    samples.sort()
    def nearest_before(at):
        candidates = [(t, p) for t, p in samples if 0 <= at-t <= 300_000]
        return candidates[-1] if candidates else None
    baseline = nearest_before(posted)
    result = {'mentionPriceUsd': baseline[1] if baseline else None, 'priceAt': baseline[0] if baseline else None,
              'priceStatus': 'precise' if baseline else 'not-available', 'returns': {}, 'maxMultiple': None,
              'maximumScope': 'available-station-samples', 'priceSource': 'station-primary-usd-samples'}
    for label, window in [('1h', 3_600_000), ('24h', 86_400_000), ('7d', 7*86_400_000)]:
        end = nearest_before(posted+window) if now >= posted+window else None
        result['returns'][label] = (end[1]/baseline[1]-1) if baseline and end else None
    if baseline:
        prices = [price for at, price in samples if posted <= at <= min(now, posted+7*86_400_000)]
        result['maxMultiple'] = max(prices)/baseline[1] if prices else None
    return result


async def lookup(url):
    init()
    match = POST_URL.fullmatch(url) if isinstance(url, str) else None
    if not match:
        raise access.AccessError('invalid-x-post-url')
    handle, post_id = match[1].casefold(), match[2]
    if not x_ready():
        return {'available': False, 'reason': 'x-not-configured', 'items': [], 'pending': []}
    with access.connection() as db:
        account = db.execute('SELECT handle FROM product_kol_accounts WHERE handle=? AND active=1', (handle,)).fetchone()
    if not account:
        raise access.AccessError('kol-not-allowlisted', 409)
    async with _read_lock:
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with access.connection() as db:
            cached = db.execute('SELECT checked_at FROM product_x_reads WHERE post_id=? AND handle=?', (post_id, handle)).fetchone()
            if cached and now_ms()-cached[0] < 86_400_000:
                return {'available': True, 'cached': True, **await mentions(handle=handle, post_id=post_id)}
            db.execute('BEGIN IMMEDIATE')
            budget = db.execute('SELECT used FROM product_x_budget WHERE day=?', (day,)).fetchone()
            if budget and budget[0] >= daily_limit():
                raise access.AccessError('x-daily-budget-exhausted', 429)
            db.execute('INSERT INTO product_x_budget VALUES (?,1) ON CONFLICT(day) DO UPDATE SET used=used+1', (day,))
            db.commit()
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
                response = await client.get('https://api.x.com/2/tweets/'+post_id,
                    params={'tweet.fields': 'created_at,author_id', 'expansions': 'author_id', 'user.fields': 'username'},
                    headers={'Authorization': 'Bearer '+os.environ['X_BEARER_TOKEN']})
            if response.status_code in {404, 410}:
                with access.connection() as db:
                    db.execute('DELETE FROM product_mentions WHERE post_id=?', (post_id,))
                    db.execute('INSERT INTO product_x_reads VALUES (?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET checked_at=excluded.checked_at,state=excluded.state', (post_id, handle, now_ms(), 'deleted'))
                return {'available': True, 'deleted': True, 'items': [], 'pending': []}
            response.raise_for_status()
            packet = response.json()
            post = packet.get('data') or {}
            authors = packet.get('includes', {}).get('users') or []
            if not any(str(author.get('id')) == str(post.get('author_id')) and str(author.get('username')).casefold() == handle for author in authors):
                raise ValueError('author-mismatch')
            posted = int(datetime.fromisoformat(str(post['created_at']).replace('Z', '+00:00')).timestamp()*1000)
            if not 0 < posted <= now_ms()+60_000 or not isinstance(post.get('text'), str):
                raise ValueError('invalid-post')
        except (httpx.HTTPError, TypeError, ValueError, KeyError):
            return {'available': True, 'reason': 'x-source-unavailable', 'items': [], 'pending': []}
        # The source text lives only in this request. Stored body is derived facts.
        payload = json.loads(await read_projection_json('full'))
        extracted = extract_mentions(post['text'], payload)
        now = now_ms()
        with access.connection() as db:
            db.execute('INSERT INTO product_x_reads VALUES (?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET checked_at=excluded.checked_at,state=excluded.state', (post_id, handle, now, 'read'))
        for item in extracted['mentions']:
            prices = await _prices(item['assetKey'], posted, now)
            record = {**item, **prices, 'postId': post_id, 'handle': handle, 'postedAt': posted,
                      'url': 'https://x.com/'+handle+'/status/'+post_id}
            with access.connection() as db:
                db.execute('INSERT INTO product_mentions VALUES (?,?,?,?,?,?) ON CONFLICT(post_id,asset_key) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at',
                           (post_id, handle, item['assetKey'], posted, json.dumps(record, ensure_ascii=False), now))
        return {'available': True, 'pending': extracted['pending'], 'themes': extracted['themes'], **await mentions(handle=handle, post_id=post_id)}


async def mentions(handle=None, post_id=None):
    init()
    clauses, params = ['handle IN (SELECT handle FROM product_kol_accounts WHERE active=1)'], []
    if handle:
        if not HANDLE.fullmatch(handle):
            raise access.AccessError('invalid-kol-account')
        clauses.append('handle=?'); params.append(handle.casefold())
    if post_id:
        clauses.append('post_id=?'); params.append(post_id)
    where = ' WHERE '+ ' AND '.join(clauses) if clauses else ''
    with access.connection() as db:
        rows = db.execute('SELECT body FROM product_mentions'+where+' ORDER BY posted_at DESC LIMIT 500', params).fetchall()
    records = [json.loads(row[0]) for row in rows]
    # Refresh returns from local samples, without extra X reads. First mention
    # per account/asset alone is included in ranking and the first caller flag.
    first, earliest, grouped = {}, {}, {}
    for record in sorted(records, key=lambda r: r['postedAt']):
        first.setdefault((record['handle'], record['assetKey']), record['postId'])
        earliest.setdefault(record['assetKey'], record['postId'])
    for record in records:
        if now_ms()-record['postedAt'] <= 8*86_400_000:
            record.update(await _prices(record['assetKey'], record['postedAt'], now_ms()))
        record['firstMention'] = earliest[record['assetKey']] == record['postId']
        record['rankingSample'] = first[(record['handle'], record['assetKey'])] == record['postId'] and record['priceStatus'] == 'precise' and record['returns']['24h'] is not None
        if record['rankingSample']:
            grouped.setdefault(record['handle'], []).append(record['returns']['24h'])
    ranking = [{'handle': h, 'samples': len(values), 'winRate': sum(v > 0 for v in values)/len(values),
                'medianReturn24h': statistics.median(values)} for h, values in grouped.items() if len(values) >= 10]
    return {'items': records[:100], 'total': len(records), 'ranking': sorted(ranking, key=lambda r: (-r['medianReturn24h'], -r['samples'])),
            'minimumRankingSamples': 10, 'range': {'from': min((r['postedAt'] for r in records), default=None), 'to': max((r['postedAt'] for r in records), default=None)},
            'scope': 'allowlisted-accounts-station-assets', 'bodyStored': False}


def _poll_claim(kind, at, lease_ms):
    """Lease one request and reserve its maximum resource cost atomically."""
    with access.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        blocked = db.execute("SELECT until_at FROM product_x_poll_control WHERE id='global'").fetchone()
        if blocked and blocked[0] > at:
            return None, 'provider-backoff'
        if kind == 'timeline':
            for account in db.execute('SELECT handle,x_id FROM product_kol_accounts WHERE active=1 AND x_id IS NOT NULL').fetchall():
                if not explicit_x_id(account['x_id']):
                    continue
                db.execute('INSERT INTO product_x_cursors(handle,x_id,bootstrap_at) VALUES (?,?,?) ON CONFLICT(handle) DO NOTHING', (account['handle'], account['x_id'], at-86_400_000))
                old = db.execute('SELECT x_id,lease_until FROM product_x_cursors WHERE handle=?', (account['handle'],)).fetchone()
                if old['x_id'] != account['x_id'] and old['lease_until'] <= at:
                    db.execute('UPDATE product_x_cursors SET x_id=?,since_id=NULL,page_token=NULL,pending_newest=NULL,bootstrap_at=?,next_at=0 WHERE handle=?', (account['x_id'], at-86_400_000, account['handle']))
            row = db.execute('SELECT c.* FROM product_x_cursors c JOIN product_kol_accounts a ON a.handle=c.handle AND a.x_id=c.x_id WHERE a.active=1 AND c.next_at<=? AND c.lease_until<=? ORDER BY c.next_at,c.handle LIMIT 1', (at, at)).fetchone()
            cost, table, key = 5, 'product_x_cursors', 'handle'
        else:
            row = db.execute('''SELECT r.post_id,r.handle,a.x_id FROM product_x_reads r
                JOIN product_kol_accounts a ON a.handle=r.handle AND a.active=1 AND a.x_id IS NOT NULL
                LEFT JOIN product_x_delete_checks d ON d.post_id=r.post_id
                WHERE r.state='read' AND r.checked_at<=? AND COALESCE(d.next_at,0)<=? AND COALESCE(d.lease_until,0)<=?
                  AND EXISTS(SELECT 1 FROM product_mentions m WHERE m.post_id=r.post_id)
                ORDER BY r.checked_at,r.post_id LIMIT 1''', (at-86_400_000, at, at)).fetchone()
            cost, table, key = 1, 'product_x_delete_checks', 'post_id'
        if row is None:
            db.commit()
            return None, 'no-due-'+kind
        claim = dict(row)
        if not explicit_x_id(claim['x_id']):
            db.commit()
            return None, 'explicit-account-ids-required'
        day = datetime.fromtimestamp(at/1000, timezone.utc).strftime('%Y-%m-%d')
        spent = db.execute('SELECT used FROM product_x_budget WHERE day=?', (day,)).fetchone()
        if (spent[0] if spent else 0)+cost > min(20, daily_limit()):
            db.commit()
            return None, 'x-daily-budget-exhausted'
        lease = uuid.uuid4().hex
        db.execute('INSERT INTO product_x_budget VALUES (?,?) ON CONFLICT(day) DO UPDATE SET used=used+excluded.used', (day, cost))
        if kind == 'delete':
            db.execute('INSERT OR IGNORE INTO product_x_delete_checks(post_id) VALUES (?)', (claim['post_id'],))
        db.execute(f'UPDATE {table} SET lease_until=?,lease_id=? WHERE {key}=?', (at+lease_ms, lease, claim[key]))
        db.commit()
        return {**claim, 'kind': kind, 'lease': lease, 'cost': cost}, None


def _poll_delay(response):
    if response.status_code in (401, 403):
        return 6*3_600_000
    if response.status_code == 429:
        try:
            seconds = int(response.headers.get('Retry-After', ''))
        except ValueError:
            try:
                seconds = int(response.headers.get('x-rate-limit-reset', ''))-now_ms()//1000
            except ValueError:
                seconds = 900
        return max(60_000, min(6*3_600_000, seconds*1000))
    return 300_000


def _poll_release(claim, delay, *, blocked=False, reason='source-unavailable'):
    at = now_ms()
    table, key = ('product_x_cursors', 'handle') if claim['kind'] == 'timeline' else ('product_x_delete_checks', 'post_id')
    with access.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(f'UPDATE {table} SET next_at=?,lease_until=0,lease_id=NULL WHERE {key}=? AND lease_id=?', (at+delay, claim[key], claim['lease']))
        if blocked:
            db.execute("INSERT INTO product_x_poll_control VALUES ('global',?,?) ON CONFLICT(id) DO UPDATE SET until_at=MAX(until_at,excluded.until_at),reason=excluded.reason", (at+delay, reason))
        db.commit()


async def _poll_records(post, handle, x_id, payload):
    if (str(post.get('author_id')) != str(x_id) or not re.fullmatch(r'[0-9]{5,25}', str(post.get('id') or ''))
            or not isinstance(post.get('text'), str)):
        raise ValueError('invalid-or-wrong-author-post')
    posted = int(datetime.fromisoformat(str(post['created_at']).replace('Z', '+00:00')).timestamp()*1000)
    if not 0 < posted <= now_ms()+60_000:
        raise ValueError('invalid-post-time')
    extracted = extract_mentions(post['text'], payload)
    records = []
    for item in extracted['mentions']:
        records.append({**item, **await _prices(item['assetKey'], posted, now_ms()), 'postId': str(post['id']),
                        'handle': handle, 'authorId': str(x_id), 'postedAt': posted,
                        'url': 'https://x.com/'+handle+'/status/'+str(post['id'])})
    return {'postId': str(post['id']), 'records': records}


def _poll_commit(claim, posts, next_token=None):
    """Facts and the next-page checkpoint commit together; body is never stored."""
    at = now_ms()
    table, key = ('product_x_cursors', 'handle') if claim['kind'] == 'timeline' else ('product_x_delete_checks', 'post_id')
    with access.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute(f'SELECT lease_id FROM {table} WHERE {key}=?', (claim[key],)).fetchone()
        active = db.execute('SELECT 1 FROM product_kol_accounts WHERE handle=? AND x_id=? AND active=1', (claim['handle'], claim['x_id'])).fetchone()
        if not current or current[0] != claim['lease'] or not active:
            return False
        for post in posts:
            db.execute('DELETE FROM product_mentions WHERE post_id=?', (post['postId'],))
            db.execute('INSERT INTO product_x_reads VALUES (?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET handle=excluded.handle,checked_at=excluded.checked_at,state=excluded.state',
                       (post['postId'], claim['handle'], at, 'deleted' if post.get('deleted') else 'read'))
            for record in post.get('records', []):
                db.execute('INSERT INTO product_mentions VALUES (?,?,?,?,?,?)', (post['postId'], claim['handle'], record['assetKey'], record['postedAt'], json.dumps(record, ensure_ascii=False), at))
        if claim['kind'] == 'timeline':
            ids = [value for value in (claim['since_id'], claim['pending_newest'], *(post['postId'] for post in posts)) if value]
            newest = max(ids, key=int) if ids else None
            db.execute('UPDATE product_x_cursors SET since_id=?,page_token=?,pending_newest=?,next_at=?,lease_until=0,lease_id=NULL WHERE handle=?',
                       (claim['since_id'] if next_token else newest, next_token, newest if next_token else None, at if next_token else at+3_600_000, claim['handle']))
        else:
            db.execute('UPDATE product_x_delete_checks SET next_at=?,lease_until=0,lease_id=NULL WHERE post_id=?', (at+86_400_000, claim['post_id']))
        db.commit()
        return True


async def poll(*, max_requests=2, budget_ms=12000):
    """Optional worker lane. No account resolution, model call or public text log."""
    reason = autopoll_reason()
    if reason:
        return {'available': False, 'reason': reason, 'requested': 0, 'updated': 0, 'accepted': 0}
    init()
    deadline = time.monotonic()+min(12000, max(1000, budget_ms))/1000
    result = {'available': True, 'requested': 0, 'updated': 0, 'accepted': 0, 'failed': 0, 'reservedReadUnits': 0}
    async def read_one(claim):
        remaining = max(.1, deadline-time.monotonic())
        params = {'tweet.fields': 'created_at,author_id'}
        if claim['kind'] == 'timeline':
            url = 'https://api.x.com/2/users/'+claim['x_id']+'/tweets'
            params['max_results'] = 5
            if claim['since_id']:
                params['since_id'] = claim['since_id']
            else:
                params['start_time'] = datetime.fromtimestamp(claim['bootstrap_at']/1000, timezone.utc).isoformat().replace('+00:00', 'Z')
            if claim['page_token']:
                params['pagination_token'] = claim['page_token']
        else:
            url = 'https://api.x.com/2/tweets/'+claim['post_id']
        async with httpx.AsyncClient(timeout=min(8, remaining), follow_redirects=False) as client:
            response = await client.get(url, params=params, headers={'Authorization': 'Bearer '+os.environ['X_BEARER_TOKEN']})
        if claim['kind'] == 'delete' and response.status_code in (404, 410):
            return _poll_commit(claim, [{'postId': claim['post_id'], 'deleted': True}])
        if response.status_code in (401, 403, 429):
            result['reason'] = 'x-http-'+str(response.status_code)
            _poll_release(claim, _poll_delay(response), blocked=True, reason=result['reason'])
            return False
        response.raise_for_status()
        packet = response.json()
        if not isinstance(packet, dict):
            raise ValueError('invalid-x-response')
        if packet.get('errors'):
            raise ValueError('partial-x-response')
        posts = packet.get('data', []) if claim['kind'] == 'timeline' else [packet.get('data') or {}]
        if not isinstance(posts, list) or len(posts) > (5 if claim['kind'] == 'timeline' else 1) or any(not isinstance(post, dict) for post in posts):
            raise ValueError('invalid-page')
        next_token = (packet.get('meta') or {}).get('next_token') if claim['kind'] == 'timeline' else None
        if next_token is not None and (not isinstance(next_token, str) or not 1 <= len(next_token) <= 512 or next_token == claim.get('page_token')):
            raise ValueError('invalid-page-cursor')
        payload = json.loads(await read_projection_json('full')) if posts else {}
        derived = [await _poll_records(post, claim['handle'], claim['x_id'], payload) for post in posts]
        if claim['kind'] == 'delete' and (not derived or derived[0]['postId'] != claim['post_id']):
            raise ValueError('wrong-post-id')
        committed = _poll_commit(claim, derived, next_token)
        if committed:
            result['updated'] += sum(len(post['records']) for post in derived)
        return committed
    # The same local lock and persisted budget gate manual and scheduled reads.
    if _read_lock.locked():
        return {**result, 'reason': 'read-in-progress'}
    async with _read_lock:
        for kind in ('timeline', 'delete')[:min(2, max(1, max_requests))]:
            if deadline-time.monotonic() <= .1:
                break
            claim, reason = _poll_claim(kind, now_ms(), int(max(1, deadline-time.monotonic())*1000)+1000)
            if not claim:
                result.setdefault('reason', reason)
                continue
            result['requested'] += 1
            result['reservedReadUnits'] += claim['cost']
            try:
                if await asyncio.wait_for(read_one(claim), max(.1, deadline-time.monotonic())):
                    result['accepted'] += 1
                else:
                    result['failed'] += 1
            except (httpx.HTTPError, ValueError, TypeError, KeyError, asyncio.TimeoutError):
                # Never refund ambiguous external reads or advance a failed page.
                _poll_release(claim, 300_000)
                result['failed'] += 1
                result['reason'] = 'x-source-unavailable'
    return result


def alias_candidates():
    init()
    with access.connection() as db:
        return [dict(r) for r in db.execute('SELECT * FROM product_alias_candidates ORDER BY created_at DESC LIMIT 300')]


async def propose_alias(ticker, source_text, alias=None):
    init()
    ticker = str(ticker).upper()
    if not re.fullmatch(r'[A-Z0-9.^-]{1,24}', ticker) or not isinstance(source_text, str) or not 1 <= len(source_text) <= 600:
        raise access.AccessError('invalid-alias-input')
    source, confidence, evidence = 'manual', 'medium', alias
    if alias is None:
        packet = await structured_model(source_text, {'type': 'object', 'required': ['ticker', 'alias', 'evidence', 'confidence'], 'additionalProperties': False,
            'properties': {'ticker': {'const': ticker}, 'alias': {'type': 'string'}, 'evidence': {'type': 'string'}, 'confidence': {'enum': ['high', 'medium', 'low']}}})
        if not packet:
            return {'available': False, 'reason': 'alias-model-not-configured', 'items': []}
        alias, evidence, confidence, source = packet.get('alias'), packet.get('evidence'), packet.get('confidence'), 'model'
        if packet.get('ticker') != ticker or confidence not in {'high', 'medium', 'low'}:
            raise access.AccessError('invalid-alias-proposal')
    if not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 60 or not isinstance(evidence, str) or not evidence.strip() or evidence not in source_text or alias not in source_text:
        raise access.AccessError('alias-evidence-not-in-source')
    alias = alias.strip()
    if alias.upper() in {'BTC', 'BITCOIN', 'ETH', 'ETHEREUM', 'AI', 'CRYPTO'}:
        raise access.AccessError('generic-alias-not-allowed')
    ident = hashlib.sha256((ticker+'\n'+alias).encode()).hexdigest()[:24]
    with access.connection() as db:
        db.execute('INSERT OR IGNORE INTO product_alias_candidates VALUES (?,?,?,?,?,?,?,\'pending\',?,NULL)',
                   (ident, ticker, alias, evidence, source_text, confidence, source, now_ms()))
    return {'available': True, 'items': alias_candidates()}


def review_alias(ident, approved):
    global _alias_cache
    init()
    if not isinstance(approved, bool):
        raise access.AccessError('invalid-alias-review')
    with access.connection() as db:
        result = db.execute('UPDATE product_alias_candidates SET status=?,reviewed_at=? WHERE id=?', ('confirmed' if approved else 'rejected', now_ms(), ident))
        if not result.rowcount:
            raise access.AccessError('alias-not-found', 404)
    _alias_cache = (0, [])
    return {'items': alias_candidates()}


def confirmed_aliases():
    global _alias_cache
    at, items = _alias_cache
    if time.monotonic()-at > 1:
        items = [a for a in alias_candidates() if a['status'] == 'confirmed']
        _alias_cache = (time.monotonic(), items)
    return items


def alias_matches(name, aliases):
    def matches(alias):
        if re.search(r'[A-Za-z0-9]', alias):
            return bool(re.search(r'(?<![A-Za-z0-9])'+re.escape(alias)+r'(?![A-Za-z0-9])', name, re.I))
        return alias in name
    return sorted({a['ticker'] for a in aliases if a['status'] == 'confirmed' and matches(a['alias'])})


def backtest_aliases(payload):
    unified = payload.get('unified') or payload
    truths = {}
    for rel in unified.get('relations') or []:
        ticker = str((rel.get('stockIdentity') or {}).get('ticker') or rel.get('ticker') or '')
        if rel.get('level') == 'A' and rel.get('status') == 'verified' and ticker:
            truths.setdefault((str(rel.get('chainId')), str(rel.get('token')).lower()), set()).add(ticker)
    aliases = alias_candidates()
    tp = fp = fn = 0
    records = []
    for asset in unified.get('assets') or []:
        key = str(asset.get('chainId')), str(asset.get('token')).lower()
        if key not in truths:
            continue
        # Relations never enter the predictor input; held-out truth scores it.
        predicted = set(alias_matches(' '.join(str(asset.get(k) or '') for k in ('name', 'symbol')), aliases))
        correct, unexpected, missed = predicted & truths[key], predicted-truths[key], truths[key]-predicted
        tp += len(correct); fp += len(unexpected); fn += len(missed)
        records.append({'chainId': key[0], 'token': key[1], 'name': asset.get('name'), 'predicted': sorted(predicted), 'actual': sorted(truths[key])})
    return {'sampleCount': len(records), 'truePositive': tp, 'falsePositive': fp, 'falseNegative': fn,
            'precision': tp/(tp+fp) if tp+fp else None, 'recall': tp/(tp+fn) if tp+fn else None,
            'items': records[:100], 'method': 'confirmed-dictionary-whole-word; relationship-hidden-input', 'rankEligible': False}
