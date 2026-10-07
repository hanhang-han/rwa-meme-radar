"""Bounded on-host acceptance; outputs contain metrics, never credentials."""
import argparse
import collections
import json
import os
import pathlib
import shutil
import statistics
import time
import urllib.request

ROUTES = {
    'home': '/api/v2/home?chain=all',
    'stocks': '/api/v2/stocks?chain=all&limit=20',
    'memes': '/api/v2/memes?chain=all&limit=20',
    'theme9992': '/api/v2/stocks/9992?chain=all',
    'theme1024': '/api/v2/stocks/1024?chain=all',
    'themeSPY': '/api/v2/stocks/SPY?chain=4663',
    'overview': '/api/dashboard?view=overview',
}


def sample(output):
    result = {'atMs': int(time.time()*1000), 'routes': {}}
    for label, route in ROUTES.items():
        timings, statuses, sizes = [], [], []
        for _ in range(7):
            began = time.monotonic()
            with urllib.request.urlopen('http://127.0.0.1:8010'+route, timeout=12) as response:
                body = response.read()
                statuses.append(response.status)
                sizes.append(len(body))
                json.loads(body)
            timings.append(round((time.monotonic()-began)*1000, 2))
        result['routes'][label] = {'route': route, 'status': statuses, 'bytes': sizes,
            'elapsedMs': timings, 'medianMs': statistics.median(timings), 'maxMs': max(timings)}
    for name in ('worker-health.json', 'market-worker-health.json', 'read-model-health.json'):
        path = pathlib.Path('data')/name
        if path.exists():
            data = json.loads(path.read_text())
            if name == 'worker-health.json':
                result[name] = {k: data.get(k) for k in ('pid', 'updatedAt', 'resourcePressure')}
                tasks = data.get('tasks', data)
                result[name]['quotes'] = {k: tasks.get(k) for k in ('liveQuotes','baseCandidates','baseStocks','dexBatchMarket')}
            elif name == 'read-model-health.json':
                result[name] = data
            else:
                result[name] = {k: data.get(k) for k in ('pid','updatedAt','mode','ready','chains','poolMarket')}
    result['resources'] = {'diskFreeGiB': round(shutil.disk_usage('.').free/1024**3, 2), 'load': os.getloadavg()}
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps({k: {m: v[m] for m in ('medianMs','maxMs')} for k,v in result['routes'].items()}))


def verify(output):
    from app.read_model_store import connect_postgres, settings, shared_manifest, shared_body, valid_manifest
    from app.read_model_worker import capture_publication, decode_entities
    config = settings()
    for attempt in range(12):
        _, (source, bodies) = capture_publication('data/research.sqlite')
        with connect_postgres(config) as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            current = connection.execute('SELECT manifest,checked_at_ms FROM publication_state WHERE singleton=1').fetchone()
            if not current or current['manifest']['publication'] != source['publication']:
                time.sleep(1)
                continue
            stored = {r['view']: r for r in connection.execute('SELECT view,body,sha256,publication FROM published_views')}
            rows = list(connection.execute('SELECT kind,chain,identity,price,quote_at_ms,body FROM current_entities'))
        manifest = {**current['manifest'], 'checkedAtMs': current['checked_at_ms']}
        assert valid_manifest(config, manifest), 'stale-shadow-publication'
        assert set(stored) == set(source['views'])
        for view, meta in source['views'].items():
            assert stored[view]['publication'] == source['publication']
            assert bytes(stored[view]['body']) == bodies[view], view+' body mismatch'
            assert stored[view]['sha256'] == meta['sha256']
        expected = {tuple(row[:3]): (*row[3:5], json.loads(row[5])) for row in decode_entities(bodies['full'])}
        actual = {(r['kind'],r['chain'],r['identity']): (r['price'],r['quote_at_ms'],r['body']) for r in rows}
        assert actual == expected, 'current entity fields differ'
        cached = shared_manifest(config)
        if not cached or cached['publication'] != source['publication']:
            time.sleep(1)
            continue
        assert all(shared_body(config,cached,view) == bodies[view] for view in stored), 'cache bodies differ'
        result = {'atMs': int(time.time()*1000), 'publication':source['publication'],
                  'revision':source['views']['full']['revision'], 'sourceCheckedAtMs': manifest['checkedAtMs'],
                  'views':{v:{**m, 'bytes':len(bodies[v])} for v,m in source['views'].items()},
                  'entityCounts':dict(collections.Counter(r['kind'] for r in rows)),
                  'allFieldsEqual':True, 'cacheBodiesEqual':True}
        with connect_postgres(config) as connection:
            result['postgresBytes'] = connection.execute('SELECT pg_database_size(current_database()) AS size').fetchone()['size']
        output.write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
        return
    raise RuntimeError('shadow publication did not catch up within the verification window')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['sample','verify'])
    parser.add_argument('output', type=pathlib.Path)
    args = parser.parse_args()
    {'sample':sample,'verify':verify}[args.mode](args.output)
