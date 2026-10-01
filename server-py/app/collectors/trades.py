"""On-demand trade refresh for assets a detail page is watching: one page of
recent trades, persisted and broadcast, so the activity feed keeps rolling
between scan rounds. Ports the Node refreshAssetOnDemand core.

Requests are per (chain, address): a shared in-flight set deduplicates
concurrent views of the same asset, and interactive calls skip the scan-round
budget so the collection round can never stall a user-facing refresh."""
import asyncio

from ..db import store
from ..demand_leases import all_leases
from ..okx_client import okx_get, request_lane, QuotaExceeded
from ..stream_hub import broadcast
from .assets import now_ms
from .queue import checkpoint, due, jobs, result

_inflight: dict[tuple[str, str], asyncio.Task] = {}
_gate = asyncio.Lock()
BASE_QUOTE_SYMBOLS = {
    "USDT", "USDC", "USDG", "DAI", "WOKB", "OKB", "WETH", "ETH", "WBTC", "BTC", "XBTC",
    "WBNB", "BTCB", "WTBC", "USD1", "XUSD",
}


def _num(v):
    try:
        n = float(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def normalize_trade(row: dict, token: str, chain: str) -> dict | None:
    try:
        t = int(row.get("time"))
    except (TypeError, ValueError):
        t = None
    if str(row.get("chainIndex")) != chain or (row.get("tokenContractAddress") or "").lower() != token:
        return None
    if not row.get("id") or not t or row.get("type") not in ("buy", "sell"):
        return None
    tx = row.get("txHashUrl")
    return {
        "id": str(row["id"]), "t": t, "type": row["type"],
        "price": _num(row.get("price")), "volume": _num(row.get("volume")),
        "user": (row.get("userAddress") or "").lower() or None,
        "hash": tx if isinstance(tx, str) and tx.startswith("0x") and len(tx) == 66 else None,
        "dex": str(row.get("dexName") or ""), "source": "OKX trades",
    }


async def refresh_asset_on_demand(chain: str, address: str, priority: str = "interactive"):
    key = (chain, address)
    task = _inflight.get(key)
    if task and not task.done():
        return result(skipped=1)
    task = asyncio.ensure_future(_run_on_demand(chain, address, priority))
    _inflight[key] = task
    try:
        return await task
    finally:
        _inflight.pop(key, None)


def _gap_id(gap):
    return f"{gap['from']}:{gap['to']}"


def merge_gaps(existing, new):
    """Keep each missing interval until its own cursor reaches its lower edge."""
    rows = {g.get('id') or _gap_id(g): dict(g, id=g.get('id') or _gap_id(g)) for g in existing}
    if new:
        key = _gap_id(new)
        rows.setdefault(key, {**new, 'id': key})
    return sorted(rows.values(), key=lambda g: (g.get('lastAttemptAt') or 0, g['from']))


async def _run_on_demand(chain, address, priority="interactive"):
    totals, s = result(), await store(chain)
    asset = await s.get("asset", address)
    if not asset:
        return result(skipped=1)
    job = await s.get('collector-job', 'trades:' + address) or {}
    if not due(job, now_ms(), 300_000):
        return result(skipped=1)
    previous = await s.recent_trades(address, 1)
    checkpoint_body = await s.get('trade-gaps', address) or {}
    gaps = checkpoint_body.get('intervals') or []
    # Recover the legacy marker without assuming that today's newest page
    # fills an older gap. A missing cursor restarts the bounded walk at head.
    legacy = asset.get('tradeGap')
    if not checkpoint_body and isinstance(legacy, dict) and legacy.get('from') and legacy.get('to'):
        gaps = merge_gaps(gaps, {**legacy, 'cursor': None, 'observationFrom': legacy['from'], 'observationTo': legacy['to']})
    observed_at = now_ms()
    all_rows, fresh = [], []
    current_gap = False
    try:
        with request_lane('trades', 1000):
            totals['requested'] += 1
            head = await okx_get('/api/v6/dex/market/trades', {
                'chainIndex': chain, 'tokenContractAddress': address, 'limit': '100',
            }, {'skip_round': True, 'priority': priority})
            if not isinstance(head, list):
                raise ValueError('Invalid trades response')
            parsed = [r for r in (normalize_trade(x, address, chain) for x in head) if r]
            if head and not parsed:
                raise ValueError('No valid trades in nonempty response')
            # A first full page does not prove a complete 24h window.
            target = previous[0]['t'] if previous else observed_at - 86_400_000
            oldest = min((r['t'] for r in parsed), default=observed_at)
            current_gap = len(head) >= 100 and oldest > target
            new_gap = {'from': target, 'to': oldest, 'cursor': str(head[-1]['id']),
                       'detectedAt': observed_at, 'observationFrom': asset.get('tradeAt') or target,
                       'observationTo': observed_at} if current_gap else None
            gaps = merge_gaps(gaps, new_gap)
            all_rows.extend(parsed)
            # Persist head before advancing any cursor. If interrupted, replay
            # is idempotent and the old checkpoint continues to describe work.
            await s.put_trades(address, [{**r, 'chainId': chain, 'token': address, 'symbol': asset.get('symbol')} for r in parsed])
            await s.put('trade-gaps', address, {'token': address, 'intervals': gaps, 'updatedAt': observed_at})
            if asset.get('tradeAt') and not current_gap:
                await s.record_trade_observation(address, int(asset['tradeAt']), observed_at)
            # One old interval per round. Its cursor, not the newest stored
            # trade, determines how far back the next round resumes.
            pending = [g for g in gaps if (g.get("nextRetryAt") or 0) <= observed_at]
            if pending:
                gap = pending[0]
                params = {'chainIndex': chain, 'tokenContractAddress': address, 'limit': '100'}
                if gap.get('cursor'):
                    params['after'] = str(gap['cursor'])
                totals['requested'] += 1
                try:
                    page = await okx_get('/api/v6/dex/market/trades', params,
                                         {'skip_round': True, 'priority': priority})
                    if not isinstance(page, list):
                        raise ValueError('Invalid backfill response')
                    older = [r for r in (normalize_trade(x, address, chain) for x in page) if r]
                    if page and not older:
                        raise ValueError('No valid backfill rows')
                    all_rows.extend(older)
                    await s.put_trades(address, [{**r, 'chainId': chain, 'token': address, 'symbol': asset.get('symbol')} for r in older])
                    reached = bool(older) and min(r['t'] for r in older) <= gap['from']
                    if reached:
                        await s.record_trade_observation(address, int(gap.get('observationFrom') or gap['from']),
                                                         int(gap.get('observationTo') or gap['to']))
                        gaps.remove(gap)
                    elif not page:
                        gap['nextRetryAt'] = observed_at + 3_600_000
                        gap['reason'] = 'provider-history-boundary'
                        totals['unsupported'] += 1
                    else:
                        cursor = str(page[-1]['id'])
                        if cursor == gap.get('cursor'):
                            raise ValueError('Backfill cursor did not advance')
                        gap['cursor'] = cursor
                        gap['to'] = min(gap['to'], min(r['t'] for r in older))
                        gap['lastAttemptAt'] = observed_at
                except QuotaExceeded:
                    totals['quotaBlocked'] += 1
                except Exception:
                    totals['failed'] += 1
                    gap['lastAttemptAt'] = observed_at
            await s.put('trade-gaps', address, {'token': address, 'intervals': gaps, 'updatedAt': observed_at})
        # The checkpoint has been persisted before the head anchor moves.
        rows = sorted({r['id']: r for r in all_rows}.values(), key=lambda r: -r['t'])
        # Track arrival IDs separately: head was already idempotently saved.
        seen = set(checkpoint_body.get('recentIds') or [])
        fresh = [r for r in rows if r['id'] not in seen and (not previous or r['t'] >= previous[0]['t'])]
        await s.put('trade-gaps', address, {'token': address, 'intervals': gaps, 'updatedAt': observed_at,
                                           'recentIds': [r['id'] for r in rows[:200]]})
        buckets = await s.rebuild_trade_buckets(address, observed_at)
        patch = {'tradeGap': gaps[0] if gaps else None, 'tradeGapCount': len(gaps),
                 'tradeCoverage': 'partial-gap' if gaps else 'window' if previous else 'observed', 'tradeAt': observed_at}
        oldest = min((r['t'] for r in rows), default=asset.get('oldestTradeAt'))
        if oldest:
            patch['oldestTradeAt'] = min(asset.get('oldestTradeAt') or oldest, oldest)
        timed_fields = {}
        for bar, suffix in (('1m', '1m'), ('5m', '5m'), ('1h', '1h')):
            bucket = buckets.get(bar)
            if bucket:
                timed_fields[f'observedVolume{suffix}'] = (bucket.get('volumeUsd'), bucket['closeTime'])
                timed_fields[f'observedTrades{suffix}'] = (bucket.get('tradeCount'), bucket['closeTime'])
                patch[f'observedBucket{suffix}Complete'] = bool(bucket.get('complete'))
                patch[f'observedBucket{suffix}Coverage'] = bucket.get('coverageRatio')
        await s.merge_asset_observation(address, patch, timed_fields)
        await checkpoint(s, 'trades', address, success=True)
        totals['accepted'] += 1  # an explicit empty provider window is valid
        totals['updated'] += len(rows)
        if fresh:
            broadcast('trade', {'chainId': chain, 'token': address, 'fresh': [
                {**r, 'chainId': chain, 'token': address, 'symbol': asset.get('symbol')} for r in fresh
            ]})
    except QuotaExceeded:
        totals['quotaBlocked'] += 1
    except Exception as error:
        totals['failed'] += 1
        await checkpoint(s, 'trades', address, success=False, reason=type(error).__name__)
    return totals


async def refresh_hot_trades():
    """Single 3m lane for active pages and bounded market hotspots (<=960/day)."""
    candidates, now = [], now_ms()
    for chain in ('196', '56', '4663'):
        s = await store(chain)
        queue = await jobs(s, 'trades')
        watches = {w.get('token') for w in await all_leases(s, 'watch') if (w.get('expiresAt') or 0) > now}
        related = {r.get('token') for r in await s.all('relation') if r.get('status') == 'verified'}
        assets = [a for a in await s.all('asset') if a.get('kind') in ('candidate', 'stock')
                  and str(a.get('symbol') or '').upper() not in BASE_QUOTE_SYMBOLS]
        hot = sorted([a for a in assets if a.get('kind') == 'candidate'], key=lambda a: (a.get('token') not in related, -(a.get('volume24h') or 0)))[:6]
        selected = {a['token']: a for a in assets if a.get('token') in watches}
        selected.update({a['token']: a for a in hot})
        for token, asset in selected.items():
            job = queue.get(token, {})
            if due(job, now, 300_000):
                # Watched pages receive priority, but a waiting market asset
                # older than one hour cannot be starved indefinitely.
                attempted = job.get('lastAttemptAt') or 0
                priority = 0 if now - attempted > 3_600_000 else int(token not in watches)
                candidates.append((priority, attempted, chain, token))
    if not candidates:
        return result(skipped=1)
    _, _, chain, token = min(candidates)
    return await refresh_asset_on_demand(chain, token, 'interactive')


async def refresh_watched_trades():
    return await refresh_hot_trades()
