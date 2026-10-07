"""Read-only public acceptance for the deployed watch queries; no session."""
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'docs/acceptance/watch-account-release-20261003/public-api-after.json'
BASE = 'https://cliperx.com/dashboard/api/'
CT = '56:0x0a092e544da31150b439a1aaa1a3a2214a867f46'
report = {'checkedAt': int(time.time() * 1000), 'reads': {}}


def read(name, path, body=None):
    args = ['curl', '--compressed', '-fsS', '--max-time', '25']
    if body is not None:
        args += ['-H', 'Content-Type: application/json', '--data-binary', '@-']
    started = time.monotonic()
    data = json.loads(subprocess.check_output(args + [BASE + path],
                      input=json.dumps(body).encode() if body is not None else None))
    report['reads'][name] = {'status': 200, 'elapsedMs': round((time.monotonic() - started) * 1000),
                             'request': body, 'data': data}
    return data


try:
    ready = read('ready', 'health/ready')
    assert ready.get('ok') is True and ready.get('storage') == 'ready'
    keys = ['stock:NVDA', 'stock:700', CT, '1:' + CT.split(':')[1]]
    summary = read('summary', 'v2/watch/summary', {'keys': keys, 'chain': 'all', 'limit': 20})
    items = {item['key']: item for item in summary['items']}
    assert set(items) == set(keys)
    for key in keys[:3]:
        asset = items[key]['asset']
        assert asset and asset['price'] > 0 and asset.get('priceCurrency')
        assert asset.get('fieldTimes', {}).get('price') or asset.get('quoteAt')
    assert items[keys[-1]]['status'] == 'unsupported-chain' and items[keys[-1]]['asset'] is None
    assert summary.get('snapshotAt') and summary.get('realtime')
    theme_body = {'keys': ['stock:NVDA'], 'chain': 'all', 'limit': 2}
    first = read('theme-events', 'v2/watch/events', theme_body)
    assert first['items'] and all('stock:NVDA' in item['matchedKeys'] for item in first['items'])
    if first['hasMore']:
        second = read('theme-history', 'v2/watch/events', {**theme_body, 'cursor': first['nextCursor']})
        assert second['items'] and not {item['id'] for item in first['items']} & {item['id'] for item in second['items']}
        ordered = first['items'] + second['items']
        assert [(item['t'], item['id']) for item in ordered] == sorted([(item['t'], item['id']) for item in ordered], reverse=True)
    exact = read('exact-events', 'v2/watch/events', {'keys': [CT], 'chain': 'all', 'limit': 20})
    assert all(item['chainId'] == '56' and CT in item['matchedKeys'] for item in exact['items'])
    unsupported = read('unsupported-events', 'v2/watch/events', {'keys': [keys[-1]], 'chain': 'all', 'limit': 20})
    assert unsupported['items'] == [] and unsupported['unsupportedKeys'] == [keys[-1]]
    report['passed'] = True
finally:
    OUTPUT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps({'passed': report.get('passed', False), 'proof': str(OUTPUT), 'reads': {
        name: {'status': row['status'], 'elapsedMs': row['elapsedMs'], 'items': len(row['data'].get('items', []))}
        for name, row in report['reads'].items()}}, ensure_ascii=False))
