#!/usr/bin/env python3
"""Bounded read-only evidence collection; no env, credentials or RPC URLs.

Run on the production host using standard Python, or send this file to python
over SSH stdin. It queries only the small market journal and public local API.
"""
import argparse
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ADDRESS = re.compile(r'^0x[0-9a-f]{40}$')
CHAINS = ('196', '56', '4663')
HEALTH_KEYS = ('pid', 'updatedAt', 'ready', 'mode')
CHAIN_KEYS = ('chainId', 'status', 'poolCount', 'queueDepth', 'processed', 'decodedEvents',
              'lastReceivedAt', 'lastProcessedAt', 'lastSourceEventAt', 'updatedAt',
              'liveLogProcessingP95Ms', 'liveTradeLockWaitP95Ms', 'liveTradeTransactionP95Ms')


def clock_ms():
    return int(time.time() * 1000)


def safe_error(error):
    # Network and SQL exception text may contain URLs; print type only.
    return type(error).__name__


def resources():
    result = {'at': clock_ms(), 'processes': []}
    result['cpu'] = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:]))
    result['memoryKiB'] = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in ('MemAvailable', 'SwapTotal', 'SwapFree', 'Dirty', 'Writeback'):
            result['memoryKiB'][key] = int(value.split()[0])
    result['vm'] = {k: int(v) for k, v in
                    (line.split() for line in Path('/proc/vmstat').read_text().splitlines())
                    if k in ('pswpin', 'pswpout', 'pgpgin', 'pgpgout')}
    for line in Path('/proc/diskstats').read_text().splitlines():
        fields = line.split()
        if fields[2] == 'vda':
            result['disk'] = dict(zip(('reads', 'readMerged', 'readSectors', 'readMs', 'writes',
                                      'writeMerged', 'writeSectors', 'writeMs', 'inFlight', 'ioMs',
                                      'weightedMs'), map(int, fields[3:14])))
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            command = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            if '/opt/memedashboard' not in command and 'app.worker' not in command and 'app.projection' not in command and 'app.market_worker' not in command:
                continue
            label = ('live-market' if 'app.market_worker' in command else 'projection' if 'projection' in command
                     else 'collector' if 'app.worker' in command else 'api' if 'uvicorn' in command else 'node-service')
            stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            status = {k: v.strip() for k, v in
                      (line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)}
            io = {k: int(v) for k, v in (line.split(':') for line in Path(f'/proc/{pid}/io').read_text().splitlines())}
            result['processes'].append({'pid': int(pid), 'label': label,
                'cpuTicks': int(stat[11]) + int(stat[12]), 'rssKiB': int(status.get('VmRSS', '0').split()[0]),
                'swapKiB': int(status.get('VmSwap', '0').split()[0]), 'io': io})
        except OSError:
            continue
    return result


def resource_delta(before, after):
    seconds = (after['at'] - before['at']) / 1000
    ticks = [b - a for a, b in zip(before['cpu'], after['cpu'])]
    total = sum(ticks[:8])
    disk = {k: after['disk'][k] - before['disk'][k] for k in before.get('disk', {}) if k != 'inFlight'}
    result = {'seconds': seconds, 'iowaitPct': round(ticks[4] / total * 100, 2),
              'idlePct': round(ticks[3] / total * 100, 2)}
    if disk:
        result.update(diskReadMiBps=round(disk['readSectors'] * 512 / seconds / 1048576, 2),
                      diskWriteMiBps=round(disk['writeSectors'] * 512 / seconds / 1048576, 2),
                      diskBusyPct=round(disk['ioMs'] / seconds / 10, 2))
    result.update(swapInKiBps=round((after['vm']['pswpin'] - before['vm']['pswpin']) * os.sysconf('SC_PAGE_SIZE') / 1024 / seconds, 2),
                  swapOutKiBps=round((after['vm']['pswpout'] - before['vm']['pswpout']) * os.sysconf('SC_PAGE_SIZE') / 1024 / seconds, 2))
    return result


def read_health(path):
    try:
        body = json.loads(Path(path).read_text())
        result = {k: body.get(k) for k in HEALTH_KEYS}
        result['chains'] = {chain: {k: row.get(k) for k in CHAIN_KEYS if k != 'chainId'}
                            for chain, row in body.get('chains', {}).items() if chain in CHAINS}
        result['sampledAt'] = clock_ms()
        for row in result['chains'].values():
            for field, stamp in (('lastSourceEventAgeMs', row.get('lastSourceEventAt')),
                                 ('lastReceiptAgeMs', row.get('lastReceivedAt'))):
                row[field] = result['sampledAt'] - stamp if isinstance(stamp, (int, float)) and stamp > 0 else None
        return result
    except (OSError, ValueError) as error:
        return {'available': False, 'errorType': safe_error(error)}


def read_market(db_path, previous_cursor):
    if not Path(db_path).is_file():
        return {'available': False, 'selected': {}, 'events': [], 'cursor': 0}
    result = {'available': True, 'sampledAt': clock_ms(), 'selected': {}, 'chains': [], 'events': []}
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.25)
    con.execute('PRAGMA query_only=ON')
    started = time.monotonic()
    con.set_progress_handler(lambda: time.monotonic() - started > .75, 1000)
    try:
        result['cursor'] = con.execute('SELECT COALESCE(MAX(id),0) FROM realtime_events').fetchone()[0]
        result['dbBytes'] = Path(db_path).stat().st_size
        for chain in CHAINS:
            row = con.execute("SELECT body FROM facts WHERE kind=? AND id='pools'", (chain + ':live-selection',)).fetchone()
            if row:
                body = json.loads(row[0])
                result['selected'][chain] = {k: body.get(k) for k in ('poolIds', 'tokenIds', 'capacity', 'scope', 'updatedAt', 'historyPolicy')}
            row = con.execute("SELECT body FROM facts WHERE kind=? AND id='pools'", (chain + ':chain-stream',)).fetchone()
            if row:
                body = json.loads(row[0])
                result['chains'].append({k: body.get(k) for k in CHAIN_KEYS})
        # Always bounded to 1024 rows via the journal primary key; this is a
        # sample, not a full consumer, and explicitly reports skipped frames.
        rows = con.execute('SELECT id,event,body,at FROM realtime_events WHERE id>? ORDER BY id DESC LIMIT 1024',
                           (previous_cursor if previous_cursor is not None else max(0, result['cursor'] - 1024),)).fetchall()
        now = clock_ms()
        for seq, event, encoded, journal_at in reversed(rows):
            packet = json.loads(encoded)
            if event != 'candle' or packet.get('bar') != '5m':
                continue
            source, received, prepared = packet.get('sourceEventAt'), packet.get('receivedAt'), packet.get('persistedAt')
            item = {k: packet.get(k) for k in ('chainId', 'token', 'poolId', 'bar', 'priceCurrency')}
            item.update(id=seq, sourceEventAt=source, receivedAt=received, preparedPersistAt=prepared,
                        durableObservedAt=now, journalRecordAt=journal_at)
            if isinstance(source, (int, float)):
                item['sourceToDurableObservedUpperMs'] = now - source
                if isinstance(prepared, (int, float)):
                    item['sourceToPreparedPersistMs'] = prepared - source
            if isinstance(received, (int, float)):
                item['receiveToDurableObservedUpperMs'] = now - received
            result['events'].append(item)
        result['sampledJournalRows'] = len(rows)
        result['potentiallySkippedJournalRows'] = max(0, result['cursor'] - (previous_cursor or max(0, result['cursor'] - 1024)) - len(rows))
        result['observedAt'] = now
    except sqlite3.Error as error:
        result['errorType'] = safe_error(error)
    finally:
        con.close()
    return result


def select_probes(db_path, events):
    probes = {}
    if not Path(db_path).is_file():
        return probes
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.25)
    try:
        classifications = {}
        for chain in CHAINS:
            for token, encoded in con.execute('SELECT id,body FROM facts WHERE kind=? LIMIT 256', (chain + ':asset',)):
                classifications[(chain, token)] = json.loads(encoded).get('kind')
        for event in reversed(events):
            chain, token, pool = str(event.get('chainId')), event.get('token'), event.get('poolId')
            kind = classifications.get((chain, token))
            group = 'stock' if kind == 'stock' else 'meme' if kind == 'candidate' else 'other'
            key = chain + ':' + group
            if key not in probes and chain in CHAINS and ADDRESS.fullmatch(token or '') and ADDRESS.fullmatch(pool or ''):
                probes[key] = {'chain': chain, 'token': token, 'pool': pool, 'bar': '5m', 'group': group}
        for chain in CHAINS:
            row = con.execute("SELECT body FROM facts WHERE kind=? AND id='pools'", (chain + ':live-selection',)).fetchone()
            selected = set(json.loads(row[0]).get('poolIds', [])) if row else set()
            for row in con.execute('SELECT body FROM facts WHERE kind=? LIMIT 256', (chain + ':market-registry',)):
                d = json.loads(row[0]).get('definition') or {}
                kind = classifications.get((chain, d.get('token')))
                group = 'stock' if kind == 'stock' else 'meme' if kind == 'candidate' else 'other'
                key = chain + ':' + group
                if key in probes:
                    continue
                if d.get('pool_id') in selected and ADDRESS.fullmatch(d.get('token', '')) and ADDRESS.fullmatch(d.get('pool_id', '')):
                    probes[key] = {'chain': chain, 'token': d['token'], 'pool': d['pool_id'], 'bar': '5m', 'group': group}
    finally:
        con.close()
    return probes


def watch_sse(base, probe, stop, result, lock):
    url = base.rstrip('/') + '/live-market/stream?' + urllib.parse.urlencode({k: v for k, v in probe.items() if k != 'group'})
    with lock:
        result.update(probe=probe, connected=False, eventCount=0, candleCount=0, ohlcvChanges=0,
                      currentBarRevisions=0, latencySamples=[], examples=[])
    last = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'Accept': 'text/event-stream'}), timeout=20) as response:
            with lock:
                result['httpStatus'] = response.status
                result['connected'] = response.status == 200
            event, fields = '', []
            for line in response:
                if stop.is_set():
                    break
                line = line.decode('utf-8').rstrip('\r\n')
                if line.startswith('event:'):
                    event = line[6:].strip()
                elif line.startswith('data:'):
                    fields.append(line[5:].strip())
                elif not line and fields:
                    with lock:
                        result['eventCount'] += 1
                    body = json.loads('\n'.join(fields)); fields = []
                    if event != 'market.candle':
                        continue
                    now = clock_ms(); row = body.get('row') or {}
                    values = {k: row.get(k) for k in ('t', 'o', 'h', 'l', 'c', 'v', 'vu', 'confirmed')}
                    with lock:
                        result['candleCount'] += 1
                        if last is not None and values != last:
                            result['ohlcvChanges'] += 1
                            if values.get('t') == last.get('t'):
                                result['currentBarRevisions'] += 1
                        last = values
                    record = {'observedAt': now, 'sourceEventAt': body.get('sourceEventAt'),
                              'receivedAt': body.get('receivedAt'), 'preparedPersistAt': body.get('persistedAt'), 'row': values}
                    for key, stamp in (('sourceToSseMs', body.get('sourceEventAt')),
                                       ('receiveToSseMs', body.get('receivedAt')),
                                       ('preparedPersistToSseMs', body.get('persistedAt'))):
                        if isinstance(stamp, (int, float)):
                            record[key] = now - stamp
                    with lock:
                        result['latencySamples'].append({k: v for k, v in record.items() if k.endswith('Ms')})
                        result['latencySamples'] = result['latencySamples'][-1000:]
                        result['examples'].append(record); result['examples'] = result['examples'][-30:]
    except Exception as error:
        with lock:
            result['errorType'] = safe_error(error)


def percentiles(rows):
    result = {}
    for key in ('sourceToSseMs', 'receiveToSseMs', 'preparedPersistToSseMs'):
        values = sorted(row[key] for row in rows if key in row)
        if values:
            result[key] = {'count': len(values), 'p50': values[math.ceil(.5 * len(values)) - 1],
                           'p95': values[math.ceil(.95 * len(values)) - 1], 'max': values[-1],
                           'negativeCount': sum(v < 0 for v in values)}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=45)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--db', default='/opt/memedashboard/data/live-market.sqlite')
    parser.add_argument('--health', default='/opt/memedashboard/data/market-worker-health.json')
    parser.add_argument('--api', default='http://127.0.0.1:8011/api')
    parser.add_argument('--no-sse', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60 or not 1 <= args.interval <= 15:
        parser.error('Use a bounded 1–60 second observation and 1–15 second interval')
    parsed = urllib.parse.urlparse(args.api)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.scheme not in ('http', 'https'):
        parser.error('Provide a public API base without credentials, query or fragment')
    started = time.monotonic()
    result = {'scope': 'Bounded small-DB sampling plus one public SSE chart subscription per chain; no environment or credential reads',
              'startedAt': clock_ms(), 'samples': [], 'resourceIntervals': [], 'sse': {},
              'timestampMeaning': 'preparedPersistAt is the application timestamp before its write transaction; durableObservedAt/SSE observation confirms a committed event and supplies an upper bound, not a storage fsync trace.'}
    first = read_market(args.db, None)
    probes = select_probes(args.db, first['events'])
    stop = threading.Event(); threads = []; sse_lock = threading.Lock()
    if not args.no_sse:
        for chain, probe in probes.items():
            evidence = result['sse'].setdefault(chain, {})
            thread = threading.Thread(target=watch_sse, args=(args.api, probe, stop, evidence, sse_lock), daemon=True)
            thread.start(); threads.append(thread)
    prior = None; cursor = None
    while True:
        current = resources()
        market = first if cursor is None else read_market(args.db, cursor)
        result['samples'].append({'resources': current, 'market': market, 'health': read_health(args.health)})
        if prior:
            result['resourceIntervals'].append(resource_delta(prior, current))
        prior = current; cursor = market.get('cursor', cursor)
        remaining = args.seconds - (time.monotonic() - started)
        if remaining <= 0:
            break
        stop.wait(min(args.interval, remaining))
    stop.set()
    with sse_lock:
        # A socket may remain blocked until its next heartbeat. Freeze only
        # the evidence observed before completion, without waiting on reads.
        result['sse'] = json.loads(json.dumps(result['sse']))
    for evidence in result['sse'].values():
        evidence['percentiles'] = percentiles(evidence.get('latencySamples', []))
        evidence['observationConclusion'] = ('Observed real current-bar OHLCV revisions' if evidence.get('currentBarRevisions')
                                            else 'No changing current-bar evidence in this short observation; zero frames cannot prove realtime performance')
    result['finishedAt'] = clock_ms()
    result['elapsedSeconds'] = round(time.monotonic() - started, 3)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
