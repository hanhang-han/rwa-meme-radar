"""Single-owner, budgeted OKX quotes: core 5m, candidates 25m, stocks 55m.

Every eligible asset has a baseline lane, including unpaired candidates.
Persistent last-attempt and retry checkpoints provide fairness after restarts.
Watching joins the bounded core allocation; visitors cannot create extra calls.
"""
import os
import sqlite3
import time
from math import isfinite
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ..db import store
from ..demand_leases import all_leases
from ..okx_client import okx_post, request_lane, QuotaExceeded
from ..stream_hub import broadcast
from .assets import now_ms, save_asset
from .queue import checkpoint, combine, due, jobs, result

CHAINS = ('196', '56', '4663')
CORE_COUNTS = {'196': 100, '56': 30, '4663': 20}
_watched: dict[tuple[str, str], float] = {}


def valid_token(token):
    return isinstance(token, str) and token.startswith('0x') and len(token) == 42


def price_time(asset):
    try:
        at = float((asset.get('fieldTimes') or {}).get('price') or asset.get('quoteAt') or 0)
    except (TypeError, ValueError):
        return 0
    return at if isfinite(at) and at > 0 else 0


def fresh_price(asset, now, interval):
    """Judge quote coverage by the stored price observation, not job success."""
    try:
        price = float(asset.get('price'))
        at = price_time(asset)
    except (TypeError, ValueError):
        return False
    return isfinite(price) and price > 0 and 0 < at <= now + 1000 and now - at <= interval


def quote_due(asset, job, now, interval):
    if (job.get('nextRetryAt') or 0) > now:
        return False
    return not fresh_price(asset, now, interval) or due(job, now, interval)


async def _fetch_entries(entries, lane, priority='background'):
    """Batch across chains, accepting only requested chain/address identities."""
    totals = result()
    baseline_age = {'base-candidates': 1_800_000, 'base-stocks': 3_600_000}.get(lane)
    for start in range(0, len(entries), 50):
        batch = list(dict.fromkeys((str(c), t.lower()) for c, t in entries[start:start + 50]))
        expected = set(batch)
        stores = {c: await store(c) for c, _ in batch}
        totals['requested'] += 1
        try:
            rows = await okx_post('/api/v6/dex/market/price-info', [
                {'chainIndex': c, 'tokenContractAddress': t} for c, t in batch
            ], {'skip_round': True, 'priority': priority})
            if not isinstance(rows, list):
                raise ValueError('Invalid quote response schema')
        except QuotaExceeded:
            totals['quotaBlocked'] += 1
            break
        except Exception as error:
            totals['failed'] += len(batch)
            for c, t in batch:
                await checkpoint(stores[c], 'quote', t, success=False, reason=type(error).__name__)
            continue
        answered = set()
        for row in rows:
            c, t = str(row.get('chainIndex')), str(row.get('tokenContractAddress') or '').lower()
            if (c, t) not in expected or (c, t) in answered:
                continue
            # Metadata without a usable quote does not advance quote success.
            try:
                usable = float(row.get('price')) > 0 and float(row.get('price')) < float('inf')
            except (TypeError, ValueError):
                usable = False
            updated, changed = await save_asset(stores[c], row, return_changed=True)
            if not updated or not usable:
                continue
            if baseline_age is not None and not fresh_price(updated, now_ms(), baseline_age):
                answered.add((c, t))
                totals['unsupported'] += 1
                await checkpoint(stores[c], 'quote', t, success=False, reason='stale-price-observation')
                continue
            answered.add((c, t))
            await checkpoint(stores[c], 'quote', t, success=True)
            totals['accepted'] += 1
            if not changed:
                continue
            totals['updated'] += 1
            source = (updated.get('fieldObservations') or {}).get('price') or {}
            broadcast('price', {
                'chainId': c, 'token': t, 'price': updated.get('price'),
                'change24h': updated.get('change24h'), 'at': (updated.get('fieldTimes') or {}).get('price'),
                'receivedAt': source.get('receivedAt') or now_ms(), 'timeKind': source.get('timeKind', 'received'),
                'venue': 'dex', 'quoteType': 'dex', 'provider': 'OKX',
            })
        for c, t in expected - answered:
            totals['unsupported'] += 1
            await checkpoint(stores[c], 'quote', t, success=False, reason='missing-price-row')
    return totals


async def _fetch_and_merge(cid, tokens, lane, priority='background'):
    return await _fetch_entries([(cid, t) for t in tokens if valid_token(t)], lane, priority)


async def refresh_quotes(assets):
    return await _fetch_and_merge('196', [a.get('token') for a in assets], 'core-quotes')


async def refresh_quotes_chain(cid, assets):
    return await _fetch_and_merge(cid, [a.get('token') for a in assets], 'core-quotes')


def watch_asset(chain, token):
    if chain in CHAINS and valid_token(token):
        _watched[(chain, token.lower())] = time.time()


async def quote_on_demand(chain, address):
    watch_asset(chain, address)


async def core_assets(cid, s, now, queue=None):
    assets = [a for a in await s.all('asset') if a.get('kind') in ('stock', 'candidate') and valid_token(a.get('token'))]
    watches = {x.get('token') for x in await all_leases(s, 'watch') if (x.get('expiresAt') or 0) > now}
    watches |= {t for (c, t), at in _watched.items() if c == cid and now / 1000 - at < 60}
    relations = [r for r in await s.all('relation') if r.get('status') == 'verified']
    related = {r.get('token') for r in relations} | {r.get('stock') for r in relations}
    basket = {m.get('token') for b in await s.all('basket') for m in b.get('members', [])}
    # Stable eligibility/ranking. Quote age determines queue order only after
    # selecting the bounded core; it cannot make thousands of rows "core".
    queue = queue or {}
    assets.sort(key=lambda a: (a['token'] not in watches, (queue.get(a['token'], {}).get('lastAttemptAt') or 0) if a['token'] in watches else 0, a['token'] not in basket,
                              a['token'] not in related, -(a.get('volume24h') or 0), a['token']))
    return assets[:CORE_COUNTS[cid]]


async def refresh_live_quotes():
    now, totals = now_ms(), result()
    with request_lane('core-quotes', 1152):
        for cid in CHAINS:
            s = await store(cid)
            queue = await jobs(s, 'quote')
            work = [a for a in await core_assets(cid, s, now, queue) if due(queue.get(a['token'], {}), now, 300_000)]
            work.sort(key=lambda a: ((queue.get(a['token'], {}).get('lastAttemptAt') or 0), a['token']))
            if work:
                combine(totals, await _fetch_and_merge(cid, [a['token'] for a in work], 'core-quotes', 'interactive'))
    return totals


async def refresh_watched():
    """Compatibility entry point; watches are serviced by the same 5m core lane."""
    return await refresh_live_quotes()


async def _baseline(kind, interval, lane, limit, max_batches, now=None):
    now, work, soon = now if now is not None else now_ms(), [], []
    coverage_interval = 1_800_000 if kind == 'candidate' else interval
    for cid in CHAINS:
        s = await store(cid)
        queue = await jobs(s, 'quote')
        for a in await s.all('asset'):
            token = a.get('token')
            if a.get('kind') != kind or not valid_token(token):
                continue
            job = queue.get(token, {})
            if quote_due(a, job, now, interval):
                work.append((job.get('lastAttemptAt') or 0, cid, token))
            elif (job.get('nextRetryAt') or 0) <= now:
                at = price_time(a)
                expires_in = at + coverage_interval - now
                # A 15m lookahead smooths clustered coverage expiries. The
                # spacing prevents an unchanged provider timestamp from
                # consuming the spare positions every five minutes.
                if 0 < expires_in <= 900_000 and now - (job.get('lastAttemptAt') or 0) >= 900_000:
                    soon.append((at + interval, job.get('lastAttemptAt') or 0, cid, token))
    work.sort()
    selected = [(cid, token) for _, cid, token in work[:50 * max_batches]]
    if soon:
        soon.sort()
        # Due quotes always take their positions first. Use remaining batches
        # for impending expiries, including whole batches that were formerly
        # idle when the due work ended on a 50-token boundary.
        capacity = 50 * max_batches
        selected.extend((cid, token) for _, _, cid, token in soon[:capacity - len(selected)])
    with request_lane(lane, limit):
        return await _fetch_entries(selected, lane)


async def refresh_base_candidates():
    # Six 50-token batches per five-minute run cover up to 1,500 candidates
    # in a 25-minute scheduling window. The published freshness threshold
    # remains 30 minutes; the earlier target absorbs polling and source lag.
    # 6 * 288 = 1,728 daily batches, still inside the shared background cap.
    return await _baseline('candidate', 1_500_000, 'base-candidates', 1728, 6)


async def refresh_base_stocks():
    now = now_ms()
    used = stock_lane_used(now)
    # Six batches per five-minute slot provide 3,600 quote positions/hour
    # for ~3,000 stock assets, leaving room for upstream and scheduler lag.
    # The published freshness threshold remains 60 minutes.
    return await _baseline('stock', 3_300_000, 'base-stocks', 1728,
                           stock_batch_allowance(now, used), now=now)


def stock_batches_scheduled_through(at_ms):
    """Six batches per UTC slot: 3,600 rows/hour, 1,728 batches/day."""
    slots = int(at_ms // 300_000) % 288 + 1
    return slots * 6


def stock_lane_used(at_ms):
    """Read the shared lane ledger; never charge it to inspect catch-up debt."""
    path = Path(os.environ.get('OKX_LEDGER_PATH', 'data/okx-budget.sqlite'))
    if not path.is_file():
        return None
    day = datetime.fromtimestamp(at_ms / 1000, timezone.utc).strftime('%Y-%m-%d')
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.25)) as db:
            row = db.execute('SELECT used FROM lane_budget WHERE day=? AND lane=?',
                             (day, 'base-stocks')).fetchone()
        return max(0, int(row[0])) if row else 0
    except (OSError, sqlite3.Error, ValueError):
        # On a first start or a busy ledger, retain the original paced limit.
        # The charging path still enforces the shared daily and lane caps.
        return None


def stock_batch_allowance(at_ms, used):
    """Spend at most six batches each run without front-loading the day.

    The shared ledger is the source of truth for what actually ran before a
    restart. Never spend beyond today's cumulative allocation; repeated calls
    within one slot get no new credit. A missed slot can be recovered across
    later slots without exceeding the six-batch rate or 1,728-batch daily cap.
    """
    base = 6
    if used is None:
        return base
    return max(0, min(base, stock_batches_scheduled_through(at_ms) - used))


async def refresh_side_quotes():
    """Compatibility entry point; the baseline now covers all three chains."""
    return combine(await refresh_base_candidates(), await refresh_base_stocks())
