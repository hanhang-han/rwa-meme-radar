import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from fastapi.responses import Response
from app import asset_logo_metadata as logos
from app.api import product_v2 as api

A = '0x'+'a'*40
B = '0x'+'b'*40
URL = 'https://static.oklink.com/a.png'


class LogoReads(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logos._cache.clear()
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE facts(kind TEXT,id TEXT,body TEXT,PRIMARY KEY(kind,id))')
        self.reads = []
        async def scoped(chain):
            result = AsyncMock()
            result.key = lambda kind: chain+':'+kind
            async def fetch(query, args):
                self.reads.append((query,args))
                return self.db.execute(query,args).fetchall()
            result.fetchall.side_effect = fetch
            return result
        self.mock_store = patch.object(logos, 'store', scoped)
        self.mock_store.start()

    def tearDown(self):
        self.mock_store.stop()
        self.db.close()
        logos._cache.clear()

    def fact(self, chain, address, url, kind='asset'):
        self.db.execute('INSERT OR REPLACE INTO facts VALUES(?,?,?)', (chain+':'+kind,address,json.dumps({'logoUrl':url,'evidence':'x'*10000})))

    def test_identity_validation_is_bounded_exact_and_deduplicated(self):
        self.assertEqual(logos.parse_ids('56:'+A.upper().replace('0X','0x')+',56:'+A), ['56:'+A])
        for bad in ['', '1:'+A, '56:NVDA', '56:'+A+' ', '56:'+A+",56:' OR 1=1", ','.join(['56:'+A]*51)]:
            with self.assertRaises(ValueError): logos.parse_ids(bad)
        self.assertEqual(len(logos.parse_ids(','.join('56:0x'+format(i,'040x') for i in range(50)))),50)

    def test_unsafe_or_malformed_image_urls_are_omitted(self):
        for bad in [None,{},'http://a.com/x','data:image/png,x','https://u:p@a.com/x','https://[bad/x','https://a.com/'+'x'*2048]:
            self.assertEqual(logos.safe_logo(bad),'')
        self.assertEqual(logos.safe_logo(URL),URL)

    async def test_reads_only_exact_contract_and_chain_and_keeps_responses_small(self):
        self.fact('56',A,URL)
        self.fact('196',A,'https://static.oklink.com/other.png','stock')
        self.fact('56',B,'javascript:alert(1)')
        result = await logos.asset_logos(['56:'+A,'196:'+A,'56:'+B])
        self.assertEqual(result, {'logos':{'56:'+A:URL,'196:'+A:'https://static.oklink.com/other.png'}})
        self.assertLess(len(json.dumps(result)),300)
        self.assertTrue(all("json_extract(body,'$.logoUrl')" in q and 'SELECT body' not in q for q,_ in self.reads))
        self.assertEqual(len(self.reads),2)

    async def test_positive_and_negative_cache_avoid_repeated_reads_but_expire(self):
        self.fact('56',A,URL)
        with patch.object(logos.time,'monotonic',return_value=100):
            await logos.asset_logos(['56:'+A,'56:'+B])
            await logos.asset_logos(['56:'+A,'56:'+B])
        self.assertEqual(len(self.reads),1)
        self.fact('56',B,'https://static.oklink.com/b.png')
        with patch.object(logos.time,'monotonic',return_value=161):
            result=await logos.asset_logos(['56:'+A,'56:'+B])
        self.assertEqual(len(self.reads),2)
        self.assertEqual(result['logos']['56:'+B],'https://static.oklink.com/b.png')
        self.assertNotIn(A,self.reads[-1][1])

    async def test_metadata_is_not_tied_to_projection_or_provider_fetch(self):
        self.fact('56',A,URL,'stock')
        with patch.object(api,'published',AsyncMock(side_effect=RuntimeError('projection unavailable'))):
            response=Response()
            self.assertEqual(await api.logos(response,'56:'+A),{'logos':{'56:'+A:URL}})
            self.assertEqual(response.headers['Cache-Control'],'public, max-age=60')
        with self.assertRaises(HTTPException) as exc:
            await api.logos(Response(),'invalid')
        self.assertEqual(exc.exception.status_code,400)

    async def test_cache_has_a_fixed_capacity(self):
        with patch.object(logos.time,'monotonic',return_value=100):
            for i in range(2048): logos._cache['56:0x'+format(i,'040x')]=(200,'')
            await logos.asset_logos(['56:'+A])
        self.assertEqual(len(logos._cache),2048)
