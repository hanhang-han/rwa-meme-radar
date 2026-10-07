"""Explicit PostgreSQL runtime setup after a verified shadow import.

Application startup only verifies these objects. Installing triggers is a
deployment step, separate from SQLite schema setup and migration backfill.
"""
import hashlib
import re

from .realtime_schema import _IGNORED_FACTS, trigger_schema


_SCHEMA = re.compile(r'^cliperx_storage_[a-z0-9_]{1,40}$')
PG_TRIGGER_VERSION = hashlib.sha256(('postgres-v1:' + trigger_schema()).encode()).hexdigest()
RESEARCH_COLUMNS = {
    'facts': ('kind', 'id', 'body'),
    'samples': ('asset', 't', 'price', 'cap'),
    'sample_evidence': ('asset', 't', 'body'),
    'trades': ('asset', 'id', 't', 'body'),
    'events': ('id', 'asset', 't', 'body'),
    'candles': ('asset', 'bar', 'openTime', 'open', 'high', 'low', 'close',
                'volume', 'volumeUsd', 'confirmed'),
    'trade_coverage': ('asset', 'startTime', 'endTime'),
    'trade_buckets': ('asset', 'bar', 'openTime', 'buyCount', 'sellCount',
                      'tradeCount', 'buyVolumeUsd', 'sellVolumeUsd', 'volumeUsd',
                      'traders', 'coverageRatio', 'complete', 'updatedAt'),
    'comparison_samples': ('scope', 'subject', 'fingerprint', 't', 'body'),
    'change_outbox': ('id', 'kind', 'entity', 'operation', 'at'),
    'realtime_events': ('id', 'event', 'body', 'at'),
    'realtime_latest': ('key', 'event', 'body', 'at'),
    'realtime_schema_version': ('name', 'value'),
    'projection_dirty': ('chain', 'token', 'generation'),
    'dashboard_projection': ('name', 'revision', 'cursor', 'input_cursor', 'body', 'built_at'),
    'chain_stream_logs': ('chain', 'id', 'pool', 'block', 'hash', 'at', 'body', 'processed'),
    'live_market_identity': ('name', 'value'),
    'factory_discovery_events': ('id', 'chain', 'factory', 'pool', 'block', 'block_hash',
                                 'tx_hash', 'log_index', 'created_at', 'discovered_at',
                                 'status', 'body', 'processing_status', 'processing_reason',
                                 'next_attempt_at', 'processed_at', 'relation_id', 'retracted_at'),
    'factory_discovery_cursors': ('chain', 'block', 'hash', 'coverage_from', 'anchors',
                                  'batch_blocks', 'updated_at'),
}
BASE_TABLES = tuple(list(RESEARCH_COLUMNS)[:15])


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


async def verify_tables(connection, tables):
    """Fail closed on an incomplete import, without changing its schema."""
    for name in tables:
        columns = ','.join(_quote(column) for column in RESEARCH_COLUMNS[name])
        await connection.execute_fetchall('SELECT ' + columns + ' FROM ' + _quote(name) + ' LIMIT 0')


async def verify_research_schema(connection, *, realtime_outbox=True):
    await verify_tables(connection, BASE_TABLES)
    rows = await connection.execute_fetchall(
        "SELECT value FROM realtime_schema_version WHERE name='postgres-write-order'")
    if not rows or rows[0][0] != PG_TRIGGER_VERSION:
        raise RuntimeError('postgres-transaction-order-not-installed')
    if realtime_outbox:
        rows = await connection.execute_fetchall(
            "SELECT value FROM realtime_schema_version WHERE name='postgres-triggers'")
        if not rows or rows[0][0] != PG_TRIGGER_VERSION:
            raise RuntimeError('postgres-realtime-triggers-not-installed')


def postgres_runtime_schema_sql(schema, tables, *, realtime_outbox=True,
                                identity_tables=None):
    """Return owned, explicit PostgreSQL DDL for the final deployment step.

    Every business table receives a BEFORE STATEMENT transaction lock. Thus
    Node writers and direct SQL participate in the same commit ordering as
    the Python adapter, before identity defaults allocate event/outbox ids.
    Research and native domains use different keys and remain independent.
    """
    from .storage_runtime import advisory_lock_key
    if not _SCHEMA.fullmatch(schema):
        raise ValueError('unowned-storage-schema')
    specs = tables if isinstance(tables, dict) else None
    tables = tuple(tables)
    if not tables or any(not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]*', name) for name in tables):
        raise ValueError('invalid-runtime-table-list')
    if identity_tables is None:
        identity_tables = {}
        if specs is not None:
            for name, spec in specs.items():
                primary = [column for column in spec['columns'] if column['pk']]
                if len(primary) == 1 and primary[0]['type'].upper() == 'INTEGER':
                    identity_tables[name] = primary[0]['name']
        else:
            identity_tables = {name: 'id' for name in ('change_outbox', 'realtime_events') if name in tables}
    elif not isinstance(identity_tables, dict):
        identity_tables = {name: 'id' for name in identity_tables}
    if (any(name not in tables for name in identity_tables)
            or any(not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]*', column) for column in identity_tables.values())):
        raise ValueError('invalid-runtime-identity-table-list')
    quoted = _quote(schema)
    key = advisory_lock_key(schema)
    statements = [f'''CREATE OR REPLACE FUNCTION {quoted}._cliperx_write_order()
        RETURNS trigger LANGUAGE plpgsql SET search_path TO {quoted},pg_catalog AS $$
        BEGIN
          PERFORM pg_advisory_xact_lock({key});
          RETURN NULL;
        END;
        $$;''']
    for name in tables:
        qualified = quoted + '.' + _quote(name)
        statements.extend((f'DROP TRIGGER IF EXISTS _cliperx_write_order ON {qualified};',
            f'''CREATE TRIGGER _cliperx_write_order BEFORE INSERT OR UPDATE OR DELETE
                ON {qualified} FOR EACH STATEMENT EXECUTE FUNCTION {quoted}._cliperx_write_order();'''))
    statements.append(f'''CREATE OR REPLACE FUNCTION {quoted}._cliperx_identity_rowid()
        RETURNS trigger LANGUAGE plpgsql SET search_path TO {quoted},pg_catalog AS $$
        BEGIN
          NEW._cliperx_source_rowid := (to_jsonb(NEW)->>TG_ARGV[0])::bigint;
          RETURN NEW;
        END;
        $$;''')
    for name, column in identity_tables.items():
        qualified = quoted + '.' + _quote(name)
        statements.extend((f'DROP TRIGGER IF EXISTS _cliperx_identity_rowid ON {qualified};',
            f'''CREATE TRIGGER _cliperx_identity_rowid BEFORE INSERT OR UPDATE
                ON {qualified} FOR EACH ROW EXECUTE FUNCTION {quoted}._cliperx_identity_rowid('{column}');'''))
    if 'realtime_schema_version' in tables:
        statements.append(f'''INSERT INTO {quoted}.realtime_schema_version(name,value)
            VALUES('postgres-write-order','{PG_TRIGGER_VERSION}')
            ON CONFLICT(name) DO UPDATE SET value=excluded.value;''')
    if not realtime_outbox:
        return '\n'.join(statements)
    ignored = ','.join("'" + name + "'" for name in _IGNORED_FACTS)
    statements.append(f'''CREATE OR REPLACE FUNCTION {quoted}._cliperx_realtime_change()
      RETURNS trigger LANGUAGE plpgsql SET search_path TO {quoted},pg_catalog AS $$
      DECLARE record_body jsonb; old_body jsonb; kind_value text; entity_value text;
      BEGIN
        record_body := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
        IF TG_OP='UPDATE' THEN old_body := to_jsonb(OLD); END IF;
        IF TG_TABLE_NAME='facts' THEN
          kind_value := record_body->>'kind'; entity_value := record_body->>'id';
          IF substring(kind_value from position(':' in kind_value)+1) IN ({ignored}) THEN RETURN NULL; END IF;
          IF TG_OP='UPDATE' AND old_body->'body' IS NOT DISTINCT FROM record_body->'body' THEN RETURN NULL; END IF;
        ELSE
          entity_value := record_body->>'asset';
          kind_value := CASE TG_TABLE_NAME WHEN 'trades' THEN 'trade' WHEN 'events' THEN 'feed'
            WHEN 'candles' THEN 'candle' WHEN 'samples' THEN 'sample'
            WHEN 'sample_evidence' THEN 'sample-evidence' WHEN 'trade_coverage' THEN 'trade-coverage'
            WHEN 'trade_buckets' THEN 'trade-bucket' WHEN 'comparison_samples' THEN 'comparison-sample' END;
          IF TG_TABLE_NAME='comparison_samples' THEN entity_value := (record_body->>'scope') || ':' || (record_body->>'subject'); END IF;
          IF TG_OP='UPDATE' AND TG_TABLE_NAME IN ('events','trades','sample_evidence','comparison_samples')
             AND old_body->'body' IS NOT DISTINCT FROM record_body->'body' THEN RETURN NULL; END IF;
          IF TG_OP='UPDATE' AND TG_TABLE_NAME='candles'
             AND old_body->'open' IS NOT DISTINCT FROM record_body->'open'
             AND old_body->'high' IS NOT DISTINCT FROM record_body->'high'
             AND old_body->'low' IS NOT DISTINCT FROM record_body->'low'
             AND old_body->'close' IS NOT DISTINCT FROM record_body->'close'
             AND old_body->'volume' IS NOT DISTINCT FROM record_body->'volume'
             AND old_body->'volumeUsd' IS NOT DISTINCT FROM record_body->'volumeUsd'
             AND old_body->'confirmed' IS NOT DISTINCT FROM record_body->'confirmed' THEN RETURN NULL; END IF;
        END IF;
        INSERT INTO change_outbox(kind,entity,operation,at)
          VALUES(kind_value,entity_value,lower(TG_OP),floor(extract(epoch FROM clock_timestamp())*1000)::bigint);
        RETURN NULL;
      END;
      $$;''')
    change_tables = ('facts', 'trades', 'events', 'candles', 'samples', 'sample_evidence',
                     'trade_coverage', 'trade_buckets', 'comparison_samples')
    for name in change_tables:
        if name not in tables:
            raise ValueError('incomplete-research-runtime-table-list')
        qualified = quoted + '.' + _quote(name)
        statements.extend((f'DROP TRIGGER IF EXISTS _cliperx_realtime_change ON {qualified};',
            f'''CREATE TRIGGER _cliperx_realtime_change AFTER INSERT OR UPDATE OR DELETE
                ON {qualified} FOR EACH ROW EXECUTE FUNCTION {quoted}._cliperx_realtime_change();'''))
    statements.append(f'''INSERT INTO {quoted}.realtime_schema_version(name,value)
        VALUES('postgres-triggers','{PG_TRIGGER_VERSION}')
        ON CONFLICT(name) DO UPDATE SET value=excluded.value;''')
    return '\n'.join(statements)
