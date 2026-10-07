import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import stock_quotes
from app.api import product_v2, live_market
from app.collectors.equity_market import normalize_quotes, normalize_history, normalize_tencent_history
from app.collectors import equity_market
from app.equity_identity import equity_identity, clock_session

NOW = 1790991575394  # Saturday, 2026-10-03 09:39 Shanghai.
KUAI = '0xfa8a6a57fc83e416cf45b04b7b92b4f48da9e7b8'
MEME = '0x'+'a'*40
POOL = '0x'+'b'*40


def reference_text(code='01024', currency='HKD', stamp='2026/10/02 16:08:16'):
    fields = ['']*74
    for index, value in {0:'100', 1:'快手-W', 2:code, 3:'29.580', 4:'30.940', 5:'30.400',
                         30:stamp, 32:'-4.40', 33:'30.400', 34:'29.360', 36:'23238630.0',
                         37:'687076150.620', 71:currency}.items():
        fields[index] = value
    return 'v_hk'+code+'="'+'~'.join(fields)+'";'


def stock():
    return {'chainId':'196', 'tokenContractAddress':KUAI, 'stockCode':'1024', 'tokenSymbol':'KUAIx',
            'price':3.766, 'priceCurrency':'USD', 'priceScope':'dex', 'quoteAt':NOW-20000,
            'volume24h':2053, 'fieldTimes':{'price':NOW-20000}}


class EquityContracts(unittest.TestCase):
    def test_hk_identity_comes_from_shared_mapping_and_issuer_deployment(self):
        identity = equity_identity(stock())
        self.assertEqual((identity['id'], identity['market'], identity['currency']), ('XHKG:01024','HKEX','HKD'))
        self.assertEqual(equity_identity({**stock(), 'stockCode':'9999', 'tokenSymbol':'WRONG'})['id'], 'XHKG:01024')
        for row in ({'stockCode':'1024'}, {'stockCode':'9999','tokenSymbol':'KUAIX'},
                    {'stockCode':'1024','tokenSymbol':'WRONG'}):
            self.assertIsNone(equity_identity(row)['market'])
        self.assertEqual(equity_identity({'stockCode':'9992','tokenSymbol':'POPMTx'})['id'], 'XHKG:09992')

    def test_public_reference_keeps_exchange_time_currency_and_session_volume(self):
        q = normalize_quotes(reference_text(), ['1024'], NOW)['XHKG:01024']
        self.assertEqual(q['price'],29.58)
        self.assertEqual(q['currency'],'HKD')
        self.assertEqual(q['volume'],23238630)
        self.assertEqual(q['turnover'],687076150.62)
        self.assertLess(q['marketAt'],q['observedAt'])
        self.assertFalse(q['realtime'])
        self.assertIsNone(q['delayMs'])
        self.assertFalse(normalize_quotes(reference_text(currency='USD'), ['1024'],NOW))
        self.assertFalse(normalize_quotes(reference_text(stamp='2026/10/04 16:00:00'), ['1024'],NOW))
        self.assertFalse(normalize_quotes(reference_text(code='09992'), ['1024'],NOW))

    def test_unadjusted_history_checks_security_and_real_ohlc(self):
        body={'data':{'market':116,'code':'01024','klines':[
            '2026-10-02,30.4,29.58,30.4,29.36,23238630,687076150.62',
            '2026-10-01,30,31,29,28,100,3000',
            '2026-10-04,30,30,31,29,100,3000']}}
        result=normalize_history(body,'1024',NOW)
        self.assertEqual(len(result['rows']),1)
        self.assertEqual(result['rows'][0]['c'],29.58)
        self.assertEqual(result['adjustment'],'unadjusted')
        self.assertFalse(result['realtime'])
        for change in ({'market':0},{'code':'09992'}):
            with self.assertRaises(ValueError):normalize_history({'data':{**body['data'],**change}},'1024',NOW)

    def test_overlay_matches_hk_reference_without_confusing_token_and_equity(self):
        public=normalize_quotes(reference_text(),['1024'],NOW)
        def snapshot(path):
            if path==stock_quotes.EQUITY_FILE:return {'quotes':public}
            return {'providers':{'EODHD':{'status':'entitlement-required'}}}
        row=stock()
        with patch.object(stock_quotes,'_read_snapshot',side_effect=snapshot), \
             patch.object(stock_quotes,'binance_live',return_value={}), \
             patch.object(stock_quotes,'robinhood_live',return_value={}), \
             patch.object(stock_quotes,'_now',return_value=NOW):
            result=stock_quotes.apply_stock_overlays([row])[0]
        self.assertNotIn('stockIdentity',row)
        self.assertEqual(result['price'],3.766)
        self.assertEqual(result['volume24h'],2053)
        self.assertEqual(result['stockPrice'],29.58)
        self.assertEqual(result['referenceTurnover'],687076150.62)
        self.assertEqual(result['referenceCurrency'],'HKD')
        self.assertEqual(result['referenceProvider'],'Tencent Finance')
        self.assertFalse(result['referenceRealtime'])
        self.assertEqual(product_v2.market_status([result],NOW)['status'],'closed')
        self.assertIsNone(result['premium']['value'])

    def test_tencent_history_requires_matching_hk_security_and_unadjusted_series(self):
        quote=reference_text().split('"')[1].split('~')
        data={'day':[['2026-10-02','30.4','29.58','30.4','29.36','23238630']],
              'qt':{'hk01024':quote}}
        body={'code':0,'data':{'hk01024':data}}
        result=normalize_tencent_history(body,'1024',NOW)
        self.assertEqual(result['provider'],'Tencent Finance')
        self.assertEqual(result['rows'][0]['volumeShares'],23238630)
        self.assertIsNone(result['rows'][0]['turnover'])
        self.assertEqual(result['adjustment'],'unadjusted')
        for bad in ([],{'code':0,'data':[]}, {**body,'code':1},
                    {'code':0,'data':{'hk01024':{**data,'day':None,'qfqday':data['day']}}},
                    {'code':0,'data':{'hk01024':{**data,'qt':{'hk01024':["100","wrong","09992"]}}}}):
            with self.assertRaises(ValueError):normalize_tencent_history(bad,'1024',NOW)

    def test_weekend_session_is_separate_from_old_quote_freshness(self):
        session=clock_session({'market':'HKEX'},NOW)
        self.assertEqual(session['reason'],'weekend')
        quote={'referenceAt':NOW-40*86400000,'marketSession':'closed','marketSessionAt':NOW,
               'marketSessionSource':'exchange-clock'}
        self.assertEqual(product_v2.market_status([quote],NOW)['source'],'exchange-clock')
        self.assertEqual(product_v2.market_status([{**quote,'marketSessionAt':None}],NOW)['status'],'unknown')

    def test_address_verified_wrapper_is_exposed_as_separate_trading_identity(self):
        from app.stock_identity import _manifest
        _,index=_manifest()
        wrapper=next(identity for (chain,_),identity in index.items() if chain=='196' and
                     identity['ticker']=='1024' and identity['tokenKind']=='wrapper-current')
        row={'chainId':'196','stock':KUAI,'stockSide':wrapper['address'],'token0':MEME,
             'token1':wrapper['address'],'pool':POOL,'status':'verified','liquidityUsd':None}
        result=product_v2.stock_trading_markets([row])
        self.assertEqual(result[0]['token'],wrapper['address'])
        self.assertEqual(result[0]['nativeToken'],KUAI)
        self.assertEqual(result[0]['symbol'],'wKUAIx')
        self.assertFalse(product_v2.stock_trading_markets([{**row,'confirmationStatus':'orphaned'}]))
        self.assertFalse(product_v2.stock_trading_markets([{**row,'stockSide':MEME}]))


class EquityReads(unittest.IsolatedAsyncioTestCase):
    async def test_cached_history_does_not_block_retry_of_unavailable_securities(self):
        import httpx
        client=AsyncMock();client.get.side_effect=httpx.ConnectError('fixture unavailable')
        context=AsyncMock();context.__aenter__.return_value=client
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'equity.json'
            cached={'XHKG:'+code.zfill(5):{'observedAt':NOW} for code in equity_market.hk_securities() if code not in ('1','1038')}
            path.write_text(json.dumps({'history':cached,
                                      'attempts':{'XHKG:00001':{'at':NOW-61000,'status':'unavailable','provider':'Tencent Finance'}}}))
            with patch.object(equity_market,'SNAPSHOT',str(path)), \
                 patch.object(equity_market.httpx,'AsyncClient',return_value=context), \
                 patch.object(equity_market.time,'time',return_value=NOW/1000):
                await equity_market.refresh_equity_market()
            attempts=json.loads(path.read_text())['attempts']
            self.assertEqual(attempts['XHKG:00001']['at'],NOW)
            self.assertEqual(attempts['XHKG:00001']['provider'],'Eastmoney')
            self.assertEqual(attempts['XHKG:01038']['provider'],'Tencent Finance')
            self.assertEqual(client.get.call_count,3)

    async def test_provider_failure_retains_history_and_rotation_reaches_other_securities(self):
        import httpx
        client=AsyncMock()
        client.get.side_effect=httpx.ConnectError('fixture unavailable')
        context=AsyncMock()
        context.__aenter__.return_value=client
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'equity.json'
            stored={'quotes':{'XHKG:01024':{'price':29.58,'marketAt':NOW-86400000}},'history':{},'attempts':{}}
            path.write_text(json.dumps(stored))
            with patch.object(equity_market,'SNAPSHOT',str(path)), \
                 patch.object(equity_market.httpx,'AsyncClient',return_value=context), \
                 patch.object(equity_market.time,'time',return_value=NOW/1000):
                await equity_market.refresh_equity_market()
                first=json.loads(path.read_text())
                self.assertEqual(first['quotes'],stored['quotes'])
                self.assertEqual(first['attempts']['quotes']['status'],'unavailable')
                self.assertIn('XHKG:01024',first['attempts'])
                self.assertIn('XHKG:09992',first['attempts'])
            with patch.object(equity_market,'SNAPSHOT',str(path)), \
                 patch.object(equity_market.httpx,'AsyncClient',return_value=context), \
                 patch.object(equity_market.time,'time',return_value=(NOW+61000)/1000):
                await equity_market.refresh_equity_market()
                later=json.loads(path.read_text())
                self.assertIn('XHKG:00001',later['attempts'])
                self.assertIn('XHKG:01038',later['attempts'])
                self.assertEqual(later['quotes'],stored['quotes'])

    async def test_equity_history_endpoint_returns_only_this_security_and_window(self):
        payload={'unified':{'stockTokens':[{**stock(),'stockIdentity':equity_identity(stock())}],
                            'assets':[],'relations':[]}}
        history={'id':'XHKG:01024','symbol':'1024.HK','currency':'HKD','provider':'Eastmoney',
                 'rows':[{'t':NOW-86400000,'o':30,'h':31,'l':29,'c':30},
                         {'t':NOW-100*86400000,'o':30,'h':31,'l':29,'c':30}]}
        with patch.object(product_v2,'published',AsyncMock(return_value=payload)), \
             patch.object(stock_quotes,'_read_snapshot',return_value={'history':{'XHKG:01024':history}}), \
             patch.object(product_v2.time,'time',return_value=NOW/1000):
            result=await product_v2.equity_chart('1024',range='1m')
            self.assertEqual(len(result['rows']),1)
            self.assertEqual(result['currency'],'HKD')
            self.assertEqual((await product_v2.equity_chart('9992'))['rows'],[])

    async def test_pool_wakeup_requires_a_real_matching_catalogue_record(self):
        from app.db import ResearchStore
        with tempfile.TemporaryDirectory() as folder:
            s=await ResearchStore(str(Path(folder)/'research.sqlite'),'196').connect()
            try:
                row={'pool':POOL,'token0':KUAI,'token1':MEME,'checkedAt':NOW,'protocol':'Uniswap V2'}
                await s.put('pool',POOL,row)
                with patch.object(live_market,'catalogue_db_path',return_value=s.path):
                    self.assertTrue(await live_market.catalogue_pool_hint('196',KUAI,POOL))
                    self.assertFalse(await live_market.catalogue_pool_hint('56',KUAI,POOL))
                    self.assertFalse(await live_market.catalogue_pool_hint('196','0x'+'f'*40,POOL))
                    await s.put('pool',POOL,{**row,'creationStatus':'orphaned'})
                    self.assertFalse(await live_market.catalogue_pool_hint('196',KUAI,POOL))
            finally:await s.close()
