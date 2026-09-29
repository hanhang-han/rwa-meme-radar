import unittest
from app.dashboard_projection import compact_dashboard, overview_dashboard

class ProjectionTest(unittest.TestCase):
    def test_v21_identity_liquidity_and_risk_survive_compaction(self):
        asset = {
            'chainId': '196', 'token': '0x' + 'a' * 40, 'kind': 'candidate',
            'totalLiquidityUsd': None, 'totalLiquidityStatus': 'unknown',
            'pairLiquidityUsd': 1250, 'pairLiquidityStatus': 'current',
            'relationLevel': 'A', 'riskFlags': ['thin_spike'], 'riskStatus': 'partial',
            'riskAssessment': {'checks': {
                'thin_spike': {'status': 'triggered', 'evidence': {'change24hPercent': 1200}},
                'contract_risk': {'status': 'unknown'},
            }},
        }
        stock = {
            'chainId': '196', 'tokenContractAddress': '0x' + 'b' * 40,
            'verificationStatus': 'official',
            'issuerIdentity': {'verificationStatus': 'official', 'issuer': 'xStocks',
                               'sourceUrl': 'https://api.xstocks.fi/', 'manifestVersion': 'v1'},
        }
        result = compact_dashboard({'now': 1000, 'unified': {
            'assets': [asset], 'stockTokens': [stock], 'relations': [], 'signals': [],
        }})['unified']
        self.assertEqual(result['identityCatalog']['status'], 'ready')
        self.assertGreater(result['identityCatalog']['entries'], 0)
        actual = result['assets'][0]
        self.assertIn('totalLiquidityUsd', actual)
        self.assertIsNone(actual['totalLiquidityUsd'])
        self.assertEqual(actual['pairLiquidityUsd'], 1250)
        self.assertEqual(actual['relationLevel'], 'A')
        self.assertEqual(actual['riskAssessment']['checks'].keys(), {'thin_spike'})
        self.assertEqual(result['stockTokens'][0]['issuerIdentity']['issuer'], 'xStocks')

    def test_overview_preserves_full_counts_and_coverage_without_full_catalogue(self):
        assets=[{'chainId':'196','token':str(i),'kind':'candidate','price':1,'priceCurrency':'USD','volume24h':100-i,'fieldTimes':{'price':1000,'volume24h':1000},'fieldSources':{'price':'OKX'},'dataQuality':{'tier':'current','eligible':{'relationRanking':True}},'fieldObservations':{'price':{'large':'proof'}}} for i in range(20)]
        stocks=[{'chainId':'196','tokenContractAddress':'0xstock','price':100,'priceCurrency':'USDT','stockPrice':780,'referenceCurrency':'HKD','premium':{'value':None,'reason':'missing-fx','inputs':{'large':'proof'}},'marketQuotes':{'large':'proof'}}]
        raw={'now':2000,'unified':{'assets':assets,'stockTokens':stocks,'relations':[],'groups':[{'assets':assets}], 'quality':{'summary':{'total':20}},'metrics':{'verifiedPools':123},'sources':[{'provider':'OKX'}]}}
        full=compact_dashboard(raw);small=overview_dashboard(full)
        self.assertEqual(small['unified']['totals'],{'assets':20,'stockTokens':1,'relations':0})
        self.assertEqual(small['unified']['quality']['assets']['price']['total'],20)
        self.assertEqual(small['unified']['quality']['assets']['price']['fresh'],20)
        self.assertEqual(len(small['unified']['assets']),12)
        self.assertEqual(small['unified']['stockTokens'],[])
        self.assertEqual(full['unified']['groups'], [])
        self.assertEqual(raw['unified']['groups'], [{'assets': assets}])
        self.assertEqual(small['unified']['metrics']['verifiedPools'],123)
        self.assertEqual(full['unified']['stockTokens'][0]['priceCurrency'],'USDT')
        self.assertEqual(full['unified']['stockTokens'][0]['referenceCurrency'],'HKD')
        self.assertEqual(full['unified']['stockTokens'][0]['premium'],{'value':None,'reason':'missing-fx'})
        self.assertNotIn('fieldObservations',full['unified']['assets'][0])
        self.assertEqual(raw['unified']['stockTokens'][0]['premium']['inputs'],{'large':'proof'})
