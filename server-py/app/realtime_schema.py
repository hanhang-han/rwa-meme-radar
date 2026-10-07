"""Durable publication primitives shared by collectors and the query service.

Triggers belong to SQLite (not a Python callback), so Node writers participate in
exactly the same commit boundary. Projections never write facts and cannot feed
back into their own change log.
"""
import json
import time

REALTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS change_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, entity TEXT NOT NULL,
 operation TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS realtime_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, body TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS realtime_events_time ON realtime_events(at);
CREATE TABLE IF NOT EXISTS realtime_latest (
 key TEXT PRIMARY KEY, event TEXT NOT NULL, body TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS realtime_schema_version (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projection_dirty (
 chain TEXT NOT NULL, token TEXT NOT NULL, generation INTEGER NOT NULL,
 PRIMARY KEY(chain,token));
CREATE INDEX IF NOT EXISTS projection_dirty_generation ON projection_dirty(generation,chain,token);
CREATE TABLE IF NOT EXISTS dashboard_projection (
 name TEXT PRIMARY KEY, revision INTEGER NOT NULL, cursor INTEGER NOT NULL,
 input_cursor INTEGER NOT NULL, body TEXT NOT NULL, built_at INTEGER NOT NULL);
"""

# Demand leases and retry checkpoints do not change a displayed entity. Domain
# health, briefing-job, chain-stream and every other fact remain observable.
_IGNORED_FACTS = ('watch', 'detail-watch', 'detailwatch', 'collector-job', 'insight-demand',
                  'comparison-demand', 'market-candle', 'candle-meta', 'pool-candle-acc', 'candle-watch', 'chain-stream-cursor', 'cursor', 'discovery-cursor', 'trade-cursor')
_IGNORED_SQL = ','.join("'" + name + "'" for name in _IGNORED_FACTS)


def trigger_schema(replace=False):
    statements = []
    specs = {
        'facts': ("{r}.kind", "{r}.id"),
        'trades': ("'trade'", "{r}.asset"),
        'events': ("'feed'", "{r}.asset"),
        'candles': ("'candle'", "{r}.asset"),
        'samples': ("'sample'", "{r}.asset"),
        'sample_evidence': ("'sample-evidence'", "{r}.asset"),
        'trade_coverage': ("'trade-coverage'", "{r}.asset"),
        'trade_buckets': ("'trade-bucket'", "{r}.asset"),
        'comparison_samples': ("'comparison-sample'", "{r}.scope || ':' || {r}.subject"),
    }
    for table, (kind, entity) in specs.items():
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            ref = 'OLD' if operation == 'DELETE' else 'NEW'
            when = ''
            if table == 'facts':
                when = f"WHEN substr({ref}.kind,instr({ref}.kind,':')+1) NOT IN ({_IGNORED_SQL})"
                if operation == 'UPDATE':
                    when += ' AND OLD.body IS NOT NEW.body'
            elif operation == 'UPDATE':
                # Candle refreshes frequently return an identical last bar.
                if table == 'candles':
                    when = 'WHEN ' + ' OR '.join(f'OLD.{f} IS NOT NEW.{f}' for f in ('open','high','low','close','volume','volumeUsd','confirmed'))
                elif table in ('events', 'trades', 'sample_evidence', 'comparison_samples'):
                    when = 'WHEN OLD.body IS NOT NEW.body'
            if replace:
                statements.append(f'DROP TRIGGER IF EXISTS realtime_{table}_{operation.lower()};')
            statements.append(f"""CREATE TRIGGER IF NOT EXISTS realtime_{table}_{operation.lower()}
              AFTER {operation} ON {table} {when} BEGIN
              INSERT INTO change_outbox(kind,entity,operation,at)
              VALUES ({kind.format(r=ref)},{entity.format(r=ref)},'{operation.lower()}',
                      CAST(strftime('%s','now') AS INTEGER)*1000);
              END;""")
    return '\n'.join(statements)


async def enqueue_event(connection, event: str, data: dict) -> int:
    """Append inside the caller's fact transaction; deliberately never commits."""
    row = await connection.execute(
        'INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
        (event, json.dumps(data, ensure_ascii=False, separators=(',', ':')), int(time.time()*1000)),
    )
    return row.lastrowid


async def enqueue_events(connection, events: list[tuple[str, dict]]) -> None:
    """Append one stream batch inside its caller's fact transaction.

    Keeping the batch in one aiosqlite worker job prevents other reads queued
    on this shared connection from running between every trade and its event.
    The caller still owns commit/rollback, so neither half can publish alone.
    """
    if not events:
        return
    at = int(time.time() * 1000)
    await connection.executemany(
        'INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
        [(event, json.dumps(data, ensure_ascii=False, separators=(',', ':')), at)
         for event, data in events],
    )
