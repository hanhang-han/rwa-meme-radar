import copy
import asyncio
import json
import sqlite3
import threading
import tempfile
import unittest
from pathlib import Path
from contextlib import contextmanager, closing
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import product_v2, watch

NOW = 1_800_000_000_000
A, B, C, P, W = ('0x'+v*40 for v in ('a', 'b', 'c', 'd', 'e'))


@contextmanager
def connection(path, **kwargs):
    with closing(sqlite3.connect(path, **kwargs)) as current:
        with current:
            yield current


def payload():
    stock = {'chainId': '56', 'tokenContractAddress': B, 'tokenSymbol': 'NVDAx', 'stockCode': 'NVDA',
             'stockIdentity': {'code': 'NVDA', 'nameZh': '英伟达', 'nameEn': 'NVIDIA', 'currency': 'USD'},
             'price': 12.34, 'priceCurrency': 'USDT', 'priceScope': 'exchange', 'provider': 'binance',
             'venue': 'binance', 'marketId': 'NVDAUSDT', 'quoteAt': NOW-2000, 'change24h': 3.5,
             'fieldTimes': {'price': NOW-2000, 'change24h': NOW-2000},
             'fieldSources': {'price': 'binance'}, 'fieldScopes': {'price': 'exchange'}}
    asset = {'chainId': '56', 'token': A, 'symbol': 'Alpha', 'name': 'Alpha Coin', 'kind': 'candidate',
             'price': .12345, 'priceCurrency': 'USD', 'priceScope': 'token', 'provider': 'OKX',
             'quoteAt': NOW-1000, 'volume24h': 999, 'volumeCurrency': 'USD', 'volumeScope': 'token',
             'fieldTimes': {'price': NOW-1000, 'change24h': NOW-1000}, 'change24h': -2,
             'riskFlags': ['contract_risk']}
    theme = {'ticker': 'NVDA', 'stockToken': B, 'stockTokenChain': '56',
             'theme': {'pairedCount': 1, 'volume': {'value': 24, 'known': 1, 'total': 1}}}
    relation = {'chainId': '56', 'pool': P, 'token': A, 'stock': B, 'stockSide': W, 'ticker': 'NVDA'}
    return {'now': NOW-3000, 'realtime': {'cursor': 123, 'revision': 77}, 'unified': {
        'assets': [asset], 'stockTokens': [stock], 'stockThemes': [theme],
        'stockThemesByChain': {'56': [theme]}, 'relations': [relation]}}


class WatchSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_never_reads_projection_or_database(self):
        with patch.object(product_v2, 'published', AsyncMock(side_effect=AssertionError('read'))), patch.object(watch, '_read_events', side_effect=AssertionError('db')):
            result = await watch.watch_summary(watch.WatchSummaryBody(keys=[]))
            self.assertEqual(result['items'], [])
            self.assertIsNone(result['snapshotAt'])
            events = await watch.watch_events(watch.WatchEventsBody(keys=[]))
            self.assertEqual(events['items'], [])
            self.assertFalse(events['hasMore'])

    def test_quotes_scope_currency_snapshot_and_unknown_identity(self):
        source = payload()
        original = copy.deepcopy(source)
        result = watch.summary_rows(source, watch.WatchSummaryBody(keys=['stock:NVDA', f'56:{A}', f'56:{C}', 'stock:UNKNOWN']), NOW)
        self.assertEqual(result['snapshotAt'], NOW-3000)
        self.assertEqual(result['realtime'], {'cursor': 123, 'revision': 77})
        stock, meme, missing, unknown = result['items']
        self.assertEqual(stock['asset']['priceCurrency'], 'USDT')
        self.assertEqual(stock['asset']['priceScope'], 'exchange')
        self.assertEqual(stock['asset']['fieldTimes']['price'], NOW-2000)
        self.assertEqual(stock['theme']['theme']['volume']['value'], 24)
        self.assertEqual(meme['asset']['volume24h'], 999)
        self.assertEqual(stock['poolVolumeScope'], 'direct-stock-pair-pools')
        self.assertIsNone(missing['asset'])
        self.assertIsNone(unknown['theme'])
        self.assertFalse(missing['available'])
        self.assertEqual(source, original)

    def test_filter_search_sort_apply_before_pagination(self):
        source = payload()
        for i in range(25):
            token = '0x'+f'{i+1:040x}'
            source['unified']['assets'].append({'chainId': '56', 'token': token, 'symbol': f'S{i}', 'name': 'Needle' if i == 24 else 'Other',
                'price': 1, 'change24h': i, 'fieldTimes': {'price': NOW, 'change24h': NOW}})
        keys = ['stock:NVDA', f'196:{A}', *[f"56:{r['token']}" for r in source['unified']['assets']]]
        result = watch.summary_rows(source, watch.WatchSummaryBody(keys=keys, q='Needle', limit=1, kind='asset'), NOW)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['totalInScope'], len(keys))
        self.assertEqual(result['items'][0]['asset']['name'], 'Needle')
        result = watch.summary_rows(source, watch.WatchSummaryBody(keys=keys, chain='56', sort='change24h', limit=1), NOW)
        self.assertEqual(result['items'][0]['asset']['change24h'], 24)
        self.assertEqual(result['totalInScope'], len(keys)-1)
        result = watch.summary_rows(source, watch.WatchSummaryBody(keys=keys, quoteFilter='risk'), NOW)
        self.assertEqual([r['key'] for r in result['items']], [f'56:{A}'])

    def test_missing_quote_filter_does_not_turn_missing_into_zero(self):
        source = payload()
        source['unified']['assets'][0]['fieldTimes']['price'] = NOW-900_001
        result = watch.summary_rows(source, watch.WatchSummaryBody(keys=[f'56:{A}', f'56:{C}'], quoteFilter='quote'), NOW)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['items'][0]['asset']['price'], .12345)
        self.assertIsNone(result['items'][1]['asset'])

    def test_hk_padding_matches_ticker_but_preserves_saved_key(self):
        source = payload()
        stock = source['unified']['stockTokens'][0]
        stock['stockCode'] = '700'
        stock['stockIdentity']['code'] = '00700'
        source['unified']['stockThemes'][0]['ticker'] = '700'
        result = watch.summary_rows(source, watch.WatchSummaryBody(keys=['stock:00700']), NOW)
        self.assertEqual(result['items'][0]['key'], 'stock:00700')
        self.assertEqual(result['items'][0]['ticker'], '700')
        self.assertEqual(result['items'][0]['asset']['tokenContractAddress'], B)

    def test_strict_limits_keys_filters_types_and_no_duplicate_card(self):
        invalid = [{'keys': ['stock:bad ticker']}, {'keys': ['all:'+A]}, {'keys': ['56:0xbad']},
                   {'keys': ['0:'+A]}, {'keys': ['01:'+A]}, {'keys': ['12345678901:'+A]},
                   {'keys': [f'56:{A}']*201}, {'keys': [], 'limit': 21}, {'keys': [], 'limit': '20'},
                   {'keys': [], 'sort': 'price'}, {'keys': [], 'offset': -1}, {'keys': [], 'q': 'x'*81},
                   {'keys': [], 'chain': 'ethereum'}, {'keys': [], 'junk': True}]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ValidationError):
                watch.WatchSummaryBody(**body)
        self.assertEqual(watch.WatchSummaryBody(keys=[f'56:{A}', f'56:{A}']).keys, [f'56:{A}'])

    def test_historical_unknown_chain_kept_without_rejecting_supported_keys(self):
        source = payload()
        source['unified']['assets'].append(dict(source['unified']['assets'][0], chainId='1'))
        result = watch.summary_rows(source, watch.WatchSummaryBody(keys=[f'1:{A}', f'56:{A}']), NOW)
        unsupported, known = result['items']
        self.assertEqual(unsupported['key'], f'1:{A}')
        self.assertEqual(unsupported['status'], 'unsupported-chain')
        self.assertIsNone(unsupported['asset'])
        self.assertFalse(unsupported['available'])
        self.assertEqual(known['asset']['price'], .12345)
        found = watch.summary_rows(source, watch.WatchSummaryBody(keys=[f'1:{A}', f'56:{A}'], q=A), NOW)
        self.assertEqual(found['total'], 2)
        missing = watch.summary_rows(source, watch.WatchSummaryBody(keys=[f'56:{C}'], q=C), NOW)
        self.assertEqual(missing['items'][0]['key'], f'56:{C}')
        scoped = watch.summary_rows(source, watch.WatchSummaryBody(keys=[f'1:{A}', f'56:{A}'], chain='56'), NOW)
        self.assertEqual(scoped['totalInScope'], 1)

    async def test_only_unknown_chain_events_do_not_read_projection_or_database(self):
        with patch.object(product_v2, 'published', AsyncMock(side_effect=AssertionError('read'))), patch.object(watch, '_read_events', side_effect=AssertionError('db')):
            result = await watch.watch_events(watch.WatchEventsBody(keys=[f'1:{A}']))
        self.assertEqual(result['items'], [])
        self.assertEqual(result['unsupportedKeys'], [f'1:{A}'])

    def test_router_registered_without_circular_import(self):
        app = FastAPI()
        app.include_router(product_v2.router, prefix='/api/v2')
        with TestClient(app) as client:
            self.assertEqual(client.post('/api/v2/watch/summary', json={'keys': []}).status_code, 200)
            self.assertEqual(client.post('/api/v2/watch/events', json={'keys': []}).status_code, 200)
            self.assertEqual(client.post('/api/v2/watch/summary', json={'keys': ['bad']}).status_code, 422)


class WatchEventsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name)/'research.sqlite')
        with connection(self.path) as c:
            c.execute('CREATE TABLE events(id TEXT PRIMARY KEY,asset TEXT NOT NULL,t INTEGER NOT NULL,body TEXT NOT NULL)')
            c.execute('CREATE INDEX events_time ON events(t)')
            for sql in watch.WATCH_EVENT_INDEX_SQL:
                c.execute(sql)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, chain, token, at, event_id=None, **body):
        event_id = event_id or f'{chain}:candidate:{token}:{at}'
        with connection(self.path) as c:
            c.execute('INSERT INTO events VALUES (?,?,?,?)', (event_id, chain+':'+token, at, json.dumps(body)))
        return event_id

    async def get(self, keys, **body):
        with patch.object(watch.db, 'DB_PATH', self.path), patch.object(product_v2, 'published', AsyncMock(return_value=payload())):
            return await watch.watch_events(watch.WatchEventsBody(keys=keys, **body))

    async def test_matches_saved_asset_before_limit_not_recent_global_slice(self):
        old_id = self.put('56', A, NOW-500, kind='discovered')
        for i in range(150):
            self.put('56', C, NOW+i, kind='discovered')
        result = await self.get([f'56:{A}'])
        self.assertEqual([r['id'] for r in result['items']], [old_id])
        self.assertEqual(result['items'][0]['matchedKeys'], [f'56:{A}'])

    async def test_unknown_chain_does_not_hide_supported_events(self):
        ident = self.put('56', A, NOW, kind='discovered')
        result = await self.get([f'1:{A}', f'56:{A}'])
        self.assertEqual([row['id'] for row in result['items']], [ident])
        self.assertEqual(result['unsupportedKeys'], [f'1:{A}'])

    async def test_theme_events_cross_chain_order_and_stable_tie_pagination(self):
        expected = []
        for chain in ('196', '56', '4663'):
            for i in range(2):
                expected.append(self.put(chain, A, NOW, event_id=f'{chain}:event-{i}', ticker='NVDA'))
        rows, cursor = [], None
        for _ in range(3):
            page = await self.get(['stock:NVDA'], limit=2, cursor=cursor)
            rows.extend(item['id'] for item in page['items'])
            cursor = page['nextCursor']
        self.assertEqual(rows, sorted(expected, reverse=True))
        self.assertIsNone(cursor)
        scoped = await self.get(['stock:NVDA'], chain='196')
        self.assertEqual({row['chainId'] for row in scoped['items']}, {'196'})

    async def test_exact_stock_contract_is_not_expanded_to_entire_ticker(self):
        wanted = self.put('56', A, NOW-3, event_id=f'56:56:{P}:{A}:{B}:verified:{NOW-3}', ticker='NVDA', pool=P)
        self.put('56', C, NOW-2, event_id=f'56:56:{P}:{C}:{W}:verified:{NOW-2}', ticker='NVDA', pool=P)
        self.put('196', A, NOW-1, event_id=f'196:196:{P}:{A}:{B}:verified:{NOW-1}', ticker='NVDA', pool=P)
        result = await self.get([f'56:{B}'])
        self.assertEqual([r['id'] for r in result['items']], [wanted])
        self.assertEqual(result['items'][0]['matchedKeys'], [f'56:{B}'])

    async def test_exact_wrapper_uses_pool_meme_and_native_identity_together(self):
        wanted = self.put('56', A, NOW-3, event_id=f'56:56:{P}:{A}:{B}:verified:{NOW-3}', ticker='NVDA', pool=P)
        for i in range(25):
            self.put('56', C, NOW+i, event_id=f'56:56:{P}:{C}:{B}:verified:{NOW+i}', ticker='NVDA', pool=P)
        result = await self.get([f'56:{W}'])
        self.assertEqual([r['id'] for r in result['items']], [wanted])
        self.assertEqual(result['items'][0]['matchedKeys'], [f'56:{W}'])

    async def test_one_event_matches_all_keys_once_and_discovery_match_ticker(self):
        ident = self.put('56', A, NOW, match={'ticker': '00700'}, kind='discovered')
        result = await self.get([f'56:{A}', 'stock:700'])
        self.assertEqual([r['id'] for r in result['items']], [ident])
        self.assertEqual(result['items'][0]['matchedKeys'], [f'56:{A}', 'stock:700'])

    async def test_explicit_stock_side_and_native_both_match_new_body(self):
        ident = self.put('56', A, NOW, relation={'stockSide': W, 'stock': B}, kind='verified')
        result = await self.get([f'56:{W}', f'56:{B}'])
        self.assertEqual([r['id'] for r in result['items']], [ident])
        self.assertEqual(result['items'][0]['matchedKeys'], [f'56:{W}', f'56:{B}'])

    async def test_cursor_cannot_be_reused_for_changed_follow_scope(self):
        for i in range(3):
            self.put('56', A, NOW+i, ticker='NVDA')
        result = await self.get([f'56:{A}'], limit=1)
        for value in ('garbage', 'x'*100, result['nextCursor']):
            with self.assertRaises(HTTPException) as caught:
                await self.get(['stock:NVDA'], cursor=value)
            self.assertEqual(caught.exception.status_code, 400)

    async def test_schema_bad_json_index_safe_then_query_errors_instead_of_partial(self):
        with connection(self.path) as c:
            c.execute('INSERT INTO events VALUES (?,?,?,?)', ('56:bad', '56:'+A, NOW, '{bad'))
            for sql in watch.WATCH_EVENT_INDEX_SQL:
                c.execute(sql)
        with self.assertRaises(HTTPException) as caught:
            await self.get([f'56:{A}'])
        self.assertEqual(caught.exception.detail, 'watch-event-invalid')

    async def test_invalid_nested_event_schema_is_an_explicit_error(self):
        self.put('56', A, NOW, relation='not-an-object')
        with self.assertRaises(HTTPException) as caught:
            await self.get([f'56:{A}'])
        self.assertEqual(caught.exception.detail, 'watch-event-invalid')

    async def test_cancelled_request_keeps_read_slot_until_sqlite_thread_finishes(self):
        started, finish = threading.Event(), threading.Event()
        slots = asyncio.Semaphore(1)
        def read(*_):
            started.set()
            finish.wait(2)
            return []
        with patch.object(watch, '_event_slots', slots), patch.object(watch, '_read_events', read):
            request = asyncio.create_task(watch.watch_events(watch.WatchEventsBody(keys=['stock:NVDA'])))
            while not started.is_set():
                await asyncio.sleep(.005)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertTrue(slots.locked())
            finish.set()
            for _ in range(100):
                if not slots.locked():
                    break
                await asyncio.sleep(.005)
            self.assertFalse(slots.locked())

    async def test_read_only_no_tables_or_business_rows_changed(self):
        self.put('56', A, NOW, ticker='NVDA')
        with connection(self.path) as c:
            before = c.execute('SELECT * FROM events').fetchall()
            tables = c.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
        await self.get([f'56:{A}'])
        with connection(self.path) as c:
            self.assertEqual(c.execute('SELECT * FROM events').fetchall(), before)
            self.assertEqual(c.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall(), tables)
            with self.assertRaises(sqlite3.OperationalError):
                with connection(Path(self.path).as_uri()+'?mode=ro', uri=True) as readonly:
                    readonly.execute('DELETE FROM events')

    async def test_query_budget_busy_is_explicit_not_truncated_success(self):
        self.put('56', A, NOW, ticker='NVDA')
        with patch.object(watch, 'READ_SECONDS', -1), self.assertRaises(HTTPException) as caught:
            await self.get([f'56:{A}'])
        self.assertEqual(caught.exception.status_code, 503)

    def test_each_main_predicate_has_an_index_plan(self):
        with connection(self.path) as c:
            checks = [(f'asset=?', ['56:'+A], 'watch_events_asset_time'),
                      (f'{watch.SCOPE_SQL}=? AND ({watch.TICKER_SQL})=?', ['56', 'NVDA'], 'watch_events_ticker_time'),
                      (f'{watch.SCOPE_SQL}=? AND ({watch.NATIVE_STOCK_SQL})=?', ['56', B], 'watch_events_native_stock_time'),
                      # SQLite versions may prefer the equally selective
                      # asset index to the pool index for the conjunction.
                      (f'{watch.SCOPE_SQL}=? AND ({watch.POOL_SQL})=? AND asset=?', ['56', P, '56:'+A], ('watch_events_pool_time', 'watch_events_asset_time'))]
            for predicate, args, name in checks:
                plan = str(c.execute('EXPLAIN QUERY PLAN SELECT id FROM events WHERE '+predicate+' ORDER BY t DESC,id DESC LIMIT 21', args).fetchall())
                self.assertRegex(plan, r'SEARCH events USING (?:COVERING )?INDEX')
                self.assertTrue(any(index in plan for index in name) if isinstance(name, tuple) else name in plan, plan)

    def test_large_event_bodies_are_loaded_only_after_metadata_paging(self):
        # A theme and asset can both match a record, and many pool expansions
        # add further branches. The actual SQLite query must still return
        # only 20+1 bodies, with no body in intermediate SELECT projections.
        sides = {('56', '0x'+f'{i+1:040x}', A, B): {W} for i in range(40)}
        keys = ['stock:NVDA', f'56:{A}', f'56:{W}']
        for i in range(100):
            self.put('56', A, NOW+i, kind='discovered', ticker='NVDA', padding='x'*32_000)
        query, params = watch._event_query(keys, 'all', 20, None, sides)
        matched_query = query.split('JOIN (', 1)[1].split(') AS matched', 1)[0]
        self.assertNotIn('SELECT id,asset,t,body', matched_query)
        self.assertIn('source.body', query)
        with connection(self.path) as c:
            rows = c.execute(query, params).fetchall()
            plan = str(c.execute('EXPLAIN QUERY PLAN '+query, params).fetchall())
        self.assertEqual(len(rows), 21)
        self.assertEqual([row[2] for row in rows], [NOW+i for i in range(99, 78, -1)])
        self.assertTrue(all(json.loads(row[3])['padding'] == 'x'*32_000 for row in rows))
        self.assertRegex(plan, r'SEARCH source USING INDEX sqlite_autoindex_events_1')
