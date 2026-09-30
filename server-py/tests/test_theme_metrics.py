"""Document formulas checked against event-shaped, independently auditable fixtures."""
import copy
import unittest

from app.theme_metrics import DAY_MS, METHOD, rank_theme_heat, stock_pool_flow


NOW = 1_790_000_000_000


def pool(index, ticker='AAA', *, amount=10, direction='buy_meme',
         coverage=1, created=None, trader=None):
    address = '0x' + f'{index:040x}'
    tx_from = trader or ('0x' + f'{index+100:040x}')
    return {
        'pool': address, 'relation': {
            'pool': address, 'ticker': ticker, 'level': 'A',
            'status': 'verified', 'liquidityUsd': 5000,
            'liquidityAt': NOW-1000, 'poolCreatedAt': created or NOW-2*DAY_MS,
            'creationConfirmed': True,
        },
        'swapCoverage24h': {'method': METHOD, 'ratio': coverage,
                            'from': NOW-DAY_MS, 'to': NOW},
        'trades': [{
            'id': f'tx-{index}', 'pool': address,
            't': NOW-10_000, 'direction': direction, 'finality': 'confirmed',
            'stockAmount': amount, 'stockUsdQuote': {
                'value': 10, 'currency': 'USD', 'provider': 'OKX', 'at': NOW-10_000},
            'txFrom': tx_from,
        }],
    }


def history(volume=100, traders=3):
    return [{'dayStart': NOW-(day+2)*DAY_MS, 'volumeUsd': volume,
             'uniqueTraderAddresses': traders, 'coverageRatio': 1,
             'method': METHOD, 'complete': True} for day in range(3)]


class ThemeMetricTests(unittest.TestCase):
    def test_flow_uses_only_a_grade_direct_pools_and_stock_side_usd(self):
        buy = pool(1)
        sell = pool(2, amount=5, direction='sell_meme')
        excluded = pool(3, amount=999)
        excluded['riskFlags'] = ['wash_suspect']
        name_lead = pool(4, amount=999)
        name_lead['relation']['level'] = 'B'
        unrelated = pool(5, 'BBB', amount=999)
        result = stock_pool_flow('AAA', [buy, sell, excluded, name_lead, unrelated],
                                 NOW, history=[{'method': METHOD,
                                 'coverageRatio': 1, 'netUsd': 25,
                                 'from': NOW-(day+2)*DAY_MS,
                                 'to': NOW-(day+1)*DAY_MS} for day in range(7)])
        self.assertEqual((result['valueUsd'], result['buyUsd'], result['sellUsd']),
                         (50, 100, 50))
        self.assertAlmostEqual(result['buyShare'], 2/3)
        self.assertEqual(result['vsSevenDayMean'], 2)
        self.assertEqual(result['poolCount'], 2)
        self.assertEqual(result['excludedPools'], [excluded['pool']])
        self.assertTrue(result['rankEligible'])
        self.assertEqual(sum(t['signedValueUsd'] for t in result['trades']), 50)

    def test_coverage_observation_is_not_onchain_audit(self):
        row = pool(1)
        row['swapCoverage24h']['method'] = 'time-observation-buckets'
        result = stock_pool_flow('AAA', [row], NOW)
        self.assertEqual(result['status'], 'coverage-unknown')
        self.assertIsNone(result['valueUsd'])
        row['swapCoverage24h']['method'] = METHOD
        row['swapCoverage24h']['ratio'] = .80
        partial = stock_pool_flow('AAA', [row], NOW)
        self.assertEqual(partial['status'], 'sample-incomplete')
        self.assertEqual(partial['valueUsd'], 100)
        self.assertFalse(partial['rankEligible'])

    def test_confirmed_cursor_may_lag_but_window_ends_at_oldest_confirmed_head(self):
        ahead, behind = pool(1), pool(2)
        behind['swapCoverage24h']['to'] = NOW-30_000
        behind['swapCoverage24h']['from'] = NOW-DAY_MS-30_000
        ahead['swapCoverage24h']['from'] = NOW-DAY_MS-30_000
        ahead['trades'][0]['t'] = NOW-10_000  # after the common finality cutoff
        behind['trades'][0]['t'] = NOW-40_000
        behind['trades'][0]['stockUsdQuote']['at'] = NOW-40_000
        result = stock_pool_flow('AAA', [ahead, behind], NOW)
        self.assertEqual(result['status'], 'current')
        self.assertEqual(result['window']['to'], NOW-30_000)
        self.assertEqual(result['trailingLagMs'], 30_000)
        self.assertEqual(result['valueUsd'], 100)
        self.assertLess(result['coverageRatio'], 1)
        behind['swapCoverage24h']['to'] = NOW-61_000
        self.assertEqual(stock_pool_flow('AAA', [ahead, behind], NOW)['status'],
                         'coverage-unknown')

    def test_unconfirmed_or_unpriced_trade_blocks_aggregate(self):
        row = pool(1)
        row['trades'][0]['finality'] = 'pending'
        self.assertIsNone(stock_pool_flow('AAA', [row], NOW)['valueUsd'])
        row['trades'][0]['finality'] = 'confirmed'
        row['trades'][0]['stockUsdQuote']['provider'] = 'unknown'
        self.assertIsNone(stock_pool_flow('AAA', [row], NOW)['valueUsd'])
        row['trades'][0]['stockUsdQuote']['provider'] = 'OKX'
        row['trades'][0]['stockUsdQuote']['at'] = NOW-100_000
        self.assertIsNone(stock_pool_flow('AAA', [row], NOW)['valueUsd'])

    def test_heat_requires_three_pools_history_creation_and_tx_from(self):
        base = [pool(i) for i in (1, 2, 3)]
        sample = {'ticker': 'AAA', 'pools': base, 'historyDays': history()}
        result = rank_theme_heat([sample], NOW)['AAA']
        self.assertEqual(result['status'], 'current')
        self.assertEqual(result['components']['volumeRatio'], 3)
        self.assertEqual(result['components']['newPools24h'], 0)
        self.assertEqual(result['components']['traderRatio'], 1)
        self.assertEqual(result['heat'], 50)

        few = copy.deepcopy(sample)
        few['pools'] = few['pools'][:2]
        self.assertEqual(rank_theme_heat([few], NOW)['AAA']['status'], 'sample-too-small')
        no_history = copy.deepcopy(sample)
        no_history['historyDays'] = no_history['historyDays'][:2]
        self.assertEqual(rank_theme_heat([no_history], NOW)['AAA']['status'], 'history-insufficient')
        no_creation = copy.deepcopy(sample)
        no_creation['pools'][0]['relation']['creationConfirmed'] = False
        self.assertEqual(rank_theme_heat([no_creation], NOW)['AAA']['status'], 'pool-creation-unknown')
        no_trader = copy.deepcopy(sample)
        no_trader['pools'][0]['trades'][0]['txFrom'] = None
        self.assertEqual(rank_theme_heat([no_trader], NOW)['AAA']['status'], 'trader-address-unknown')

    def test_percentiles_keep_raw_components_and_equal_weights(self):
        alpha = {'ticker': 'AAA', 'pools': [pool(i, 'AAA', created=NOW-1000 if i == 1 else None)
                                            for i in (1, 2, 3)], 'historyDays': history(100, 3)}
        beta = {'ticker': 'BBB', 'pools': [pool(i, 'BBB', amount=20)
                                          for i in (4, 5, 6)], 'historyDays': history(100, 2)}
        results = rank_theme_heat([alpha, beta], NOW)
        a, b = results['AAA'], results['BBB']
        self.assertEqual(a['components']['newPools24h'], 1)
        self.assertEqual(b['components']['newPools24h'], 0)
        self.assertEqual(a['percentiles']['newPools24h'], 75)
        self.assertEqual(b['percentiles']['newPools24h'], 25)
        self.assertEqual(a['percentiles']['volumeRatio'], 25)
        self.assertEqual(b['percentiles']['volumeRatio'], 75)
        self.assertAlmostEqual(a['heat'], sum(a['percentiles'].values())/3)
        self.assertTrue(a['rankEligible'] and b['rankEligible'])
