import asyncio
import unittest
from unittest.mock import patch

from app.market_quotes import enrich_asset
from app.risk_assessment import assess_risk
from app.state import DashboardData, _official_pools, _pool_totals


TOKEN = '0x' + 'a' * 40
POOL = '0x' + 'b' * 40
OTHER_POOL = '0x' + 'c' * 40
NOW = 1_800_000_000_000


def aggregate_snapshot(liquidity=1000, volume=60000, at=NOW):
    return {'assets': {f'56:{TOKEN}': {
        'CoinGecko': {'provider': 'CoinGecko', 'updatedAt': at,
                      'liquidity': liquidity, 'volume24h': volume,
                      'fieldTimes': {'liquidity': at, 'volume24h': at},
                      'fieldScopes': {'liquidity': 'token-aggregate', 'volume24h': 'token-aggregate'}},
        'DexScreener': {'provider': 'DexScreener', 'updatedAt': at + 1000,
                        'liquidity': 50, 'volume24h': 5_000_000,
                        'fieldTimes': {'liquidity': at + 1000, 'volume24h': at + 1000},
                        'fieldScopes': {'liquidity': f'pool:{POOL}', 'volume24h': f'pool:{POOL}'}}
    }}}


class RiskAndPoolMetrics(unittest.TestCase):
    def test_newer_single_pool_cannot_replace_aggregate_liquidity_or_create_cross_scope_wash_flag(self):
        with patch('app.market_quotes.time.time', return_value=(NOW + 2000) / 1000), patch(
                'app.market_quotes._read_snapshot', return_value=aggregate_snapshot()):
            result = enrich_asset({'chainId': '56', 'token': TOKEN, 'liquidity': 50,
                                   'fieldTimes': {'liquidity': NOW + 1000},
                                   'fieldScopes': {'liquidity': f'pool:{POOL}'}})
        self.assertEqual(result['totalLiquidityUsd'], 1000)
        self.assertEqual(result['liquidity'], 1000)
        self.assertEqual(result['fieldScopes']['liquidity'], 'token-aggregate')
        self.assertEqual(result['riskFlags'], ['wash_suspect'])
        evidence = result['riskAssessment']['checks']['wash_suspect']['evidence']
        self.assertEqual(evidence['volumeLiquidityRatio'], 60)
        self.assertEqual(evidence['provider'], 'CoinGecko')
        self.assertEqual(evidence['coverage'], 'provider-indexed-pools')
        self.assertEqual(evidence['denominatorScope'], 'token-aggregate')

    def test_only_single_pool_liquidity_and_token_volume_keep_wash_unknown(self):
        with patch('app.market_quotes.time.time', return_value=NOW / 1000), patch(
                'app.market_quotes._read_snapshot', return_value={'assets': {f'56:{TOKEN}': {
                    'DexScreener': aggregate_snapshot()['assets'][f'56:{TOKEN}']['DexScreener']}}}):
            result = enrich_asset({'chainId': '56', 'token': TOKEN, 'volume24h': 1_000_000,
                                   'fieldTimes': {'volume24h': NOW, 'liquidity': NOW},
                                   'fieldScopes': {'volume24h': 'token', 'liquidity': f'pool:{POOL}'},
                                   'liquidity': 50})
        self.assertIsNone(result['totalLiquidityUsd'])
        self.assertIsNone(result['liquidity'])
        self.assertEqual(result['singlePoolLiquidityUsd'], 50)
        self.assertEqual(result['totalLiquidityStatus'], 'unknown')
        self.assertEqual(result['riskAssessment']['checks']['wash_suspect']['status'], 'unknown')
        self.assertEqual(result['riskFlags'], [])

    def test_transaction_per_holder_branch_is_separate_evidence(self):
        row = {'txs24h': 210, 'holders': 10, 'fieldTimes': {'txs24h': NOW, 'holders': NOW},
               'fieldScopes': {'txs24h': 'token'}}
        assessment = assess_risk(row, None, NOW)
        check = assessment['checks']['wash_suspect']
        self.assertEqual(check['status'], 'triggered')
        self.assertEqual(check['evidence']['transactionsPerHolder'], 21)
        self.assertNotIn('volumeLiquidityRatio', check['evidence'])
        self.assertEqual(assessment['status'], 'flagged')

    def test_compatible_spike_holder_anomaly_scan_and_excluded_distribution(self):
        row = {'change24h': 1200, 'holders': 1_500_000, 'fieldTimes': {'change24h': NOW, 'holders': NOW},
               'fieldScopes': {'change24h': 'token'}, 'totalLiquidityUsd': 80_000,
               'totalLiquidityAt': NOW, 'totalLiquidityStatus': 'current',
               'totalLiquidityCoverage': {'coverage': 'provider-indexed-pools'},
               'risk': {'top10': 90},
               'tokenScan': {'provider': 'Onchain OS', 'status': 'complete', 'checkedAt': NOW,
                             'honeypot': False, 'mintable': True, 'pausable': False,
                             'buyTaxPct': 0, 'sellTaxPct': 1}}
        assessment = assess_risk(row, None, NOW)
        self.assertEqual(assessment['flags'], ['thin_spike', 'contract_risk', 'holder_anomaly'])
        self.assertEqual(assessment['checks']['concentrated']['status'], 'unknown')
        row['holderDistribution'] = {'provider': 'audited', 'checkedAt': NOW, 'excludedKnownAddresses': True,
                                     'top10AdjustedPercent': 51}
        self.assertIn('concentrated', assess_risk(row, None, NOW)['flags'])
        self.assertEqual(assess_risk(row, None, NOW + 86_400_001)['checks']['contract_risk']['status'], 'unknown')

    def test_single_pool_set_deduplicates_relations_and_keeps_volume_compatible(self):
        relation = {'chainId': '56', 'pool': POOL, 'token': TOKEN, 'status': 'verified', 'level': 'A',
                    'checkedAt': NOW, 'liquidityUsd': 1234.56, 'liquidityAt': NOW,
                    'poolMarket': {'scope': f'pool:{POOL}', 'volume24h': 100, 'updatedAt': NOW}}
        duplicate = {**relation, 'id': 'second-relation', 'liquidityUsd': 1000, 'liquidityAt': NOW - 1000}
        fake = {**relation, 'pool': OTHER_POOL, 'level': None, 'liquidityUsd': 5_000_000}
        pools = _official_pools([duplicate, relation, fake])
        totals = _pool_totals(pools, NOW)
        self.assertEqual(len(pools), 1)
        self.assertEqual(totals['liquidityUsd'], 1234.56)
        self.assertEqual(totals['volume24hUsd'], 100)
        self.assertEqual(totals['coverage']['total'], 1)
        bad = {**relation, 'poolMarket': {'scope': f'pool:{OTHER_POOL}', 'volume24h': 1_000_000, 'updatedAt': NOW}}
        self.assertIsNone(_pool_totals([bad], NOW)['volume24hUsd'])

    def test_full_dashboard_uses_fresh_official_identity_and_same_pool_cutoff(self):
        # This address is pinned in the checked-in xStocks manifest; the meme
        # is a different contract and the pool joins exactly those two sides.
        official = '0xe7fd74df6c9c32e34af6774aebb240c990f5008c'
        data = DashboardData()
        data.assets = [{'chainId': '56', 'token': TOKEN, 'kind': 'candidate', 'symbol': 'TEST',
                        'price': 1, 'volume24h': 100,
                        'fieldTimes': {'price': NOW, 'volume24h': NOW}}]
        data.relations = [{'chainId': '56', 'token': TOKEN, 'pool': POOL, 'stock': official,
                           'stockSide': official, 'token0': TOKEN, 'token1': official,
                           'ticker': 'AAFL', 'status': 'verified', 'level': None,
                           'liquidityUsd': 1234.56, 'liquidityAt': NOW, 'checkedAt': NOW,
                           'firstSeen': NOW, 'poolMarket': {'scope': f'pool:{POOL}',
                                                            'volume24h': 100, 'updatedAt': NOW}},
                          {'chainId': '56', 'token': TOKEN, 'pool': OTHER_POOL,
                           'stock': '0x' + 'd' * 40, 'stockSide': '0x' + 'd' * 40,
                           'token0': TOKEN, 'token1': '0x' + 'd' * 40,
                           'ticker': 'AAFL', 'status': 'verified', 'level': 'A',
                           'liquidityUsd': 1_000_000, 'liquidityAt': NOW,
                           'checkedAt': NOW, 'firstSeen': NOW}]
        with patch('app.state.time.time', return_value=NOW / 1000), patch(
                'app.market_quotes._read_snapshot', return_value={}):
            payload = asyncio.run(data.payload(False))['unified']
        self.assertEqual([r['level'] for r in payload['relations']], ['A', None])
        self.assertEqual(payload['assets'][0]['relationLevel'], 'A')
        self.assertEqual(payload['assets'][0]['pairLiquidityUsd'], 1234.56)
        self.assertEqual(payload['metrics']['pairedLiquidityUsd'], 1234.56)
        self.assertEqual(payload['metricsByChain']['56']['pairedLiquidityUsd'], 1234.56)
        self.assertEqual(payload['distribution'][1]['liquidity']['value'], 1234.56)
        self.assertEqual(payload['distribution'][1]['volume']['value'], 100)
        with patch('app.state.time.time', return_value=(NOW + 900_001) / 1000), patch(
                'app.market_quotes._read_snapshot', return_value={}):
            expired = asyncio.run(data.payload(False))['unified']
        self.assertIsNone(expired['relations'][0]['level'])
        self.assertEqual(expired['metrics']['verifiedPools'], 0)
        self.assertIsNone(expired['assets'][0]['pairLiquidityUsd'])


if __name__ == '__main__':
    unittest.main()
