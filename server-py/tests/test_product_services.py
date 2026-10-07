import copy
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import httpx
from fastapi import FastAPI
from app import developer_access as access, product_filters as filters, product_wallet as wallet, product_social as social
from app.api import developer, product_services

TOKEN='0x'+'a'*40
STOCK='0x'+'b'*40
POOL='0x'+'c'*40
NOW=1_800_000_000_000


def payload():
    return {'unified': {'assets': [{'chainId':'56','token':TOKEN,'symbol':'Q','name':'Q币','price':2,'priceCurrency':'USD','liquidityUsd':10000,'volume24h':20000,'volumeCurrency':'USD','fieldTimes':{'price':NOW-1000,'liquidityUsd':NOW-1000,'volume24h':NOW-1000},'riskAssessment':{'safety':{'tax':{'status':'clear'}}}}],
        'stockTokens': [{'chainId':'56','tokenContractAddress':STOCK,'tokenSymbol':'NVDAx','stockCode':'NVDA','verificationStatus':'official','price':5,'priceCurrency':'USD','fieldTimes':{'price':NOW}}],
        'relations': [{'chainId':'56','token':TOKEN,'pool':POOL,'ticker':'700','level':'A','status':'verified','confirmationStatus':'confirmed','poolCreatedAt':NOW-3_600_000,'liquidityUsd':10000}]}}


class FilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_inspectable_conditions_percent_and_unrecognized(self):
        with patch.object(filters,'structured_model',AsyncMock(return_value=None)):
            parsed=await filters.parse_filters('腾讯相关，BNB 链，流动性大于 1 万美元，卖出 1000 美元冲击小于 5%，明天翻倍')
        self.assertEqual(parsed['source'],'rules')
        self.assertEqual(parsed['filters'],[{'field':'stock','op':'=','value':'700'},{'field':'chainId','op':'=','value':'56'},{'field':'liquidityUsd','op':'>','value':10000.0},{'field':'exitImpact1k','op':'<','value':.05}])
        self.assertEqual(parsed['ignored'],['明天翻倍'])
        self.assertTrue(parsed['requiresConfirmation'])

    async def test_schema_rejects_sql_unknown_fields_boolean_and_nan(self):
        for item in ({'field':'password','op':'=','value':1},{'field':'volume24hUsd','op':'DROP TABLE','value':1},
            {'field':'liquidityUsd','op':'>','value':True},{'field':'change24h','op':'<','value':float('nan')},
            {'field':'stock','op':'=','value':'NVDA; DROP'},{'field':'chainId','op':'>','value':56}):
            with self.assertRaises(access.AccessError): filters.validate_filters([item])
        with self.assertRaises(access.AccessError):await filters.execute_filters([],False)

    async def test_rules_filter_real_values_and_exclude_unknown_and_stale(self):
        conditions=[{'field':'stock','op':'=','value':'700'},{'field':'liquidityUsd','op':'>','value':1000}]
        with patch('app.product_metrics.v2_exit_impact',return_value={'status':'unsupported'}):
            self.assertEqual(len(filters.filter_rows(payload(),conditions,NOW)),1)
            stale=payload();stale['unified']['assets'][0]['fieldTimes']['liquidityUsd']=NOW-180001
            self.assertEqual(filters.filter_rows(stale,conditions,NOW),[])
            missing=payload();missing['unified']['assets'][0]['liquidityUsd']=None
            self.assertEqual(filters.filter_rows(missing,conditions,NOW),[])
            only_name=payload();only_name['unified']['relations'][0]['level']='B'
            self.assertEqual(filters.filter_rows(only_name,conditions,NOW),[])


class WalletTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_chain_is_not_empty_or_zero(self):
        with patch.dict(os.environ,{f'BLOCKSCOUT_BASE_{c}':'' for c in wallet.CHAINS}):
            result=await wallet.fetch_balances('56',TOKEN,AsyncMock())
            self.assertEqual(result['status'],'not-covered')
            self.assertFalse(wallet.capabilities()['available'])
        profile=wallet.build_profile(TOKEN,[result],payload(),NOW)
        self.assertIsNone(profile['metrics']['totalValueUsd'])
        self.assertTrue(profile['incomplete'])
        self.assertIsNone(profile['chains'][0]['valueUsd'])

    async def test_values_stock_tokens_unvalued_and_hhi_without_double_count(self):
        balances={'chainId':'56','status':'complete','source':'https://example.com','items':[
            {'token':{'address_hash':TOKEN,'symbol':'Q','decimals':'18','type':'ERC-20'},'value':str(10**19)},
            {'token':{'address_hash':STOCK,'symbol':'NVDAx','decimals':'18','type':'ERC-20'},'value':str(10**18)},
            {'token':{'address_hash':'0x'+'d'*40,'symbol':'Unknown','decimals':'2','type':'ERC-20'},'value':'100'}]}
        with patch('app.product_metrics.v2_exit_impact',return_value={'status':'current','valuePercent':12}):
            result=wallet.build_profile(TOKEN,[balances],payload(),NOW)
        self.assertEqual(result['metrics']['totalValueUsd'],25)
        self.assertEqual(result['metrics']['stockCount'],1)
        self.assertEqual(result['metrics']['stockShare'],.2)
        self.assertEqual(result['metrics']['memeShare'],.8)
        self.assertAlmostEqual(result['metrics']['themeHHI'],.68)
        self.assertEqual(result['unvalued'][0]['quantity'],'1')
        self.assertTrue(result['sessionOnly']);self.assertFalse(result['saved'])

    async def test_stale_prices_are_unvalued_and_bad_decimals_not_guessed(self):
        p=payload();p['unified']['assets'][0]['fieldTimes']['price']=NOW-180001
        balances={'chainId':'56','status':'complete','items':[{'token':{'address_hash':TOKEN,'decimals':'18'},'value':'1000000000000000000'}]}
        result=wallet.build_profile(TOKEN,[balances],p,NOW)
        self.assertEqual(result['metrics']['unvaluedCount'],1)
        self.assertIsNone(wallet._quantity({'token':{'decimals':True},'value':'1'}))

    async def test_fetch_boundaries_truncation_and_no_redirect(self):
        client=AsyncMock();client.get.return_value=httpx.Response(200,json={'items':[],'next_page_params':{'id':1}},request=httpx.Request('GET','https://example.com'))
        with patch.dict(os.environ,{'BLOCKSCOUT_BASE_56':'https://example.com','BLOCKSCOUT_API_KEY':''}):
            result=await wallet.fetch_balances('56',TOKEN,client)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(client.get.await_count,wallet.MAX_PAGES)


class ProductApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'DEVELOPER_DB_PATH':self.tmp.name+'/users.sqlite','DEVELOPER_SHARED_INVITE_CODE':'test','DEVELOPER_COOKIE_SECURE':'0','PRODUCT_KOL_ACCOUNTS_JSON':'[]','X_BEARER_TOKEN':'','PRODUCT_X_READ_ENABLED':'','PRODUCT_X_AUTOPOLL_ENABLED':'','PRODUCT_X_BILLING_UNIT':'','PRODUCT_FILTER_MODEL_ENABLED':'','RADAR_OPERATOR_USER_IDS':''})
        self.env.start();product_services._rates.clear();social._alias_cache=(0,[])
        with patch.object(access,'_password_hash',return_value=b'test-hash'):
            self.user,self.cookie,self.csrf=access.register('test@example.com','test','secure password 12345','one')
        self.app=FastAPI();self.app.include_router(product_services.router,prefix='/api/v2')
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://testserver')
        self.headers={'X-CSRF-Token':self.csrf,'Origin':'https://cliperx.com'}
    async def asyncTearDown(self):
        await self.client.aclose();self.env.stop();self.tmp.cleanup();social._alias_cache=(0,[])
    def login(self): self.client.cookies.set(developer.COOKIE_NAME,self.cookie)

    async def test_wallet_never_saves_on_query_and_explicit_save_requires_consent_csrf(self):
        with patch.object(wallet,'wallet_profile',AsyncMock(return_value={'address':TOKEN,'saved':False})):
            response=await self.client.post('/api/v2/product/wallet/profile',json={'address':TOKEN})
        self.assertFalse(response.json()['saved']);self.login()
        self.assertEqual((await self.client.get('/api/v2/product/wallet/saved')).json()['items'],[])
        body={'address':TOKEN,'label':'Mine','consent':True}
        self.assertEqual((await self.client.post('/api/v2/product/wallet/saved',json=body)).status_code,403)
        self.assertEqual((await self.client.post('/api/v2/product/wallet/saved',json={**body,'consent':False},headers=self.headers)).status_code,400)
        saved=await self.client.post('/api/v2/product/wallet/saved',json=body,headers=self.headers)
        self.assertEqual(saved.json()['items'][0]['address'],TOKEN)
        self.assertEqual(saved.headers['cache-control'],'no-store')
        deleted=await self.client.delete('/api/v2/product/wallet/saved/'+TOKEN,headers=self.headers)
        self.assertEqual(deleted.json()['items'],[])

    async def test_parse_never_executes_then_execute_requires_confirmation(self):
        with patch.object(filters,'structured_model',AsyncMock(return_value=None)):
            response=await self.client.post('/api/v2/product/filters/parse',json={'text':'BNB链'})
        self.assertEqual(response.json()['filters'][0]['value'],'56')
        denied=await self.client.post('/api/v2/product/filters/execute',json={'filters':response.json()['filters'],'confirmed':False})
        self.assertEqual(denied.status_code,400)

    async def test_disabled_sources_have_real_capabilities_and_no_provider_requests(self):
        with patch.dict(os.environ,{f'BLOCKSCOUT_BASE_{c}':'' for c in wallet.CHAINS}):
            result=(await self.client.get('/api/v2/product/capabilities')).json()
        self.assertFalse(result['wallet']['available']);self.assertFalse(result['kol']['available'])
        self.assertEqual(result['kol']['minimumRankingSamples'],10)
        self.assertFalse(result['assetFacts']['modelEnabled'])
        self.login()
        with patch('httpx.AsyncClient.get',AsyncMock()) as provider:
            data=await social.lookup('https://x.com/test/status/123456789')
        self.assertEqual(data['reason'],'x-not-configured');provider.assert_not_awaited()

    async def test_alias_review_operator_only_whole_word_and_backtest(self):
        self.login()
        body={'ticker':'700','sourceText':'独特小企鹅','alias':'独特小企鹅'}
        denied=await self.client.post('/api/v2/product/aliases',json=body,headers=self.headers)
        self.assertEqual(denied.status_code,403)
        with patch.dict(os.environ,{'RADAR_OPERATOR_USER_IDS':self.user['id']}):
            result=await self.client.post('/api/v2/product/aliases',json=body,headers=self.headers)
            self.assertEqual(result.status_code,200);candidate=result.json()['items'][0]
            self.assertEqual(social.alias_matches('独特小企鹅',social.alias_candidates()),[])
            confirmed=await self.client.post(f"/api/v2/product/aliases/{candidate['id']}/confirm",json={'approved':True},headers=self.headers)
            self.assertEqual(confirmed.status_code,200)
            self.assertEqual(social.alias_matches('独特小企鹅',social.alias_candidates()),['700'])
            p=payload();p['unified']['assets'][0]['name']='独特小企鹅'
            score=social.backtest_aliases(p)
            self.assertEqual((score['precision'],score['recall']),(1,1))
            from app.stock_identity import match_name
            self.assertEqual(match_name('UNIQUE','独特小企鹅')['level'],'B')
            await self.client.post(f"/api/v2/product/aliases/{candidate['id']}/confirm",json={'approved':False},headers=self.headers)
            self.assertIsNone(match_name('UNIQUE','独特小企鹅'))
        self.assertEqual(social.alias_matches('AMCAT',[{'ticker':'AMC','alias':'AMC','status':'confirmed'}]),[])

    async def test_post_extraction_ambiguous_symbols_not_performance(self):
        p=payload();duplicate=copy.deepcopy(p['unified']['assets'][0]);duplicate['token']='0x'+'e'*40;p['unified']['assets'].append(duplicate)
        result=social.extract_mentions('$Q is here',p)
        self.assertEqual(result['mentions'],[]);self.assertEqual(result['pending'][0]['reason'],'ambiguous-symbol')
        specific=social.extract_mentions(TOKEN,p)
        self.assertEqual(len(specific['mentions']),1);self.assertEqual(specific['mentions'][0]['confidence'],'contract')

    async def test_x_allowlist_and_budget_gate_before_network(self):
        social.save_account('test')
        with patch.dict(os.environ,{'X_BEARER_TOKEN':'unit-test','PRODUCT_X_READ_ENABLED':'1','PRODUCT_X_DAILY_READ_LIMIT':'1'}),patch('httpx.AsyncClient.get',AsyncMock()) as provider:
            with self.assertRaises(access.AccessError):await social.lookup('https://x.com/unlisted/status/12345678')
            with access.connection() as db:
                from datetime import datetime,timezone
                db.execute('INSERT INTO product_x_budget VALUES (?,1)',(datetime.now(timezone.utc).strftime('%Y-%m-%d'),))
            with self.assertRaises(access.AccessError) as caught:await social.lookup('https://x.com/test/status/12345678')
            self.assertEqual(caught.exception.code,'x-daily-budget-exhausted');provider.assert_not_awaited()

    async def test_x_post_cache_derived_storage_and_deletion(self):
        social.save_account('test')
        posted=int(time.time()*1000)-3_600_000
        from datetime import datetime,timezone
        packet={'data':{'id':'123456789','author_id':'1','created_at':datetime.fromtimestamp(posted/1000,timezone.utc).isoformat(),'text':'Original secret text $Q only here'},'includes':{'users':[{'id':'1','username':'test'}]}}
        good=httpx.Response(200,json=packet,request=httpx.Request('GET','https://api.x.com/2/tweets/123456789'))
        price={'mentionPriceUsd':2,'priceAt':posted,'priceStatus':'precise','returns':{'1h':.2,'24h':None,'7d':None},'maxMultiple':1.2}
        with patch.dict(os.environ,{'X_BEARER_TOKEN':'unit-test','PRODUCT_X_READ_ENABLED':'1'}), \
             patch('httpx.AsyncClient.get',AsyncMock(return_value=good)) as provider, \
             patch.object(social,'read_projection_json',AsyncMock(return_value=json.dumps(payload()))), \
             patch.object(social,'_prices',AsyncMock(return_value=price)):
            first=await social.lookup('https://x.com/test/status/123456789')
            cached=await social.lookup('https://x.com/test/status/123456789')
            self.assertEqual(provider.await_count,1)
            self.assertTrue(cached['cached'])
            self.assertEqual(len(first['items']),1)
            with access.connection() as db:
                stored=db.execute('SELECT body FROM product_mentions').fetchone()[0]
                self.assertNotIn('Original secret',stored)
                db.execute('UPDATE product_x_reads SET checked_at=0')
            provider.return_value=httpx.Response(404,json={'errors':[]},request=httpx.Request('GET','https://api.x.com/2/tweets/123456789'))
            deleted=await social.lookup('https://x.com/test/status/123456789')
            self.assertTrue(deleted['deleted'])
            self.assertEqual((await social.mentions())['items'],[])

    async def test_kol_ranking_dedupes_each_asset_and_enforces_ten_samples(self):
        social.save_account('test')
        posted=int(time.time()*1000)-2*86_400_000
        records=[]
        for index in range(10):
            key='56:0x'+f'{index:040x}'
            record={'postId':str(10000+index),'assetKey':key,'handle':'test','postedAt':posted+index,'priceStatus':'precise','returns':{'24h':.5}}
            records.append(record)
        # Repeated mention of the first asset must not increase n.
        duplicate={**records[0],'postId':'99999','postedAt':posted+100}
        with access.connection() as db:
            for record in records[:9]+[duplicate]:
                db.execute('INSERT INTO product_mentions VALUES (?,?,?,?,?,?)',(record['postId'],'test',record['assetKey'],record['postedAt'],json.dumps(record),posted))
        with patch.object(social,'_prices',AsyncMock(return_value={'priceStatus':'precise','returns':{'24h':.5}})):
            self.assertEqual((await social.mentions())['ranking'],[])
            with access.connection() as db:
                record=records[9]
                db.execute('INSERT INTO product_mentions VALUES (?,?,?,?,?,?)',(record['postId'],'test',record['assetKey'],record['postedAt'],json.dumps(record),posted))
            ranked=(await social.mentions())['ranking'][0]
            self.assertEqual(ranked['samples'],10)
            self.assertEqual(ranked['medianReturn24h'],.5)
            self.assertEqual(ranked['winRate'],1)
            social.save_account('test',False)
            self.assertEqual((await social.mentions())['ranking'],[])


class KolAutopollTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'DEVELOPER_DB_PATH':self.tmp.name+'/users.sqlite',
            'PRODUCT_KOL_ACCOUNTS_JSON':json.dumps([{'handle':'test','id':'1'}]),
            'X_BEARER_TOKEN':'unit-test','PRODUCT_X_READ_ENABLED':'1','PRODUCT_X_AUTOPOLL_ENABLED':'1',
            'PRODUCT_X_BILLING_UNIT':'post-resource','PRODUCT_X_DAILY_READ_LIMIT':'20'})
        self.env.start()
        self.clock=patch.object(social,'now_ms',return_value=NOW);self.clock.start()
        social.init()
    async def asyncTearDown(self):
        self.clock.stop();self.env.stop();self.tmp.cleanup()

    def response(self, data=None, *, token=None, status=200, headers=None):
        return httpx.Response(status,json={'data':data if data is not None else [],'meta':{'next_token':token} if token else {}},
            headers=headers,request=httpx.Request('GET','https://api.x.com/2/test'))

    def post(self, ident='10003', author='1'):
        return {'id':ident,'author_id':author,'created_at':datetime.fromtimestamp((NOW-1000)/1000,timezone.utc).isoformat(),
            'text':'Private source text only here $Q'}

    async def test_default_switch_billing_and_explicit_id_gate_make_no_calls(self):
        with patch('httpx.AsyncClient.get',AsyncMock()) as provider:
            for values,reason in [({'PRODUCT_X_AUTOPOLL_ENABLED':''},'autopoll-disabled'),
                    ({'PRODUCT_X_BILLING_UNIT':''},'x-billing-unit-unconfirmed'),
                    ({'PRODUCT_X_READ_ENABLED':''},'x-not-configured')]:
                with patch.dict(os.environ,values):
                    result=await social.poll()
                    self.assertEqual(result['reason'],reason);self.assertEqual(result['requested'],0)
            with access.connection() as db:db.execute('UPDATE product_kol_accounts SET x_id=NULL')
            result=await social.poll()
            self.assertEqual(result['requested'],0);provider.assert_not_awaited()

    async def test_invalid_account_id_cannot_advertise_automatic_capability(self):
        for ident in ('abc','0','0001',None):
            with access.connection() as db:db.execute('UPDATE product_kol_accounts SET x_id=?',(ident,))
            capabilities=social.accounts()['automatic']
            self.assertFalse(capabilities['available'])
            self.assertEqual(capabilities['reason'],'explicit-account-ids-required')
        with access.connection() as db:db.execute("UPDATE product_kol_accounts SET x_id='1'")
        self.assertTrue(social.accounts()['automatic']['available'])

    async def test_pages_cursor_restart_and_derived_facts_are_atomic_and_idempotent(self):
        price={'priceStatus':'precise','returns':{'24h':None},'mentionPriceUsd':2}
        with patch('httpx.AsyncClient.get',AsyncMock(side_effect=[self.response([self.post()],token='page2'),self.response([self.post('10002')])])) as provider, \
             patch.object(social,'read_projection_json',AsyncMock(return_value=json.dumps(payload()))), \
             patch.object(social,'_prices',AsyncMock(return_value=price)):
            first=await social.poll()
            self.assertEqual((first['requested'],first['accepted'],first['reservedReadUnits']),(1,1,5))
            with access.connection() as db:
                cursor=dict(db.execute('SELECT * FROM product_x_cursors').fetchone())
                self.assertIsNone(cursor['since_id']);self.assertEqual(cursor['page_token'],'page2')
                self.assertEqual(cursor['pending_newest'],'10003')
            # Reinitialize the same persisted DB as a restarted worker would.
            social._initialized.discard(str(access._path().resolve()));social.init()
            second=await social.poll()
            self.assertEqual(second['accepted'],1)
            self.assertEqual(provider.await_args.kwargs['params']['pagination_token'],'page2')
            with access.connection() as db:
                cursor=dict(db.execute('SELECT * FROM product_x_cursors').fetchone())
                self.assertEqual(cursor['since_id'],'10003');self.assertIsNone(cursor['page_token'])
                self.assertEqual(db.execute('SELECT SUM(used) FROM product_x_budget').fetchone()[0],10)
                self.assertEqual(db.execute('SELECT count(*) FROM product_mentions').fetchone()[0],2)
                self.assertNotIn('Private source',str([row[0] for row in db.execute('SELECT body FROM product_mentions')]))
            await social.poll();self.assertEqual(provider.await_count,2)
            with patch.object(social,'now_ms',return_value=NOW+3_600_001):
                provider.side_effect=None;provider.return_value=self.response([self.post('10004')])
                await social.poll()
                self.assertEqual(provider.await_args.kwargs['params']['since_id'],'10003')

    async def test_request_reserves_maximum_posts_and_never_increases_twenty_unit_cap(self):
        day=datetime.fromtimestamp(NOW/1000,timezone.utc).strftime('%Y-%m-%d')
        with access.connection() as db:db.execute('INSERT INTO product_x_budget VALUES (?,17)',(day,))
        with patch.dict(os.environ,{'PRODUCT_X_DAILY_READ_LIMIT':'1000'}),patch('httpx.AsyncClient.get',AsyncMock()) as provider:
            result=await social.poll()
            self.assertEqual(result['requested'],0);provider.assert_not_awaited()
        with access.connection() as db:self.assertEqual(db.execute('SELECT used FROM product_x_budget').fetchone()[0],17)

    async def test_failed_or_wrong_author_page_does_not_advance_or_refund(self):
        for packet in (self.response([self.post(author='2')]),self.response(status=500)):
            with access.connection() as db:db.execute('DELETE FROM product_x_cursors')
            with patch('httpx.AsyncClient.get',AsyncMock(return_value=packet)),patch.object(social,'read_projection_json',AsyncMock(return_value=json.dumps(payload()))):
                result=await social.poll()
                self.assertEqual(result['failed'],1)
            with access.connection() as db:
                row=dict(db.execute('SELECT * FROM product_x_cursors').fetchone())
                self.assertIsNone(row['since_id']);self.assertIsNone(row['page_token']);self.assertEqual(row['lease_until'],0)
                self.assertEqual(db.execute('SELECT count(*) FROM product_mentions').fetchone()[0],0)
        with access.connection() as db:self.assertEqual(db.execute('SELECT SUM(used) FROM product_x_budget').fetchone()[0],10)

    async def test_unauthorized_forbidden_and_rate_limit_persist_global_backoff(self):
        for status in (401,403,429):
            with access.connection() as db:
                db.execute('DELETE FROM product_x_cursors');db.execute('DELETE FROM product_x_poll_control');db.execute('DELETE FROM product_x_budget')
            with patch('httpx.AsyncClient.get',AsyncMock(return_value=self.response(status=status,headers={'Retry-After':'120'}))) as provider:
                first=await social.poll();second=await social.poll()
                self.assertEqual(first['failed'],1);self.assertEqual(second['requested'],0);self.assertEqual(provider.await_count,1)
                self.assertEqual(first['reason'],'x-http-'+str(status))
            with access.connection() as db:
                self.assertGreater(db.execute('SELECT until_at FROM product_x_poll_control').fetchone()[0],NOW)
                self.assertEqual(db.execute('SELECT used FROM product_x_budget').fetchone()[0],5)

    async def test_deletion_recheck_obeys_shared_budget_and_removes_record(self):
        record={'postId':'10003','assetKey':'56:'+TOKEN,'postedAt':NOW-2*86_400_000,'handle':'test'}
        with access.connection() as db:
            db.execute('INSERT INTO product_mentions VALUES (?,?,?,?,?,?)',('10003','test','56:'+TOKEN,record['postedAt'],json.dumps(record),NOW))
            db.execute('INSERT INTO product_x_reads VALUES (?,?,?,?)',('10003','test',NOW-86_400_001,'read'))
        with patch('httpx.AsyncClient.get',AsyncMock(side_effect=[self.response(),self.response(status=404)])) as provider:
            result=await social.poll()
            self.assertEqual((result['requested'],result['accepted'],result['reservedReadUnits']),(2,2,6))
            self.assertTrue(provider.await_args.args[0].endswith('/tweets/10003'))
        with access.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM product_mentions').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT state FROM product_x_reads').fetchone()[0],'deleted')
            self.assertEqual(db.execute('SELECT used FROM product_x_budget').fetchone()[0],6)
