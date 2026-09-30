import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Request

from app.api.dashboard import get_dashboard
from app.dashboard_projection import compact_dashboard, market_dashboard, overview_dashboard

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
        self.assertEqual(len(small['unified']['assets']),2)
        self.assertEqual(small['unified']['stockTokens'],[])
        self.assertEqual(full['unified']['groups'], [])
        self.assertEqual(raw['unified']['groups'], [{'assets': assets}])
        self.assertEqual(small['unified']['metrics']['verifiedPools'],123)
        self.assertEqual(full['unified']['stockTokens'][0]['priceCurrency'],'USDT')
        self.assertEqual(full['unified']['stockTokens'][0]['referenceCurrency'],'HKD')
        self.assertEqual(full['unified']['stockTokens'][0]['premium'],{'value':None,'reason':'missing-fx'})
        self.assertNotIn('fieldObservations',full['unified']['assets'][0])
        self.assertEqual(raw['unified']['stockTokens'][0]['premium']['inputs'],{'large':'proof'})

    def test_overview_includes_only_bounded_official_stocks_for_visible_pairs(self):
        assets=[{'chainId':'56','token':f'0xmeme{i}','kind':'candidate','volume24h':100-i,
                 'dataQuality':{'eligible':{'relationRanking':True}}} for i in range(30)]
        stocks=[{'chainId':'56','tokenContractAddress':f'0xstock{i}',
                 'issuerIdentity':{'verificationStatus':'official'}} for i in range(30)]
        stocks.append({'chainId':'56','tokenContractAddress':'0xstock0',
                       'issuerIdentity':{'verificationStatus':'unverified'}})
        relations=[{'chainId':'56','token':f'0xmeme{i}','stock':f'0xstock{i}',
                    'level':'A','status':'verified'} for i in range(30)]
        full={'now':1000,'unified':{'assets':assets,'stockTokens':stocks,'relations':relations}}
        result=overview_dashboard(full)['unified']
        self.assertEqual(result['totals'] if 'totals' in result else None,None)
        self.assertEqual(len(result['assets']),2)
        self.assertEqual(len(result['relations']),2)
        self.assertEqual([s['tokenContractAddress'] for s in result['stockTokens']],
                         [f'0xstock{i}' for i in range(2)])
        self.assertEqual(len(full['unified']['assets']),30)
        self.assertEqual(len(full['unified']['relations']),30)
        self.assertEqual(len(full['unified']['stockTokens']),31)

    def test_market_retains_every_identity_and_list_metric_without_full_evidence(self):
        heavy_evidence = 'x' * 5000
        asset = {
            'chainId':'56','token':'0xmeme','projectionKey':'56:0xmeme','kind':'candidate',
            'symbol':'MEME','name':'Meme','price':1.2,'volume24h':800,'change24h':4,
            'totalLiquidityUsd':1000,'totalLiquidityStatus':'current',
            'totalLiquidityCoverage':{'scope':'token-aggregate','coverage':'provider-indexed-pools','provider':'OKX'},
            'fieldTimes':{'price':1000,'volume24h':1000,'marketCap':999},
            'fieldSources':{'price':'OKX','volume24h':'OKX','marketCap':'OKX'},
            'fieldScopes':{'volume24h':'token-aggregate','marketCap':'unknown'},
            'fieldStatus':{'price':{'heavy':heavy_evidence}},'marketQuotes':{'heavy':heavy_evidence},
            'relationLevel':'B','match':{'level':'B','ticker':'ABC','evidenceStatus':'name-only'},
            'dataQuality':{'tier':'historical','eligible':{'relationRanking':True}},
        }
        stock = {
            'chainId':'56','tokenContractAddress':'0xstock','projectionKey':'56:0xstock',
            'stockCode':'ABC','tokenSymbol':'ABCx','price':20,'volume24h':100,
            'issuerIdentity':{'verificationStatus':'official','eligibleForPair':True,'sourceUrl':'large-proof'},
            'stockIdentity':{'id':'ABC','code':'ABC','nameZh':'示例','nameEn':'Example','sourceUrl':'large-proof'},
            'premium':{'value':2.1,'status':'realtime','at':1000,'validUntil':2000,'realtimeUntil':1500,'inputs':{'large':'proof'}},
            'marketQuotes':{'large':heavy_evidence},'fieldStatus':{'price':{'large':heavy_evidence}},
        }
        relation = {
            'id':'pair-1','chainId':'56','token':'0xmeme','stock':'0xstock','stockSide':'0xstock',
            'pool':'0xpool','ticker':'ABC','level':'A','status':'verified','evidenceStatus':'qualified',
            'liquidityUsd':2000,'liquidityAt':1000,'projectionKey':'56:pair-1',
            'sideIdentity':{'verificationStatus':'official','eligibleForPair':True,'sourceUrl':'large-proof'},
            'stockIdentity':{'verificationStatus':'official','eligibleForPair':True,'sourceUrl':'large-proof'},
            'priceComparison':{'relative':{'1h':{'value':3,'validUntil':2000},'24h':{'large':'proof'}}},
            'creationTx':heavy_evidence,
        }
        base = {'now':1000,'realtime':{'revision':9,'cursor':42},'unified':{
            'assets':[asset,dict(asset,token='0xmeme2',projectionKey='56:0xmeme2')],
            'stockTokens':[stock,dict(stock,tokenContractAddress='0xstock2',projectionKey='56:0xstock2')],
            'relations':[relation], 'themeMap':{'bubbles':[{'ticker':'ABC'}]},
            'importantChanges':[{'id':'change-1'}], 'quality':{'summary':{'total':2}},
            'metrics':{'activeMemeCount':2},'sectors':[{'sector':'test'}], 'sources':[{'provider':'OKX'}],
            'signals':[{'id':'signal-1'}], 'groups':[],
        }}
        result = market_dashboard(base)
        rows = result['unified']
        self.assertEqual(rows['snapshotScope'],'market')
        self.assertEqual(result['realtime'],base['realtime'])
        for key in ('themeMap','importantChanges','quality','metrics','sectors','sources','signals'):
            self.assertEqual(rows[key],base['unified'][key])
        self.assertEqual([r['projectionKey'] for r in rows['assets']],['56:0xmeme','56:0xmeme2'])
        self.assertEqual([r['projectionKey'] for r in rows['stockTokens']],['56:0xstock','56:0xstock2'])
        self.assertEqual(rows['relations'][0]['projectionKey'],'56:pair-1')
        self.assertEqual(rows['assets'][0]['match']['ticker'],'ABC')
        self.assertEqual(rows['assets'][0]['fieldTimes'],{'price':1000,'volume24h':1000})
        self.assertEqual(rows['assets'][0]['totalLiquidityCoverage'],asset['totalLiquidityCoverage'])
        self.assertEqual(rows['stockTokens'][0]['premium']['validUntil'],2000)
        self.assertEqual(rows['stockTokens'][0]['issuerIdentity'],{'verificationStatus':'official','eligibleForPair':True})
        self.assertEqual(rows['relations'][0]['priceComparison']['relative'],{'1h':{'value':3,'validUntil':2000}})
        self.assertNotIn('marketQuotes',rows['assets'][0])
        self.assertNotIn('marketQuotes',rows['stockTokens'][0])
        self.assertNotIn('creationTx',rows['relations'][0])
        self.assertEqual(base['unified']['snapshotScope'] if 'snapshotScope' in base['unified'] else None,None)
        self.assertEqual(base['unified']['assets'][0]['fieldStatus'],{'price':{'heavy':heavy_evidence}})
        self.assertLess(len(json.dumps(result)),len(json.dumps(base))*0.75)


class DashboardMarketApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_market_view_and_etag(self):
        full_body = json.dumps({'now':1000,'realtime':{'revision':1,'cursor':2},'unified':{
            'assets':[{'chainId':'56','token':'0xa','projectionKey':'56:0xa'}],
            'stockTokens':[],'relations':[],'themeMap':{'bubbles':[]},
        }})
        market = json.dumps(market_dashboard(json.loads(full_body)))
        selected = AsyncMock(side_effect=lambda view: {'full': full_body, 'market': market}[view])
        with patch('app.api.dashboard.read_projection_json',new=selected):
            response = await get_dashboard(Request({'type':'http','headers':[]}),view='market')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.body, market.encode())
            self.assertEqual(json.loads(response.body)['unified']['snapshotScope'],'market')
            etag = response.headers['etag']
            cached = await get_dashboard(Request({'type':'http','headers':[(b'if-none-match',etag.encode())]}),view='market')
            self.assertEqual(cached.status_code,304)
            full = await get_dashboard(Request({'type':'http','headers':[]}),view='full')
            self.assertEqual(full.body,full_body.encode())
            self.assertEqual([call.args[0] for call in selected.await_args_list], ['market','market','full'])
            with self.assertRaises(HTTPException):
                await get_dashboard(Request({'type':'http','headers':[]}),view='unknown')
