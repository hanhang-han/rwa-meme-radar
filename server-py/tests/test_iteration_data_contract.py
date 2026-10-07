"""Canonical quotes, bounded first paint, lazy detail and honest onchain feed."""
import copy
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Request
from app.api import token as token_api, dashboard
from app.dashboard_projection import asset_summary, relation_summary, compact_dashboard, market_dashboard, overview_dashboard, discovery_rankings
from app.market_quotes import bind_previous_quotes, enrich_asset
from app.realtime_projection import ProjectionUnavailable, read_token_projection, projection_delta
from app.risk_assessment import assess_risk, SCAN_MAX_AGE_MS
from app.state import newly_discovered_assets
from app.db import ResearchStore
from app.market_history import market_trades, token_market_records

NOW = 1_800_000_000_000
TOKEN = '0x' + 'a' * 40
OTHER = '0x' + 'b' * 40


def quote(provider, price, at, scope='token-aggregate', volume=100, change=2):
    return {'provider': provider, 'price': price, 'volume24h': volume, 'change24h': change,
            'fieldTimes': {f: at for f in ('price', 'volume24h', 'change24h')},
            'fieldScopes': {f: scope for f in ('price', 'volume24h', 'change24h')}}


class CanonicalQuoteTests(unittest.TestCase):
    def enrich(self, observations, prior=None):
        with patch('app.market_quotes._read_snapshot', return_value={'assets': {f'56:{TOKEN}': observations}}), \
             patch('app.market_quotes.time.time', return_value=NOW / 1000), \
             bind_previous_quotes({'unified': {'assets': [prior] if prior else []}}):
            return enrich_asset({'chainId': '56', 'token': TOKEN})

    def test_secondary_newer_tick_does_not_replace_primary_or_mix_tuple(self):
        first = self.enrich({'A': quote('A', 2, NOW-2000)})
        second = self.enrich({'A': quote('A', 3, NOW-1000, volume=9),
                              'B': quote('B', 50, NOW, volume=500)}, first)
        self.assertEqual((second['price'], second['volume24h']), (3, 9))
        self.assertEqual(second['primaryQuote']['provider'], 'A')
        self.assertEqual(second['primaryQuote']['selectionReason'], 'retained-primary')
        self.assertEqual(second['quoteAlternatives'][0]['price'], 50)
        self.assertEqual(second['fieldTimes']['price'], NOW-1000)

    def test_expired_primary_switch_is_explicit_and_missing_is_not_zero(self):
        prior = self.enrich({'A': quote('A', 2, NOW-2000)})
        result = self.enrich({'A': quote('A', 2, NOW-2_000_000),
                             'B': quote('B', 7, NOW, volume=None, change=0)}, prior)
        self.assertEqual(result['price'], 7)
        self.assertIsNone(result['volume24h'])
        self.assertEqual(result['change24h'], 0)
        self.assertEqual(result['primaryQuote']['switchedFrom']['provider'], 'A')
        self.assertEqual(result['primaryQuote']['selectionReason'], 'primary-expired')
        self.assertEqual(result['quoteAlternatives'][0]['status'], 'stale')

    def test_token_aggregate_wins_initial_selection_over_newer_single_pool(self):
        result = self.enrich({'A': quote('A', 2, NOW-1000),
                             'B': quote('B', 20, NOW, scope='pool:0x123')})
        self.assertEqual(result['primaryQuote']['provider'], 'A')
        self.assertEqual(result['fieldScopes']['volume24h'], 'token-aggregate')

    def test_stale_observation_retains_age_and_unknown_time_is_not_current(self):
        result = self.enrich({'A': quote('A', 2, NOW-2_000_000)})
        self.assertEqual(result['quoteStatus'], 'stale')
        value = quote('A', 4, NOW)
        value['fieldTimes'] = {}
        result = self.enrich({'A': value})
        self.assertEqual(result['quoteStatus'], 'unknown')
        self.assertIsNone(result['quoteAt'])

    def test_same_provider_pool_change_is_not_relabelled_as_token_change(self):
        candidate = quote('CoinGecko', 1, NOW)
        candidate['fieldScopes']['change24h'] = 'pool:one'
        result = self.enrich({'CoinGecko': candidate})
        self.assertIsNone(result['change24h'])
        self.assertIsNone(result['primaryQuote']['change24h'])
        self.assertEqual(result['volume24h'], 100)
        self.assertEqual(result['volumeCurrency'], 'USD')


class ProjectionChangeTests(unittest.TestCase):
    def test_evaluation_clock_does_not_resend_rows_but_expiry_and_evidence_do(self):
        raw = {'chainId': '56', 'token': TOKEN, 'price': 2,
               'pairLiquidityCoverage': {'known': 1, 'total': 1, 'asOf': NOW},
               'tokenScan': {'provider': 'GoPlus', 'status': 'complete', 'checkedAt': NOW,
                             'buyTaxPct': 0, 'sellTaxPct': 0, 'honeypot': False,
                             'mintable': False, 'pausable': False, 'blacklist': False}}
        def payload(at, price=2, scan_at=NOW):
            row = copy.deepcopy(raw)
            row['price'] = price
            row['tokenScan']['checkedAt'] = scan_at
            row['pairLiquidityCoverage']['asOf'] = at
            row['riskAssessment'] = assess_risk(row, now=at)
            relation = {'id': 'pool', 'chainId': '56', 'token': TOKEN,
                        'checkedAt': NOW, 'evaluatedAt': at, 'level': 'A'}
            return {'unified': {'assets': [asset_summary(row)], 'relations': [relation_summary(relation)]}}
        initial = payload(NOW)
        idle = payload(NOW + 5000)
        self.assertTrue(all(not rows for rows in projection_delta(initial, idle)['upserts'].values()))
        self.assertEqual(idle['unified']['assets'][0]['riskAssessment']['safety']['tax']['checkedAt'], NOW)
        self.assertEqual(idle['unified']['relations'][0]['checkedAt'], NOW)
        for changed in (payload(NOW + 6000, price=3), payload(NOW + 6000, scan_at=NOW + 5000),
                        payload(NOW + SCAN_MAX_AGE_MS + 1)):
            self.assertEqual(len(projection_delta(idle, changed)['upserts']['assets']), 1)
        expired = payload(NOW + SCAN_MAX_AGE_MS + 1)['unified']['assets'][0]
        self.assertEqual(expired['riskAssessment']['safety']['tax']['status'], 'unknown')


class OverviewBudgetTests(unittest.TestCase):
    def test_theme_preview_represents_each_theme_before_extra_bubbles(self):
        bubbles = [{'key': f'{ticker}:{i}', 'primaryTicker': ticker, 'token': f'{ticker}{i}',
                    'chainId': '56', 'price': {'value': 1}, 'volume24h': {'value': 100-i}}
                   for ticker in ('NVDA', 'TSLA', '700') for i in range(15)]
        full = compact_dashboard({'now': NOW, 'unified': {'assets': [], 'stockTokens': [], 'relations': [],
            'themeMap': {'themes': [{'ticker': t} for t in ('NVDA','TSLA','700')], 'bubbles': bubbles}}})
        preview = overview_dashboard(full)['unified']['themeMap']['bubbles']
        self.assertEqual([b['primaryTicker'] for b in preview[:3]], ['NVDA','TSLA','700'])
        self.assertEqual(len(preview), 6)

    def test_stock_card_selects_quote_time_not_unrelated_reference_time(self):
        stocks = [{'stockCode':'NVDA','price':1,'quoteAt':NOW-10000,'referenceAt':NOW},
                  {'stockCode':'NVDA','price':2,'quoteAt':NOW,'referenceAt':NOW-10000}]
        assets = [{'chainId':'56','token':TOKEN,'volume24h':2,'volumeCurrency':'USD',
                   'fieldTimes':{'volume24h':NOW},'fieldScopes':{'volume24h':'token'}}]
        relations = [{'chainId':'56','token':TOKEN,'ticker':'NVDA','level':'A','status':'verified'}]
        hot, _ = discovery_rankings(assets, relations, stocks, NOW)
        self.assertEqual(hot[0]['stock']['price'], 2)

    def test_pool_volume_survives_public_projection_with_its_own_scope(self):
        pool = '0x' + 'c' * 40
        relation = {'chainId': '56', 'token': TOKEN, 'pool': pool,
                    'ticker': 'NVDA', 'level': 'A', 'status': 'verified',
                    'poolMarket': {'volume24h': 17, 'provider': 'CoinGecko',
                                   'updatedAt': NOW, 'scope': 'pool:' + pool,
                                   'currency': 'USD', 'debug': 'omit'}}
        full = compact_dashboard({'now': NOW, 'unified': {
            'assets': [], 'stockTokens': [], 'relations': [relation]}})
        for payload in (full, market_dashboard(full)):
            value = payload['unified']['relations'][0]['poolMarket']
            self.assertEqual(value['volume24h'], 17)
            self.assertEqual(value['scope'], 'pool:' + pool)
            self.assertEqual(value['updatedAt'], NOW)
            self.assertNotIn('debug', value)

    def test_large_fixture_stays_below_50kb_with_real_global_counts(self):
        heavy = 'x'*10000
        rows = [{'chainId': '56', 'token': f'0x{i:040x}', 'symbol': 'MEME', 'price': 2,
                 'volume24h': i, 'fieldTimes': {'price': NOW}, 'fieldScopes': {'price': 'token'},
                 'dataQuality': {'tier': 'current', 'eligible': {'relationRanking': True}},
                 'marketQuotes': {'huge': heavy}, 'riskAssessment': {'checks': {'huge': heavy}}}
                for i in range(1500)]
        themes = [{'ticker': f'T{i}', 'breadth': {'total': 1500, 'unknown': 1500},
                   'assetKeys': [r['token'] for r in rows], 'debug': heavy} for i in range(30)]
        bubbles = [{'key': r['token'], 'token': r['token'], 'chainId': '56', 'primaryTicker': 'T0',
                    'price': {'value': 2, 'observedAt': NOW, 'status': 'current', 'debug': heavy},
                    'relation': {'pool': '0xpool', 'checkedAt': NOW, 'debug': heavy}} for r in rows[:50]]
        full = compact_dashboard({'now': NOW, 'realtime': {'revision': 9, 'cursor': 88}, 'unified': {
            'assets': rows, 'stockTokens': [], 'relations': [], 'signals': [], 'sources': [{'provider': 'A', 'debug': heavy}],
            'themeMap': {'themes': themes, 'bubbles': bubbles, 'totalAssets': 1500},
            'metrics': {'activeMemeCount': 1300, 'newAssets24h': 700},
            'newAssets24h': {'value': 700, 'byChain': {'56': 700}, 'window': '24h'},
            'quality': {'summary': {'total': 1500}}, 'debug': heavy}})
        result = overview_dashboard(full)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()), 50000)
        self.assertEqual(result['unified']['totals']['assets'], 1500)
        self.assertEqual(result['unified']['themeMap']['totalAssets'], 1500)
        self.assertEqual(result['unified']['themeMap']['totalThemes'], 30)
        self.assertEqual(result['unified']['newAssets24h']['value'], 700)
        self.assertEqual(result['unified']['metrics']['activeMemeCount'], 1300)
        self.assertNotIn('debug', result['unified'])
        self.assertEqual(result['realtime']['revision'], 9)

    def test_independent_safety_checks_survive_all_views_when_no_flags(self):
        safety = {'tax': {'status': 'clear', 'provider': 'GoPlus', 'checkedAt': NOW, 'evidence': {'buyTaxPct': 0}},
                  'liquidityLock': {'status': 'unknown', 'reason': 'lp-evidence-unavailable'}}
        asset = {'chainId': '56', 'token': TOKEN, 'riskFlags': [], 'riskAssessment': {'safety': safety},
                 'dataQuality': {'eligible': {'relationRanking': True}}}
        full = compact_dashboard({'now': NOW, 'unified': {'assets': [asset], 'stockTokens': [], 'relations': []}})
        for payload in (full, market_dashboard(full), overview_dashboard(full)):
            self.assertEqual(payload['unified']['assets'][0]['riskAssessment']['safety'], safety)

    def test_discovery_uses_first_seen_deduplicated_by_chain_and_token(self):
        assets = [{'chainId': '56', 'token': TOKEN, 'firstSeen': NOW-1000},
                  {'chainId': '56', 'token': TOKEN.upper(), 'firstSeen': NOW-2000},
                  {'chainId': '196', 'token': TOKEN, 'firstSeen': NOW-1000},
                  {'chainId': '56', 'token': OTHER, 'firstSeen': NOW-90_000_000},
                  {'chainId': '56', 'token': 'future', 'firstSeen': NOW+1},
                  {'chainId': '56', 'token': 'unknown'}]
        result = newly_discovered_assets(assets, NOW)
        self.assertEqual(result['value'], 2)
        self.assertEqual(result['byChain'], {'56': 1, '196': 1, '4663': 0, '5042': 0})
        self.assertEqual(result['from'], NOW-86_400_000)

    def test_rankings_use_full_universe_and_do_not_mix_stale_currency_or_chains(self):
        rows = []
        relations = []
        for index in range(20):
            token = f'0x{index:040x}'
            rows.append({'chainId': '56', 'token': token, 'symbol': 'M', 'volume24h': 10,
                         'volumeCurrency': 'USD', 'fieldTimes': {'volume24h': NOW},
                         'fieldScopes': {'volume24h': 'token-aggregate'}})
            relations.extend([{'chainId': '56', 'token': token, 'ticker': 'NVDA', 'level': 'A', 'status': 'verified'}]*2)
        rows.extend([dict(rows[0], chainId='196', volume24h=900),
                     dict(rows[1], token=OTHER, volume24h=1000, volumeCurrency='USDT'),
                     dict(rows[2], token=TOKEN, volume24h=10000, fieldTimes={'volume24h': NOW-900001})])
        hot, leaders = discovery_rankings(rows, relations, [], NOW, '56')
        self.assertEqual(hot[0]['assetCount'], 20)
        self.assertEqual(hot[0]['volume24h'], 200)
        self.assertEqual(hot[0]['volumeKnown'], 20)
        self.assertEqual(leaders['total'], 20)
        self.assertEqual(len(leaders['items']), 10)
        self.assertTrue(all(row['volume24h'] == 10 for row in leaders['items']))


class LazyDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_uses_canonical_values_without_history_trade_or_fact_reads(self):
        forbidden = AsyncMock(side_effect=AssertionError('summary performed a heavy query'))
        store = SimpleNamespace(get=forbidden, samples=forbidden, recent_trades=forbidden,
                                events=forbidden, activity=forbidden, trade_buckets=forbidden, fetchall=forbidden)
        asset = {'chainId': '56', 'token': TOKEN, 'symbol': 'MEME', 'price': 3, 'volume24h': None,
                 'primaryQuote': {'provider': 'A', 'scope': 'token', 'price': 3, 'volume24h': None},
                 'fieldTimes': {'price': NOW}, 'fieldSources': {'price': 'A'}}
        snapshot = {'asset': asset, 'stock': None, 'relations': [], 'snapshotAt': NOW,
                    'realtime': {'revision': 9, 'cursor': 100}}
        with patch.object(token_api, 'store', AsyncMock(return_value=store)), \
             patch.object(token_api, 'read_token_projection', AsyncMock(return_value=snapshot)), \
             patch('app.demand_leases.publish_lease'), \
             patch.object(token_api, 'attach_market_detail', forbidden), \
             patch.object(token_api, '_asset_view', side_effect=AssertionError('must not enrich at HTTP time')):
            result = await token_api.get_token('56', TOKEN, section='summary')
        self.assertEqual(result['asset']['price'], 3)
        self.assertEqual(result['asset']['primaryQuote']['provider'], 'A')
        self.assertEqual(result['revision'], 9)
        self.assertLess(len(json.dumps(result).encode()), 25000)
        forbidden.assert_not_awaited()

    async def test_missing_projection_returns_503_without_rebuild(self):
        with patch.object(dashboard, 'read_projection_json', AsyncMock(side_effect=ProjectionUnavailable())), \
             patch('app.realtime_projection.projection_tick', side_effect=AssertionError('HTTP rebuilt data')):
            with self.assertRaises(HTTPException) as error:
                await dashboard.get_dashboard(Request({'type': 'http', 'headers': []}), 'overview')
        self.assertEqual(error.exception.status_code, 503)

    async def test_token_read_matches_the_exact_published_revision(self):
        payload = {'now': NOW, 'realtime': {'revision': 5}, 'unified': {
            'assets': [{'chainId': '56', 'token': TOKEN, 'price': 3}], 'stockTokens': [], 'relations': []}}
        with patch('app.realtime_projection.read_projection_json', AsyncMock(return_value=json.dumps(payload))):
            result = await read_token_projection('56', TOKEN)
        self.assertEqual(result['asset'], payload['unified']['assets'][0])
        self.assertEqual(result['realtime']['revision'], 5)

    async def test_holders_section_exposes_newest_raw_top10_without_claiming_adjusted_distribution(self):
        store = SimpleNamespace(get=AsyncMock(return_value={
            'risk': {'top10': 12, 'checkedAt': NOW-1000, 'provider': 'CoinGecko'},
            'securityObservation': {'holders': {'top10RawPercent': 54}, 'checkedAt': NOW, 'provider': 'GoPlus'}}))
        snapshot = {'asset': {'token': TOKEN, 'chainId': '56', 'holders': 10}, 'relations': [], 'snapshotAt': NOW}
        result = await token_api._token_section('holders', store, snapshot, '56', TOKEN, 50, 0)
        distribution = result['holdersSummary']['distribution']
        self.assertEqual(distribution['top10Percent'], 54)
        self.assertEqual(distribution['provider'], 'GoPlus')
        self.assertFalse(distribution['exclusionsApplied'])
        self.assertEqual(result['asset']['holders'], 10)

    async def test_token_scoped_market_query_and_feed_filter_before_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            scoped = await ResearchStore(directory+'/test.sqlite', '56').connect()
            try:
                for token, venue, storage, at in ((TOKEN, 'dex', 'dex:wanted', 1),
                                                 (OTHER, 'dex', 'dex:other', 10),
                                                 (TOKEN, 'binance', 'binance:other', 20)):
                    await scoped.put('market-registry', storage, {'token': token, 'venue': venue, 'storage': storage})
                    await scoped.put('market-quote', storage, {'token': token, 'venue': venue, 'price': at})
                    await scoped.put_trades(storage, [{'id': storage, 't': at, 'venue': venue, 'token': token}])
                with patch.object(scoped, 'all', side_effect=AssertionError('unscoped facts read')):
                    rows = await market_trades(scoped, limit=1, dex_only=True, tokens=[TOKEN])
                    quotes = await token_market_records(scoped, 'market-quote', TOKEN)
                self.assertEqual([r['id'] for r in rows], ['dex:wanted'])
                self.assertEqual(len(quotes), 2)
            finally:
                await scoped.close()


if __name__ == '__main__':
    unittest.main()
