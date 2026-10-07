"""Direct paired-pool volume; explicit time coverage precedes any zero."""
import json
from .db import store


def interval_covered(intervals, start, end):
    covered = start
    for left, right in sorted(intervals):
        if left > covered:
            return False
        if right > covered:
            covered = right
        if covered >= end:
            return True
    return False


async def direct_pool_volumes(relations, start, end, width):
    pools = {}
    for relation in relations:
        if relation.get('status') == 'verified' and relation.get('level') == 'A' and relation.get('pool'):
            pools[(str(relation.get('chainId')), relation['pool'].lower())] = relation
    observations = {}
    for chain in sorted({key[0] for key in pools}):
        s = await store(chain)
        index = {}
        for key, body in await s.all_kv('market-registry'):
            definition = body.get('definition') or {}
            if definition.get('venue') == 'dex':
                identity = (str(definition.get('pool_id') or '').lower(), str(definition.get('token') or '').lower())
                index.setdefault(identity, []).append(key)
        intervals_by_pool = {}
        # Read covered intervals once per chain rather than rescan every pool.
        for row in await s.fetchall(
                "SELECT body FROM facts WHERE kind=? AND CAST(json_extract(body,'$.throughMs') AS INTEGER)>=? AND CAST(json_extract(body,'$.fromMs') AS INTEGER)<?",
                (s.key('pool-time-coverage'), start, end+width)):
            body = json.loads(row[0])
            if body.get('canonical') is True and body.get('decoded') is True and isinstance(body.get('fromMs'), int) and isinstance(body.get('throughMs'), int) and body['fromMs'] < body['throughMs']:
                intervals_by_pool.setdefault(str(body.get('pool') or '').lower(), []).append((body['fromMs'], body['throughMs']))
        mapped = {}
        for (pool_chain, pool), relation in pools.items():
            if pool_chain != chain:
                continue
            storages = index.get((pool, str(relation.get('token') or '').lower()), [])
            # A single pool/base definition avoids counting both token sides.
            if storages:
                mapped[pool] = s.key(sorted(storages)[0])
        rows_by_asset = {}
        valid = """json_extract(body,'$.finality')='confirmed'
            AND json_extract(body,'$.canonicalBlockHash')=json_extract(body,'$.blockHash')
            AND json_extract(body,'$.usdObservation.currency')='USD'
            AND json_extract(body,'$.usdObservation.method')='trade-time-quote'
            AND json_extract(body,'$.usdObservation.provider') IS NOT NULL
            AND json_type(body,'$.usdObservation.value') IN ('integer','real')
            AND json_extract(body,'$.usdObservation.value')>=0
            AND ABS(json_extract(body,'$.usdObservation.at')-t)<=30000"""
        covered_hours = {pool: [hour for hour in range(start, end+1, width)
                               if interval_covered(intervals_by_pool.get(pool, []), hour, hour+width)]
                         for pool in mapped}
        # Missing time coverage already forbids a value. Scanning its trades
        # cannot improve that result and can delay every other query on disk.
        storage_keys = sorted({storage for pool, storage in mapped.items() if covered_hours[pool]})
        hours = [hour for values in covered_hours.values() for hour in values]
        trade_start, trade_end = (min(hours), max(hours)+width) if hours else (start, start)
        for offset in range(0, len(storage_keys), 400):
            chunk = storage_keys[offset:offset+400]
            placeholders = ','.join('?' for _ in chunk)
            rows = await s.fetchall(f"""SELECT asset, (t/?)*? AS hour, COUNT(*) AS trades,
            SUM(CASE WHEN {valid} THEN 1 ELSE 0 END) AS priced,
            SUM(CASE WHEN {valid} THEN json_extract(body,'$.usdObservation.value') ELSE 0 END) AS volume
            FROM trades WHERE asset IN ({placeholders}) AND t>=? AND t<? GROUP BY asset, hour""",
                (width, width, *chunk, trade_start, trade_end))
            for row in rows:
                rows_by_asset.setdefault(row['asset'], {})[row['hour']] = row
        for (pool_chain, pool), relation in pools.items():
            if pool_chain == chain:
                observations[(chain, pool)] = (intervals_by_pool.get(pool, []), rows_by_asset.get(mapped.get(pool), {}), pool in mapped)
    result = []
    for hour in range(start, end+1, width):
        values = []
        for intervals, rows, mapped in observations.values():
            row = rows.get(hour)
            # An unmapped pool is never assumed to have had zero swaps.
            if mapped and interval_covered(intervals, hour, hour+width) and (row is None or row['priced'] == row['trades']):
                values.append(row['volume'] if row else 0)
        complete = bool(pools) and len(values) == len(pools)
        result.append({'t': hour, 'volumeUsd': sum(values) if complete else None,
            'coverage': {'knownPools': len(values), 'totalPools': len(pools), 'complete': complete},
            'source': 'confirmed-swaps-at-trade-time-usd' if complete else None})
    return result
