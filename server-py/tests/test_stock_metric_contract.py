"""Stock-pool UI contracts checked with independently shaped market records."""
import copy
import unittest

from app.product_metrics import (MAX_AGE_MS, THEME_VOLUME_MAX_AGE_MS,
    build_product_theme_metrics, seven_day_volume_ratio)


NOW = 1_800_000_000_000
DAY = 86_400_000
HOUR = 3_600_000
TOKEN = '0x'+'a'*40
STOCK = '0x'+'b'*40
POOL = '0x'+'c'*40
POOL2 = '0x'+'d'*40


def relation(pool=POOL, volume=80, *, chain='56', token=TOKEN, at=NOW, **fields):
    return {'chainId': chain, 'token': token, 'stock': STOCK, 'ticker': 'NVDA',
        'pool': pool, 'status': 'verified', 'level': 'A', 'liquidityUsd': 2000,
        'liquidityAt': NOW, 'checkedAt': NOW, 'poolCreatedAt': NOW-2*DAY,
        'creationConfirmed': True, 'poolMarket': {'scope': 'pool:'+pool,
            'volume24h': volume, 'provider': 'DexScreener', 'updatedAt': at,
            'volumeCurrency': 'USD'}, **fields}


def asset(volume=100, *, chain='56', token=TOKEN, **aggregate):
    return {'chainId': chain, 'token': token, 'kind': 'candidate',
        'aggregateMarket': {'scope': 'token-aggregate', 'provider': 'DexScreener',
            'volume24h': volume, 'volumeAt': NOW, **aggregate}}


def history(pool=POOL, volume=100):
    hour = NOW//HOUR*HOUR
    return [{'chainId': '56', 'pool': pool, 'hour': hour-day*DAY,
        'volume24hUsd': volume, 'provider': 'DexScreener', 'scope': 'pool:'+pool}
        for day in range(1, 8)]


class StockMetricContract(unittest.TestCase):
    def metrics(self, rows, assets=None, snapshots=None):
        return build_product_theme_metrics('NVDA', assets or [], rows, NOW, snapshots)

    def test_pool_sum_exceeding_asset_total_is_unknown_not_160_percent(self):
        result = self.metrics([relation(), relation(POOL2)], [asset()])
        self.assertEqual(result['volume24hUsd'], 160)
        self.assertIsNone(result['mainPoolVolumeRatio'])
        self.assertEqual(result['mainPoolVolumeRatioReason'], 'paired-volume-exceeds-asset-total')
        self.assertEqual(result['contributions'][0]['volume24hUsd'], 160)
        self.assertEqual(result['coverage']['volumeKnown'], 2)
        self.assertEqual(result['memeCount'], 1)

    def test_asset_total_is_counted_once_for_multiple_direct_pools(self):
        result = self.metrics([relation(volume=40), relation(POOL2, volume=10)], [asset()])
        self.assertEqual(result['mainPoolVolumeRatio'], .5)
        self.assertIsNone(result['mainPoolVolumeRatioReason'])
        self.assertEqual(result['contributions'][0]['share'], 1)

    def test_currency_conflicts_and_pool_scope_are_not_comparable_volume(self):
        for patch in ({'scope': 'token-aggregate'}, {'volumeCurrency': 'EUR'},
                      {'currency': 'EUR'}, {'provider': None}):
            row = relation()
            row['poolMarket'].update(patch)
            with self.subTest(patch=patch):
                result = self.metrics([row])
                self.assertIsNone(result['volume24hUsd'])
                self.assertEqual(result['coverage']['volumeKnown'], 0)
                self.assertEqual(result['coverage']['poolCount'], 1)
                self.assertIsNone(seven_day_volume_ratio({('56', POOL): row}, history(), NOW)['value'])

    def test_unknown_currency_for_other_provider_cannot_be_inferred_usd(self):
        row = relation()
        row['poolMarket'].pop('volumeCurrency')
        row['poolMarket']['provider'] = 'Other'
        self.assertIsNone(self.metrics([row])['volume24hUsd'])
        row['poolMarket']['volumeCurrency'] = 'USD'
        self.assertEqual(self.metrics([row])['volume24hUsd'], 80)

    def test_documented_dex_usd_default_matches_snapshot_collector(self):
        row = relation(volume=100)
        row['poolMarket'].pop('volumeCurrency')
        self.assertEqual(self.metrics([row])['volume24hUsd'], 100)
        self.assertEqual(seven_day_volume_ratio({('56', POOL): row}, history(), NOW)['value'], 1)

    def test_theme_volume_expiration_is_fifteen_minutes_without_global_change(self):
        self.assertEqual(MAX_AGE_MS, 30*60_000)
        self.assertEqual(THEME_VOLUME_MAX_AGE_MS, 15*60_000)
        boundary = relation(at=NOW-15*60_000)
        self.assertEqual(self.metrics([boundary])['volume24hUsd'], 80)
        for at in (NOW-15*60_000-1, NOW-20*60_000, NOW+1):
            row = relation(at=at)
            result = self.metrics([row], snapshots=history())
            self.assertIsNone(result['volume24hUsd'])
            self.assertIsNone(result['volumeRatio7d'])

    def test_true_zero_volume_and_missing_pool_do_not_share_denominator(self):
        result = self.metrics([relation(volume=0), relation(POOL2, volume=None)])
        self.assertEqual(result['volume24hUsd'], 0)
        self.assertEqual((result['coverage']['volumeKnown'], result['coverage']['poolCount']), (1, 2))
        self.assertFalse(result['coverage']['complete'])
        self.assertIsNone(result['contributions'][0]['share'])
        self.assertIsNone(result['topTwoShare'])
        self.assertIsNone(result['mainPoolVolumeRatio'])

    def test_zero_denominator_is_unknown_but_zero_numerator_can_be_valid(self):
        result = self.metrics([relation(volume=0)], [asset(volume=0)])
        self.assertEqual(result['volume24hUsd'], 0)
        self.assertIsNone(result['mainPoolVolumeRatio'])
        self.assertEqual(result['mainPoolVolumeRatioReason'], 'asset-volume-denominator-zero')
        result = self.metrics([relation(volume=0)], [asset(volume=100)])
        self.assertEqual(result['mainPoolVolumeRatio'], 0)
        self.assertTrue(result['coverage']['complete'])

    def test_ratio_rejects_asset_scope_currency_source_and_time_mismatch(self):
        cases = [({'scope': 'pool:'+POOL}, 'asset-volume-scope-incompatible'),
            ({'volumeCurrency': 'EUR'}, 'asset-volume-currency-incompatible'),
            ({'currency': 'EUR'}, 'asset-volume-currency-incompatible'),
            ({'provider': 'Other', 'volumeCurrency': 'USD'}, 'volume-provider-mismatch'),
            ({'volumeAt': NOW-300_001}, 'observation-time-mismatch'),
            ({'volumeAt': NOW-900_001}, 'asset-volume-unavailable'),
            ({'volume24h': None}, 'asset-volume-unavailable')]
        for aggregate, reason in cases:
            with self.subTest(aggregate=aggregate):
                result = self.metrics([relation()], [asset(**aggregate)])
                self.assertEqual(result['volume24hUsd'], 80)
                self.assertIsNone(result['mainPoolVolumeRatio'])
                self.assertEqual(result['mainPoolVolumeRatioReason'], reason)

    def test_unknown_asset_keeps_independently_evidenced_pool(self):
        result = self.metrics([relation()])
        self.assertEqual(result['memeCount'], 1)
        self.assertEqual(result['volume24hUsd'], 80)
        self.assertEqual(result['coverage']['poolCount'], 1)
        self.assertIsNone(result['mainPoolVolumeRatio'])

    def test_known_derivative_is_excluded_from_current_theme_totals(self):
        derivative = {**asset(), 'assetCategory': 'derivative'}
        result = self.metrics([relation()], [derivative])
        self.assertEqual(result['memeCount'], 0)
        self.assertEqual(result['coverage']['poolCount'], 0)
        self.assertIsNone(result['volume24hUsd'])
        self.assertEqual(result['contributions'], [])

    def test_duplicate_pool_chooses_newest_observation_in_either_input_order(self):
        old = relation(volume=1000, liquidityAt=NOW-1000, checkedAt=NOW-1000)
        current = relation(volume=80)
        for rows in ([old, current], [current, old]):
            result = self.metrics(rows)
            self.assertEqual(result['volume24hUsd'], 80)
            self.assertEqual(result['coverage']['poolCount'], 1)

    def test_duplicate_pool_ties_use_check_then_market_observation(self):
        old = relation(volume=1000, checkedAt=NOW-1000)
        current = relation(volume=80)
        self.assertEqual(self.metrics([current, old])['volume24hUsd'], 80)
        old['checkedAt'] = NOW
        old['poolMarket']['updatedAt'] = NOW-1000
        self.assertEqual(self.metrics([current, old])['volume24hUsd'], 80)

    def test_newer_ineligible_pool_cannot_revive_older_a_record(self):
        old = relation(liquidityAt=NOW-1000)
        current = relation(level=None)
        result = self.metrics([current, old])
        self.assertEqual(result['coverage']['poolCount'], 0)
        self.assertIsNone(result['volume24hUsd'])

    def test_cross_chain_same_addresses_are_independent_and_denominators_dedup_per_chain(self):
        result = self.metrics([relation(volume=40), relation(volume=10, chain='196')],
            [asset(volume=100), asset(volume=100, chain='196')])
        self.assertEqual(result['coverage']['poolCount'], 2)
        self.assertEqual(result['memeCount'], 2)
        self.assertEqual(result['volume24hUsd'], 50)
        self.assertEqual(result['mainPoolVolumeRatio'], .25)
        self.assertEqual(sum(row['share'] for row in result['contributions']), 1)

    def test_rounding_does_not_create_false_inconsistent_totals(self):
        result = self.metrics([relation(volume=.1), relation(POOL2, volume=.2)], [asset(volume=.3)])
        self.assertEqual(result['mainPoolVolumeRatio'], 1)

    def test_input_records_are_not_mutated(self):
        rows, assets = [relation(), relation(POOL2)], [asset()]
        before = copy.deepcopy((rows, assets))
        self.metrics(rows, assets, history())
        self.assertEqual((rows, assets), before)


if __name__ == '__main__':
    unittest.main()
