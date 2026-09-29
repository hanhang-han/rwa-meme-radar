import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.comparisons import stock_premium, relative_point, relative_return, pool_spread
from app.comparison_alerts import advance
from app.comparison_service import at_reference, record, subject, select_independent_quotes, read_comparison
from app.collectors.comparison_inputs import normalize_fx, native_pool_quotes, exchange_quotes, refresh_comparison_inputs
from app.db import ResearchStore
from tests.test_comparisons import stock, NOW


class ComparisonInputTest(unittest.TestCase):
    def test_provider_name_is_not_ratio_verification(self):
        self.assertEqual(stock_premium(stock(ratioVerified=False, ratioSource='OKX'), NOW)['reason'], 'unverified-ratio')
        self.assertEqual(stock_premium(stock(ratioAt=NOW-86400000*365, ratioValidUntil=NOW-1), NOW)['reason'], 'stale-ratio')
        self.assertEqual(stock_premium(stock(priceProvenance={'timeKind':'observed'}), NOW)['status'], 'snapshot')
        self.assertEqual(stock_premium(stock(referenceScope='issuer-reference'), NOW)['reason'], 'issuer-reference')

    def test_fx_never_assumes_stablecoin_parity(self):
        fx = normalize_fx({'data':{'currency':'USD','rates':{'USDT':'1.01','HKD':'7.8'}}}, NOW)
        self.assertEqual(fx['USDT']['timeKind'], 'observed')
        self.assertAlmostEqual(fx['USDT']['rate'], 1/1.01)
        q = stock(price=102*1.01, priceCurrency='USDT', quoteFx=fx['USDT'])
        self.assertAlmostEqual(stock_premium(q, NOW)['value'], 2)
        self.assertEqual(stock_premium(q, NOW)['status'], 'snapshot')
        with self.assertRaises(ValueError):
            normalize_fx({'data':{'currency':'EUR','rates':{'USDT':'1'}}}, NOW)

    def test_native_two_pool_evidence_requires_same_actual_currency(self):
        rel = {'chainId':'56','pool':'0xtarget','token':'0xmeme','stock':'0xstock','stockSide':'0xstock','status':'verified'}
        def p(token, pool, price, quote='0xquote'):
            return {'DexScreener':{'chainId':'56','pool':pool,'baseToken':{'address':token},'quoteToken':{'address':quote},
                                  'priceNative':str(price),'priceUsd':999,'observedAt':NOW}}
        snapshot = {'pools':{'56:0xothermeme':p('0xmeme','0xothermeme',.01), '56:0xotherstock':p('0xstock','0xotherstock',100)}}
        q = native_pool_quotes(snapshot)
        meme, side = select_independent_quotes(rel, q[('56','0xmeme')], q[('56','0xstock')], NOW)
        ratio = {**rel,'memePerStock':10200,'at':NOW,'timeKind':'market'}
        result = pool_spread(rel,ratio,meme,side,NOW)
        self.assertAlmostEqual(result['value'],2)
        self.assertEqual(result['status'],'snapshot')
        self.assertEqual(result['comparisonCurrency'],'asset:56:0xquote')
        self.assertIsNone(result['impliedPriceUsd'])
        self.assertEqual(pool_spread(rel,ratio,meme,{**side,'currency':'asset:56:0xothercurrency'},NOW)['reason'],'currency-mismatch')
        self.assertEqual(pool_spread(rel,ratio,{**meme,'dependencies':['0xtarget']},side,NOW)['reason'],'circular-price')
        # A legacy aggregate with no native pair proof does not produce a quote.
        self.assertEqual(native_pool_quotes({'pools':{'56:p':{'DexScreener':{'priceUsd':1}}}}),{})

    def test_exchange_quote_requires_fresh_observed_fx(self):
        fx=normalize_fx({'data':{'currency':'USD','rates':{'USDT':'1.01'}}},NOW)
        payload={'tokens':[{'chainIndex':'56','tokenContractAddress':'0xstock','tokenSymbol':'TESTB','price':102,'quoteAt':NOW}]}
        q=exchange_quotes(payload,fx,NOW)[('56','0xstock')][0]
        self.assertAlmostEqual(q['price'],102/1.01)
        self.assertEqual(q['dependencies'],[])
        self.assertEqual(exchange_quotes(payload,fx,NOW+300001),{})

    def test_relative_rejects_currency_and_unverified_corporate_actions(self):
        self.assertEqual(relative_point(stock(),{'price':1,'priceCurrency':'HKD','quoteAt':NOW},NOW)['reason'],'missing-token-currency')
        p=relative_point(stock(referenceAdjustmentVersion=None),{'price':1,'priceCurrency':'USD','quoteAt':NOW},NOW)
        self.assertEqual(relative_return(p,[],3600000)['reason'],'adjustment-unverified')

    def test_disconnect_resets_sustained_alert_window(self):
        def metric(at,n):
            return {'status':'realtime','value':3,'realtimeUntil':at+30000,'inputs':{'n':n}}
        st,_=advance(None,metric(NOW,1),2,NOW)
        st,_=advance(st,metric(NOW+10000,2),2,NOW+10000)
        st,_=advance(st,{'status':'unavailable'},2,NOW+20000)
        st,event=advance(st,metric(NOW+100000,3),2,NOW+100000)
        self.assertIsNone(event)
        self.assertEqual(st['count'],1)
        self.assertEqual(st['firstAt'],NOW+100000)
        st,event=advance(st,metric(NOW+200000,4),2,NOW+200000)
        self.assertIsNone(event)
        self.assertEqual(st['count'],1)

    def test_delayed_reference_uses_only_matching_observation(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                s=await ResearchStore(d+'/db','196').connect()
                try:
                    a=stock(tokenContractAddress='0xstock',price=999,referenceAt=NOW-1200000,referenceDelayMs=1200000,referenceRealtime=False)
                    q={'at':NOW-1200000,'quoteAt':NOW-1200000,'price':102,'provider':'OKX','priceScope':'dex','priceCurrency':'USD','priceProvenance':{'timeKind':'market'}}
                    await record(s,subject('0xstock')+':token',q)
                    aligned=await at_reference(s,a,a)
                    result=stock_premium(aligned,NOW)
                    self.assertAlmostEqual(result['value'],2)
                    self.assertEqual(result['status'],'delayed')
                    missing=await at_reference(s,{**a,'provider':'Other'},a)
                    self.assertEqual(missing['alignmentReason'],'missing-aligned-history')
                finally:
                    await s.close()
        asyncio.run(run())

    def test_production_input_path_persists_exact_quote_units(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                s=await ResearchStore(d+'/db','56').connect()
                try:
                    snapshot={'pools':{'56:p':{'DexScreener':{'chainId':'56','pool':'0xpool','baseToken':{'address':'0xbase'},'quoteToken':{'address':'0xquote'},'priceNative':'2','observedAt':NOW}}}}
                    fake=AsyncMock()
                    response=type('Response',(),{'raise_for_status':lambda _:None,'json':lambda _:{'data':{'currency':'USD','rates':{'USDT':'1.01'}}}})()
                    fake.__aenter__.return_value.get.return_value=response
                    with patch('app.collectors.comparison_inputs.store',AsyncMock(return_value=s)),patch('app.collectors.comparison_inputs.httpx.AsyncClient',return_value=fake),patch('app.collectors.comparison_inputs.time.time',return_value=NOW/1000),patch('app.collectors.comparison_inputs._read_snapshot',return_value=snapshot),patch('app.collectors.comparison_inputs.status_snapshot',return_value={'tokens':[]}):
                        result=await refresh_comparison_inputs()
                    self.assertGreater(result['accepted'],0)
                    saved=await s.get('independent-quote','0xbase')
                    self.assertEqual(saved['quotes'][0]['currency'],'asset:56:0xquote')
                    self.assertEqual((await s.get('fx-quote','USDT'))['rate'],1/1.01)
                finally:
                    await s.close()
        asyncio.run(run())

    def test_missing_unpaired_quote_invalidates_existing_projection_and_alert(self):
        from app.comparison_service import refresh_comparisons
        class Data:
            assets = []
            relations = []
            missing = False
            def stock_views(self, include_comparisons=True):
                return [stock(stockPrice=None) if self.missing else stock()]
        data = Data()
        async def run():
            with tempfile.TemporaryDirectory() as d:
                s=await ResearchStore(d+'/db','196').connect()
                try:
                    with patch('app.comparison_service.store',AsyncMock(return_value=s)),patch('app.comparison_service.DATA',data),patch('app.comparison_service.reload_data',AsyncMock()),patch('app.comparison_service.time.time',return_value=NOW/1000),patch('app.comparison_service.broadcast'):
                        await refresh_comparisons()
                        data.missing=True
                        await refresh_comparisons()
                    self.assertIsNone((await s.get('comparison','0xstock'))['premium']['value'])
                    self.assertTrue((await s.get('comparison-alert-state','0xstock:premium'))['gap'])
                finally:
                    await s.close()
        asyncio.run(run())

    def test_api_keeps_corporate_action_versions(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                s=await ResearchStore(d+'/db','196').connect()
                try:
                    from app.comparisons import VERSION
                    await s.put('comparison','0xstock',{'token':'0xstock','chainId':'196','method':VERSION,'premium':stock_premium(stock(),NOW),'pairs':[]})
                    point={'at':NOW,'stock':50,'meme':1,'inputs':{'adjustmentVersion':'split:2','memeCurrency':'USD','memeScope':'dex'}}
                    await record(s,subject('0xstock','0xpool')+':relative',point)
                    with patch('app.comparison_service.store',AsyncMock(return_value=s)),patch('app.comparison_service.time.time',return_value=NOW/1000):
                        response=await read_comparison('196','0xstock','0xpool')
                    self.assertEqual(response['relativeHistory'][0]['adjustmentVersion'],'split:2')
                finally:
                    await s.close()
        asyncio.run(run())

    def test_fx_overflow_and_underflow_fail_closed(self):
        for price, rate in ((1e308, 100), (1e-300, 1e-300)):
            row=stock(price=price,priceCurrency='USDT',quoteFx={'fromCurrency':'USDT','toCurrency':'USD','rate':rate,'provider':'FX','at':NOW})
            self.assertEqual(stock_premium(row,NOW)['reason'],'invalid-price')
            self.assertEqual(relative_point(stock(),row,NOW)['reason'],'invalid-price')

    def test_dashboard_uses_cached_independent_premium_beside_newer_issuer_reference(self):
        from app.state import DashboardData
        from app.comparisons import VERSION
        data=DashboardData()
        row=stock(referenceAt=NOW,referenceScope='issuer-reference',referenceProvider='Robinhood',
                  referenceObservations={'equity':{'price':100,'marketAt':NOW-1200000,'provider':'EODHD','currency':'USD','scope':'equity-exchange','identityVerified':True}})
        row['premium']={'value':None,'reason':'unaligned'}
        cached={'value':2,'status':'delayed','validUntil':NOW+300000,'inputs':{'referenceAt':NOW-1200000,'ratioVersion':row['ratioVersion']}}
        data.comparisons[('196','0xstock')]={'method':VERSION,'premium':cached}
        with patch('app.stock_quotes.apply_stock_overlays',side_effect=lambda _: [dict(row)]),patch('app.state.time.time',return_value=NOW/1000):
            self.assertEqual(data.stock_views()[0]['premium']['value'],2)
            row['referenceObservations']['equity']['marketAt']+=1000
            self.assertIsNone(data.stock_views()[0]['premium']['value'])
