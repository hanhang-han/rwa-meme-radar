import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

from app import db as storage, state
from app.db import ResearchStore
from app.projection_facts import ProjectionFacts


class SharedFactCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name + '/facts.sqlite'
        self.saved_stores = storage._stores
        self.addCleanup(setattr, storage, '_stores', self.saved_stores)
        storage._stores = {}
        self.write_lock = storage.WriterLock()
        self.addAsyncCleanup(storage.close_all)
        for chain in ('196', '56', '4663', 'system'):
            scoped = ResearchStore(self.path, chain, write_lock=self.write_lock)
            storage._stores[chain] = scoped
            await scoped.connect()
        self.s = storage._stores['196']
        self.saved_state = {key: getattr(state, key) for key in
                            ('DATA', '_loaded_at', '_reload_task', '_shared_fact_cache', '_invalidate_generation')}
        for key, value in self.saved_state.items():
            self.addCleanup(setattr, state, key, value)
        state.DATA = state.DashboardData()
        state._loaded_at = 0
        state._reload_task = None
        state._shared_fact_cache = None
        state._invalidate_generation = 0

    @staticmethod
    def asset(token, price):
        return {'token': token, 'symbol': token.upper(), 'kind': 'candidate', 'price': price}

    async def put(self, token, price):
        await self.s.put('asset', token, self.asset(token, price))

    def prices(self):
        return {row['token']: row['price'] for row in state.DATA.assets}

    def test_unquoted_candidate_requires_price_specific_evidence(self):
        now = time.time() * 1000
        row = {'kind': 'candidate', 'price': None, 'quoteStatus': 'missing',
               'totalLiquidityUsd': 38, 'totalLiquidityAt': now,
               'totalLiquidityCoverage': {'scope': 'token-aggregate'}}
        error = {'failureCount': 10, 'reason': 'TimeoutError'}
        self.assertEqual(state.unquoted_market_status(row, error, now)['quoteStatus'], 'missing')
        missing = {'failureCount': 5, 'reason': 'missing-price-row', 'lastSuccessAt': None}
        reported = state.unquoted_market_status(row, missing, now)
        self.assertEqual(reported['quoteStatus'], 'no-verified-market')
        self.assertEqual(reported['quoteReason'], 'insufficient-liquidity')
        self.assertIsNone(reported['price'])
        self.assertEqual(state.unquoted_market_status({**row, 'price': 1}, missing, now)['quoteStatus'], 'missing')

    def test_stock_catalogue_uses_same_confirmed_missing_market_state(self):
        token = '0x' + 'b' * 40
        data = state.DashboardData()
        data.stock_tokens = [{'chainId':'56', 'tokenContractAddress':token, 'tokenSymbol':'TEST'}]
        data.quote_jobs = {('56', token): {'failureCount':10, 'reason':'missing-price-row',
                                          'lastSuccessAt':None}}
        unquoted = {'chainId':'56', 'tokenContractAddress':token,
                    'price':None, 'quoteStatus':'missing'}
        with patch('app.stock_quotes.apply_stock_overlays', return_value=[unquoted]):
            row = data.stock_views(include_comparisons=False)[0]
        self.assertIsNone(row['price'])
        self.assertEqual(row['quoteStatus'], 'no-verified-market')

    async def test_basket_view_without_base_is_captured_updated_and_removed(self):
        first = {'sector': '芯片', 'chainId': '56', 'dataStatus': 'unavailable',
                 'value': None, 'projectionVersion': 'official-a-fixed-cap-35-v3'}
        bsc = storage._stores['56']
        await bsc.put('basket-view', '芯片', first)
        await state.reload_data()
        old_cache = state._shared_fact_cache[2]
        self.assertEqual(state.DATA.basket_views[('56', '芯片')], first)
        self.assertEqual(state.DATA.baskets, {})

        second = {**first, 'dataStatus': 'paused', 'reason': '行情过期'}
        await bsc.put('basket-view', '芯片', second)
        await state.reload_data()
        self.assertEqual(state.DATA.basket_views[('56', '芯片')], second)
        self.assertEqual(old_cache.rows['56:basket-view']['芯片'], first)

        with closing(sqlite3.connect(self.path)) as external:
            external.execute('DELETE FROM facts WHERE kind=? AND id=?', ('56:basket-view', '芯片'))
            external.commit()
        await state.reload_data()
        self.assertNotIn(('56', '芯片'), state.DATA.basket_views)

    async def test_quote_checkpoints_use_fact_view_without_per_asset_database_reads(self):
        for index in range(30):
            token = 'unquoted-' + str(index)
            await self.put(token, None)
            await self.s.put('collector-job', 'quote:' + token, {'failureCount': 5})
        with patch.object(ResearchStore, 'get', side_effect=AssertionError('per-asset-checkpoint-read')):
            await state.reload_data()
        self.assertEqual(len(state.DATA.quote_jobs), 30)
        old_cache = state._shared_fact_cache[2]
        await self.s.put('collector-job', 'quote:unquoted-0', {'failureCount': 1})
        with patch.object(ResearchStore, 'get', side_effect=AssertionError('per-asset-checkpoint-read')):
            await state.reload_data()
        self.assertEqual(state.DATA.quote_jobs[('196', 'unquoted-0')]['failureCount'], 1)
        self.assertEqual(old_cache.rows['196:collector-job']['quote:unquoted-0']['failureCount'], 5)
        with closing(sqlite3.connect(self.path)) as external:
            external.execute('DELETE FROM facts WHERE kind=? AND id=?',
                             ('196:collector-job', 'quote:unquoted-0'))
            external.commit()
        await state.reload_data()
        self.assertNotIn(('196', 'unquoted-0'), state.DATA.quote_jobs)

    async def test_asset_and_quote_checkpoint_stay_in_same_snapshot_then_advance_together(self):
        await self.put('unquoted', None)
        await self.s.put('collector-job', 'quote:unquoted', {'failureCount': 5})
        capture = ProjectionFacts.capture
        async def change_after_capture(connection, previous, changes):
            facts = await capture(connection, previous, changes)
            with closing(sqlite3.connect(self.path)) as external:
                external.execute('UPDATE facts SET body=? WHERE kind=? AND id=?',
                                 (json.dumps(self.asset('unquoted', 2)), '196:asset', 'unquoted'))
                external.execute('UPDATE facts SET body=? WHERE kind=? AND id=?',
                                 ('{"failureCount":0}', '196:collector-job', 'quote:unquoted'))
                external.commit()
            return facts
        with patch.object(ProjectionFacts, 'capture', side_effect=change_after_capture):
            await state.reload_data()
        original = state._shared_fact_cache[2]
        self.assertIsNone(self.prices()['unquoted'])
        self.assertEqual(state.DATA.quote_jobs[('196', 'unquoted')]['failureCount'], 5)
        await state.reload_data()
        self.assertEqual(self.prices()['unquoted'], 2)
        self.assertNotIn(('196', 'unquoted'), state.DATA.quote_jobs)
        self.assertNotIn('quote:unquoted', state._shared_fact_cache[2].rows['196:collector-job'])
        self.assertEqual(original.rows['196:collector-job']['quote:unquoted']['failureCount'], 5)

    async def test_repeated_missing_price_is_disclosed_without_deleting_candidate(self):
        token = '0x' + 'a' * 40
        await self.s.put('asset', token, {'token': token, 'symbol': 'TEST', 'kind': 'candidate'})
        await self.s.put('collector-job', 'quote:' + token, {
            'domain': 'quote', 'key': token, 'failureCount': 5,
            'reason': 'missing-price-row', 'lastSuccessAt': None})
        await state.reload_data()
        with patch('app.market_quotes._read_snapshot', return_value={}):
            rows = state.DATA.visible_assets(now=time.time() * 1000)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].get('price'))
        self.assertEqual(rows[0]['quoteStatus'], 'no-verified-market')
        self.assertEqual(rows[0]['quoteReason'], 'source-returned-no-price')

        await self.s.put('asset', token, {'token': token, 'symbol': 'TEST',
                                          'kind': 'candidate', 'price': 2})
        await state.reload_data()
        with patch('app.market_quotes._read_snapshot', return_value={}):
            row = state.DATA.visible_assets(now=time.time() * 1000)[0]
        self.assertEqual(row['price'], 2)
        self.assertNotEqual(row['quoteStatus'], 'no-verified-market')

    async def test_external_update_and_delete_are_incremental_and_leave_old_cache_unchanged(self):
        for token, price in [('a', 1), ('b', 2), ('unchanged', 3)]:
            await self.put(token, price)
        await state.reload_data()
        original = state._shared_fact_cache[2]
        with closing(sqlite3.connect(self.path)) as other_process:
            other_process.execute('UPDATE facts SET body=? WHERE kind=? AND id=?',
                                  (json.dumps(self.asset('a', 10)), '196:asset', 'a'))
            other_process.execute('DELETE FROM facts WHERE kind=? AND id=?', ('196:asset', 'b'))
            other_process.commit()
        with patch.object(ProjectionFacts, 'capture', wraps=ProjectionFacts.capture) as capture:
            await state.reload_data()
        self.assertIs(capture.await_args.args[1], original)
        self.assertEqual(self.prices(), {'a': 10, 'unchanged': 3})
        self.assertEqual(original.rows['196:asset']['a']['price'], 1)
        self.assertIn('b', original.rows['196:asset'])
        self.assertIs(state._shared_fact_cache[2].rows['196:asset']['unchanged'],
                      original.rows['196:asset']['unchanged'])

    async def test_pruned_outbox_rebuilds_facts_instead_of_retaining_missed_updates(self):
        await self.put('a', 1)
        await self.put('b', 2)
        await state.reload_data()
        with closing(sqlite3.connect(self.path)) as other_process:
            other_process.execute('DELETE FROM facts WHERE kind=? AND id=?', ('196:asset', 'a'))
            other_process.execute('UPDATE facts SET body=? WHERE kind=? AND id=?',
                                  (json.dumps(self.asset('b', 20)), '196:asset', 'b'))
            other_process.execute('DELETE FROM change_outbox')
            other_process.commit()
        await self.put('c', 3)  # A surviving id beyond the cached continuity boundary.
        with patch.object(ProjectionFacts, 'capture', wraps=ProjectionFacts.capture) as capture:
            await state.reload_data()
        self.assertIsNone(capture.await_args.args[1])
        self.assertEqual(self.prices(), {'b': 20, 'c': 3})

    async def test_failed_reload_cannot_publish_partial_cache(self):
        await self.put('a', 1)
        await state.reload_data()
        original = state._shared_fact_cache
        await self.put('a', 2)
        with patch.object(state.DATA, 'reload', AsyncMock(side_effect=RuntimeError('interrupted view'))):
            with self.assertRaisesRegex(RuntimeError, 'interrupted view'):
                await state.reload_data()
        self.assertIs(state._shared_fact_cache, original)
        self.assertEqual(self.prices(), {'a': 1})
        await state.reload_data()
        self.assertEqual(self.prices(), {'a': 2})

    async def test_invalidation_during_reload_is_not_lost_and_events_share_the_snapshot(self):
        await self.put('a', 1)
        capture = ProjectionFacts.capture

        async def write_after_capture(connection, previous, changes):
            facts = await capture(connection, previous, changes)
            with closing(sqlite3.connect(self.path)) as other_process:
                other_process.execute('UPDATE facts SET body=? WHERE kind=? AND id=?',
                                      (json.dumps(self.asset('a', 2)), '196:asset', 'a'))
                other_process.execute('INSERT INTO events VALUES (?,?,?,?)',
                                      ('event-a', '196:a', 100, '{"id":"event-a","t":100,"asset":"196:a"}'))
                other_process.commit()
            state.invalidate()
            return facts

        with patch.object(ProjectionFacts, 'capture', side_effect=write_after_capture):
            await state.reload_data()
        self.assertEqual(self.prices(), {'a': 1})
        self.assertEqual(state.DATA.signals, [])
        self.assertEqual(state._loaded_at, 0)
        await state.reload_if_stale()
        self.assertEqual(self.prices(), {'a': 2})
        self.assertEqual([row['id'] for row in state.DATA.signals], ['event-a'])
        self.assertGreater(state._loaded_at, 0)

    async def test_reopened_connection_or_new_database_discards_previous_cache(self):
        await self.put('old', 1)
        await state.reload_data()
        await self.s.close()
        self.s = storage._stores['196'] = await ResearchStore(self.path, '196', write_lock=self.write_lock).connect()
        with patch.object(ProjectionFacts, 'capture', wraps=ProjectionFacts.capture) as capture:
            await state.reload_data()
        self.assertIsNone(capture.await_args.args[1])
        await self.s.close()
        self.s = storage._stores['196'] = await ResearchStore(self.tmp.name + '/replacement.sqlite', '196',
                                                           write_lock=self.write_lock).connect()
        await self.put('replacement', 2)
        await state.reload_data()
        self.assertEqual(self.prices(), {'replacement': 2})
