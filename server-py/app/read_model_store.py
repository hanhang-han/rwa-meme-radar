"""Versioned PostgreSQL read models and disposable, bounded Redis caching.

The SQLite publication is still the migration authority. Only a separately
verified, committed copy can be served; outages fall back to the old reader.
No account or private endpoint is cached here.
"""
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS publication_state (
 singleton integer PRIMARY KEY CHECK(singleton=1), source_epoch text NOT NULL,
 publication text NOT NULL, checked_at_ms bigint NOT NULL, manifest jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS published_views (
 view text PRIMARY KEY, publication text NOT NULL, body bytea NOT NULL, sha256 text NOT NULL);
CREATE TABLE IF NOT EXISTS current_entities (
 kind text NOT NULL, chain text NOT NULL, identity text NOT NULL,
 price double precision, quote_at_ms bigint, body jsonb NOT NULL, body_hash text NOT NULL,
 PRIMARY KEY(kind,chain,identity));
CREATE INDEX IF NOT EXISTS current_entities_price ON current_entities(kind,chain,price) WHERE price IS NOT NULL;
CREATE INDEX IF NOT EXISTS current_entities_quote_time ON current_entities(kind,chain,quote_at_ms);
"""
_config_cache = None
_redis_client = None
_redis_url = None
_stats = {'redisManifestHits': 0, 'redisBodyHits': 0, 'postgresManifestHits': 0,
          'postgresBodyHits': 0, 'fallbacks': 0, 'redisErrors': 0, 'postgresErrors': 0}


def settings():
    global _config_cache
    path = Path(os.environ.get('ARCHITECTURE_SETTINGS', 'data/architecture-runtime.json'))
    try:
        stat = path.stat()
        identity = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        if _config_cache and _config_cache[0] == identity:
            return _config_cache[1]
        value = json.loads(path.read_text())
        if value.get('managedBy') != 'cliperx-architecture-v1':
            return {}
        schema = value.get('schema', 'cliperx_read_model')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', schema):
            return {}
        value['schema'] = schema
        value['maxVerifiedAgeMs'] = min(10_000, max(1000, int(value.get('maxVerifiedAgeMs', 6000))))
        _config_cache = (identity, value)
        return value
    except (OSError, ValueError, TypeError):
        return {}


def connect_postgres(config):
    import psycopg
    from psycopg.rows import dict_row
    schema = config['schema']
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', schema):
        raise ValueError('invalid-read-model-schema')
    return psycopg.connect(config['dsn'], connect_timeout=2, row_factory=dict_row,
        options=f'-c search_path={schema} -c statement_timeout=5000 -c lock_timeout=750')


def redis_client(config):
    global _redis_client, _redis_url
    if not config.get('redisUrl'):
        return None
    if _redis_client is None or _redis_url != config['redisUrl']:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
        if _redis_client is not None:
            _redis_client.close()
        _redis_url = config['redisUrl']
        _redis_client = redis.Redis.from_url(_redis_url, socket_connect_timeout=.15,
            socket_timeout=.2, max_connections=4, retry=Retry(NoBackoff(), 0),
            retry_on_timeout=False, health_check_interval=30)
    return _redis_client


def valid_manifest(config, value, now=None):
    if not isinstance(value, dict):
        return False
    now = int(time.time()*1000) if now is None else now
    observed = value.get('checkedAtMs')
    if not isinstance(observed, int) or not 0 <= now-observed <= config.get('maxVerifiedAgeMs', 6000):
        return False
    if not isinstance(value.get('sourceEpoch'), str) or not re.fullmatch(r'[0-9a-f]{64}', str(value.get('publication', ''))):
        return False
    views = value.get('views')
    if not isinstance(views, dict) or set(views) != {'full', 'overview', 'market', 'feed'}:
        return False
    for meta in views.values():
        if not isinstance(meta, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(meta.get('sha256', ''))):
            return False
        if any(not isinstance(meta.get(k), int) or meta[k] < 0 for k in ('revision', 'cursor', 'builtAt')):
            return False
    versions = {(m['revision'], m['cursor'], m['builtAt']) for m in views.values()}
    return len(versions) == 1


def manifest_key(config):
    return config.get('prefix', 'cliperx:read-model:v1') + ':manifest'


def body_key(config, manifest, view):
    return config.get('prefix', 'cliperx:read-model:v1') + ':body:' + manifest['publication'] + ':' + view


def shared_manifest(config):
    try:
        client = redis_client(config)
        raw = client.get(manifest_key(config)) if client else None
        if raw:
            value = json.loads(raw)
            if valid_manifest(config, value):
                _stats['redisManifestHits'] += 1
                return value
    except Exception:
        _stats['redisErrors'] += 1
    try:
        with connect_postgres(config) as connection:
            row = connection.execute('SELECT manifest,checked_at_ms FROM publication_state WHERE singleton=1').fetchone()
        if row:
            value = {**row['manifest'], 'checkedAtMs': row['checked_at_ms']}
            if valid_manifest(config, value):
                _stats['postgresManifestHits'] += 1
                return value
    except Exception:
        _stats['postgresErrors'] += 1
    _stats['fallbacks'] += 1
    return None


def shared_body(config, manifest, view):
    if not valid_manifest(config, manifest):
        return None
    expected = manifest['views'][view]['sha256']
    try:
        client = redis_client(config)
        raw = client.get(body_key(config, manifest, view)) if client else None
        if raw is not None and hashlib.sha256(raw).hexdigest() == expected:
            _stats['redisBodyHits'] += 1
            return raw
    except Exception:
        _stats['redisErrors'] += 1
    try:
        with connect_postgres(config) as connection:
            row = connection.execute('''SELECT v.body,v.sha256 FROM published_views v
                JOIN publication_state s ON s.publication=v.publication
                WHERE s.singleton=1 AND v.view=%s AND s.publication=%s
                AND s.source_epoch=%s AND s.checked_at_ms>=%s''',
                (view, manifest['publication'], manifest['sourceEpoch'],
                 int(time.time()*1000)-config.get('maxVerifiedAgeMs', 6000))).fetchone()
        if row:
            raw = bytes(row['body'])
            if row['sha256'] == expected and hashlib.sha256(raw).hexdigest() == expected:
                _stats['postgresBodyHits'] += 1
                try:
                    if client:
                        client.set(body_key(config, manifest, view), raw, ex=45)
                except Exception:
                    _stats['redisErrors'] += 1
                return raw
    except Exception:
        _stats['postgresErrors'] += 1
    _stats['fallbacks'] += 1
    return None


def publication_stamp(manifest, view):
    meta = manifest['views'][view]
    return ('shared-read-model', manifest['sourceEpoch'], manifest['publication'], view,
            meta['revision'], meta['cursor'], meta['builtAt'], True)


def cache_publication(config, manifest, bodies=None):
    """Called only after the durable PostgreSQL transaction has committed."""
    try:
        client = redis_client(config)
        if client is None:
            return False
        pipeline = client.pipeline(transaction=True)
        for view in manifest['views']:
            key = body_key(config, manifest, view)
            if bodies is not None:
                pipeline.set(key, bodies[view], ex=45)
            else:
                pipeline.expire(key, 45)
        pipeline.set(manifest_key(config), json.dumps(manifest, separators=(',', ':')),
                     px=config.get('maxVerifiedAgeMs', 6000))
        pipeline.execute()
        return True
    except Exception:
        _stats['redisErrors'] += 1
        return False


def entity_rows(payload):
    """Current canonical entities retain their exact fields, including nulls."""
    unified = payload.get('unified') or {}
    rows = []
    for kind, collection, key in [('asset', 'assets', 'token'), ('stock', 'stockTokens', 'tokenContractAddress'),
                                  ('relation', 'relations', 'id'), ('theme', 'stockThemes', 'ticker')]:
        for item in unified.get(collection, []):
            identity = item.get(key)
            if not identity:
                continue
            body = json.dumps(item, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            price = item.get('price')
            price = price if isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price) else None
            observed = (item.get('fieldTimes') or {}).get('price') or item.get('quoteAt')
            observed = int(observed) if isinstance(observed, (int, float)) and math.isfinite(observed) else None
            rows.append((kind, str(item.get('chainId', 'all')), str(identity).lower(), price,
                         observed, body, hashlib.sha256(body.encode()).hexdigest()))
    return rows


class PostgresReadModel:
    def __init__(self, config):
        self.config = config

    def initialize(self):
        from psycopg import sql
        with connect_postgres(self.config) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.config['schema'])))
            connection.execute(SCHEMA_SQL)

    def publish(self, manifest, bodies, entities):
        from psycopg.types.json import Jsonb
        with connect_postgres(self.config) as connection:
            # The source worker is single-owner. Keep the four views and their
            # cursor visible atomically; readers never join different commits.
            old = connection.execute('SELECT manifest,source_epoch FROM publication_state WHERE singleton=1 FOR UPDATE').fetchone()
            if old and old['source_epoch'] == manifest['sourceEpoch']:
                before = old['manifest']['views']['full']
                after = manifest['views']['full']
                if after['builtAt'] < before['builtAt'] or after['revision'] < before['revision'] or after['cursor'] < before['cursor']:
                    raise ValueError('read-model-publication-regression')
            with connection.cursor() as cursor:
                cursor.executemany('''INSERT INTO published_views VALUES (%s,%s,%s,%s)
                    ON CONFLICT(view) DO UPDATE SET publication=excluded.publication,
                    body=excluded.body,sha256=excluded.sha256''',
                    [(view, manifest['publication'], bodies[view], meta['sha256']) for view, meta in manifest['views'].items()])
                cursor.execute('''CREATE TEMP TABLE incoming_entities
                    (LIKE current_entities INCLUDING DEFAULTS) ON COMMIT DROP''')
                with cursor.copy('COPY incoming_entities (kind,chain,identity,price,quote_at_ms,body,body_hash) FROM STDIN') as copy:
                    for row in entities:
                        copy.write_row((*row[:5], row[5], row[6]))
                cursor.execute('''INSERT INTO current_entities SELECT * FROM incoming_entities
                    ON CONFLICT(kind,chain,identity) DO UPDATE SET price=excluded.price,
                    quote_at_ms=excluded.quote_at_ms,body=excluded.body,body_hash=excluded.body_hash
                    WHERE current_entities.body_hash<>excluded.body_hash''')
                cursor.execute('''DELETE FROM current_entities c WHERE NOT EXISTS
                    (SELECT 1 FROM incoming_entities i WHERE i.kind=c.kind AND i.chain=c.chain AND i.identity=c.identity)''')
                cursor.execute('''INSERT INTO publication_state VALUES (1,%s,%s,%s,%s)
                    ON CONFLICT(singleton) DO UPDATE SET source_epoch=excluded.source_epoch,
                    publication=excluded.publication,checked_at_ms=excluded.checked_at_ms,manifest=excluded.manifest''',
                    (manifest['sourceEpoch'], manifest['publication'], manifest['checkedAtMs'], Jsonb(manifest)))
        return len(entities)

    def verify_unchanged(self, manifest, checked_at):
        with connect_postgres(self.config) as connection:
            result = connection.execute('''UPDATE publication_state SET checked_at_ms=%s
                WHERE singleton=1 AND publication=%s AND source_epoch=%s''',
                (checked_at, manifest['publication'], manifest['sourceEpoch']))
            if result.rowcount != 1:
                raise ValueError('read-model-heartbeat-generation-mismatch')
        return {**manifest, 'checkedAtMs': checked_at}


def read_model_health():
    config = settings()
    return {'configured': bool(config.get('dsn')), 'publicReadsEnabled': bool(config.get('enabled')),
            'maxVerifiedAgeMs': config.get('maxVerifiedAgeMs'), 'stats': dict(_stats)}


def close_cache():
    global _redis_client, _redis_url
    if _redis_client is not None:
        _redis_client.close()
    _redis_client, _redis_url = None, None
