import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ['NODE_ENV'] = 'test'

from app.comparisons import (stock_premium, pool_spread, timing, relative_point,
                             relative_return, MAX_AGE, VERSION)
from app.pool_quotes import pool_ratio

NOW = 1_800_000_000_000


def stock(**patches):
    return {"chainId": "196", "tokenContractAddress": "0xstock", "price": 102,
            "quoteAt": NOW, "priceScope": "dex", "priceCurrency": "USD", "provider": "OKX",
            "stockPrice": 100, "referenceAt": NOW, "referenceCurrency": "USD",
            "referenceProvider": "exchange", "referenceSymbol": "TEST.US", "referenceIdentityVerified": True,
            "marketSession": "regular", "referenceRealtime": True,
            "tokenToAssetRatio": 1, "ratioSource": "issuer-api:TEST", "ratioVerified": True,
            "ratioVersion": "TEST:1", "ratioAt": NOW - 60000, "ratioValidUntil": NOW + 86400000,
            "referenceAdjustmentVersion": "TEST:1", "priceProvenance": {"timeKind": "market"}, **patches}


class ComparisonTest(unittest.TestCase):
    def test_valid_premium_and_ratio_units(self):
        self.assertAlmostEqual(stock_premium(stock(), NOW)['value'], 2)
        self.assertAlmostEqual(stock_premium(stock(price=204, tokenToAssetRatio=2), NOW)['value'], 2)

    def test_bad_numbers_and_missing_ratio_fail_closed(self):
        for price in (None, 0, -1, float('inf'), float('nan'), True, 'bad'):
            self.assertIsNone(stock_premium(stock(price=price), NOW)['value'])
        self.assertEqual(stock_premium(stock(ratioSource=None), NOW)['reason'], 'unverified-ratio')
        self.assertIsNone(stock_premium(stock(tokenToAssetRatio=1e-300, stockPrice=1e-300), NOW)['value'])
        self.assertIsNone(stock_premium(stock(tokenToAssetRatio=1e300, stockPrice=1e300), NOW)['value'])

    def test_stale_future_and_skewed_quotes(self):
        self.assertEqual(stock_premium(stock(quoteAt=NOW-300001), NOW)['reason'], 'stale')
        self.assertEqual(stock_premium(stock(quoteAt=NOW+1001), NOW)['reason'], 'future-quote')
        self.assertEqual(stock_premium(stock(referenceAt=NOW-60001), NOW)['reason'], 'unaligned')
        self.assertEqual(stock_premium(stock(referenceAt=None), NOW)['reason'], 'missing-time')

    def test_derived_reference_and_closed_market(self):
        self.assertEqual(stock_premium(stock(priceScope='issuer-derived'), NOW)['reason'], 'derived-price')
        self.assertEqual(stock_premium(stock(marketSession='closed'), NOW)['reason'], 'market-closed')
        self.assertEqual(stock_premium(stock(isTradingHalt=True), NOW)['reason'], 'market-closed')
        self.assertEqual(stock_premium(stock(marketSession='unknown'), NOW)['status'], 'snapshot')
        self.assertEqual(stock_premium(stock(referenceRealtime=False), NOW)['status'], 'snapshot')

    def test_non_usd_reference_needs_observed_fx(self):
        hk = stock(stockPrice=780, referenceCurrency='HKD')
        self.assertEqual(stock_premium(hk, NOW)['reason'], 'missing-fx')
        hk['referenceFx'] = {'fromCurrency':'HKD', 'toCurrency':'USD', 'rate':1/7.8, 'provider':'FX', 'at':NOW}
        self.assertAlmostEqual(stock_premium(hk, NOW)['value'], 2)
        hk['referenceFx']['at'] = NOW-300001
        self.assertEqual(stock_premium(hk, NOW)['reason'], 'stale')
        self.assertEqual(stock_premium(stock(priceCurrency='USDT'), NOW)['reason'], 'missing-token-currency')

    def test_pool_ratio_orientation_and_decimals(self):
        self.assertEqual(pool_ratio('stock', 'meme', 18, 6, reserves=(10**18, 10000*10**6)), 10000)
        self.assertEqual(pool_ratio('meme', 'meme', 6, 18, reserves=(10000*10**6, 10**18)), 10000)
        self.assertEqual(pool_ratio('stock', 'meme', 18, 18, sqrt_price_x96=2**96), 1)
        self.assertIsNone(pool_ratio('stock', 'meme', None, 18, reserves=(1, 1)))
        self.assertIsNone(pool_ratio('stock', 'meme', 18, 18, reserves=(0, 1)))

    def test_liquidity_lane_persists_ratio_at_the_queried_block(self):
        from app.db import ResearchStore
        from app.collectors.main_round import refresh_liquidity
        from types import SimpleNamespace
        async def run():
            with tempfile.TemporaryDirectory() as d:
                s = await ResearchStore(d+'/db.sqlite', '196').connect()
                relation = {'id':'r','pool':'pool','token':'meme','stock':'stock','stockSide':'wrapped',
                            'token0':'meme','token1':'wrapped','status':'verified'}
                await s.put('relation', 'r', relation)
                raw = (10000*10**6).to_bytes(32,'big') + (10**18).to_bytes(32,'big')
                mock_w3 = SimpleNamespace(eth=SimpleNamespace(get_block=lambda _: {'number':123, 'timestamp':NOW//1000}))
                empty = await ResearchStore(d+'/db.sqlite', '56').connect()
                try:
                    with patch('app.collectors.main_round.token_identity', return_value={'eligibleForPair':True}), patch('app.collectors.main_round.store', AsyncMock(side_effect=lambda chain: s if chain == '196' else empty)), patch('app.collectors.main_round.now_ms', return_value=NOW), patch('app.collectors.main_round.chain_web3', return_value=mock_w3), patch('app.collectors.main_round._check_rpc_identity', AsyncMock()), patch('app.collectors.main_round.cached_decimals', AsyncMock(side_effect=[6,18])), patch('app.collectors.main_round.rpc_call', AsyncMock(return_value=raw)) as rpc:
                        await refresh_liquidity()
                        rpc.assert_called_once_with('pool', '0x0902f1ac', 123)
                    quote = await s.get('pool-quote', 'pool')
                    self.assertEqual(quote['memePerStock'], 10000)
                    self.assertEqual(quote['stockSide'], 'wrapped')
                    self.assertEqual(quote['at'], NOW)
                finally:
                    await s.close()
                    await empty.close()
        asyncio.run(run())

    def test_pool_spread_excludes_circular_unknown_and_wrong_wrapper(self):
        rel = {'chainId':'196', 'pool':'0xpool', 'token':'0xmeme', 'stock':'0xstock', 'stockSide':'0xwrapped', 'status':'verified'}
        p = {**rel, 'memePerStock':10000, 'at':NOW, 'timeKind':'market'}
        q = {'chainId':'196', 'price':.0105, 'currency':'USD', 'provider':'independent', 'at':NOW,
             'token':'0xmeme', 'dependencies':['0xother'], 'dependenciesComplete':True, 'timeKind':'market'}
        s = {**q, 'token':'0xwrapped', 'price':102}
        self.assertAlmostEqual(pool_spread(rel, p, q, s, NOW)['value'], 2.941176470588235)
        self.assertEqual(pool_spread(rel, p, {**q, 'dependencies':['0xpool']}, s, NOW)['reason'], 'circular-price')
        self.assertEqual(pool_spread(rel, p, {**q, 'dependenciesComplete':False}, s, NOW)['reason'], 'unknown-price-path')
        self.assertEqual(pool_spread(rel, p, q, {**s, 'token':'0xstock'}, NOW)['reason'], 'wrong-instrument')
        self.assertEqual(pool_spread({**rel, 'status':'invalid'}, p, q, s, NOW)['reason'], 'unverified-pair')

    def test_relative_returns_use_same_window_and_percentage_points(self):
        current = relative_point(stock(stockPrice=103), {'price':112, 'quoteAt':NOW, 'provider':'DEX', 'priceCurrency':'USD', 'priceScope':'dex'}, NOW)
        base = {**current, 'at':NOW-3600000, 'stock':100, 'meme':100}
        r = relative_return(current, [base], 3600000)
        self.assertAlmostEqual(r['value'], 9)
        self.assertEqual(r['unit'], 'pp')
        self.assertEqual(relative_return(current, [], 3600000)['reason'], 'insufficient-history')
        self.assertEqual(relative_return(current, [{**base, 'at':NOW-3700000}], 3600000)['reason'], 'insufficient-history')
        self.assertEqual(relative_return(current, [{**base, 'inputs':{}}], 3600000)['reason'], 'insufficient-history')

    def test_overlay_always_invalidates_legacy_and_keeps_reference_currency(self):
        from app.stock_quotes import apply_stock_overlays
        with patch('app.stock_quotes.binance_live', return_value={}), patch('app.stock_quotes.robinhood_live', return_value={}), patch('app.stock_quotes.eodhd_quotes', return_value={}):
            self.assertIsNone(apply_stock_overlays([{'premium':{'value':123}}])[0]['premium']['value'])
        ref = {'price':780, 'currency':'HKD', 'marketAt':NOW, 'symbol':'1024.HK', 'id':'XHKG:01024', 'identityVerified':True}
        with patch('app.stock_quotes.binance_live', return_value={}), patch('app.stock_quotes.robinhood_live', return_value={}), patch('app.stock_quotes.eodhd_quotes', return_value={'XHKG:01024':ref}):
            row = apply_stock_overlays([stock(stockCode='1024', stockIdentity={'id':'XHKG:01024','code':'01024','market':'HKEX'})])[0]
            self.assertEqual(row['referenceCurrency'], 'HKD')
            self.assertIsNone(row['premium']['value'])

    def test_public_read_expires_metrics_without_collector_or_new_price(self):
        from app.comparison_service import public_metric
        metric = stock_premium(stock(), NOW)
        self.assertEqual(public_metric(metric, NOW+30001)['status'], 'snapshot')
        self.assertIsNone(public_metric(metric, NOW+MAX_AGE+1)['value'])
        self.assertIsNotNone(metric['value'])

    def test_persistence_deduplicates_and_survives_reopen(self):
        from app.db import ResearchStore
        from app.comparison_service import record, read_comparison
        async def run():
            with tempfile.TemporaryDirectory() as d:
                s = await ResearchStore(d+'/db.sqlite', '196').connect()
                m = stock_premium(stock(), NOW)
                await record(s, 'key', m)
                await record(s, 'key', {**m, 'status':'snapshot'})
                self.assertEqual(len(await s.comparison_history('key')), 1)
                await s.close()
                s = await ResearchStore(d+'/db.sqlite', '196').connect()
                try:
                    self.assertEqual(len(await s.comparison_history('key')), 1)
                    with patch('app.comparison_service.store', AsyncMock(return_value=s)), patch('app.state.reload_if_stale', AsyncMock()), patch('app.comparison_service.record', AsyncMock()) as writer:
                        result = await read_comparison('196', 'unknown')
                        self.assertEqual(result['current']['premium']['reason'], 'pending')
                        writer.assert_not_called()
                finally:
                    await s.close()
        asyncio.run(run())

    def test_alert_needs_three_distinct_live_observations_and_hysteresis(self):
        from app.comparison_alerts import advance
        def m(n, value=3):
            return {'status':'realtime', 'value':value, 'realtimeUntil':NOW+60000, 'inputs':{'sequence':n}}
        state, event = advance(None, m(1), 2, NOW)
        self.assertIsNone(event)
        state, event = advance(state, m(1), 2, NOW+30000)
        self.assertIsNone(event)
        state, event = advance(state, m(2), 2, NOW+30001)
        self.assertIsNone(event)
        state, event = advance(state, m(3), 2, NOW+30002)
        self.assertEqual(event, 'spread-expanded')
        state, event = advance(state, m(4, 1.5), 2, NOW+30003)
        self.assertIsNone(event)
        state, event = advance(state, m(5, 1), 2, NOW+30004)
        self.assertEqual(event, 'spread-recovered')
        _, event = advance(None, {**m(5), 'status':'snapshot'}, 2, NOW+30005)
        self.assertIsNone(event)

    def test_worker_records_changed_inputs_only_and_publishes_consistent_packet(self):
        from app.db import ResearchStore
        from app.comparison_service import refresh_comparisons, subject
        class Data:
            assets = []
            relations = []
            def stock_views(self, include_comparisons=True):
                row = stock()
                row['premium'] = stock_premium(row, NOW)
                return [row]
        async def run():
            with tempfile.TemporaryDirectory() as d:
                s = await ResearchStore(d+'/db.sqlite', '196').connect()
                try:
                    with patch('app.comparison_service.store', AsyncMock(return_value=s)), patch('app.comparison_service.DATA', Data()), patch('app.comparison_service.reload_data', AsyncMock()), patch('app.comparison_service.broadcast') as broadcast, patch('app.comparison_service.time.time', return_value=NOW / 1000):
                        await refresh_comparisons()
                        await refresh_comparisons()
                        self.assertEqual(broadcast.call_count, 1)
                        self.assertEqual(len(await s.comparison_history(subject('0xstock'))), 1)
                        packet = await s.get('comparison', '0xstock')
                        self.assertAlmostEqual(packet['premium']['value'], 2)
                finally:
                    await s.close()
        asyncio.run(run())
