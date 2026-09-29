"""Delivery invariants, not frontend implementation snapshots."""
import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from app import db as storage
from app import realtime_projection as projection
from app.dashboard_projection import market_dashboard, overview_dashboard
from app.db import ResearchStore
from app.realtime_schema import enqueue_event


class ProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/research.sqlite'
        self.saved_stores = storage._stores
        storage._stores = {}
        for chain in ('196', '56', '4663', 'system'):
            storage._stores[chain] = await ResearchStore(self.path, chain).connect()
        self.s = storage._stores['196']
        self.sources = patch.object(projection, 'SHARED_FILES', ())
        self.sources.start()
        projection._file_signatures.clear()

    async def asyncTearDown(self):
        self.sources.stop()
        await storage.close_all()
        storage._stores = self.saved_stores
        self.temp.cleanup()

    async def put_asset(self, **changes):
        now = int(time.time()*1000)
        await self.s.put('asset', '0xabc', {'kind': 'candidate', 'token': '0xabc', 'symbol': 'MEME',
                         'price': 1, 'volume24h': 10, 'fieldTimes': {'price': now, 'volume24h': now}, **changes})

    async def tick(self, **kwargs):
        # Avoid external request accounting state; all product data below is
        # still built through the real DashboardData and pinned DB connection.
        with patch('app.state._source_statuses', return_value=[]):
            return await projection.projection_tick(**kwargs)

    async def test_facts_and_outbox_are_one_transaction_even_for_non_python_writer(self):
        with closing(sqlite3.connect(self.path)) as external:
            external.execute('BEGIN')
            external.execute('INSERT INTO facts VALUES (?,?,?)', ('196:asset', 'external', '{"price":1}'))
            self.assertEqual(external.execute('SELECT COUNT(*) FROM change_outbox').fetchone()[0], 1)
            external.rollback()
            self.assertEqual(external.execute('SELECT COUNT(*) FROM change_outbox').fetchone()[0], 0)
            external.execute('INSERT INTO facts VALUES (?,?,?)', ('196:asset', 'external', '{"price":2}'))
            external.commit()
        self.assertEqual((await self.s.get('asset', 'external'))['price'], 2)
        self.assertEqual((await (await self.s.db.execute('SELECT COUNT(*) FROM change_outbox')).fetchone())[0], 1)

    async def test_read_helpers_do_not_pin_a_busy_snapshot_between_jobs(self):
        await self.s.put('asset', 'a', {'price': 1})
        await self.s.put('asset', 'b', {'price': 2})
        # Reproduce the old two-await read: execute has stepped onto the first
        # row while this shared connection has not drained/closed its cursor.
        cursor = await self.s.db.execute("SELECT body FROM facts WHERE kind='196:asset' ORDER BY id")
        with closing(sqlite3.connect(self.path)) as other_process:
            other_process.execute("UPDATE facts SET body=? WHERE kind='196:asset' AND id='b'", ('{"price":3}',))
            other_process.commit()
        with patch('builtins.print'), self.assertRaises(sqlite3.OperationalError) as failure:
            await self.s.put('asset', 'c', {'price': 4})
        self.assertEqual(failure.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY_SNAPSHOT)
        await cursor.close()
        rows = await self.s.fetchall("SELECT body FROM facts WHERE kind='196:asset' ORDER BY id")
        self.assertEqual(len(rows), 2)
        with closing(sqlite3.connect(self.path)) as other_process:
            other_process.execute("UPDATE facts SET body=? WHERE kind='196:asset' AND id='b'", ('{"price":5}',))
            other_process.commit()
        # The atomic execute_fetchall has already released the read snapshot;
        # an intervening external commit cannot break this connection's write.
        await self.s.put('asset', 'c', {'price': 6})
        self.assertEqual((await self.s.get('asset', 'c'))['price'], 6)

    async def test_projection_retries_writer_contention_without_consuming_changes(self):
        await self.put_asset()
        execute = self.s.db.execute
        attempts = 0
        async def occasionally_busy(query, *args):
            nonlocal attempts
            if query == 'BEGIN IMMEDIATE' and attempts == 0:
                attempts += 1
                raise sqlite3.OperationalError('database is locked')
            return await execute(query, *args)
        with patch.object(self.s.db, 'execute', side_effect=occasionally_busy), patch('builtins.print'):
            result = await self.tick()
        self.assertTrue(result['changed'])
        self.assertEqual(attempts, 1)
        self.assertEqual((await self.s.fetchone("SELECT revision FROM dashboard_projection WHERE name='full'"))[0], 1)
        self.assertEqual((await self.s.fetchone('SELECT COUNT(*) FROM realtime_events'))[0], 1)
        self.assertEqual((await self.s.fetchone('SELECT COUNT(*) FROM change_outbox'))[0], 1)
        self.assertEqual((await projection.read_projection())['unified']['assets'][0]['token'], '0xabc')

    async def test_pending_facts_survive_publication_crash_snapshot_cursor_is_atomic(self):
        await self.put_asset()
        original = projection.enqueue_event
        async def crash_after_event(*args):
            await original(*args)
            raise RuntimeError('simulated process failure before commit')
        with patch.object(projection, 'enqueue_event', side_effect=crash_after_event):
            with self.assertRaises(RuntimeError):
                await self.tick()
        self.assertIsNone(await (await self.s.db.execute('SELECT * FROM dashboard_projection')).fetchone())
        self.assertIsNone(await (await self.s.db.execute('SELECT * FROM realtime_events')).fetchone())
        self.assertIsNotNone(await (await self.s.db.execute('SELECT * FROM change_outbox')).fetchone())
        await self.tick()
        snapshot = await projection.read_projection()
        row = await (await self.s.db.execute('SELECT id,body FROM realtime_events')).fetchone()
        # Cursor marks the consistent fact read, before publication. Replaying
        # its own same-revision delta is harmless; intervening trades must not
        # be skipped by advancing to the later publication id.
        self.assertLess(snapshot['realtime']['cursor'], row['id'])
        delta = json.loads(row['body'])
        self.assertEqual(snapshot['realtime']['revision'], delta['revision'])
        self.assertEqual(snapshot['unified']['assets'], delta['upserts']['assets'])
        # Restart: a new connection can read the committed, cursor-bound state.
        with closing(sqlite3.connect(self.path)) as reader:
            stored = reader.execute('SELECT body FROM dashboard_projection').fetchone()[0]
            self.assertIsInstance(stored, bytes)
            self.assertEqual(json.loads(projection._decode_snapshot(stored)), snapshot)

    async def test_projection_build_does_not_block_live_writes_or_skip_their_replay(self):
        await self.put_asset(price=1)
        entered, resume = asyncio.Event(), asyncio.Event()
        original_build = projection._build_payload

        async def paused_build(connection):
            entered.set()
            await resume.wait()
            return await original_build(connection)

        with patch.object(projection, '_build_payload', side_effect=paused_build):
            publishing = asyncio.create_task(self.tick())
            await asyncio.wait_for(entered.wait(), 1)
            try:
                await asyncio.wait_for(self.put_asset(price=2), 1)
                async with self.s._guard_write():
                    trade_cursor = await enqueue_event(self.s.db, 'trade', {'id': 'during-build'})
                    await self.s.db.commit()
            finally:
                resume.set()
            await publishing
        snapshot = await projection.read_projection()
        self.assertEqual(snapshot['unified']['assets'][0]['price'], 1)
        self.assertLess(snapshot['realtime']['cursor'], trade_cursor)
        replay = await self.s.fetchall('SELECT id,event FROM realtime_events WHERE id>? ORDER BY id',
                                       (snapshot['realtime']['cursor'],))
        self.assertEqual(replay[0]['id'], trade_cursor)
        await self.tick()
        latest = await projection.read_projection()
        self.assertEqual(latest['unified']['assets'][0]['price'], 2)

    async def test_incremental_fact_view_reads_external_changes_without_redecoding_unchanged_assets(self):
        await self.put_asset(price=1)
        await self.s.put('asset', 'unchanged', {'token': 'unchanged', 'kind': 'candidate', 'symbol': 'FIXED', 'price': 10})
        await self.tick()
        original = projection._committed_facts[1]
        unchanged = original.rows['196:asset']['unchanged']
        with closing(sqlite3.connect(self.path)) as external:
            external.execute("UPDATE facts SET body=? WHERE kind='196:asset' AND id='0xabc'",
                             (json.dumps({'token': '0xabc', 'kind': 'candidate', 'symbol': 'MEME', 'price': 2}),))
            external.execute('INSERT INTO facts VALUES (?,?,?)', ('196:asset', 'new',
                             json.dumps({'token': 'new', 'kind': 'candidate', 'symbol': 'NEW', 'price': 3})))
            external.commit()
        await self.tick()
        current = projection._committed_facts[1]
        self.assertIs(current.rows['196:asset']['unchanged'], unchanged)
        self.assertEqual(original.rows['196:asset']['0xabc']['price'], 1)
        self.assertEqual(current.rows['196:asset']['0xabc']['price'], 2)
        self.assertNotIn('new', original.rows['196:asset'])
        self.assertEqual(len((await projection.read_projection())['unified']['assets']), 3)
        with closing(sqlite3.connect(self.path)) as external:
            external.execute("DELETE FROM facts WHERE kind='196:asset' AND id='0xabc'")
            external.commit()
        await self.tick()
        self.assertNotIn('0xabc', projection._committed_facts[1].rows['196:asset'])
        self.assertEqual(len((await projection.read_projection())['unified']['assets']), 2)

    async def test_failed_incremental_publish_preserves_cache_then_retries_latest_fact(self):
        await self.put_asset(price=1)
        await self.tick()
        before = projection._committed_facts
        await self.put_asset(price=2)
        with patch.object(projection, 'enqueue_event', side_effect=RuntimeError('rollback')):
            with self.assertRaises(RuntimeError):
                await self.tick()
        self.assertIs(projection._committed_facts, before)
        self.assertEqual(before[1].rows['196:asset']['0xabc']['price'], 1)
        await self.put_asset(price=3)
        await self.tick()
        self.assertEqual((await projection.read_projection())['unified']['assets'][0]['price'], 3)

    async def test_outbox_rewind_rebuilds_before_idle_shortcut(self):
        await self.put_asset(price=1)
        await self.tick()
        with closing(sqlite3.connect(self.path)) as external:
            external.execute("DELETE FROM facts WHERE kind='196:asset' AND id='0xabc'")
            external.execute('DELETE FROM change_outbox')
            external.commit()
        outcome = await self.tick()
        self.assertTrue(outcome['changed'])
        self.assertEqual((await projection.read_projection())['unified']['assets'], [])
        self.assertNotIn('0xabc', projection._committed_facts[1].rows['196:asset'])
        published = await self.s.fetchone('SELECT body FROM realtime_events ORDER BY id DESC LIMIT 1')
        invalidations = json.loads(published['body'])['invalidations']
        self.assertIn({'kind': 'detail'}, invalidations)
        self.assertIn({'kind': 'briefing'}, invalidations)

    async def test_pruned_fact_changes_cannot_be_hidden_by_remaining_tape_only_changes(self):
        await self.put_asset(price=1)
        await self.tick()
        with closing(sqlite3.connect(self.path)) as external:
            external.execute("DELETE FROM facts WHERE kind='196:asset' AND id='0xabc'")
            external.execute('DELETE FROM change_outbox')
            external.execute('INSERT INTO trades VALUES (?,?,?,?)',
                             ('196:binance:0xabc', 'new-trade', 100, '{"t":100,"price":2}'))
            external.commit()
        outcome = await self.tick()
        self.assertTrue(outcome['changed'])
        self.assertEqual((await projection.read_projection())['unified']['assets'], [])
        self.assertNotIn('0xabc', projection._committed_facts[1].rows['196:asset'])

    async def test_forced_calibration_reads_facts_without_reusable_outbox_evidence(self):
        await self.put_asset(price=1)
        await self.tick()
        old_input = projection._committed_facts[0][2]
        with closing(sqlite3.connect(self.path)) as external:
            external.execute("UPDATE facts SET body=? WHERE kind='196:asset' AND id='0xabc'",
                             (json.dumps({'token': '0xabc', 'kind': 'candidate', 'symbol': 'MEME', 'price': 2}),))
            external.execute('DELETE FROM change_outbox WHERE id>?', (old_input,))
            external.commit()
        await self.tick(force=True)
        self.assertEqual((await projection.read_projection())['unified']['assets'][0]['price'], 2)
        self.assertEqual(projection._committed_facts[1].rows['196:asset']['0xabc']['price'], 2)

    async def test_contiguous_tape_only_preserves_cache_then_applies_next_asset_update(self):
        await self.put_asset(price=1)
        await self.tick()
        previous = projection._committed_facts
        with closing(sqlite3.connect(self.path)) as external:
            external.execute('INSERT INTO trades VALUES (?,?,?,?)',
                             ('196:binance:0xabc', 'new-trade', 100, '{"t":100,"price":2}'))
            external.commit()
        outcome = await self.tick()
        self.assertFalse(outcome['changed'])
        self.assertIs(projection._committed_facts[1], previous[1])
        self.assertGreater(projection._committed_facts[0][2], previous[0][2])
        await self.put_asset(price=3)
        await self.tick()
        self.assertEqual((await projection.read_projection())['unified']['assets'][0]['price'], 3)

    async def test_compressed_snapshot_reads_legacy_text_and_caches_exact_cursor(self):
        await self.put_asset()
        await self.tick()
        original = await projection.read_projection_json()
        with closing(sqlite3.connect(self.path)) as writer:
            stored = writer.execute('SELECT body FROM dashboard_projection').fetchone()[0]
            self.assertTrue(stored.startswith(projection._SNAPSHOT_MAGIC))
            writer.execute("UPDATE dashboard_projection SET body=? WHERE name='full'", (original,))
            writer.commit()
        projection._serialized_projection = None
        projection._committed_projection = None
        with patch.object(projection, '_decode_snapshot', wraps=projection._decode_snapshot) as decode:
            self.assertEqual(await projection.read_projection_json(), original)
            self.assertEqual(await projection.read_projection_json(), original)
            self.assertEqual(decode.call_count, 1)
        await self.put_asset(price=2)
        await self.tick()
        updated = await projection.read_projection()
        self.assertEqual(updated['unified']['assets'][0]['price'], 2)
        self.assertGreater(updated['realtime']['cursor'], json.loads(original)['realtime']['cursor'])

    async def test_named_views_share_full_revision_and_roll_forward_safely(self):
        await self.put_asset()
        await self.tick()
        rows = await self.s.fetchall('''SELECT name,revision,cursor,input_cursor,built_at,body
            FROM dashboard_projection''')
        self.assertEqual({row['name'] for row in rows}, {'full', 'overview', 'market', 'feed'})
        metadata = {tuple(row[key] for key in ('revision', 'cursor', 'input_cursor', 'built_at'))
                    for row in rows}
        self.assertEqual(len(metadata), 1)
        full = json.loads(await projection.read_projection_json('full'))
        self.assertEqual(json.loads(await projection.read_projection_json('overview')),
                         overview_dashboard(full))
        self.assertEqual(json.loads(await projection.read_projection_json('market')),
                         market_dashboard(full))
        feed = json.loads(await projection.read_projection_json('feed'))
        self.assertEqual(feed['realtime'], full['realtime'])
        self.assertEqual(feed['assets'], [])

        # An old worker can leave named rows absent or at an older revision.
        await self.s.db.execute("DELETE FROM dashboard_projection WHERE name='overview'")
        await self.s.db.execute("UPDATE dashboard_projection SET revision=0 WHERE name='market'")
        await self.s.db.commit()
        self.assertEqual(json.loads(await projection.read_projection_json('overview')),
                         overview_dashboard(full))
        self.assertEqual(json.loads(await projection.read_projection_json('market')),
                         market_dashboard(full))

        # Feed cannot be recovered from the dashboard's globally truncated
        # signals, so a missing row must be rebuilt from the fact snapshot.
        await self.s.db.execute("DELETE FROM dashboard_projection WHERE name='feed'")
        await self.s.db.commit()
        rebuilt = json.loads(await projection.read_projection_json('feed'))
        self.assertEqual(rebuilt['realtime'], (await projection.read_projection())['realtime'])
        refreshed = await self.s.fetchall('SELECT name,revision,cursor,input_cursor,built_at FROM dashboard_projection')
        self.assertEqual(len({tuple(row[key] for key in ('revision','cursor','input_cursor','built_at'))
                              for row in refreshed}), 1)

    async def test_same_price_volume_null_deletion_and_metadata_are_published(self):
        await self.put_asset()
        await self.tick()
        await self.put_asset(volume24h=None, holders=0)
        await self.s.put('chain-stream', 'pool', {'status': 'reconnecting'})
        await self.tick()
        delta = json.loads((await (await self.s.db.execute('SELECT body FROM realtime_events ORDER BY id DESC LIMIT 1')).fetchone())[0])
        changed = delta['upserts']['assets'][0]
        self.assertEqual(changed['price'], 1)
        self.assertEqual(changed['holders'], 0)
        self.assertNotIn('volume24h', changed)  # complete replacement clears old 10
        self.assertTrue(delta['meta']['sources'])
        self.assertIn({'kind': 'detail', 'chainId': '196', 'token': '0xabc'}, delta['invalidations'])
        await self.s.db.execute("DELETE FROM facts WHERE kind='196:asset' AND id='0xabc'")
        await self.s.db.commit()
        await self.tick()
        delta = json.loads((await (await self.s.db.execute('SELECT body FROM realtime_events ORDER BY id DESC LIMIT 1')).fetchone())[0])
        self.assertEqual(delta['removes']['assets'], ['196:0xabc'])

    async def test_no_dashboard_rebuild_for_demand_leases_or_unchanged_sources(self):
        await self.put_asset()
        await self.tick()
        for kind in ('watch', 'collector-job', 'insight-demand', 'pool-candle-acc'):
            await self.s.put(kind, '0xabc', {'expiresAt': 999, 'attempts': 2})
        with patch.object(projection, '_build_payload', side_effect=AssertionError('unexpected rebuild')):
            result = await self.tick()
        self.assertFalse(result['changed'])

    async def test_bridge_is_restart_safe_and_clears_deleted_snapshot(self):
        filename = self.temp.name + '/node.json'
        Path(filename).write_text(json.dumps({'tokens': [{'price': 1}]}))
        self.assertEqual(await projection.bridge_shared_sources([filename]), 1)
        projection._file_signatures.clear()  # fresh process, persisted signature
        self.assertEqual(await projection.bridge_shared_sources([filename]), 0)
        saved = await storage._stores['system'].get('shared-source', filename)
        self.assertEqual(saved['data']['tokens'][0]['price'], 1)
        Path(filename).write_text('{broken')
        await projection.bridge_shared_sources([filename])
        saved = await storage._stores['system'].get('shared-source', filename)
        self.assertEqual(saved['data']['tokens'][0]['price'], 1)
        self.assertEqual(saved['data']['status'], 'error')
        Path(filename).unlink()
        await projection.bridge_shared_sources([filename])
        saved = await storage._stores['system'].get('shared-source', filename)
        self.assertEqual(saved['data']['tokens'], [])

    async def test_pinned_source_overlay_and_explicit_product_invalidations(self):
        await self.put_asset()
        await self.s.put('comparison', '0xabc', {'token': '0xabc', 'pairs': []})
        await self.s.put('insight', '0xabc:zh', {'text': 'new'})
        await storage._stores['system'].put('shared-source', 'data/binance.json', {'data': {'tokens': [{'tokenContractAddress': '0xstock', 'price': 12, 'quoteAt': int(time.time()*1000)}]}})
        await storage._stores['56'].put('stock', '0xstock', {'tokenContractAddress': '0xstock', 'stockCode': 'AAPL'})
        await self.tick()
        snapshot = await projection.read_projection()
        self.assertEqual(snapshot['unified']['stockTokens'][0]['price'], 12)
        delta = json.loads((await (await self.s.db.execute('SELECT body FROM realtime_events ORDER BY id DESC LIMIT 1')).fetchone())[0])
        self.assertIn({'kind': 'comparison', 'chainId': '196', 'token': '0xabc'}, delta['invalidations'])
        self.assertIn({'kind': 'insight', 'chainId': '196', 'token': '0xabc'}, delta['invalidations'])

    async def test_shared_catalogue_adds_new_listing_without_waiting_for_db_scan(self):
        await storage._stores['system'].put('shared-source', 'data/robinhood.json', {'data': {'tokens': [
            {'tokenContractAddress': '0xnew', 'tokenSymbol': 'NEW', 'stockCode': 'NEW',
             'price': 20, 'quoteAt': int(time.time()*1000)}]}})
        await self.tick()
        snapshot = await projection.read_projection()
        row = next(row for row in snapshot['unified']['stockTokens'] if row['tokenContractAddress'] == '0xnew')
        self.assertEqual(row['chainId'], '4663')
        self.assertEqual(row['price'], 20)
        self.assertIsNone(await storage._stores['4663'].get('stock', '0xnew'))

    async def test_delayed_price_cannot_replace_newer_price_metadata(self):
        await self.s.merge_asset_observation('0xabc', {
            'priceProvenance': {'provider': 'new', 'dependencies': []}, 'priceCurrency': 'USD',
            'quoteAt': 2000, 'provider': 'new', 'venue': 'dex', 'fieldSources': {'price': 'new'},
        }, {'price': (2, 2000)})
        row = await self.s.merge_asset_observation('0xabc', {
            'priceProvenance': {'provider': 'late'}, 'priceCurrency': 'USDT', 'quoteAt': 1000,
            'provider': 'late', 'venue': 'exchange', 'fieldSources': {'price': 'late'},
            'fieldTimes': {'price': 0}, 'symbol': 'updated-business-name', 'updatedAt': 1,
        }, {'price': (1, 1000)})
        self.assertEqual((row['price'], row['quoteAt'], row['priceCurrency'], row['provider'], row['venue']),
                         (2, 2000, 'USD', 'new', 'dex'))
        self.assertEqual(row['priceProvenance']['provider'], 'new')
        self.assertEqual(row['fieldTimes']['price'], 2000)
        self.assertEqual(row['symbol'], 'updated-business-name')
        self.assertEqual(row['updatedAt'], 2000)

    async def test_candle_transport_only_does_not_rebuild_whole_dashboard(self):
        await self.put_asset()
        await self.tick()
        await self.s.put('market-candle', 'cache', {'c': 2})
        await self.s.put('candle-meta', 'binance:196:0xabc:1m', {'lastSourceEventAt': 1000})
        await self.s.put_candles('binance:0xabc', '1m', [{'t': 0, 'o': 1, 'h': 2, 'l': 1, 'c': 2, 'confirmed': False}])
        with patch.object(projection, '_build_payload', side_effect=AssertionError('candle caused list rebuild')):
            result = await self.tick()
        self.assertFalse(result['changed'])

    async def test_market_fact_and_candle_event_rollback_together(self):
        await self.s.db.execute('BEGIN IMMEDIATE')
        await self.s.db.execute('INSERT INTO facts VALUES (?,?,?)', ('196:market-quote', 'dex:pool:USD:0xabc', '{"price":3}'))
        await enqueue_event(self.s.db, 'candle', {'token': '0xabc', 'row': {'c': 3}})
        await self.s.db.rollback()
        self.assertIsNone(await self.s.get('market-quote', 'dex:pool:USD:0xabc'))
        self.assertIsNone(await (await self.s.db.execute('SELECT * FROM realtime_events')).fetchone())
        self.assertIsNone(await (await self.s.db.execute('SELECT * FROM change_outbox')).fetchone())


    async def test_derived_dependencies_are_durable_scoped_and_do_not_requeue_themselves(self):
        from app.collectors.baskets import refresh_baskets
        from app.comparison_service import refresh_comparisons
        from app.state import DashboardData
        now = int(time.time()*1000)
        for i in range(3):
            token = f'0xmeme{i}'
            await self.s.put('asset', token, {'kind': 'candidate', 'token': token, 'symbol': f'M{i}',
                'price': 1, 'marketCap': 100, 'priceCurrency': 'USD',
                'fieldTimes': {'price': now, 'marketCap': now}})
            await self.s.put('relation', str(i), {'id': str(i), 'token': token, 'stock': '0xstock',
                'ticker': 'NVDA', 'pool': f'0xpool{i}', 'status': 'verified', 'checkedAt': now,
                'liquidityUsd': 2000, 'liquidityAt': now})
        for token in ('0xstock', '0xunrelated'):
            await self.s.put('stock', token, {'tokenContractAddress': token, 'stockCode': 'NVDA'})
        await self.tick()
        await self.s.db.execute('DELETE FROM projection_dirty')
        await self.s.db.commit()
        await self.s.patch_fact('asset', '0xmeme0', {'price': 1.1})
        await self.tick()
        dirty = await (await self.s.db.execute('SELECT chain,token FROM projection_dirty')).fetchall()
        self.assertEqual({tuple(r) for r in dirty}, {('196', '0xmeme0')})
        data = DashboardData()
        await data.reload()
        baskets = await refresh_baskets(affected={('196', '0xmeme0')}, data=data)
        self.assertEqual(baskets['requested'], 1)  # only the chip sector
        with patch('app.comparison_service.broadcast'):
            comparisons = await refresh_comparisons(affected={('196', '0xmeme0')}, data=data)
        self.assertEqual(comparisons['requested'], 1)  # its related stock, not the directory
        with patch('app.collectors.baskets.refresh_baskets', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                await projection.refresh_derived()
        self.assertEqual((await (await self.s.db.execute('SELECT COUNT(*) FROM projection_dirty')).fetchone())[0], 1)
        with patch('app.comparison_service.broadcast'):
            await projection.refresh_derived()
        await self.tick()
        self.assertEqual((await (await self.s.db.execute('SELECT COUNT(*) FROM projection_dirty')).fetchone())[0], 0)

    async def test_derived_backlog_is_bounded_oldest_first_and_requeued_work_survives(self):
        # A provider refresh can dirty hundreds of tokens in one projection.
        # The new generation written by another connection during calculation
        # must survive this turn's conditional acknowledgement.
        from app.state import DashboardData
        # Adjacent rows share a generation, exercising the primary-key tie
        # breaker as well as the oldest-generation ordering.
        await self.s.db.executemany('INSERT INTO projection_dirty VALUES (?,?,?)',
                                    [('196', f'0x{i:04x}', i // 2 + 1) for i in range(450)])
        await self.s.db.commit()
        batches = []

        async def no_reload(_data):
            return None

        async def no_baskets(*, affected, data):
            return {'requested': len(affected)}

        async def comparisons(*, affected, data):
            batches.append(set(affected))
            if len(batches) == 1:
                with closing(sqlite3.connect(self.path)) as publisher:
                    publisher.execute('''INSERT INTO projection_dirty VALUES (?,?,?)
                        ON CONFLICT(chain,token) DO UPDATE SET generation=excluded.generation''',
                        ('196', '0x0000', 999))
                    publisher.commit()
            return {'requested': len(affected)}

        with patch.object(DashboardData, 'reload', no_reload), \
             patch('app.collectors.baskets.refresh_baskets', no_baskets), \
             patch('app.comparison_service.refresh_comparisons', comparisons):
            first = await projection.refresh_derived()
            self.assertEqual(first['affected'], 200)
            self.assertEqual((await self.s.fetchone('SELECT COUNT(*) FROM projection_dirty'))[0], 251)
            self.assertEqual((await self.s.fetchone("SELECT generation FROM projection_dirty WHERE token='0x0000'"))[0], 999)
            second = await projection.refresh_derived()
            third = await projection.refresh_derived()
            self.assertEqual((second['affected'], third['affected']), (200, 51))
            self.assertEqual((await self.s.fetchone('SELECT COUNT(*) FROM projection_dirty'))[0], 0)
        self.assertEqual(batches[0], {('196', f'0x{i:04x}') for i in range(200)})
        self.assertEqual(batches[1], {('196', f'0x{i:04x}') for i in range(200, 400)})
        self.assertEqual(batches[2], {('196', f'0x{i:04x}') for i in range(400, 450)} | {('196', '0x0000')})

    async def test_derived_acknowledgement_retries_busy_without_recalculating(self):
        from app.state import DashboardData
        await self.s.db.executemany('INSERT INTO projection_dirty VALUES (?,?,?)',
                                    [('196', '0xone', 1), ('196', '0xtwo', 2)])
        await self.s.db.commit()
        original = self.s.db.executemany
        attempts = 0

        async def busy_once(query, arguments):
            nonlocal attempts
            if query.startswith('DELETE FROM projection_dirty') and attempts == 0:
                attempts += 1
                # SQLite has performed one DELETE before another statement in
                # the same executemany encounters contention. Roll it back in
                # full before the idempotent retry starts a new transaction.
                await original(query, arguments[:1])
                raise sqlite3.OperationalError('database is locked')
            return await original(query, arguments)

        async def no_reload(_data):
            return None

        async def completed(*, affected, data):
            return {'requested': len(affected)}

        with patch.object(DashboardData, 'reload', no_reload), \
             patch('app.collectors.baskets.refresh_baskets', side_effect=completed) as baskets, \
             patch('app.comparison_service.refresh_comparisons', side_effect=completed) as comparisons, \
             patch.object(self.s.db, 'executemany', side_effect=busy_once), \
             patch('builtins.print'):
            outcome = await projection.refresh_derived()
        self.assertEqual(outcome['affected'], 2)
        self.assertEqual(attempts, 1)
        self.assertEqual(baskets.await_count, 1)
        self.assertEqual(comparisons.await_count, 1)
        self.assertEqual((await self.s.fetchone('SELECT COUNT(*) FROM projection_dirty'))[0], 0)


    async def test_legacy_publish_does_not_block_the_task_holding_write_transaction(self):
        from app import stream_hub as hub
        shared_lock = asyncio.Lock()
        with patch.dict(os.environ, STREAM_LEDGER_PATH=self.path), patch.object(storage, '_global_write_lock', shared_lock):
            await hub.stop_hub()
            async with shared_lock:
                await self.s.db.execute('BEGIN IMMEDIATE')
                await self.s.db.execute('INSERT INTO facts VALUES (?,?,?)', ('196:asset', 'held', '{}'))
                hub.broadcast('discovery', {'token': 'held'})
                # The old synchronous writer stalled here for SQLite's 15s
                # timeout, preventing this same task from reaching commit.
                await asyncio.wait_for(asyncio.sleep(.02), timeout=.2)
                await self.s.db.commit()
            await asyncio.wait_for(hub.flush_legacy(), timeout=2)
            self.assertEqual(hub.cursor(), 1)
            await hub.stop_hub()

    async def test_volume_only_change_does_not_recompute_price_comparisons(self):
        await self.put_asset()
        await self.tick()
        await self.s.db.execute('DELETE FROM projection_dirty')
        await self.s.db.commit()
        await self.s.patch_fact('asset', '0xabc', {'volume24h': 20})
        await self.tick()
        self.assertEqual((await (await self.s.db.execute('SELECT COUNT(*) FROM projection_dirty')).fetchone())[0], 0)


class StoreInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_initializers_publish_one_connection_per_scope(self):
        original_stores = storage._stores
        original_lock = storage._store_init_lock
        original_writer = storage._global_write_lock
        storage._stores = {}
        storage._store_init_lock = asyncio.Lock()
        storage._global_write_lock = asyncio.Lock()
        with tempfile.TemporaryDirectory() as directory, patch.object(storage, 'DB_PATH', directory+'/db.sqlite'):
            try:
                calls = 0
                original = ResearchStore.connect
                async def connect(instance):
                    nonlocal calls
                    calls += 1
                    await asyncio.sleep(.01)
                    return await original(instance)
                with patch.object(ResearchStore, 'connect', connect):
                    results = await asyncio.gather(*(storage.store(chain) for chain in ['196']*20+['56']*10))
                self.assertEqual(calls, 2)
                self.assertEqual(len({id(s.db) for s in results[:20]}), 1)
                self.assertEqual(len({id(s.db) for s in results[20:]}), 1)
                # A second scope's schema initialization waits without taking
                # SQLite locks while the first scope's writer can still commit.
                first = results[0]
                async with first._write_lock:
                    await first.db.execute('BEGIN IMMEDIATE')
                    pending = asyncio.create_task(storage.store('4663'))
                    await asyncio.sleep(.02)
                    self.assertFalse(pending.done())
                    await first.db.commit()
                await asyncio.wait_for(pending, timeout=1)
            finally:
                await storage.close_all()
                storage._stores = original_stores
                storage._store_init_lock = original_lock
                storage._global_write_lock = original_writer


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_filter_checkpoints_preserve_replay_cursor(self):
        from app import stream_hub as hub
        from app.api.stream import frame_visible, get_stream
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, STREAM_LEDGER_PATH=directory+'/journal.sqlite'):
            await hub.stop_hub()
            hub.broadcast('price', {'chainId': '56', 'token': 'a', 'price': 1})
            await hub.flush_legacy()
            response = await get_stream(last_event_id='0', snapshot=False, protocol=1)
            frame = await anext(response.body_iterator)
            self.assertIn(b'event: checkpoint', frame)
            self.assertIn(b'id: 1', frame)
            await response.body_iterator.aclose()
            candle = hub._frame('candle', {'chainId': '56', 'token': '0xABC', 'venue': 'binance', 'marketId': 'NVDABUSDT', 'bar': '1m'})
            self.assertTrue(frame_visible(candle, protocol=1, candle_keys={'56:0xabc:binance:nvdabusdt:1m'}))
            self.assertFalse(frame_visible(candle, protocol=1, candle_keys={'none'}))
            self.assertTrue(frame_visible(hub._frame('trade', {}), protocol=1, candle_keys={'none'}))
            await hub.stop_hub()

    async def test_large_replay_pages_and_cross_process_tailer(self):
        from app import stream_hub as hub
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, STREAM_LEDGER_PATH=directory+'/journal.sqlite'):
            await hub.stop_hub()
            journal = hub._ledger()
            journal.executemany('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
                                [('trade', '{"id":1}', int(time.time()*1000)) for _ in range(1205)])
            journal.commit()
            frames, reached, done = hub.replay_page(0, limit=500)
            self.assertEqual(len(frames), 500)
            self.assertFalse(done)
            self.assertEqual(reached, 500)
            all_frames = b''.join(hub.replay_after(0))
            self.assertEqual(all_frames.count(b'event: trade'), 1205)
            self.assertNotIn(b'event: reset', all_frames)
            await hub.start_hub()
            q = asyncio.Queue(maxsize=10)
            hub.clients().add(q)
            with closing(sqlite3.connect(directory+'/journal.sqlite')) as producer:
                producer.execute('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
                                 ('trade', '{"id":"after-commit"}', int(time.time()*1000)))
                producer.commit()
            seq, frame = await asyncio.wait_for(q.get(), timeout=2)
            self.assertIn(b'after-commit', frame)
            self.assertEqual(seq, 1206)
            await hub.stop_hub()


class WriterDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_stack_and_release_are_safe_for_health(self):
        lock = storage.WriterLock()
        entered, resume = asyncio.Event(), asyncio.Event()
        async def held_writer():
            async with lock:
                entered.set()
                await resume.wait()
        task = asyncio.create_task(held_writer(), name='diagnostic-writer')
        await entered.wait()
        view = lock.snapshot()
        self.assertTrue(view['locked'])
        self.assertEqual(view['owner'], 'diagnostic-writer')
        self.assertIn('held_writer', [frame['function'] for frame in view['stack']])
        self.assertTrue(all(set(frame) == {'file', 'function', 'line'} for frame in view['stack']))
        resume.set()
        await task
        self.assertEqual(lock.snapshot(), {'locked': False, 'owner': None, 'heldMs': 0, 'stack': []})


if __name__ == '__main__':
    unittest.main()
