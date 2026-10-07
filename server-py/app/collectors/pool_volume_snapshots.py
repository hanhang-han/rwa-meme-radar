"""Worker-owned hourly rolling-volume observations, retained for eight days.

A rolling volume.h24 observation is never treated as an hourly candle. This
history supports same-hour seven-day medians and a explicitly named rolling
24-hour series; true hourly bars come from covered, priced Swap events.
"""
from __future__ import annotations

import asyncio
import json
import time

from ..db import store
from ..market_quotes import enrich_relation
from ..product_metrics import DAY_MS, HOUR_MS, fresh, number

CHAINS = ('196', '56', '4663', '5042')
KIND = 'pool-volume-hour'
_lock = asyncio.Lock()
_sparkline_lock = asyncio.Lock()
SPARKLINE_CADENCE_MS = 300_000


async def refresh_pool_volume_snapshots(now=None):
    now = int(time.time()*1000) if now is None else int(now)
    hour = now//HOUR_MS*HOUR_MS
    result = {'requested': 0, 'accepted': 0, 'updated': 0, 'failed': 0, 'skipped': 0}
    async with _lock:
        system = await store('system')
        previous_round = await system.get('collector', 'pool-volume-snapshots') or {}
        missing_current_hour = 0
        all_relations = []
        for chain in CHAINS:
            s = await store(chain)
            selected = {}
            for relation in await s.all('relation'):
                row = enrich_relation({**relation, 'chainId': chain})
                pool = str(row.get('pool') or '').lower()
                if pool:
                    selected[pool] = row
            all_relations.extend(selected.values())
            observations = []
            for pool, row in selected.items():
                market = row.get('poolMarket') or {}
                value = number(market.get('volume24h'), 0)
                at = market.get('updatedAt')
                if value is None or not fresh(at, now) or market.get('scope') != 'pool:'+pool or not market.get('provider'):
                    result['skipped'] += 1
                    missing_current_hour += 1
                    continue
                if (market.get('volumeCurrency') or market.get('currency') or ('USD' if market['provider'] == 'DexScreener' else None)) != 'USD':
                    result['skipped'] += 1
                    missing_current_hour += 1
                    continue
                # Observation hour, not ingestion hour. A stale round must
                # never place yesterday's value into today's sample.
                observation_hour = int(at//HOUR_MS)*HOUR_MS
                if observation_hour != hour:
                    missing_current_hour += 1
                ident = f'{pool}:{observation_hour}'
                observations.append((ident, pool, value, at, observation_hour, market, row))
            previous_samples = await s.get_many(KIND, [row[0] for row in observations])
            for ident, pool, value, at, observation_hour, market, row in observations:
                previous = previous_samples.get(ident)
                if previous and previous.get('observedAt', 0) >= at:
                    continue
                await s.put(KIND, ident, {'chainId': chain, 'pool': pool, 'hour': observation_hour,
                    'volume24hUsd': value, 'provider': market['provider'], 'observedAt': at,
                    'savedAt': now, 'scope': 'pool:'+pool, 'metric': 'rolling-volume-24h',
                    'ticker': ((row.get('stockIdentity') or {}).get('ticker') or row.get('ticker'))})
                result['accepted'] += 1
                result['updated'] += 1
            if previous_round.get('completedHour') != hour:
                async with s._guard_write():
                    await s.db.execute("DELETE FROM facts WHERE kind=? AND CAST(json_extract(body,'$.hour') AS INTEGER)<?",
                        (s.key(KIND), hour-8*DAY_MS))
                    await s.db.commit()
        # Short local retries need only relation observations. Avoid rebuilding
        # every asset/theme when upstream has still supplied no fresh data.
        if not result['accepted'] and previous_round.get('completedHour') == hour:
            await system.put('collector', 'pool-volume-snapshots', {**result, 'at': now,
                'completedHour': hour, 'missingCurrentHourPools': missing_current_hour,
                'retentionDays': 8, 'cadenceMs': 60_000, 'observationCadenceMs': HOUR_MS,
                'metric': 'rolling-volume-24h', 'firstEligibleRatioAfterMs': 7*DAY_MS})
            # These skips mean missing/stale pool observations. The scheduler
            # reserves noChange+skipped for incomplete stock identity rows.
            return result
        from ..product_metrics import build_product_theme_metrics
        from ..stock_identity import assess_pool_relation
        assessed = [{**r, **assess_pool_relation(r, now)} for r in all_relations]
        themes = {}
        for relation in assessed:
            ticker = _ticker(relation)
            if relation.get('level') == 'A' and ticker:
                themes.setdefault(ticker, []).append(relation)
        # Read each chain's seven-day window once, then partition by pool.
        # A theme may contain the same address on several chains.
        selected_pools = set().union(*(_theme_pools(rows) for rows in themes.values())) if themes else set()
        history = await _read_pool_history(selected_pools, now, 168)
        history_by_pool = {}
        for sample in history:
            history_by_pool.setdefault((str(sample.get('chainId')), str(sample.get('pool') or '').lower()), []).append(sample)
        assets_by_key = {}
        for chain in sorted({key[0] for key in selected_pools}):
            tokens = {str(row.get('token') or '').lower() for rows in themes.values()
                      for row in rows if str(row.get('chainId')) == chain and row.get('status') == 'verified'}
            assets = await (await store(chain)).get_many('asset', tokens)
            for asset in assets.values():
                asset = {**asset, 'chainId': chain}
                assets_by_key[(chain, str(asset.get('token') or '').lower())] = asset
        for ticker, relations in sorted(themes.items()):
            pools = _theme_pools(relations)
            snapshots = [sample for key in pools for sample in history_by_pool.get(key, ())]
            keys = {(str(row.get('chainId')), str(row.get('token') or '').lower()) for row in relations}
            assets = [assets_by_key[key] for key in keys if key in assets_by_key]
            if not ticker:
                continue
            metric = build_product_theme_metrics(ticker, assets, relations, now=now, snapshots=snapshots)
            await system.put('product-theme', ticker, metric)
        sparkline_result = await refresh_stock_sparklines(assessed, now)
        if not sparkline_result.get('skipped'):
            result['sparklinesRefreshed'] = True
        await system.put('collector', 'pool-volume-snapshots', {
            **result, 'at': now, 'completedHour': hour, 'missingCurrentHourPools': missing_current_hour,
            'retentionDays': 8, 'cadenceMs': 60_000, 'observationCadenceMs': HOUR_MS,
            'metric': 'rolling-volume-24h', 'firstEligibleRatioAfterMs': 7*DAY_MS})
    return result


def _ticker(row):
    return str((row.get('stockIdentity') or {}).get('ticker') or row.get('ticker') or '').upper()


def _theme_pools(relations):
    return {(str(row.get('chainId')), str(row.get('pool') or '').lower()) for row in relations
            if row.get('pool') and row.get('status') == 'verified' and row.get('level') == 'A'}


async def _read_pool_history(selected, now, hours):
    snapshots = []
    for chain in sorted({key[0] for key in selected}):
        s = await store(chain)
        for row in await s.fetchall("SELECT body FROM facts WHERE kind=? AND CAST(json_extract(body,'$.hour') AS INTEGER)>=?",
                (s.key(KIND), now-hours*HOUR_MS-HOUR_MS)):
            sample = json.loads(row[0])
            if (chain, str(sample.get('pool') or '').lower()) in selected:
                snapshots.append(sample)
    return snapshots


async def read_theme_pool_history(ticker, relations, now=None, hours=168):
    now = int(time.time()*1000) if now is None else int(now)
    hours = max(1, min(192, int(hours)))
    selected = _theme_pools(row for row in relations if _ticker(row) == str(ticker).upper())
    snapshots = await _read_pool_history(selected, now, hours)
    by_hour = {}
    for row in snapshots:
        by_hour.setdefault(row['hour'], {})[(str(row['chainId']), row['pool'])] = row
    series = []
    end_hour = now//HOUR_MS*HOUR_MS
    for hour in range(end_hour-hours*HOUR_MS, end_hour+1, HOUR_MS):
        observations = by_hour.get(hour, {})
        complete = bool(selected) and set(observations) == selected
        series.append({'t': hour, 'volume24hUsd': sum(row['volume24hUsd'] for row in observations.values()) if complete else None,
            'volumeUsd': None, 'metric': 'rolling-volume-24h', 'coverage': {'knownPools': len(observations), 'totalPools': len(selected), 'complete': complete}})
    return {'ticker': str(ticker).upper(), 'at': now, 'metric': 'rolling-volume-24h',
            'series': series, 'snapshots': snapshots, 'retentionDays': 8,
            'coverage': {'totalPools': len(selected), 'completeHours': sum(row['coverage']['complete'] for row in series), 'requiredSameHours': 7}}


async def _sparklines_due(system, now):
    previous = await system.get('collector', 'product-sparklines') or {}
    at = previous.get('at') or 0
    return not (0 < at <= now and now-at < SPARKLINE_CADENCE_MS)


async def refresh_stock_sparklines(relations, now):
    async with _sparkline_lock:
        system = await store('system')
        if not await _sparklines_due(system, now):
            return {'accepted': 0, 'updated': 0, 'skipped': 1}
        return await _refresh_stock_sparklines(relations, now, system)


async def _refresh_stock_sparklines(relations, now, system):
    """Eight real USD observations per popular theme, never drawn placeholders."""
    scores = {}
    for relation in relations:
        if relation.get('level') != 'A' or relation.get('status') != 'verified':
            continue
        ticker = str((relation.get('stockIdentity') or {}).get('ticker') or relation.get('ticker') or '').upper()
        key = (str(relation.get('chainId')), str(relation.get('stock') or '').lower(), ticker)
        scores[key] = scores.get(key, 0)+(number((relation.get('poolMarket') or {}).get('volume24h'), 0) or 0)
    updated = 0
    for (chain, token, ticker), _ in sorted(scores.items(), key=lambda item: -item[1])[:24]:
        s = await store(chain)
        points = []
        for sample in await s.samples(token, 288):
            evidence = sample.get('provenance') or {}
            at = evidence.get('marketAt') or sample.get('t')
            value = number(sample.get('price'), 0)
            if evidence.get('currency') != 'USD' or evidence.get('scope') not in ('token', 'token-aggregate') or value is None or not fresh(at, now, DAY_MS):
                continue
            points.append({'t': at, 'value': value})
        points.sort(key=lambda row: row['t'])
        if len(points) > 8:
            indices = {round(i*(len(points)-1)/7) for i in range(8)}
            points = [points[i] for i in sorted(indices)]
        await s.put('stock-sparkline', token, {'chainId': chain, 'token': token, 'ticker': ticker,
            'points': points, 'at': now, 'currency': 'USD', 'scope': 'token',
            'coverage': {'knownPoints': len(points), 'requiredPoints': 8, 'complete': len(points) == 8}})
        updated += 1
    await system.put('collector', 'product-sparklines', {'at': now, 'updated': updated,
        'cadenceMs': SPARKLINE_CADENCE_MS, 'currency': 'USD', 'scope': 'token'})
    return {'accepted': updated, 'updated': updated, 'sparklinesRefreshed': True}


async def refresh_product_sparklines():
    now = int(time.time()*1000)
    # Both scheduled entry points share the same durable cadence. Check it
    # before loading relations, not only before the sample reads/writes.
    async with _sparkline_lock:
        system = await store('system')
        if not await _sparklines_due(system, now):
            return {'accepted': 0, 'updated': 0, 'skipped': 1}
        relations = []
        from ..stock_identity import assess_pool_relation
        for chain in CHAINS:
            s = await store(chain)
            for relation in await s.all('relation'):
                row = enrich_relation({**relation, 'chainId': chain})
                relations.append({**row, **assess_pool_relation(row, now)})
        return await _refresh_stock_sparklines(relations, now, system)
