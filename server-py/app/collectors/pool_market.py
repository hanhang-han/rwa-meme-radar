"""Bounded direct-pool observations, isolated from research writers and scans.

Only existing chain-verified official-side pools are queried. Index absence is
not zero. Producer files retain original observation clocks across retries.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from ..live_market_store import catalogue_store
from ..stock_identity import token_identity
from .market_enrichment import NETWORKS, _address, _number, normalize_batch

SNAPSHOT = 'data/pool-market.json'
MAX_BATCHES = 8
BATCH_SIZE = 30
GECKO_NETWORKS = {'196': 'x-layer', '4663': 'robinhood'}
DIRECT_CHAINS = ('196', '56', '4663')


def gecko_pairs(chain, body):
    """Adapt verified-network pool rows without turning null metrics into zeros."""
    network = GECKO_NETWORKS.get(chain)
    if not network or not isinstance(body, dict) or not isinstance(body.get('data'), list):
        raise ValueError('invalid-gecko-pool-response')
    out = []
    for raw in body['data']:
        if not isinstance(raw, dict) or raw.get('type') != 'pool':
            continue
        attributes = raw.get('attributes')
        relationships = raw.get('relationships')
        if not isinstance(attributes, dict) or not isinstance(relationships, dict):
            continue
        pool = _address(attributes.get('address'))
        def token(side):
            relation = relationships.get(side)
            datum = relation.get('data') if isinstance(relation, dict) else None
            ident = datum.get('id') if isinstance(datum, dict) else None
            return _address(ident[len(network)+1:]) if isinstance(ident, str) and ident.startswith(network+'_') else None
        base, quote = token('base_token'), token('quote_token')
        if not pool or raw.get('id') != network+'_'+pool or not base or not quote or base == quote:
            continue
        volume = attributes.get('volume_usd')
        out.append({'chainId': NETWORKS[chain], 'pairAddress': pool,
            'baseToken': {'address': base}, 'quoteToken': {'address': quote},
            'liquidity': {'usd': _number(attributes.get('reserve_in_usd'))},
            'volume': {'h24': _number(volume.get('h24')) if isinstance(volume, dict) else None},
            'priceChange': attributes.get('price_change_percentage') or {},
            'txns': attributes.get('transactions') or {}, 'fdv': _number(attributes.get('fdv_usd'))})
    return out


def read_snapshot():
    from ..stock_quotes import _read_snapshot
    return _read_snapshot(SNAPSHOT)


def eligible_relation(row, chain):
    side = str(row.get('stockSide') or row.get('stock') or '').lower()
    sides = {str(row.get('token0') or '').lower(), str(row.get('token1') or '').lower()}
    stock = token_identity(chain, row.get('stock'))
    actual = token_identity(chain, side)
    return (row.get('status') == 'verified' and row.get('verificationStatus') != 'reorged'
        and row.get('confirmationStatus') != 'orphaned' and row.get('pool')
        and len(sides) == 2 and sides == {side, str(row.get('token') or '').lower()}
        and stock['eligibleForPair'] and actual['eligibleForPair']
        and stock['underlyingId'] == actual['underlyingId'])


def overlay_relation(row, snapshot):
    chain = str(row.get('chainId') or '196')
    pool = str(row.get('pool') or '').lower()
    entry = (snapshot.get('pools') or {}).get(chain + ':' + pool)
    if not isinstance(entry, dict) or not eligible_relation(row, chain):
        return row
    if (str(entry.get('chainId')) != chain or entry.get('pool') != pool
            or {entry.get('token0'), entry.get('token1')} != {row.get('token0'), row.get('token1')}):
        return row
    out = dict(row)
    # The new source cannot roll a newer reserve/price valuation backwards.
    if entry.get('liquidityAt') and entry['liquidityAt'] > (row.get('liquidityAt') or 0):
        for field in ('liquidityUsd', 'liquidityAt', 'liquidityProvider', 'liquidityTimeKind'):
            out[field] = entry.get(field)
        out['liquidityMethod'] = 'provider-direct-pool-usd'
    market = entry.get('poolMarket') or {}
    if market.get('updatedAt', 0) > (row.get('poolMarket') or {}).get('updatedAt', 0):
        out['poolMarket'] = market
    return out


class PoolMarketCollector:
    def __init__(self, *, path=SNAPSHOT, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: int(time.time()*1000))
        try:
            self.body = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.body = {}
        self.health = {}

    async def catalogue(self):
        pools = {}
        for chain in DIRECT_CHAINS:
            scoped = await catalogue_store(chain)
            for row in await scoped.all('relation'):
                if eligible_relation(row, chain):
                    pool = str(row['pool']).lower()
                    pools[chain+':'+pool] = {**row, 'pool': pool, 'chainId': chain}
        return pools

    async def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        encoded = json.dumps(self.body, separators=(',', ':'), allow_nan=False)
        await asyncio.to_thread(temp.write_text, encoded)
        os.replace(temp, self.path)

    async def run_once(self, *, fetch=None, metadata=None):
        rows = metadata if metadata is not None else await self.catalogue()
        attempts = {key: value for key, value in (self.body.get('attempts') or {}).items() if key in rows}
        pools = {key: value for key, value in (self.body.get('pools') or {}).items() if key in rows}
        now = self.clock()
        hot, cold = [], []
        for key, row in rows.items():
            job = attempts.get(key) or {}
            if job.get('nextAttemptAt', 0) > now:
                continue
            entry = pools.get(key) or {}
            group = hot if entry.get('liquidityUsd', 0) and entry.get('liquidityUsd', 0) >= 1_000 or row.get('level') == 'A' else cold
            group.append((key, row))
        order = lambda item: (attempts.get(item[0], {}).get('lastAttemptAt', 0), item[0])
        def batches(items):
            items.sort(key=order)
            by_chain = {}
            for item in items:
                by_chain.setdefault(item[1]['chainId'], []).append(item)
            out = [group[offset:offset+BATCH_SIZE] for group in by_chain.values()
                   for offset in range(0, len(group), BATCH_SIZE)]
            return sorted(out, key=lambda batch: order(batch[0]))
        hot_batches, cold_batches = batches(hot), batches(cold)
        selected = hot_batches[:6] + cold_batches[:2]
        selected += (hot_batches[6:]+cold_batches[2:])[:MAX_BATCHES-len(selected)]
        result = {'requested': 0, 'accepted': 0, 'missing': 0, 'failed': 0,
                  'poolCount': len(rows), 'batches': 0}
        self.body = {'pools': pools, 'attempts': attempts}

        async def collect(get):
            for batch in selected:
                chain = batch[0][1]['chainId']
                result['batches'] += 1
                result['requested'] += len(batch)
                error = None
                provider = 'GeckoTerminal' if chain in GECKO_NETWORKS else 'DexScreener'
                try:
                    body = await get(chain, [row['pool'] for _, row in batch])
                    if not isinstance(body, dict) or 'pairs' not in body or body['pairs'] is not None and not isinstance(body['pairs'], list):
                        raise ValueError('invalid-direct-pool-response')
                    at = self.clock()
                    tokens = {row[side] for _, row in batch for side in ('token0', 'token1')}
                    _, observed = normalize_batch(chain, tokens, body['pairs'] or [], at)
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    observed = {}
                    error = type(exc).__name__
                for key, row in batch:
                    entry = observed.get(row['pool'])
                    valid = (entry and {entry['token0'], entry['token1']} == {row['token0'], row['token1']}
                             and entry.get('liquidityUsd') is not None)
                    previous = attempts.get(key) or {}
                    failures = 0 if valid else min(8, previous.get('failures', 0)+1)
                    attempts[key] = {'lastAttemptAt': self.clock(), 'failures': failures,
                        'nextAttemptAt': self.clock()+(60_000 if valid else min(1_800_000, 60_000*2**failures)),
                        'reason': None if valid else error or 'pool-not-indexed-or-incomplete'}
                    if valid:
                        pools[key] = {**entry, 'chainId': chain, 'liquidityProvider': provider,
                                      'poolMarket': {**entry['poolMarket'], 'provider': provider}}
                        result['accepted'] += 1
                    else:
                        # Preserve last observation and its clock on failures.
                        result['failed' if error else 'missing'] += 1
                self.body.update(updatedAt=self.clock(), health=result)
                await self.persist()
                if fetch is None:
                    await asyncio.sleep(2.1 if chain in GECKO_NETWORKS else .6)
        if fetch is not None:
            await collect(fetch)
        else:
            async with httpx.AsyncClient(timeout=8) as client:
                async def get(chain, addresses):
                    if chain in GECKO_NETWORKS:
                        url = 'https://api.geckoterminal.com/api/v2/networks/' + GECKO_NETWORKS[chain] + '/pools/multi/'
                    else:
                        url = 'https://api.dexscreener.com/latest/dex/pairs/' + NETWORKS[chain] + '/'
                    response = await client.get(url + ','.join(addresses))
                    response.raise_for_status()
                    return {'pairs': gecko_pairs(chain, response.json())} if chain in GECKO_NETWORKS else response.json()
                await collect(get)
        self.health = {**result, 'updatedAt': self.clock(), 'status': 'ready'}
        return result

    async def run(self):
        while True:
            try:
                await asyncio.wait_for(self.run_once(), 75)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health = {'updatedAt': self.clock(), 'status': 'retry', 'reason': type(exc).__name__}
            await asyncio.sleep(60)
