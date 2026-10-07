"""Free, batched DexScreener liquidity supplements, owned by the worker.

At most eight 30-contract requests per minute. Indexed returned pools form an
explicit lower bound, never an assertion of all-chain coverage. This writes
separate observations and cannot replace an OKX primary quote or chain proof.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time

import httpx

from ..config import bounded_env_int
from ..db import store
from ..demand_leases import all_leases

ADDRESS = re.compile(r'0x[0-9a-f]{40}\Z', re.I)
NETWORKS = {'196': 'xlayer', '56': 'bsc', '4663': 'robinhood', '5042': 'arc'}
API = 'https://api.dexscreener.com'
MAX_AGE = 20*60_000
_lock = asyncio.Lock()


def _number(value, signed=False):
    if value is None or isinstance(value, bool) or value == '':
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and (signed or 0 <= value < 1e10) else None


def _address(value):
    text = str(value or '').lower()
    return text if ADDRESS.fullmatch(text) else None


def normalize_batch(chain, tokens, rows, at):
    network = NETWORKS.get(str(chain))
    if not network or not isinstance(rows, list):
        raise ValueError('invalid-indexed-market-batch')
    expected = set(tokens)
    indexed = {token: {} for token in expected}
    pools = {}
    conflicts = set()
    for raw in rows:
        if not isinstance(raw, dict) or raw.get('chainId') != network:
            continue
        pool = _address(raw.get('pairAddress'))
        if not all(isinstance(raw.get(field), dict) for field in ('baseToken', 'quoteToken')):
            continue
        base, quote = (_address(raw[field].get('address')) for field in ('baseToken', 'quoteToken'))
        if not pool or not base or not quote or base == quote or not ({base, quote} & expected):
            continue
        if pool in pools and {pools[pool]['token0'], pools[pool]['token1']} != {base, quote}:
            conflicts.add(pool)
            continue
        volume = _number(raw['volume'].get('h24')) if isinstance(raw.get('volume'), dict) else None
        liquidity = _number(raw['liquidity'].get('usd')) if isinstance(raw.get('liquidity'), dict) else None
        txs = raw['txns'].get('h24') if isinstance(raw.get('txns'), dict) else None
        txs = txs if isinstance(txs, dict) else {}
        changes = raw.get('priceChange') if isinstance(raw.get('priceChange'), dict) else {}
        buys, sells = _number(txs.get('buys')), _number(txs.get('sells'))
        buys = buys if buys is not None and buys.is_integer() else None
        sells = sells if sells is not None and sells.is_integer() else None
        market = {'scope': 'pool:'+pool, 'provider': 'DexScreener', 'updatedAt': at,
            'volumeCurrency': 'USD', 'volume24h': volume, 'buys24h': buys, 'sells24h': sells,
            'priceChange': {window: _number(changes.get(window), signed=True) for window in ('m5', 'h1', 'h6', 'h24')},
            'baseToken': base, 'quoteToken': quote, 'fdv': _number(raw.get('fdv'))}
        entry = {'pool': pool, 'token0': base, 'token1': quote, 'poolMarket': market,
            'liquidityUsd': liquidity, 'liquidityAt': at if liquidity is not None else None,
            'liquidityProvider': 'DexScreener', 'liquidityTimeKind': 'observed'}
        pools[pool] = entry
        for token in {base, quote} & expected:
            indexed[token][pool] = entry
    for pool in conflicts:
        pools.pop(pool, None)
        for entries in indexed.values():
            entries.pop(pool, None)
    assets = {}
    for token, entries in indexed.items():
        if not entries:
            continue  # Empty index response is not evidence of zero liquidity.
        liquidity_rows = [p for p in entries.values() if p['liquidityUsd'] is not None]
        volume_rows = [p for p in entries.values() if p['poolMarket']['volume24h'] is not None]
        all_liquidity = len(liquidity_rows) == len(entries)
        all_volume = len(volume_rows) == len(entries)
        liquid = sum(p['liquidityUsd'] for p in liquidity_rows) if all_liquidity else None
        volume = sum(p['poolMarket']['volume24h'] for p in volume_rows) if all_volume else None
        assets[token] = {'aggregateMarket': {'provider': 'DexScreener', 'scope': 'token-aggregate',
            'coverage': 'provider-indexed-pools', 'complete': False, 'indexedPools': len(entries),
            'liquidityUsd': liquid, 'liquidityAt': at if liquid is not None else None,
            'volume24h': volume, 'volumeAt': at if volume is not None else None,
            'status': 'current' if liquid is not None else 'partial',
            'resultLimitSuspected': len(rows) >= 30, 'bound': 'returned-indexed-pools',
            'sourceUrl': 'https://docs.dexscreener.com/api/reference'}}
        # Dex FDV describes baseToken. Select an observed liquid pool, retaining
        # the valuation's pool evidence, and never copy it to the quote token.
        main = max(liquidity_rows, key=lambda row: (row['liquidityUsd'], row['pool']), default=None)
        if main and main['liquidityUsd'] > 0 and main['token0'] == token and main['poolMarket']['fdv'] is not None:
            assets[token].update({'fdv': main['poolMarket']['fdv'],
                'fieldTimes': {'fdv': at}, 'fieldSources': {'fdv': 'DexScreener'},
                'fieldScopes': {'fdv': 'token'}, 'fieldTimeKinds': {'fdv': 'observed'},
                'fieldObservations': {'fdv': {'provider': 'DexScreener', 'scope': 'token',
                    'observedAt': at, 'receivedAt': at, 'currency': 'USD',
                    'pool': main['pool'], 'baseToken': token, 'method': 'provider-base-token-fdv'}}})
        activity = [p for p in entries.values() if p['poolMarket']['buys24h'] is not None and p['poolMarket']['sells24h'] is not None]
        if len(activity) == len(entries):
            # Dex buys/sells are relative to baseToken. Reverse for quote side.
            buys = sum(p['poolMarket']['buys24h' if p['token0'] == token else 'sells24h'] for p in activity)
            sells = sum(p['poolMarket']['sells24h' if p['token0'] == token else 'buys24h'] for p in activity)
            assets[token]['dexActivity'] = {'provider': 'DexScreener', 'scope': 'token-aggregate',
                'buys24h': buys, 'sells24h': sells, 'txs24h': buys+sells, 'at': at,
                'coverage': 'provider-indexed-pools', 'complete': False}
    return assets, pools


async def refresh_market_enrichment():
    async with _lock:
        return await _refresh()


async def _asset_page(s, after, limit):
    """Use the facts primary key; every asset remains reachable next cycle."""
    rows = await s.fetchall('SELECT id,body FROM facts WHERE kind=? AND id>? ORDER BY id LIMIT ?',
                           (s.key('asset'), after, limit))
    if not rows and after:
        rows = await s.fetchall('SELECT id,body FROM facts WHERE kind=? AND id>? ORDER BY id LIMIT ?',
                               (s.key('asset'), '', limit))
    cursor = str(rows[-1][0]) if len(rows) == limit else ''
    return [json.loads(row[1]) for row in rows], cursor


def _priority_page(tokens, after, limit):
    ordered = sorted(tokens)
    remaining = [token for token in ordered if token > after]
    if not remaining:
        remaining = ordered
    page = remaining[:limit]
    return page, page[-1] if len(remaining) > limit else ''


def _choose_batches(work, limit, chain_cursor, round_number):
    """Rotate chains and reserve bounded progress for unobserved ordinary coins.

    Page cursors bound local reads; attempts order eligible coins oldest first.
    A fair ordinary batch prevents an endless watch set starving discovery.
    With a one-request allowance it gets one turn in four instead.
    """
    chains = list(NETWORKS)
    offset = chain_cursor % len(chains)
    order = chains[offset:]+chains[:offset]
    queues = {chain: sorted((row for row in work if row[2] == chain),
                            key=lambda row: (row[0], row[1])) for chain in order}
    fair = None
    if limit > 1 or round_number % 4 == 3:
        for chain in order:
            fair = next((row for row in queues[chain] if row[0] == 2), None)
            if fair:
                queues[chain].remove(fair)
                break
    selected = []
    allowance = limit-int(fair is not None)
    while len(selected) < allowance:
        progressed = False
        for chain in order:
            if queues[chain] and len(selected) < allowance:
                selected.append(queues[chain].pop(0))
                progressed = True
        if not progressed:
            break
    if fair:
        selected.append(fair)
    return selected


async def _refresh():
    now = int(time.time()*1000)
    system = await store('system')
    attempts = await system.get('collector', 'dex-batch-market') or {}
    if now < (attempts.get('nextAttemptAt') or 0):
        return {'requested': 0, 'accepted': 0, 'updated': 0, 'skipped': 1}
    limit = bounded_env_int('DEX_MARKET_BATCH_LIMIT', 8, 1, 8)
    scan_limit = bounded_env_int('DEX_MARKET_SCAN_LIMIT', 256, 30, 2048)
    scan_cursors = dict(attempts.get('scanCursors') or {})
    priority_cursors = dict(attempts.get('priorityCursors') or {})
    chain_cursor = int(attempts.get('requestChainCursor') or 0)
    round_number = int(attempts.get('round') or 0)
    work = []
    stores = {}
    relation_ids = {}
    scanned = 0
    for chain in NETWORKS:
        if chain == '5042':
            from .arc_poller import configured
            if not configured():
                continue
        s = stores[chain] = await store(chain)
        watches = {token for row in await all_leases(s, 'watch')
                   if row.get('expiresAt', 0) > now and (token := _address(row.get('token')))}
        # Only identities are needed to select requests. Do not materialize
        # reserves, histories and field metadata for every known relation.
        relations = await s.fetchall("SELECT id,json_extract(body,'$.token'),json_extract(body,'$.pool') "
            "FROM facts WHERE kind=? AND json_extract(body,'$.status')='verified'", (s.key('relation'),))
        related = {str(row[1]).lower() for row in relations if row[1]}
        pool_ids = relation_ids[chain] = {}
        for ident, _, pool in relations:
            if pool:
                pool_ids.setdefault(str(pool).lower(), []).append(str(ident))
        priority_tokens, priority_cursors[chain] = _priority_page(
            {token for value in watches | related if (token := _address(value))},
            str(priority_cursors.get(chain) or ''), scan_limit)
        assets, scan_cursors[chain] = await _asset_page(s, str(scan_cursors.get(chain) or ''), scan_limit)
        assets_by_token = {_address(asset.get('token')): asset for asset in assets if _address(asset.get('token'))}
        assets_by_token.update(await s.get_many('asset', priority_tokens))
        scanned += len(assets_by_token)
        attempt_states = await s.get_many('dex-market-attempt', assets_by_token)
        candidates = []
        for asset in assets_by_token.values():
            token = _address(asset.get('token'))
            at = (asset.get('aggregateMarket') or {}).get('liquidityAt') or 0
            attempt = attempt_states.get(token)
            if asset.get('kind') != 'candidate' or not token or (attempt or {}).get('nextAttemptAt', 0) > now:
                continue
            target = 5*60_000 if token in watches or token in related else MAX_AGE
            if 0 < at <= now and now-at < target:
                continue
            priority = 0 if token in watches else 1 if token in related else 2
            candidates.append((priority, (attempt or {}).get('at', 0), token))
        candidates.sort()
        for offset in range(0, len(candidates), 30):
            batch = candidates[offset:offset+30]
            work.append((batch[0][0], batch[0][1], chain, [row[2] for row in batch]))
    selected = _choose_batches(work, limit, chain_cursor, round_number)
    cursor_state = {'scanCursors': scan_cursors, 'priorityCursors': priority_cursors,
                    'requestChainCursor': (chain_cursor+1) % len(NETWORKS),
                    'round': round_number+1, 'scanLimitPerChain': scan_limit,
                    'assetsExamined': scanned}
    # Preserve scan progress even if a provider disconnects or a round is
    # cancelled. Unselected eligible assets return on a later complete cycle.
    await system.put('collector', 'dex-batch-market', {**attempts, **cursor_state,
        'at': now, 'nextAttemptAt': now+60_000, 'status': 'running'})
    result = {'requested': 0, 'accepted': 0, 'updated': 0, 'failed': 0, 'skipped': 0}
    async with httpx.AsyncClient(timeout=10, headers={'Accept': 'application/json'}) as client:
        for index, (_, _, chain, tokens) in enumerate(selected):
            result['requested'] += 1
            s = stores[chain]
            at = int(time.time()*1000)
            for token in tokens:
                await s.put('dex-market-attempt', token, {'at': at, 'nextAttemptAt': at+5*60_000})
            try:
                response = await client.get(API+'/tokens/v1/'+NETWORKS[chain]+'/'+','.join(tokens))
                response.raise_for_status()
                assets, pools = normalize_batch(chain, tokens, response.json(), int(time.time()*1000))
                for token, patch in assets.items():
                    if 'fdv' in patch:
                        # Merge nested field clocks under the writer lock so a
                        # supplement cannot erase or restore the primary quote.
                        fdv = patch.pop('fdv')
                        await s.merge_asset_observation(token, patch, {'fdv': (fdv, patch['fieldTimes']['fdv'])})
                    else:
                        await s.patch_fact('asset', token, patch)
                    result['accepted'] += 1
                # Only already-verified relation identities receive valuations.
                # An index response cannot create an official relationship.
                ids = {ident for pool in pools for ident in relation_ids[chain].get(pool, ())}
                for relation in (await s.get_many('relation', ids)).values():
                    pool = pools.get(str(relation.get('pool') or '').lower())
                    if relation.get('status') != 'verified' or not pool or {relation.get('token0'), relation.get('token1')} != {pool['token0'], pool['token1']}:
                        continue
                    patch = {key: value for key, value in pool.items() if key not in ('pool', 'token0', 'token1') and value is not None}
                    await s.patch_fact('relation', relation['id'], patch)
                    result['updated'] += 1
                result['skipped'] += len(tokens)-len(assets)
            except (httpx.HTTPError, ValueError) as exc:
                result['failed'] += 1
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                    await system.put('collector', 'dex-batch-market', {**result, **cursor_state,
                        'at': now, 'nextAttemptAt': now+5*60_000, 'status': 'rate-limited'})
                    return result
            if index+1 < len(selected):
                await asyncio.sleep(0.6)
    await system.put('collector', 'dex-batch-market', {**result, **cursor_state, 'at': now,
        'nextAttemptAt': now+60_000, 'status': 'partial' if result['failed'] or result['skipped'] else 'ready',
        'scope': 'returned-indexed-pools', 'complete': False, 'requestLimitPerMinute': 8})
    return result
