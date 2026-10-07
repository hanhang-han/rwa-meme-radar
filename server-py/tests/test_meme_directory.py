"""Paging changes transport size, never identities, observations or scope."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from fastapi import HTTPException
from starlette.requests import Request

from app.api import product_v2 as api
from app.api import stream as stream_api
from app.api.stream import get_stream, scope_frame
from app.stream_hub import _frame
from app.meme_directory import (select_memes, meme_page, meme_members, meme_chart,
                                normalize_query, pool_directory, selection_valid_until)

NOW = 1_800_000_000_000


def asset(index, *, symbol=None, chain='196', **extra):
    return {'chainId': chain, 'token': '0x'+format(index, '040x'), 'kind': 'candidate',
            'symbol': symbol or f'T{index:04}', 'name': f'Meme {index}', 'price': 1,
            'priceCurrency': 'USD', 'volume24h': index, 'volumeCurrency': 'USD',
            'volumeScope': 'token-aggregate', 'totalLiquidityUsd': 2000,
            'totalLiquidityStatus': 'current', 'totalLiquidityAt': NOW,
            'fieldTimes': {'price': NOW, 'volume24h': NOW, 'change24h': NOW},
            'change24h': 0, **extra}


def payload(rows, relations=None):
    return {'now': NOW-10_000, 'realtime': {'cursor': 71, 'revision': 5, 'schema': 1},
            'unified': {'snapshotScope': 'market', 'assets': rows, 'relations': relations or [],
                        'stockTokens': [{'trace': 'large '*1000} for _ in range(3286)],
                        'stockThemes': [{'trace': 'unused'}], 'sources': [{'trace': 'unused'}]}}


def relation(index, row, **extra):
    return {'id': f'pool-{index}', 'chainId': row['chainId'], 'token': row['token'],
            'stock': '0x'+'f'*40, 'pool': '0x'+format(10000+index, '040x'),
            'ticker': 'NVDA', 'level': 'A', 'status': 'verified',
            'liquidityUsd': 1500, 'liquidityAt': NOW, 'checkedAt': NOW,
            'poolMarket': {'scope': 'pool:0x'+format(10000+index, '040x'),
                           'currency': 'USD', 'updatedAt': NOW, 'volume24h': index}, **extra}


def request(**query):
    return Request({'type': 'http', 'query_string': urlencode(query).encode(), 'headers': []})


class MemeDirectoryTests(unittest.TestCase):
    def test_page_keeps_full_counts_stats_and_only_representatives(self):
        rows = [asset(i) for i in range(1, 2201)]
        source = payload(rows, [relation(1, rows[0]), relation(2, rows[-1])])
        before = copy.deepcopy(source)
        selection = select_memes(source, {}, NOW)
        page = meme_page(selection, 20, 0)
        self.assertEqual(page['directory']['catalogTotal'], 2200)
        self.assertEqual(page['directory']['filteredTotal'], 2200)
        self.assertEqual(page['directory']['groupTotal'], 2200)
        self.assertEqual(page['summary']['comparableCount'], 2200)
        self.assertEqual(page['summary']['observedVolumeTotal'], sum(range(1, 2201)))
        self.assertEqual(page['summary']['volumeMax'], 2200)
        self.assertEqual(len(page['groups']), 20)
        self.assertEqual(page['groups'][0]['representative']['volume24h'], 2200)
        self.assertEqual(page['unified']['stockTokens'], [])
        self.assertEqual(len(page['unified']['relations']), 1)
        self.assertEqual(page['chart'], {'points': [], 'total': 2200, 'requiresFetch': True})
        self.assertEqual(len(meme_chart(selection)['chart']['points']), 2200)
        self.assertLess(len(json.dumps(page).encode()), 80_000)
        self.assertEqual(source, before)
        self.assertEqual(page['now'], NOW-10_000)
        self.assertEqual(page['snapshotAt'], NOW-10_000)
        self.assertEqual(page['evaluatedAt'], NOW)

    def test_same_name_cross_chain_members_and_focus_are_not_lost(self):
        rows = [asset(i, symbol='SAME', chain='196' if i%2 else '56') for i in range(1, 142)]
        duplicate = {**rows[0], 'volume24h': 999}
        rows.append(duplicate)
        selection = select_memes(payload(rows), {}, NOW)
        page = meme_page(selection)
        group = page['groups'][0]
        self.assertEqual(group['memberCount'], 141)
        self.assertEqual(group['representative']['token'], rows[0]['token'])
        self.assertEqual(group['representative']['memberIndex'], 0)
        focus = f"{rows[20]['chainId']}:{rows[20]['token']}"
        members = meme_members(selection, 'SAME', 50, 0, focus)
        self.assertEqual(len(members['rows']), 41)
        self.assertEqual(members['group']['offset'], 100)
        self.assertEqual(members['rows'][0]['memberIndex'], 100)
        self.assertTrue(any(f"{row['chainId']}:{row['token']}" == focus for row in members['rows']))
        self.assertTrue(all(row['groupIndex'] == 0 and row['groupSymbol'] == 'SAME' for row in members['rows']))

    def test_missing_zero_historical_and_future_keep_original_scopes(self):
        rows = [asset(1, volume24h=0, totalLiquidityUsd=0),
                asset(2, volume24h=None),
                asset(3, fieldTimes={'price': NOW, 'volume24h': NOW-900_001}),
                asset(4, totalLiquidityAt=NOW+1),
                asset(5, volumeCurrency='USDT'),
                asset(6, totalLiquidityUsd=None),
                asset(7, totalLiquidityUsd=90_000, totalLiquidityAt=NOW-1_800_001)]
        result = meme_page(select_memes(payload(rows), {}, NOW))
        self.assertEqual(result['directory']['filteredTotal'], 7)
        self.assertEqual(result['directory']['hiddenMissingCount'], 3)
        self.assertEqual(result['summary']['comparableCount'], 1)
        self.assertEqual(result['summary']['observedVolumeTotal'], 0)
        self.assertEqual(result['chart']['points'][0]['liquidity'], 0)
        self.assertEqual(result['summary']['volumeMax'], 7)
        self.assertEqual(result['summary']['liquidityMax'], 2000)
        filtered = meme_page(select_memes(payload(rows), {'showMissing': '0'}, NOW))
        self.assertEqual(filtered['directory']['filteredTotal'], 4)

    def test_qualified_keeps_no_volume_and_bypasses_ordinary_risk_relation_filters(self):
        rows = [asset(1, volume24h=None, riskFlags=['contract_risk'], relationLevel='B'),
                asset(2, fieldTimes={'price': NOW-900_001}),
                asset(3, totalLiquidityAt=NOW+1),
                asset(4, totalLiquidityUsd=999)]
        query = {'qualified': '1', 'rel': 'A', 'risk': 'hide', 'filter': 'history', 'showMissing': '0'}
        result = meme_page(select_memes(payload(rows), query, NOW))
        self.assertEqual(result['directory']['filteredTotal'], 1)
        self.assertIsNone(result['groups'][0]['representative']['volume24h'])
        self.assertEqual(result['summary']['comparableCount'], 0)

    def test_relation_search_new_rank_and_legacy_aliases(self):
        a, b, c = asset(1, firstSeen=NOW-10), asset(2, firstSeen=NOW+1), asset(3, assetCategory='derivative')
        source = payload([a, b, c], [relation(1, a, ticker='000700'), relation(2, b, level='B')])
        result = select_memes(source, {'q': '700', 'ticker': '00700', 'new': '24h'}, NOW)
        self.assertEqual(result['filteredTotal'], 1)
        self.assertEqual(normalize_query({'filter': 'related'})['fresh'], '1')
        self.assertEqual(normalize_query({'filter': 'name'})['rel'], 'B')
        self.assertEqual(normalize_query({'minLiquidity': '1234'})['minLiq'], '1234')
        self.assertEqual(select_memes(source, {'rank': 'volume24h'}, NOW)['filteredTotal'], 2)
        self.assertEqual(select_memes(source, {'chain': 'bsc'}, NOW)['filteredTotal'], 0)

    def test_expiry_cache_boundary_and_list_evidence_are_bounded(self):
        row = asset(1, fieldTimes={'price': NOW-899_999, 'volume24h': NOW, 'change1h': NOW-800},
                    primaryQuote={'trace': 'x'*80_000}, marketQuotes=[{'trace': 'x'*80_000}],
                    productMetrics={'changes': {'h1': {'value': -2, 'at': NOW-800, 'source': 'OKX', 'trace': 'x'*80_000}},
                                    'mainPool': {'trace': 'x'*80_000}},
                    riskFlags=['wash_suspect'], riskStatus='triggered', riskAssessment={
                        'safety': {'tax': {'status': 'triggered', 'severity': 'critical', 'checkedAt': NOW-200, 'provider': 'GoPlus', 'trace': 'x'*80_000}},
                        'checks': {'wash_suspect': {'evidence': {'volumeLiquidityRatio': 40, 'trace': 'x'*80_000}}}})
        source = payload([row])
        self.assertEqual(selection_valid_until(source, NOW), NOW+2)
        page = meme_page(select_memes(source, {}, NOW))
        dto = page['groups'][0]['representative']
        self.assertNotIn('primaryQuote', dto)
        self.assertEqual(dto['fieldTimes']['change1h'], NOW-800)
        self.assertEqual(dto['productMetrics']['changes']['h1']['at'], NOW-800)
        self.assertEqual(dto['riskAssessment']['safety']['tax']['status'], 'triggered')
        self.assertEqual(dto['riskAssessment']['checks']['wash_suspect']['evidence']['volumeLiquidityRatio'], 40)
        self.assertLess(len(json.dumps(page)), 6000)

    def test_pool_pages_pair_groups_with_full_summary_and_true_pool_volume(self):
        rows = [asset(i, volume24h=999999) for i in range(1, 31)]
        relations = [relation(i, row, poolCreatedAt=NOW-i*1000) for i, row in enumerate(rows, 1)]
        relations.append(relation(1, rows[0], liquidityAt=NOW+1, liquidityUsd=9999))
        relations[3]['poolMarket']['scope'] = 'token-aggregate'
        relations[4]['poolMarket']['currency'] = 'USDT'
        source = payload(rows, relations)
        before = copy.deepcopy(source)
        result = pool_directory(source, limit=15, now=NOW)
        self.assertEqual(result['directory']['totalPools'], 30)
        self.assertEqual(result['directory']['groupTotal'], 30)
        self.assertEqual(len(result['groups']), 15)
        self.assertEqual(result['summary']['liquidity']['known'], 29)
        self.assertEqual(result['summary']['liquidity']['max'], 1500)
        self.assertEqual(result['summary']['volume24h']['known'], 28)
        all_rows = [row for group in pool_directory(source, limit=50, now=NOW)['groups'] for row in group['items']]
        self.assertIsNone(next(row for row in all_rows if row['relation']['id'] == 'pool-4')['volume24h'])
        self.assertTrue(all(row['volume24h'] != 999999 for row in all_rows))
        self.assertEqual(pool_directory(source, qualified=True, now=NOW)['directory']['totalPools'], 29)
        self.assertLess(len(json.dumps(result)), 30_000)
        self.assertEqual(source, before)

    def test_pool_dedup_precedes_search_and_preserves_issuer_pair_identity(self):
        row = asset(1)
        old = relation(1, row, ticker='OLD', checkedAt=NOW-1)
        current = relation(1, row, ticker='NEW', checkedAt=NOW)
        other = relation(2, row, stock='0x'+'a'*40, liquidityUsd=0,
                         poolMarket={'scope': 'pool:0x'+format(10002, '040x'), 'updatedAt': NOW, 'volume24h': 0})
        source = payload([row], [old, current, other, relation(3, row, level='B')])
        self.assertEqual(pool_directory(source, q='OLD', now=NOW)['directory']['totalPools'], 0)
        result = pool_directory(source, now=NOW)
        self.assertEqual(result['directory']['groupTotal'], 2)
        self.assertEqual(result['summary']['liquidity']['known'], 2)
        zero = result['groups'][1]['items'][0]
        self.assertEqual(zero['liquidity'], 0)
        self.assertEqual(zero['volume24h'], 0)
        self.assertIsNone(zero['createdAt'])

    def test_pool_liquidity_source_is_independent_of_volume_source(self):
        row = asset(1)
        pair = relation(1, row, liquidityProvider='GeckoTerminal')
        pair['poolMarket']['provider'] = 'DexScreener'
        source = payload([row], [pair])
        before = copy.deepcopy(source)
        dto = pool_directory(source, now=NOW)['groups'][0]['items'][0]['relation']
        self.assertEqual(dto['liquiditySource'], 'GeckoTerminal')
        self.assertEqual(dto['poolMarket']['provider'], 'DexScreener')
        self.assertEqual(source, before)
        pair['liquiditySource'] = 'Chain RPC'
        dto = pool_directory(source, now=NOW)['groups'][0]['items'][0]['relation']
        self.assertEqual(dto['liquiditySource'], 'Chain RPC')
        pair.pop('liquiditySource');pair.pop('liquidityProvider')
        dto = pool_directory(source, now=NOW)['groups'][0]['items'][0]['relation']
        self.assertNotIn('liquiditySource', dto)


class MemeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        api._meme_queries.clear()

    async def test_route_uses_market_snapshot_and_caches_selection_for_two_seconds(self):
        source = payload([asset(1), asset(2)])
        with patch.object(api, 'published_market', AsyncMock(return_value=source)), patch.object(api.time, 'time', return_value=NOW/1000), patch.object(api, 'select_memes', wraps=select_memes) as selector:
            first = await api.memes(request(chain='all'))
            second = await api.memes(request(chain='all'), offset=20)
            self.assertEqual(selector.call_count, 1)
            self.assertEqual(first['directory']['filteredTotal'], second['directory']['filteredTotal'])
            with self.assertRaises(HTTPException) as error:
                await api.memes_chart(request(chain='all'), revision=4)
            self.assertEqual(error.exception.status_code, 409)
            with self.assertRaises(HTTPException):
                await api.memes(request(sort='unknown'))
            with self.assertRaises(HTTPException):
                await api.memes(request(), limit=51)

    async def test_cached_fresh_selection_expires_at_actual_field_boundary(self):
        source = payload([asset(1, fieldTimes={'price': NOW-899_999, 'volume24h': NOW})])
        with patch.object(api, 'published_market', AsyncMock(return_value=source)), patch.object(api, 'select_memes', wraps=select_memes) as selector:
            with patch.object(api.time, 'time', return_value=NOW/1000):
                first = await api.memes(request(fresh='1'))
            with patch.object(api.time, 'time', return_value=(NOW+3)/1000):
                expired = await api.memes(request(fresh='1'))
        self.assertEqual(first['directory']['filteredTotal'], 1)
        self.assertEqual(expired['directory']['filteredTotal'], 0)
        self.assertEqual(selector.call_count, 2)
        self.assertEqual(first['now'], expired['now'])

    async def test_actual_memes_stream_filters_queued_data_and_cleans_subscriber(self):
        subscribers = set()
        def hello(queue):
            queue.put_nowait((None, _frame('hello', {'cursor': 0})))
        with patch.object(stream_api, 'clients', return_value=subscribers), patch.object(stream_api, 'hello', side_effect=hello), patch.object(stream_api, 'cursor', return_value=0):
            response = await get_stream(protocol=2, scope='memes', trades='none', candles='none', snapshot=False)
            iterator = response.body_iterator
            self.assertIn(b'event: hello', await anext(iterator))
            queue = next(iter(subscribers))
            queue.put_nowait((1, _frame('trade', {'trace': 'x'*80_000}, 1)))
            queue.put_nowait((2, _frame('candle', {'trace': 'x'*80_000}, 2)))
            queue.put_nowait((3, _frame('projection.delta', {'revision': 6, 'now': NOW, 'upserts': {'assets': [asset(1)]}}, 3)))
            frame = await anext(iterator)
            self.assertIn(b'event: directory.invalidate', frame)
            self.assertLess(len(frame), 250)
            queue.put_nowait(None)
            with self.assertRaises(StopAsyncIteration):
                await anext(iterator)
            self.assertEqual(subscribers, set())

    async def test_memes_stream_has_small_invalidations_and_no_quotes_or_market_rows(self):
        packet = {'schema': 1, 'revision': 5, 'now': NOW, 'upserts': {'assets': [asset(1)]},
                  'meta': {'stockTokens': ['x'*80_000]}, 'views': {'market': {'trace': 'x'*80_000}}}
        frame = scope_frame(_frame('projection.delta', packet, 72), 2, 'memes')
        self.assertLess(len(frame), 250)
        self.assertIn(b'event: directory.invalidate', frame)
        value = json.loads(frame.split(b'data: ')[1])
        self.assertEqual(value, {'scope': 'memes', 'revision': 5, 'cursor': 72, 'now': NOW})
        for event in ('price', 'stock-quote', 'trade', 'candle', 'relationship', 'comparison', 'discovery', 'checkpoint'):
            self.assertIsNone(scope_frame(_frame(event, {'trace': 'large'}, 73), 2, 'memes'))
        for event in ('hello', 'heartbeat', 'reset'):
            allowed = _frame(event, {'cursor': 72})
            self.assertEqual(scope_frame(allowed, 2, 'memes'), allowed)
        # Initial creation accepts trades=none and never reads feed/full data.
        response = await get_stream(protocol=2, scope='memes', trades='none', snapshot=False)
        self.assertEqual(response.media_type, 'text/event-stream')
        await response.body_iterator.aclose()
