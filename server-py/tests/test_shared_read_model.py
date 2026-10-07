"""Migration correctness, fallback and real PostgreSQL/Redis integration.

Set READMODEL_TEST_SETTINGS to the dedicated host settings for integration.
Only uniquely named test schemas/cache prefixes are created or removed.
"""
import asyncio
import copy
import gc
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import read_model_store as storage, read_model_worker as worker
from app import realtime_projection as projection


def publication(revision=1, price=0, missing=None):
    now = int(time.time()*1000)
    payload = {'now': now, 'realtime': {'revision': revision, 'cursor': revision*10},
               'unified': {'assets': [{'chainId': '56', 'token': '0xa', 'price': price,
                   'holders': missing, 'provider': 'Fixture', 'priceCurrency': 'USD',
                   'fieldTimes': {'price': now-500_000}, 'riskStatus': 'unknown'}],
                   'stockTokens': [], 'relations': [], 'stockThemes': []}}
    body = projection._encode_snapshot(json.dumps(payload))
    bodies = {view: body for view in worker.VIEWS}
    manifest = {'sourceEpoch': 'test-source', 'checkedAtMs': now, 'views': {
        view: {'revision': revision, 'cursor': revision*10, 'builtAt': 1000*revision,
               'sha256': hashlib.sha256(body).hexdigest()} for view in worker.VIEWS}}
    manifest['publication'] = hashlib.sha256(json.dumps(manifest['views'], sort_keys=True).encode()).hexdigest()
    return manifest, bodies, payload


class ReadModelUnitTests(unittest.TestCase):
    def test_fast_parser_preserves_arbitrary_integers_precision_and_null(self):
        value = {'large': 2**100, 'negative': -2**100, 'zero': 0, 'missing': None,
                 'decimal': 1.2345678912345, 'unit': 'USD', 'name': '中文'}
        text = json.dumps(value, ensure_ascii=False)
        self.assertEqual(projection._load_query_json(text), json.loads(text))

    def test_large_parse_restores_gc_after_invalid_json(self):
        self.assertTrue(gc.isenabled())
        with self.assertRaises(ValueError):
            projection._load_query_json(b'['+b' '*2_100_000)
        self.assertTrue(gc.isenabled())

    def test_large_parse_respects_previously_disabled_gc(self):
        gc.disable()
        try:
            value = projection._load_query_json(b'{"body":"'+b'a'*2_100_000+b'"}')
            self.assertEqual(len(value['body']), 2_100_000)
            self.assertFalse(gc.isenabled())
        finally:
            gc.enable()

    def test_old_future_or_mixed_publications_are_not_accepted(self):
        manifest, _, _ = publication()
        config = {'maxVerifiedAgeMs': 6000}
        self.assertTrue(storage.valid_manifest(config, manifest))
        self.assertFalse(storage.valid_manifest(config, manifest, manifest['checkedAtMs']+6001))
        self.assertFalse(storage.valid_manifest(config, manifest, manifest['checkedAtMs']-1))
        mixed = copy.deepcopy(manifest)
        mixed['views']['market']['revision'] += 1
        self.assertFalse(storage.valid_manifest(config, mixed))
        mixed['views']['market']['revision'] -= 1
        mixed['publication'] = 'not-a-version'
        self.assertFalse(storage.valid_manifest(config, mixed))

    def test_entities_keep_zero_null_source_unit_and_original_time(self):
        _, _, payload = publication()
        rows = storage.entity_rows(payload)
        decoded = json.loads(rows[0][5])
        self.assertEqual(rows[0][3], 0)
        self.assertIsNone(decoded['holders'])
        self.assertEqual(decoded['riskStatus'], 'unknown')
        self.assertEqual(decoded['provider'], 'Fixture')
        self.assertEqual(decoded['priceCurrency'], 'USD')
        self.assertEqual(rows[0][4], payload['unified']['assets'][0]['fieldTimes']['price'])

    def test_redis_errors_fall_back_to_verified_postgres_manifest(self):
        manifest, _, _ = publication()
        client = MagicMock();client.get.side_effect = OSError('secret must not be logged')
        connection = MagicMock();connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = {'manifest': manifest, 'checked_at_ms': manifest['checkedAtMs']}
        with patch.object(storage, 'redis_client', return_value=client), patch.object(storage, 'connect_postgres', return_value=connection):
            self.assertEqual(storage.shared_manifest({'maxVerifiedAgeMs': 6000}), manifest)

    def test_corrupt_cached_body_uses_matching_postgres_copy(self):
        manifest, bodies, _ = publication()
        client = MagicMock();client.get.return_value = b'corrupt'
        connection = MagicMock();connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = {'body': bodies['full'], 'sha256': manifest['views']['full']['sha256']}
        with patch.object(storage, 'redis_client', return_value=client), patch.object(storage, 'connect_postgres', return_value=connection):
            self.assertEqual(storage.shared_body({}, manifest, 'full'), bodies['full'])

    def test_unavailable_new_backends_return_none_for_original_reader(self):
        with patch.object(storage, 'redis_client', side_effect=OSError('redis down')), \
             patch.object(storage, 'connect_postgres', side_effect=OSError('postgres down')):
            self.assertIsNone(storage.shared_manifest({}))

    def test_source_reads_advance_while_another_sqlite_writer_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'source.sqlite'
            manifest, bodies, _ = publication()
            with closing(sqlite3.connect(path)) as database:
                database.execute('PRAGMA journal_mode=WAL')
                database.execute('CREATE TABLE dashboard_projection (name TEXT PRIMARY KEY,revision INTEGER,cursor INTEGER,built_at INTEGER,body BLOB)')
                database.executemany('INSERT INTO dashboard_projection VALUES (?,?,?,?,?)',
                    [(view, 1, 10, 1000, bodies[view]) for view in worker.VIEWS]);database.commit()
                database.execute('BEGIN IMMEDIATE')
                signature, captured = worker.capture_publication(path)
                self.assertEqual(captured[0]['views']['full']['revision'], 1)
                self.assertEqual(worker.capture_publication(path, signature), (signature, None))
                database.rollback()
                database.execute("UPDATE dashboard_projection SET revision=2 WHERE name='market'");database.commit()
                with self.assertRaisesRegex(ValueError, 'incomplete-source-publication'):
                    worker.capture_publication(path, signature)

    def test_failed_postgres_commit_never_publishes_redis_or_advances_source(self):
        manifest, bodies, _ = publication()
        bridge = worker.Bridge({}, 'unused');bridge.initialized = True
        bridge.postgres = MagicMock();bridge.postgres.publish.side_effect = RuntimeError('commit failure')
        with patch.object(worker, 'capture_publication', return_value=('signature', (manifest, bodies))), \
             patch.object(worker, 'cache_publication') as cached:
            with self.assertRaises(RuntimeError):bridge.turn()
            cached.assert_not_called()
        self.assertIsNone(bridge.signature)


class SharedReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await projection.stop_projection_queries()

    async def test_shared_cache_avoids_research_store_and_reuses_decoded_graph(self):
        manifest, bodies, _ = publication()
        with patch.object(storage, 'settings', return_value={'enabled': True, 'dsn': 'fixture'}), \
             patch.object(storage, 'shared_manifest', return_value=manifest), \
             patch.object(storage, 'shared_body', return_value=bodies['full']) as body, \
             patch.object(projection, 'store', side_effect=AssertionError('Research DB was touched')):
            values = await asyncio.gather(*[projection.read_projection_payload('full') for _ in range(30)])
            self.assertTrue(all(value is values[0] for value in values))
            self.assertEqual(values[0]['unified']['assets'][0]['price'], 0)
            self.assertIsNone(values[0]['unified']['assets'][0]['holders'])
            self.assertEqual(body.call_count, 1)
            self.assertIs(await projection.read_projection_payload('full'), values[0])
            self.assertEqual(body.call_count, 1)

    async def test_expired_or_missing_copy_uses_original_storage(self):
        with patch.object(storage, 'settings', return_value={'enabled': True, 'dsn': 'fixture'}), \
             patch.object(storage, 'shared_manifest', return_value=None), \
             patch.object(projection, 'store', side_effect=RuntimeError('original-path')):
            with self.assertRaisesRegex(RuntimeError, 'original-path'):
                await projection.read_projection_json('market')

    async def check_publication_refresh(self, *, expired=False, source_reset=False, grace_expired=False):
        first, bodies, _ = publication(1, 11)
        current, next_bodies, _ = publication(2, 22)
        if source_reset:
            current['sourceEpoch'] = 'new-source'
        entered, release = threading.Event(), threading.Event()
        def fetch(_config, manifest, view):
            if manifest['publication'] == current['publication']:
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('test release was not signalled')
                return next_bodies[view]
            return bodies[view]
        manifest = first
        with patch.object(storage, 'settings', return_value={'enabled': True, 'dsn': 'fixture', 'maxVerifiedAgeMs':6000}), \
             patch.object(storage, 'shared_manifest', side_effect=lambda _config:manifest), \
             patch.object(storage, 'shared_body', side_effect=fetch), \
             patch.object(projection, 'store', side_effect=AssertionError('Research DB was touched')):
            original = await projection.read_projection_payload('full')
            if expired:
                projection._serialized_projection['full'][1].shared_verified_at -= 10_000
            manifest = current
            request = asyncio.create_task(projection.read_projection_payload('full'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                await asyncio.sleep(.02)
                if expired or source_reset:
                    self.assertFalse(request.done())
                else:
                    self.assertIs(await asyncio.wait_for(request, .2), original)
                    self.assertEqual(original['realtime']['revision'], 1)
                    self.assertEqual(original['unified']['assets'][0]['price'], 11)
                    if grace_expired:
                        projection._shared_refreshes['full'].refresh_started -= 4
                        request = asyncio.create_task(projection.read_projection_payload('full'))
                        await asyncio.sleep(.02)
                        self.assertFalse(request.done())
                    else:
                        concurrent = await asyncio.gather(*[projection.read_projection_payload('full') for _ in range(40)])
                        self.assertTrue(all(value is original for value in concurrent))
                        self.assertEqual(len(projection._shared_refreshes), 1)
            finally:
                release.set()
            if not request.done():
                self.assertEqual((await request)['realtime']['revision'], 2)
            tasks = list(projection._shared_refreshes.values())
            if tasks:
                await asyncio.gather(*tasks)
            latest = await projection.read_projection_payload('full')
            self.assertEqual(latest['realtime']['revision'], 2)
            self.assertEqual(latest['unified']['assets'][0]['price'], 22)
            self.assertEqual(original['realtime']['revision'], 1)

    async def test_new_publication_prepares_without_blocking_recent_verified_page(self):
        await self.check_publication_refresh()

    async def test_expired_verification_never_uses_previous_page_grace(self):
        await self.check_publication_refresh(expired=True)

    async def test_source_reset_never_uses_previous_page_grace(self):
        await self.check_publication_refresh(source_reset=True)

    async def test_previous_page_grace_cannot_exceed_three_seconds(self):
        await self.check_publication_refresh(grace_expired=True)


@unittest.skipUnless(os.environ.get('READMODEL_TEST_SETTINGS'), 'dedicated PostgreSQL integration settings not provided')
class RealBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(Path(os.environ['READMODEL_TEST_SETTINGS']).read_text())
        cls.config['schema'] = 'cliperx_test_'+uuid.uuid4().hex
        cls.config['prefix'] = 'cliperx:test:'+uuid.uuid4().hex
        cls.postgres = storage.PostgresReadModel(cls.config)
        cls.postgres.initialize()

    @classmethod
    def tearDownClass(cls):
        from psycopg import sql
        with storage.connect_postgres(cls.config) as connection:
            assert cls.config['schema'].startswith('cliperx_test_')
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.config['schema'])))
        client = storage.redis_client(cls.config)
        keys = list(client.scan_iter(cls.config['prefix']+':*'))
        if keys:client.delete(*keys)
        storage.close_cache()

    def setUp(self):
        with storage.connect_postgres(self.config) as connection:
            connection.execute('TRUNCATE publication_state,published_views,current_entities')
        client = storage.redis_client(self.config)
        keys = list(client.scan_iter(self.config['prefix']+':*'))
        if keys:client.delete(*keys)

    def test_real_copy_and_redis_round_trip_preserve_values(self):
        manifest, bodies, payload = publication()
        self.assertEqual(self.postgres.publish(manifest, bodies, storage.entity_rows(payload)), 1)
        self.assertTrue(storage.cache_publication(self.config, manifest, bodies))
        self.assertEqual(storage.shared_manifest(self.config), manifest)
        self.assertEqual(storage.shared_body(self.config, manifest, 'full'), bodies['full'])
        with storage.connect_postgres(self.config) as connection:
            row = connection.execute('SELECT price,body FROM current_entities').fetchone()
            self.assertEqual(row['price'], 0)
            self.assertIsNone(row['body']['holders'])

    def test_redis_miss_and_corruption_rebuild_from_postgres(self):
        manifest, bodies, payload = publication()
        self.postgres.publish(manifest, bodies, storage.entity_rows(payload))
        self.assertEqual(storage.shared_manifest(self.config), manifest)
        storage.cache_publication(self.config, manifest, bodies)
        storage.redis_client(self.config).set(storage.body_key(self.config, manifest, 'market'), b'wrong')
        self.assertEqual(storage.shared_body(self.config, manifest, 'market'), bodies['market'])
        self.assertEqual(storage.redis_client(self.config).get(storage.body_key(self.config, manifest, 'market')), bodies['market'])

    def test_redis_connection_failure_keeps_postgres_available(self):
        manifest, bodies, payload = publication()
        self.postgres.publish(manifest, bodies, storage.entity_rows(payload))
        with patch.object(storage, 'redis_client', side_effect=ConnectionError('offline')):
            self.assertEqual(storage.shared_manifest(self.config), manifest)
            self.assertEqual(storage.shared_body(self.config, manifest, 'full'), bodies['full'])

    def test_failed_entity_write_rolls_back_views_and_publication(self):
        first, first_bodies, payload = publication(2, 22)
        self.postgres.publish(first, first_bodies, storage.entity_rows(payload))
        next_manifest, next_bodies, payload = publication(3, 33)
        bad = storage.entity_rows(payload)
        bad[0] = (*bad[0][:5], 'invalid JSON', bad[0][6])
        with self.assertRaises(Exception):self.postgres.publish(next_manifest, next_bodies, bad)
        self.assertEqual(storage.shared_manifest(self.config)['publication'], first['publication'])
        self.assertEqual(storage.shared_body(self.config, first, 'full'), first_bodies['full'])

    def test_old_publication_is_rejected_and_deleted_entity_is_removed(self):
        first, bodies, payload = publication(4, 4)
        self.postgres.publish(first, bodies, storage.entity_rows(payload))
        old, old_bodies, old_payload = publication(3, 3)
        with self.assertRaises(ValueError):self.postgres.publish(old, old_bodies, storage.entity_rows(old_payload))
        next_manifest, next_bodies, next_payload = publication(5)
        next_payload['unified']['assets'] = []
        self.postgres.publish(next_manifest, next_bodies, storage.entity_rows(next_payload))
        with storage.connect_postgres(self.config) as connection:
            self.assertEqual(connection.execute('SELECT count(*) n FROM current_entities').fetchone()['n'], 0)

    def test_source_heartbeat_expiry_cannot_be_relabelled_fresh(self):
        manifest, bodies, payload = publication()
        self.postgres.publish(manifest, bodies, storage.entity_rows(payload))
        storage.cache_publication(self.config, manifest, bodies)
        with patch.object(storage.time, 'time', return_value=(manifest['checkedAtMs']+60_000)/1000):
            self.assertIsNone(storage.shared_manifest(self.config))
            self.assertIsNone(storage.shared_body(self.config, manifest, 'full'))
