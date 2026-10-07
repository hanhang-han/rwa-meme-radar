"""Single-owner, read-only bridge from the authoritative storage publication.

Copies committed public publications and current entities, not the 37GiB
history file. PostgreSQL commits before Redis becomes visible. A heartbeat is
renewed only after checking the authoritative publication again.
"""
import asyncio
import fcntl
import hashlib
import json
import os
import signal
import time
from contextlib import closing
from pathlib import Path

from .storage_runtime import connect
from .config import load_env
from .read_model_store import (PostgresReadModel, cache_publication, close_cache,
                               entity_rows, settings)

VIEWS = {'full', 'overview', 'market', 'feed'}


def capture_publication(path, previous=None):
    """Read four keyed rows in one consistent snapshot; no catalogue scan."""
    path = Path(path).resolve()
    with closing(connect(path.as_uri()+'?mode=ro', readonly=True, uri=True, timeout=.75)) as connection:
        if getattr(connection, 'backend', 'sqlite') == 'postgres':
            epoch = 'postgres:' + connection.schema
        else:
            stat = path.stat()
            epoch = f'sqlite:{stat.st_dev}:{stat.st_ino}'
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        rows = connection.execute('''SELECT name,revision,cursor,built_at
            FROM dashboard_projection WHERE name IN ('full','overview','market','feed') ORDER BY name''').fetchall()
        if {row[0] for row in rows} != VIEWS or len({tuple(row[1:]) for row in rows}) != 1:
            raise ValueError('incomplete-source-publication')
        signature = (epoch, tuple(tuple(row) for row in rows))
        if signature == previous:
            return signature, None
        blobs = connection.execute('''SELECT name,body FROM dashboard_projection
            WHERE name IN ('full','overview','market','feed')''').fetchall()
    # Neither decoding nor writing PostgreSQL owns a SQLite read transaction.
    bodies = {name: value.encode() if isinstance(value, str) else bytes(value) for name, value in blobs}
    if sum(len(body) for body in bodies.values()) > 128*1024*1024:
        raise ValueError('read-model-publication-too-large')
    manifest = {'sourceEpoch': epoch, 'checkedAtMs': int(time.time()*1000), 'views': {}}
    for name, revision, cursor, built in rows:
        manifest['views'][name] = {'revision': revision, 'cursor': cursor, 'builtAt': built,
                                   'sha256': hashlib.sha256(bodies[name]).hexdigest()}
    manifest['publication'] = hashlib.sha256(json.dumps(
        {'sourceEpoch': epoch, 'views': manifest['views']}, sort_keys=True).encode()).hexdigest()
    return signature, (manifest, bodies)


def decode_entities(body):
    from .realtime_projection import _decode_snapshot
    payload = json.loads(_decode_snapshot(body, 128*1024*1024))
    return entity_rows(payload)


class Bridge:
    def __init__(self, config, source):
        self.config, self.source = config, source
        self.postgres = PostgresReadModel(config)
        self.signature = None
        self.manifest = None
        self.initialized = False
        self.health = {'publicReadsEnabled': bool(config.get('enabled')), 'publications': 0,
                       'sourceChecks': 0, 'postgresRows': 0}

    def turn(self):
        began = time.monotonic()
        if not self.initialized:
            self.postgres.initialize()
            self.initialized = True
        source_started = time.monotonic()
        signature, update = capture_publication(self.source, self.signature)
        self.health['sourceReadMs'] = round((time.monotonic()-source_started)*1000, 1)
        self.health['sourceChecks'] += 1
        bodies = None
        if update:
            manifest, bodies = update
            entities = decode_entities(bodies['full'])
            write_started = time.monotonic()
            count = self.postgres.publish(manifest, bodies, entities)
            self.health.update(postgresRows=count, postgresPublishMs=round((time.monotonic()-write_started)*1000, 1),
                publications=self.health['publications']+1)
            # Progress only after the complete PostgreSQL commit succeeds.
            self.signature, self.manifest = signature, manifest
        else:
            self.manifest = self.postgres.verify_unchanged(self.manifest, int(time.time()*1000))
        cached = cache_publication(self.config, self.manifest, bodies)
        self.health.update(status='ready' if cached else 'postgres-ready-cache-unavailable',
            redisReady=cached, lastVerifiedAt=self.manifest['checkedAtMs'],
            publication=self.manifest['publication'],
            revision=self.manifest['views']['full']['revision'],
            durationMs=round((time.monotonic()-began)*1000, 1), error=None)
        return dict(self.health)


def save_health(value):
    path = Path(os.environ.get('READ_MODEL_HEALTH_PATH', 'data/read-model-health.json'))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps({'pid': os.getpid(), 'updatedAt': int(time.time()*1000), **value}))
    os.replace(temporary, path)


async def run():
    load_env()
    config = settings()
    if not config.get('dsn'):
        raise RuntimeError('read-model-settings-unavailable')
    from .projection_worker import configure_process_priority
    configure_process_priority()
    owner = Path('data/read-model-worker.lock')
    owner.parent.mkdir(parents=True, exist_ok=True)
    with owner.open('a+') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        bridge = Bridge(config, os.environ.get('RESEARCH_DB', 'data/research.sqlite'))
        try:
            while not stop.is_set():
                try:
                    health = await asyncio.to_thread(bridge.turn)
                except Exception as exc:
                    # Driver errors may contain DSNs, queries or private
                    # credentials. Health logs expose the exception class only.
                    health = {**bridge.health, 'status': 'error', 'error': type(exc).__name__}
                save_health(health)
                try:
                    await asyncio.wait_for(stop.wait(), min(10, max(1, float(config.get('pollSeconds', 2)))))
                except TimeoutError:
                    pass
        finally:
            close_cache()


if __name__ == '__main__':
    asyncio.run(run())
