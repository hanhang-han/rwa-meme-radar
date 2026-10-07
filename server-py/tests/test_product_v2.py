import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from app.api import product_v2 as api
from app.product_history import interval_covered
from app.product_quotes import calldata, verified_mapping, quote
from eth_abi import decode, encode
from eth_utils import keccak

NOW = 1_800_000_000_000
A, B, P = ('0x'+c*40 for c in ('a', 'b', 'c'))


class ProductReads(unittest.IsolatedAsyncioTestCase):
    def payload(self):
        stock = {'chainId': '56', 'stockCode': 'NVDA', 'tokenContractAddress': B,
                 'tokenName': 'NVIDIA', 'stockIdentity': {'code': 'NVDA', 'nameZh': '英伟达'},
                 'price': 150, 'priceCurrency': 'USD', 'fieldTimes': {'price': NOW}}
        asset = {'chainId': '56', 'token': A, 'name': 'Meme', 'kind': 'candidate'}
        relation = {'chainId': '56', 'pool': P, 'stock': B, 'token': A, 'ticker': 'NVDA',
                    'status': 'verified', 'level': None, 'confirmationStatus': 'confirmed', 'poolCreatedAt': NOW-1000}
        return {'now': NOW, 'unified': {'stockTokens': [stock], 'assets': [asset], 'relations': [relation],
                'stockThemes': [{'ticker': 'NVDA', 'stockToken': B, 'stockTokenChain': '56', 'theme': {'pairedCount': 1}}],
                'stockThemesByChain': {'56': [{'ticker': 'NVDA', 'stockToken': B, 'stockTokenChain': '56', 'theme': {'pairedCount': 1}}]}}}

    async def test_theme_preserves_recorded_pool_but_never_counts_stale_grade(self):
        payload = self.payload()
        before = copy.deepcopy(payload)
        with patch.object(api, 'published', AsyncMock(return_value=payload)), patch.object(api, 'read_theme_pool_history', AsyncMock(return_value={'snapshots': []})):
            result = await api.theme('NVDA', '56')
            self.assertEqual(result['unified']['snapshotScope'], 'theme')
            self.assertEqual(len(result['unified']['relations']), 1)
            self.assertEqual(result['themeMetrics']['memeCount'], 0)
            self.assertIsNone(result['themeMetrics']['volume24hUsd'])
        self.assertEqual(payload, before)

    async def test_directory_scope_and_versions_are_not_borrowed_from_other_chains(self):
        with patch.object(api, 'published', AsyncMock(return_value=self.payload())):
            result = await api.stocks('56')
            self.assertEqual(result['unified']['stockThemes'][0]['stock']['tokenContractAddress'], B)
            self.assertEqual(result['unified']['assets'], [])
            empty = await api.stocks('196')
            self.assertEqual(empty['unified']['stockThemes'], [])
            self.assertEqual(empty['unified']['stockTokens'], [])
            with self.assertRaises(HTTPException):
                await api.stocks('999')

    async def test_directory_pages_before_transferring_quotes_or_nested_evidence(self):
        payload = self.payload()
        base = payload['unified']['stockTokens'][0]
        payload['unified']['stockTokens'] = [{**base, 'stockCode': f'T{i:04}',
            'stockIdentity': {'code': f'T{i:04}', 'nameZh': '公司'+str(i)},
            'marketQuotes': [{'evidence': 'x'*5000}], 'fieldStatus': {'trace': 'x'*5000}}
            for i in range(1000)]
        payload['unified']['stockThemes'] = [{'ticker': f'T{i:04}', 'stockToken': B, 'stockTokenChain': '56',
            'theme': {'pairedCount': 1, 'volume': {'value': i, 'known': 1, 'total': 1}},
            'themeMetrics': {'evidence': 'x'*5000}} for i in range(1000)]
        before = copy.deepcopy(payload)
        with patch.object(api, 'published', AsyncMock(return_value=payload)):
            result = await api.stocks('all', sort='ticker', catalog='all', limit=20, offset=980)
        self.assertEqual(result['directory']['total'], 1000)
        self.assertEqual(len(result['unified']['stockThemes']), 20)
        self.assertEqual(result['unified']['stockThemes'][0]['ticker'], 'T0980')
        self.assertEqual(result['unified']['stockTokens'], [])
        self.assertNotIn('list', result['unified']['stockThemes'][0])
        self.assertLess(len(json.dumps(result)), 30_000)
        self.assertEqual(payload, before)

    async def test_directory_preserves_independent_equity_and_token_quotes_without_pool_volume(self):
        payload=self.payload()
        stock=payload['unified']['stockTokens'][0]
        stock.update(stockIdentity={'id':'NVDA','code':'NVDA','market':'US','currency':'USD'},
                     stockPrice=190,referenceAt=NOW-86400000,referenceObservedAt=NOW,
                     referenceCurrency='USD',referenceProvider='Reference fixture',referenceScope='equity-exchange',
                     referenceIdentityVerified=True,referenceRealtime=False)
        other={**stock,'chainId':'196','price':99,'stockPrice':195,'referenceAt':NOW-3600000}
        payload['unified']['stockTokens'].append(other)
        payload['unified']['stockThemes'][0]['theme']={'pairedCount':0,'volume':{'value':None,'known':0,'total':0}}
        before=copy.deepcopy(payload)
        with patch.object(api,'published',AsyncMock(return_value=payload)):
            result=await api.stocks('all',catalog='all')
            row=result['unified']['stockThemes'][0]
            self.assertEqual(row['stock']['price'],150)
            self.assertEqual(row['equity']['stockPrice'],195)
            self.assertFalse(row['equity']['referenceRealtime'])
            self.assertEqual(row['stock']['stockIdentity']['market'],'US')
            self.assertIsNone(row['theme']['volume']['value'])
        self.assertEqual(payload,before)
        # The production directory reads published summaries, rather than
        # the unsummarized quote object used to build the projection.
        from app.dashboard_projection import stock_summary, _market_stock
        for summarized in (stock_summary(stock), _market_stock(stock_summary(stock))):
            self.assertEqual(api.directory_equity([summarized])['stockPrice'],190)
        for malformed in ({'referenceScope':'issuer-reference'},{'referenceIdentityVerified':False},
                          {'referenceCurrency':'HKD'},{'referenceAt':None}):
            self.assertIsNone(api.directory_equity([{**stock,**malformed}]))

    async def test_directory_search_sort_and_scope_keep_global_counts(self):
        payload = self.payload()
        payload['unified']['stockThemes'].append({'ticker': 'EMPTY', 'theme': {'pairedCount': 0}})
        with patch.object(api, 'published', AsyncMock(return_value=payload)):
            paired = await api.stocks('all')
            all_rows = await api.stocks('all', catalog='all')
            searched = await api.stocks('all', q='英伟达', catalog='all')
            self.assertEqual(paired['directory']['total'], 1)
            self.assertEqual(paired['directory']['totalThemes'], 2)
            self.assertEqual(all_rows['directory']['total'], 2)
            self.assertEqual(searched['unified']['stockThemes'][0]['ticker'], 'NVDA')
            self.assertEqual(searched['directory']['total'], 1)

    def test_theme_dto_keeps_return_provenance_and_risk_freshness_without_detail_trees(self):
        row = {'chainId': '56', 'token': A, 'kind': 'candidate', 'volume24h': 12, 'volumeCurrency': 'USD',
            'fieldTimes': {'volume24h': NOW}, 'fieldSources': {'volume24h': 'DexScreener'},
            'fieldScopes': {'volume24h': 'token-aggregate'},
            'productMetrics': {'changes': {'h1': {'value': -2, 'at': NOW-1000, 'source': 'DexScreener'}}},
            'riskStatus': 'triggered', 'riskFlags': ['wash_suspect'], 'riskAssessment': {
                'safety': {'tax': {'status': 'triggered', 'severity': 'critical', 'checkedAt': NOW-2000,
                                   'provider': 'GoPlus', 'evidence': {'trace': 'x'*5000}}},
                'checks': {'wash_suspect': {'evidence': {'volumeLiquidityRatio': 99, 'trace': 'x'*5000}}}}}
        result = api.theme_asset(row)
        self.assertEqual(result['change1h'], -2)
        self.assertEqual(result['fieldTimes']['change1h'], NOW-1000)
        self.assertEqual(result['fieldSources']['change1h'], 'DexScreener')
        self.assertEqual(result['riskAssessment']['safety']['tax']['severity'], 'critical')
        self.assertEqual(result['riskAssessment']['safety']['tax']['checkedAt'], NOW-2000)
        self.assertEqual(result['riskAssessment']['checks']['wash_suspect']['evidence'], {'volumeLiquidityRatio': 99})
        self.assertNotIn('productMetrics', result)
        self.assertLess(len(json.dumps(result)), 1000)

    async def test_theme_keeps_meme_quote_and_its_original_observation(self):
        payload = self.payload()
        asset = payload['unified']['assets'][0]
        asset.update(price=0.0093, priceCurrency='USD', provider='OKX', quoteAt=NOW-2000000,
                     quoteStatus='stale', fieldTimes={'price':NOW-2000000},
                     fieldSources={'price':'OKX'}, fieldScopes={'price':'token'})
        before = copy.deepcopy(payload)
        with patch.object(api, 'published', AsyncMock(return_value=payload)), patch.object(api, 'read_theme_pool_history', AsyncMock(return_value={'snapshots': []})):
            result = await api.theme('NVDA', '56')
        quote = result['unified']['assets'][0]
        self.assertEqual(quote['price'], asset['price'])
        self.assertEqual(quote['priceCurrency'], 'USD')
        self.assertEqual(quote['fieldTimes']['price'], NOW-2000000)
        self.assertEqual(quote['fieldSources']['price'], 'OKX')
        self.assertEqual(quote['fieldScopes']['price'], 'token')
        self.assertEqual(quote['quoteStatus'], 'stale')
        self.assertEqual(payload, before)
        missing = api.theme_asset({'chainId':'56','token':A,'price':None,'quoteStatus':'missing'})
        self.assertIsNone(missing.get('price'))
        self.assertEqual(missing['quoteStatus'], 'missing')
        self.assertEqual(missing['fieldTimes'], {})

    async def test_search_matches_localized_name_and_links_stock_address_to_asset(self):
        with patch.object(api, 'published', AsyncMock(return_value=self.payload())):
            result = await api.search('英伟达', 12, '56')
            self.assertEqual(result['items'][0]['path'], f'/asset/56/{B}')
            self.assertEqual(result['unified']['stockTokens'][0]['tokenContractAddress'], B)
            self.assertEqual((await api.search(B, 12, '196'))['items'], [])

    def test_session_uses_recent_provider_evidence_and_not_weekday_guesses(self):
        self.assertEqual(api.market_status([{'marketSession': 'regular', 'referenceAt': NOW}], NOW)['status'], 'open')
        self.assertEqual(api.market_status([{'marketSession': 'regular', 'referenceAt': NOW-2_000_000}], NOW)['status'], 'unknown')
        self.assertEqual(api.market_status([], NOW)['status'], 'unknown')

    def test_prices_reject_missing_provenance_currency_and_future_observations(self):
        evidence = {'provider': 'OKX', 'currency': 'USD', 'venue': 'dex', 'timeKind': 'market', 'marketAt': NOW}
        sample = {'t': NOW, 'price': 0.0000016, 'provenance': evidence}
        self.assertEqual(len(api.eligible_price_samples([sample], {'priceScope': 'dex'}, NOW-1000, NOW)), 1)
        for invalid in ({}, {**evidence, 'currency': 'USDT'}, {**evidence, 'marketAt': NOW+300001}, {**evidence, 'marketAt': NOW+1}):
            self.assertEqual(api.eligible_price_samples([{**sample, 'provenance': invalid}], {'priceScope': 'dex'}, NOW-1000, NOW), [])

    def test_time_coverage_rejects_a_gap_and_combines_overlaps(self):
        self.assertTrue(interval_covered([(0, 700), (600, 1500)], 100, 1400))
        self.assertFalse(interval_covered([(0, 700), (701, 1500)], 100, 1400))
        self.assertFalse(interval_covered([], 100, 1400))

    async def test_unconfigured_quoter_never_makes_rpc_calls(self):
        with patch('app.product_quotes.httpx.AsyncClient') as client:
            result = await quote('56', A, P, 1000, [], [])
            self.assertIsNone(result['valuePercent'])
            self.assertEqual(result['reason'], 'verified-quoter-unconfigured')
            client.assert_not_called()

    def test_verified_quoter_mapping_and_calldata_are_pool_specific(self):
        mapping = {'chainId': '56', 'pool': P, 'tokenIn': A, 'tokenOut': B,
                   'quoter': '0x'+'d'*40, 'version': 'v3-v2', 'verified': True,
                   'verificationUrl': 'https://docs.uniswap.org/', 'decimalsIn': 18, 'decimalsOut': 18, 'fee': 3000}
        self.assertEqual(verified_mapping('56', P, A, [mapping]), mapping)
        self.assertIsNone(verified_mapping('196', P, A, [mapping]))
        self.assertIsNone(verified_mapping('56', P, A, [{**mapping, 'verified': False}]))
        encoded = bytes.fromhex(calldata(mapping, 10**18)[2:])
        decoded = decode(['(address,address,uint256,uint24,uint160)'], encoded[4:])[0]
        self.assertEqual(decoded, (A, B, 10**18, 3000, 0))
        v4 = {**mapping, 'pool': '0x'+'f'*64, 'version': 'v4', 'tickSpacing': 60, 'hooks': '0x'+'0'*40}
        v4['pool']='0x'+keccak(encode(['address','address','uint24','int24','address'],[A,B,3000,60,v4['hooks']])).hex()
        self.assertEqual(verified_mapping('56', v4['pool'], A, [v4]), v4)
        self.assertIsNone(verified_mapping('56', v4['pool'], A, [{**v4, 'tickSpacing': True}]))
        self.assertIsNone(verified_mapping('56', '0x'+'f'*64, A, [{**v4, 'pool': '0x'+'f'*64}]))
        encoded = bytes.fromhex(calldata(v4, 10**18)[2:])
        decoded = decode(['((address,address,uint24,int24,address),bool,uint128,bytes)'], encoded[4:])[0]
        self.assertEqual(decoded[0][:4], (A, B, 3000, 60))
        self.assertEqual(decoded[1:], (True, 10**18, b''))
        self.assertIsNone(verified_mapping('56', v4['pool'], A, [{**v4, 'hooks': P}]))
