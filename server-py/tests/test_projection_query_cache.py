"""Resource bounds preserve the exact published revision under concurrent reads."""
import asyncio
import copy
import gc
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Request
from app import realtime_projection as projection
from app.api import dashboard, product_v2
from app.projection_query_cache import QueryWork, SnapshotBody


class SnapshotStorageTests(unittest.TestCase):
    def test_unicode_snapshot_uses_bytes_and_preserves_exact_json(self):
        payload = {'name': '中文😀', 'body': 'a'*1_000_000, 'large': 2**100, 'missing': None}
        text = json.dumps(payload, ensure_ascii=False)
        body = SnapshotBody(text, ('test', 1))
        self.assertIs(type(body.wire), bytes)
        self.assertFalse(hasattr(body.wire, 'payload'))
        self.assertEqual(body.wire, text.encode('utf-8'))
        self.assertEqual(json.loads(body), payload)
        self.assertLess(body.serialized_bytes, 2*len(body.wire)+1024)

    def test_snapshot_release_does_not_need_cycle_collection(self):
        released = []
        class Watched(SnapshotBody):
            def __del__(self):
                released.append(True)
        gc.disable()
        try:
            body = Watched('{"name":"中文😀"}', ('test',1))
            body.payload = {'known': None}
            del body
            self.assertEqual(released, [True])
        finally:
            gc.enable()


class QueryCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/projection.sqlite'
        self.scoped = SimpleNamespace(path=self.path, db=object())
        self.store = patch.object(projection, 'store', AsyncMock(return_value=self.scoped))
        self.store.start()
        projection._serialized_projection = None
        projection._query_work = None
        product_v2._meme_queries.clear()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE dashboard_projection (name TEXT PRIMARY KEY, revision INTEGER,cursor INTEGER,input_cursor INTEGER,body BLOB,built_at INTEGER)')
        self.publish(1)

    async def asyncTearDown(self):
        await projection.stop_projection_queries()
        product_v2._meme_queries.clear()
        self.store.stop()
        self.temp.cleanup()

    def publish(self, revision, views=('full', 'market', 'overview', 'feed')):
        payload = {'now': revision*1000, 'realtime': {'revision': revision, 'cursor': revision*10},
                   'unified': {'assets': [{'chainId': '56', 'token': '0xa', 'kind': 'candidate', 'price': revision}],
                               'stockTokens': [], 'relations': []}}
        with closing(sqlite3.connect(self.path)) as db:
            for view in views:
                body = projection._encode_snapshot(json.dumps(payload))
                db.execute('INSERT OR REPLACE INTO dashboard_projection VALUES (?,?,?,?,?,?)',
                           (view, revision, revision*10, revision*100, body, revision*1000))
            db.commit()

    async def test_cold_viewers_share_read_parse_and_token_index(self):
        with patch.object(projection, '_decode_snapshot', wraps=projection._decode_snapshot) as decode, \
             patch.object(projection, '_load_query_json', wraps=projection._load_query_json) as parse, \
             patch.object(projection, '_token_indexes', wraps=projection._token_indexes) as index:
            results = await asyncio.gather(*[product_v2.published() if i % 2 else projection.read_token_projection('56', '0xa') for i in range(40)])
            self.assertEqual(decode.call_count, 1)
            self.assertEqual(parse.call_count, 1)
            self.assertEqual(index.call_count, 1)
        self.assertIs(results[0]['asset'], results[1]['unified']['assets'][0])
        health = projection.projection_query_health()
        self.assertEqual(health['stats']['reads'], 1)
        self.assertGreater(health['stats']['mergedReads'], 1)
        self.assertEqual(health['stats']['peakConnections'], 1)

    async def test_later_reads_verify_revision_and_reuse_same_graph_for_tape_only_commit(self):
        first = await product_v2.published()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('UPDATE dashboard_projection SET input_cursor=input_cursor+1')
            db.commit()
        self.assertIs(await product_v2.published(), first)
        self.publish(2)
        latest = await product_v2.published()
        self.assertIsNot(latest, first)
        self.assertEqual(latest['realtime']['revision'], 2)
        self.assertEqual((await projection.read_token_projection('56', '0xa'))['asset']['price'], 2)
        self.assertEqual(projection.projection_query_health()['stats']['parsed'], 2)

    async def test_deleted_or_missing_projection_never_serves_cached_snapshot_as_fresh(self):
        await product_v2.published()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DELETE FROM dashboard_projection WHERE name='full'")
            db.commit()
        with self.assertRaises(HTTPException) as error:
            await product_v2.published()
        self.assertEqual(error.exception.status_code, 503)
        with self.assertRaises(projection.ProjectionUnavailable):
            await projection.read_token_projection('56', '0xa')

    async def test_old_named_view_is_derived_from_matching_full_revision(self):
        await projection.read_projection_payload('market')
        self.publish(2, views=('full',))
        payload = await projection.read_projection_payload('market')
        self.assertEqual(payload['realtime']['revision'], 2)
        self.assertEqual(payload['unified']['snapshotScope'], 'market')
        self.assertEqual(payload['unified']['assets'][0]['price'], 2)
        with self.assertRaises(projection.ProjectionUnavailable):
            await projection.read_projection_json('feed')

    async def test_database_replacement_changes_cache_epoch_even_with_same_publication_metadata(self):
        first = await product_v2.published()
        self.scoped.db = object()
        with patch.object(projection, '_decode_snapshot', wraps=projection._decode_snapshot) as decode:
            refreshed = await product_v2.published()
        self.assertEqual(decode.call_count, 1)
        self.assertIsNot(first, refreshed)

    async def test_cancelled_viewer_does_not_cancel_shared_read(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = projection._read_query_snapshot
        async def delayed(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        with patch.object(projection, '_read_query_snapshot', delayed):
            first = asyncio.create_task(projection.read_projection_json())
            await entered.wait()
            other = asyncio.create_task(projection.read_projection_json())
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            result = await other
        self.assertEqual(json.loads(result)['realtime']['revision'], 1)
        self.assertFalse(projection._query_coordinator().inflight)

    async def test_four_views_use_at_most_two_connections_and_two_cpu_workers(self):
        await asyncio.gather(*[projection.read_projection_payload(view) for view in ('full', 'market', 'overview', 'feed') for _ in range(8)])
        stats = projection.projection_query_health()['stats']
        self.assertLessEqual(stats['peakConnections'], 2)
        self.assertLessEqual(stats['peakWorkers'], 2)
        self.assertEqual(stats['decoded'], 4)

    async def test_decode_parse_and_index_run_off_event_loop(self):
        main = threading.get_ident()
        threads = []
        original_decode, original_parse, original_index = projection._decode_snapshot, projection._load_query_json, projection._token_indexes
        def record(function):
            def call(*args, **kwargs):
                threads.append(threading.get_ident())
                return function(*args, **kwargs)
            return call
        with patch.object(projection, '_decode_snapshot', record(original_decode)), \
             patch.object(projection, '_load_query_json', record(original_parse)), \
             patch.object(projection, '_token_indexes', record(original_index)):
            await projection.read_token_projection('56', '0xa')
        self.assertEqual(len(threads), 3)
        self.assertTrue(all(thread != main for thread in threads))

    async def test_serialized_byte_budget_evicts_other_view_and_oversize_is_explicit(self):
        body = await projection.read_projection_json('full')
        with patch.object(projection, 'QUERY_CACHE_BYTES', body.serialized_bytes+40):
            await projection.read_projection_json('market')
            self.assertLessEqual(projection.projection_query_health()['serializedBytes'], body.serialized_bytes+40)
            self.assertEqual(len(projection._serialized_projection), 1)
        self.publish(2)
        with patch.object(projection, 'QUERY_CACHE_BYTES', 1):
            with self.assertRaises(HTTPException) as error:
                await product_v2.published()
            self.assertEqual(error.exception.status_code, 503)
            self.assertEqual(error.exception.detail, 'projection-cache-budget-exceeded')

    async def test_decompression_size_limit_rejects_large_snapshot_without_old_fallback(self):
        await product_v2.published()
        self.publish(2)
        with patch.object(projection, 'QUERY_DECODED_LIMIT', 20):
            with self.assertRaises(projection.ProjectionUnavailable) as error:
                await projection.read_projection_json()
        self.assertEqual(str(error.exception), 'projection-snapshot-too-large')

    async def test_dashboard_reuses_wire_bytes_and_etag_then_changes_with_revision(self):
        request = Request({'type': 'http', 'headers': []})
        first = await dashboard.get_dashboard(request, 'full')
        second = await dashboard.get_dashboard(request, 'full')
        self.assertIs(first.body, second.body)
        self.assertEqual(first.headers['etag'], second.headers['etag'])
        cached = await dashboard.get_dashboard(Request({'type': 'http', 'headers': [(b'if-none-match', first.headers['etag'].encode())]}), 'full')
        self.assertEqual(cached.status_code, 304)
        self.publish(2)
        latest = await dashboard.get_dashboard(request, 'full')
        self.assertNotEqual(first.headers['etag'], latest.headers['etag'])

    async def test_legacy_private_projection_can_be_mutated_without_changing_shared_graph(self):
        private = await projection.read_projection()
        private['unified']['assets'][0]['price'] = 999
        self.assertEqual((await product_v2.published())['unified']['assets'][0]['price'], 1)

    async def test_equal_plain_strings_reuse_version_compatible_parse_graph(self):
        text = json.dumps({'now': 1, 'realtime': {'revision': 7}, 'unified': {'assets': [], 'stockTokens': [], 'relations': []}})
        async def plain(_view):
            return text.encode().decode()
        with patch.object(projection, 'read_projection_json', plain), patch.object(product_v2, 'read_projection_json', plain):
            first = await product_v2.published()
            await projection.read_token_projection('56', '0xa')
            self.assertIs(await product_v2.published(), first)
        self.assertEqual(projection.projection_query_health()['stats']['parsed'], 1)

    async def test_meme_queries_retain_only_current_and_previous_generations(self):
        request = Request({'type': 'http', 'headers': [], 'query_string': b'showMissing=1'})
        payload = await projection.read_projection_payload('market')
        payload = copy.deepcopy(payload)
        with patch.object(product_v2, 'published_market', AsyncMock(side_effect=lambda: copy.deepcopy(payload))):
            await product_v2.selected_memes(request)
            payload['realtime']['revision'] = 2
            payload['now'] = 2000
            await product_v2.selected_memes(request)
            # An old chart request must not move generation 1 ahead of 2.
            await product_v2.selected_memes(request, revision=1)
            payload['realtime']['revision'] = 3
            payload['now'] = 3000
            await product_v2.selected_memes(request)
        self.assertEqual({key[1] for key in product_v2._meme_queries}, {2, 3})

    async def test_complete_read_admission_covers_parse_waiters_and_maps_overload_to_503(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = projection._read_query_snapshot
        async def delayed(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        with patch.object(projection, '_read_query_snapshot', delayed):
            tasks = [asyncio.create_task(product_v2.published()) for _ in range(70)]
            await entered.wait()
            self.assertEqual(projection.projection_query_health()['waiters'], 64)
            release.set()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        failures = [value for value in outcomes if isinstance(value, HTTPException)]
        self.assertEqual(len(failures), 6)
        self.assertTrue(all(value.status_code == 503 and value.detail == 'projection-query-busy' for value in failures))
        self.assertEqual(projection.projection_query_health()['waiters'], 0)

    async def test_product_builders_leave_shared_full_and_market_graphs_unchanged(self):
        full = await product_v2.published()
        before = copy.deepcopy(full)
        await product_v2.stocks(catalog='all')
        await product_v2.search('0xa')
        await projection.read_token_projection('56', '0xa')
        self.assertEqual(full, before)
        market = await product_v2.published_market()
        before = copy.deepcopy(market)
        await product_v2.memes(Request({'type': 'http', 'headers': [], 'query_string': b'showMissing=1'}))
        self.assertEqual(market, before)

    async def test_meme_selection_cannot_reuse_previous_database_epoch_with_same_revision(self):
        request = Request({'type': 'http', 'headers': [], 'query_string': b'showMissing=1'})
        first = await product_v2.selected_memes(request)
        self.scoped.db = object()
        fresh = await product_v2.selected_memes(request, revision=1)
        self.assertIsNot(first['payload'], fresh['payload'])
        self.assertEqual(len(product_v2._meme_queries), 1)

    async def test_corrupt_new_snapshot_is_503_and_does_not_return_last_good_revision(self):
        await product_v2.published()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE dashboard_projection SET revision=2,body=? WHERE name='full'", (projection._SNAPSHOT_MAGIC+b'invalid-zlib',))
            db.commit()
        with self.assertRaises(HTTPException) as error:
            await dashboard.get_dashboard(Request({'type': 'http', 'headers': []}))
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(error.exception.detail, 'projection-snapshot-invalid')


class QueryAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_bounds_waiting_readers_and_recovers_after_completion(self):
        work = QueryWork(readers=2)
        entered, release = asyncio.Event(), asyncio.Event()
        async def reader():
            async with work.admit():
                if work.waiters == 2:
                    entered.set()
                await release.wait()
        tasks = [asyncio.create_task(reader()) for _ in range(2)]
        await entered.wait()
        with self.assertRaises(RuntimeError):
            await reader()
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(work.waiters, 0)
        await reader()
        await work.close()

    async def test_canceled_cpu_client_cannot_release_slot_while_thread_is_running(self):
        work = QueryWork(workers=1, jobs=2)
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(3)
            return 1
        first = asyncio.create_task(work.cpu(slow))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(work.cpu(lambda: 2))
        await asyncio.sleep(0)
        self.assertEqual(work.stats['activeWorkers'], 1)
        with self.assertRaises(RuntimeError):
            await work.cpu(lambda: 3)
        release.set()
        self.assertEqual(await second, 2)
        await work.close()
        self.assertEqual(work.stats['peakWorkers'], 1)
        self.assertFalse(work.jobs)
