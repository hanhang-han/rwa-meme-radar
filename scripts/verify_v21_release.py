#!/usr/bin/env python3
"""Read-only PRD v2.1 production contract check for a deployed dashboard."""
import argparse
import gzip
import json
import math
import time
import urllib.request
from decimal import Decimal


def get_json(url):
    request = urllib.request.Request(url, headers={'Accept-Encoding': 'gzip'})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        if response.headers.get('Content-Encoding') == 'gzip':
            body = gzip.decompress(body)
    return json.loads(body)


def metric_sum(rows, key):
    values = [Decimal(str(row[key])) for row in rows if row.get(key) is not None]
    return sum(values, Decimal(0)).quantize(Decimal('0.01')) if values else None


def verify(base):
    dashboard = get_json(base + '/api/dashboard')
    unified = dashboard['unified']
    now = dashboard['now']
    assert dashboard.get('realtime', {}).get('revision') is not None, 'missing projection revision'
    catalog = unified.get('identityCatalog') or {}
    assert catalog.get('status') == 'ready' and catalog.get('entries', 0) > 0, 'official catalog unavailable'
    metrics = unified['metrics']
    by_chain = unified['metricsByChain']
    assert set(by_chain) == {'196', '56', '4663'}, 'missing chain statistics'
    assert metrics['verifiedPools'] == sum(row['verifiedPools'] for row in by_chain.values()), 'pair count mismatch'
    assert metric_sum(by_chain.values(), 'pairedLiquidityUsd') == (
        Decimal(str(metrics['pairedLiquidityUsd'])).quantize(Decimal('0.01'))
        if metrics['pairedLiquidityUsd'] is not None else None), 'pair TVL mismatch'

    active = set()
    for row in unified['assets']:
        chain, token = str(row.get('chainId')), str(row.get('token') or '').lower()
        total, total_at = row.get('totalLiquidityUsd'), row.get('totalLiquidityAt')
        price_at = (row.get('fieldTimes') or {}).get('price')
        if (row.get('kind') == 'candidate' and isinstance(total, (int, float))
                and math.isfinite(total) and total >= 1000
                and row.get('totalLiquidityStatus') == 'current'
                and isinstance(total_at, (int, float)) and 0 <= now - total_at <= 1_800_000
                and row.get('price') is not None and isinstance(price_at, (int, float))
                and 0 <= now - price_at <= 900_000):
            active.add((chain, token))
        if row.get('totalLiquidityStatus') == 'unknown':
            assert row.get('totalLiquidityUsd') is None, 'unknown total liquidity shown as a number'
        for flag in row.get('riskFlags') or []:
            check = ((row.get('riskAssessment') or {}).get('checks') or {}).get(flag)
            assert check and check.get('status') == 'triggered', 'risk flag without triggered evidence'
    assert len(active) == metrics.get('activeMemeCount'), 'active Meme KPI differs from displayed assets'
    for chain, row in by_chain.items():
        assert row.get('activeMemeCount') == sum(cid == chain for cid, _ in active), 'chain active count mismatch'

    level_a = [row for row in unified['relations'] if row.get('level') == 'A']
    pools = {(str(row.get('chainId')), str(row.get('pool') or '').lower()) for row in level_a}
    assert len(pools) == metrics['verifiedPools'], 'A relations and pool KPI differ'
    for row in level_a:
        side = row.get('sideIdentity') or {}
        assert side.get('verificationStatus') == 'official', 'non-official pair graded A'
        assert row.get('status') == 'verified' and row.get('evidenceStatus') == 'qualified'
        assert (row.get('liquidityUsd') or 0) >= 1000, 'A pair below threshold'
        assert 0 <= now - row.get('liquidityAt', 0) <= 900_000, 'stale A pair'
    coverage = metrics.get('newPair24hCoverage') or {}
    if coverage.get('known') != coverage.get('total') or not coverage.get('total'):
        assert metrics.get('newPair24h') is None, 'scan time misrepresented as creation time'

    feed = get_json(base + '/api/feed?chain=196')
    assert all(str(row.get('chainId')) == '196' for row in feed.get('trades', [])), 'cross-chain trade leaked into filtered feed'
    assert all(str(row.get('chainId')) == '196' for row in feed.get('relationships', [])), 'cross-chain relation leaked into filtered feed'
    # A scheduled atomic publication can land between these two HTTP reads.
    # Retry that boundary, but still fail if the public languages stay split.
    for attempt in range(3):
        zh = get_json(base + '/api/ai/briefing?lang=zh')
        en = get_json(base + '/api/ai/briefing?lang=en')
        if zh.get('generationId') == en.get('generationId'):
            break
        if attempt < 2:
            time.sleep(.25)
    assert zh.get('model') == en.get('model') == 'template-v3', 'non-template briefing'
    assert zh.get('generationId') == en.get('generationId'), 'language briefings from different runs'
    assert zh.get('items') == en.get('items'), 'language briefings describe different facts'
    assert all(item.get('asset', {}).get('address') and item.get('snapshotId')
               for item in zh.get('items') or []), 'briefing item without exact identity'
    return {'revision': dashboard['realtime']['revision'], 'assets': len(unified['assets']),
            'officialCatalogEntries': catalog['entries'], 'activeMemes': len(active),
            'officialPairs': len(pools), 'briefingItems': len(zh.get('items') or []),
            'xLayerTrades': len(feed.get('trades') or [])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='https://cliperx.com/dashboard')
    args = parser.parse_args()
    print(json.dumps(verify(args.base.rstrip('/')), ensure_ascii=False))
