import unittest
from unittest.mock import patch

from app.product_metrics import (asset_product_metrics, build_product_theme_metrics,
    field_availability, qualified_asset, seven_day_volume_ratio, v2_exit_impact)
from app.market_quotes import enrich_asset
from app.collectors.market_enrichment import normalize_batch
from app.collectors.risk_enrichment import normalize_goplus
from app.risk_assessment import assess_risk

NOW = 1_800_000_000_000
TOKEN = '0x'+'a'*40
STOCK = '0x'+'b'*40
POOL = '0x'+'c'*40
DAY = 86_400_000
HOUR = 3_600_000


def relation(**kwargs):
    return {'chainId': '56', 'token': TOKEN, 'ticker': 'NVDA', 'stock': STOCK,
        'pool': POOL, 'status': 'verified', 'level': 'A', 'poolType': 'uniswap_v2',
        'factory': '0xca143ce32fe78f1f7019d7d551a6402fc5350c73', 'factoryVerified': True,
        'protocol': 'PancakeSwap V2', 'liquidityUsd': 35870, 'liquidityAt': NOW,
        'poolCreatedAt': NOW-50000, 'confirmationStatus': 'confirmed',
        'poolMarket': {'scope': 'pool:'+POOL, 'volume24h': 1450000, 'provider': 'DexScreener', 'updatedAt': NOW, 'volumeCurrency': 'USD'}, **kwargs}


class ProductMetricsTests(unittest.TestCase):
    def test_documented_v2_example_and_fee_or_version_guards(self):
        result = v2_exit_impact(relation(), 1000, NOW)
        self.assertAlmostEqual(result['valuePercent'], 5.51, delta=0.03)
        self.assertAlmostEqual(result['outputUsd'], 944.943, delta=1)
        self.assertFalse(result['executable'])
        self.assertTrue(result['excludesTokenTax'])
        self.assertIsNone(v2_exit_impact(relation(factoryVerified=False), 1000, NOW)['valuePercent'])
        self.assertEqual(v2_exit_impact(relation(poolType='uniswap_v3', protocol='PancakeSwap V3'), 1000, NOW)['status'], 'unsupported')
        self.assertIsNone(v2_exit_impact(relation(liquidityAt=NOW-1_800_001), 1000, NOW)['valuePercent'])

    def test_expired_activity_and_incomplete_zero_are_not_current(self):
        row = {'chainId': '56', 'token': TOKEN, 'kind': 'candidate', 'price': 1, 'buys24h': 66, 'sells24h': 54,
            'activityComparable': True, 'fieldTimes': {'price': NOW, 'buys24h': NOW-DAY, 'sells24h': NOW-DAY},
            'totalLiquidityUsd': 0, 'totalLiquidityAt': NOW, 'totalLiquidityStatus': 'current',
            'totalLiquidityCoverage': {'scope': 'token-aggregate', 'complete': False}}
        self.assertIsNone(field_availability(row, NOW)['totalLiquidityUsd']['value'])
        self.assertEqual(field_availability(row, NOW)['totalLiquidityUsd']['status'], 'partial')
        self.assertIsNone(asset_product_metrics(row, [relation()], now=NOW)['buyShare24h']['value'])
        self.assertFalse(qualified_asset(row, NOW))
        row['totalLiquidityUsd'] = 1000
        self.assertTrue(qualified_asset(row, NOW))
        row['price'] = 0
        self.assertFalse(qualified_asset(row, NOW))
        self.assertFalse(asset_product_metrics(row, now=NOW)['qualified'])

    def test_aggregate_liquidity_does_not_overwrite_primary_quote(self):
        row = {'chainId': '56', 'token': TOKEN, 'price': 1, 'volume24h': 100, 'provider': 'OKX',
            'fieldTimes': {'price': NOW, 'volume24h': NOW},
            'fieldScopes': {'price': 'token', 'volume24h': 'token'},
            'fieldSources': {'price': 'OKX', 'volume24h': 'OKX'},
            'aggregateMarket': {'provider': 'DexScreener', 'scope': 'token-aggregate',
                'liquidityUsd': 35870, 'liquidityAt': NOW, 'volume24h': 1450000, 'volumeAt': NOW,
                'coverage': 'provider-indexed-pools', 'complete': False}}
        with patch('app.market_quotes.time.time', return_value=NOW/1000), patch('app.market_quotes._read_snapshot', return_value={}):
            result = enrich_asset(row)
        self.assertEqual(result['primaryQuote']['provider'], 'OKX')
        self.assertEqual(result['volume24h'], 100)
        self.assertEqual(result['totalLiquidityUsd'], 35870)
        self.assertAlmostEqual(result['productMetrics']['volumeLiquidityRatio']['value'], 1450000/35870)

    def test_empty_theme_or_snapshot_cohort_returns_unknown_not_exception(self):
        self.assertIsNone(seven_day_volume_ratio({}, [], NOW)['value'])
        result = build_product_theme_metrics('EMPTY', [], [], NOW, snapshots=[])
        self.assertEqual(result['memeCount'], 0)
        self.assertIsNone(result['volume24hUsd'])
        self.assertIsNone(result['volumeRatio7d'])
        self.assertEqual(result['volumeRatio7dEvidence']['status'], 'no-eligible-pools')

    def test_seven_day_ratio_uses_same_hour_same_cohort_and_median(self):
        row = relation(poolMarket={'scope': 'pool:'+POOL, 'volume24h': 800, 'provider': 'DexScreener', 'updatedAt': NOW})
        selected = {('56', POOL): row}
        hour = NOW//HOUR*HOUR
        samples = [{'chainId': '56', 'pool': POOL, 'hour': hour-day*DAY,
            'volume24hUsd': value, 'provider': 'DexScreener', 'scope': 'pool:'+POOL}
            for day, value in enumerate((100, 100, 100, 200, 1000, 2000, 3000), 1)]
        self.assertEqual(seven_day_volume_ratio(selected, samples, NOW)['value'], 4)
        self.assertIsNone(seven_day_volume_ratio(selected, samples[:-1], NOW)['value'])
        samples[0]['provider'] = 'CoinGecko'
        self.assertIsNone(seven_day_volume_ratio(selected, samples, NOW)['value'])

    def test_theme_partial_creation_or_volume_withholds_ratios(self):
        r1 = relation()
        r2 = relation(pool='0x'+'d'*40, token='0x'+'e'*40, confirmationStatus=None, poolMarket={})
        metric = build_product_theme_metrics('NVDA', [], [r1, r2], NOW)
        self.assertEqual(metric['memeCount'], 2)
        self.assertEqual(metric['volume24hUsd'], 1450000)
        self.assertIsNone(metric['topTwoShare'])
        self.assertIsNone(metric['newPairs24h'])
        self.assertEqual(metric['coverage']['volumeKnown'], 1)
        metric = build_product_theme_metrics('NVDA', [], [r1], NOW)
        self.assertEqual(metric['newPairs24h'], 1)
        self.assertEqual(metric['topTwoShare'], 1)

    def test_batch_flips_base_buy_direction_for_quote_and_rejects_empty_zero(self):
        rows = [{'chainId': 'bsc', 'pairAddress': POOL, 'baseToken': {'address': STOCK},
            'quoteToken': {'address': TOKEN}, 'liquidity': {'usd': 2000}, 'volume': {'h24': 5000},
            'txns': {'h24': {'buys': 2, 'sells': 7}}}]
        assets, pools = normalize_batch('56', [TOKEN], rows, NOW)
        self.assertEqual(assets[TOKEN]['dexActivity']['buys24h'], 7)
        self.assertEqual(assets[TOKEN]['dexActivity']['sells24h'], 2)
        self.assertEqual(assets[TOKEN]['aggregateMarket']['liquidityUsd'], 2000)
        self.assertFalse(assets[TOKEN]['aggregateMarket']['complete'])
        self.assertEqual(normalize_batch('56', [TOKEN], [], NOW), ({}, {}))
        rows.append({**rows[0], 'baseToken': {'address': '0x'+'f'*40}})
        self.assertEqual(normalize_batch('56', [TOKEN], rows, NOW), ({}, {}))

    def test_high_tax_cannot_sell_creator_and_adjusted_provider_top10(self):
        security = {'is_honeypot': '0', 'cannot_sell_all': '1', 'buy_tax': '0', 'sell_tax': '.51',
            'creator_address': STOCK, 'creator_percent': '.11', 'holder_count': '3',
            'holders': [{'address': POOL, 'percent': '.3', 'is_contract': '1'},
                        {'address': '0x'+'0'*36+'dead', 'percent': '.1'},
                        {'address': STOCK, 'percent': '.6', 'is_contract': '1'}],
            'dex': [{'pair': POOL, 'liquidity_type': 'UniV2'}]}
        normalized = normalize_goplus('56', TOKEN, security, NOW)
        safety = assess_risk(normalized, now=NOW)['safety']
        self.assertEqual(safety['tax']['severity'], 'red')
        self.assertIn('cannotSellAll', safety['tax']['evidence']['triggers'])
        self.assertEqual(safety['creatorHolding']['status'], 'triggered')
        self.assertEqual(safety['concentration']['evidence']['top10AdjustedPercent'], 60)
        self.assertEqual(safety['concentration']['evidence']['contractAddressCount'], 1)
        self.assertEqual(assess_risk(normalized, now=NOW+21_600_001)['safety']['tax']['status'], 'unknown')

    def test_fdv_uses_observed_main_pool_base_token_without_changing_primary_quote(self):
        rows = [{'chainId': 'bsc', 'pairAddress': POOL, 'baseToken': {'address': TOKEN},
            'quoteToken': {'address': STOCK}, 'liquidity': {'usd': 2000}, 'volume': {'h24': 5000}, 'fdv': 8000}]
        assets, _ = normalize_batch('56', [TOKEN, STOCK], rows, NOW)
        self.assertEqual(assets[TOKEN]['fdv'], 8000)
        self.assertEqual(assets[TOKEN]['fieldSources']['fdv'], 'DexScreener')
        self.assertEqual(assets[TOKEN]['fieldObservations']['fdv']['pool'], POOL)
        self.assertNotIn('fdv', assets[STOCK])
        result = asset_product_metrics({'chainId': '56', 'token': TOKEN, 'provider': 'OKX', **assets[TOKEN]}, now=NOW)
        self.assertEqual(result['fdvUsd']['value'], 8000)
        self.assertEqual(result['fdvUsd']['source'], 'DexScreener')
        rows.append({**rows[0], 'pairAddress': '0x'+'d'*40, 'liquidity': {'usd': 4000}, 'fdv': 9000})
        self.assertEqual(normalize_batch('56', [TOKEN], rows, NOW)[0][TOKEN]['fdv'], 9000)

    def test_malformed_index_fields_do_not_raise_or_prove_zero(self):
        rows = [{'chainId': 'bsc', 'pairAddress': POOL, 'baseToken': {'address': TOKEN},
            'quoteToken': {'address': STOCK}, 'volume': [], 'liquidity': '',
            'txns': {'h24': 'invalid'}, 'priceChange': ['invalid']}]
        assets, _ = normalize_batch('56', [TOKEN], rows, NOW)
        self.assertIsNone(assets[TOKEN]['aggregateMarket']['liquidityUsd'])
        self.assertNotIn('dexActivity', assets[TOKEN])
        rows[0]['baseToken'] = None
        self.assertEqual(normalize_batch('56', [TOKEN], rows, NOW), ({}, {}))


if __name__ == '__main__':
    unittest.main()
