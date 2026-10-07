import copy
import unittest

from app.services.stock_catalog_search import normalize_stock_ticker, stock_catalog_matches


TENCENT_ADDRESS = '0x' + 'a' * 40
UNRELATED_ADDRESS = '0x' + 'b' * 35 + '00700'


class StockCatalogSearch(unittest.TestCase):
    def test_numeric_codes_normalize_padding_without_substring_matches(self):
        row = {'ticker': '1024'}
        for query in ('1024', '01024', '001024', ' 01024 '):
            self.assertTrue(stock_catalog_matches(row, [], query))
        for query in ('102', '24', '1'):
            self.assertFalse(stock_catalog_matches(row, [], query))
        self.assertTrue(stock_catalog_matches({'ticker': '01024'}, [], '1024'))

    def test_numeric_query_never_matches_contract_or_name_fragments(self):
        token = {'stockCode': 'OTHER', 'tokenContractAddress': UNRELATED_ADDRESS,
                 'tokenName': 'Company 700', 'tokenSymbol': '700x'}
        self.assertFalse(stock_catalog_matches({'ticker': 'OTHER'}, [token], '700'))
        self.assertFalse(stock_catalog_matches({'ticker': '1700'}, [], '700'))
        self.assertTrue(stock_catalog_matches({'ticker': '700'}, [], '00700'))

    def test_known_ascii_ticker_has_exact_priority_over_other_names(self):
        codes = {'FCX', 'OTHER'}
        self.assertTrue(stock_catalog_matches({'ticker': 'FCX'}, [], 'fcx', codes))
        token = {'stockCode': 'OTHER', 'tokenName': 'FCX themed asset', 'tokenSymbol': 'FCXx'}
        self.assertFalse(stock_catalog_matches({'ticker': 'OTHER'}, [token], 'FCX', codes))
        self.assertFalse(stock_catalog_matches({'ticker': 'FCXY'}, [], 'FCX', codes))
        self.assertTrue(stock_catalog_matches({'ticker': 'TSLA'}, [], 'Tesla', codes))

    def test_localized_display_aliases_are_searchable(self):
        cases = {'700': ('腾讯', '腾讯控股', 'Tencent'),
                 '1024': ('快手', 'Kuaishou'),
                 '1': ('长江和记', 'CK Hutchison'),
                 '1038': ('长江基建', 'CK Infrastructure'),
                 '1088': ('中国神华', 'China Shenhua'),
                 '1093': ('石药', 'CSPC Pharmaceutical'),
                 '1810': ('小米', 'Xiaomi'),
                 '9992': ('泡泡玛特', 'Pop Mart'),
                 'NVDA': ('英伟达', 'NVIDIA'),
                 'TSLA': ('特斯拉', 'Tesla')}
        for ticker, aliases in cases.items():
            for alias in aliases:
                with self.subTest(ticker=ticker, alias=alias):
                    self.assertTrue(stock_catalog_matches({'ticker': ticker}, [], alias))

    def test_company_names_use_actual_identity_and_token_fields(self):
        versions = [{'stockIdentity': {'code': 'FCX', 'nameZh': '自由港麦克莫兰',
                                      'nameEn': 'Freeport-McMoRan'},
                     'tokenName': 'Freeport token', 'tokenSymbol': 'FCXx'}]
        for query in ('自由港', 'freeport', 'McMoRan', 'FCXx'):
            self.assertTrue(stock_catalog_matches({'ticker': 'FCX'}, versions, query))
        self.assertTrue(stock_catalog_matches({'ticker': 'X', 'name': 'Example Company'}, [], 'example'))
        self.assertTrue(stock_catalog_matches({'stockIdentity': {'nameZh': '示例公司'}}, [], '示例'))

    def test_token_suffix_does_not_assign_company_identity(self):
        row = {'ticker': 'UNRELATED'}
        versions = [{'tokenName': 'NVDAx', 'tokenSymbol': 'NVDAx'}]
        self.assertFalse(stock_catalog_matches(row, versions, '英伟达'))
        self.assertFalse(stock_catalog_matches(row, versions, 'NVDA', {'NVDA', 'UNRELATED'}))

    def test_complete_addresses_are_exact_case_insensitive_and_fragments_invalid(self):
        row = {'ticker': '700', 'stockToken': TENCENT_ADDRESS}
        versions = [{'tokenContractAddress': TENCENT_ADDRESS}]
        self.assertTrue(stock_catalog_matches(row, versions, TENCENT_ADDRESS.upper()))
        self.assertFalse(stock_catalog_matches(row, versions, UNRELATED_ADDRESS))
        self.assertTrue(stock_catalog_matches({'ticker': '700'}, versions, TENCENT_ADDRESS))
        for query in ('0xaaaa', TENCENT_ADDRESS[:-1], TENCENT_ADDRESS + 'a', '0x' + 'g'*40):
            self.assertFalse(stock_catalog_matches(row, versions, query))
        self.assertFalse(stock_catalog_matches({'name': '0xaaaa'}, [], '0xaaaa'))

    def test_empty_search_and_zero_or_missing_ticker_are_distinct(self):
        self.assertEqual(normalize_stock_ticker(None), '')
        self.assertEqual(normalize_stock_ticker(''), '')
        self.assertEqual(normalize_stock_ticker(0), '0')
        self.assertEqual(normalize_stock_ticker('000'), '0')
        self.assertTrue(stock_catalog_matches({}, [], ''))
        self.assertTrue(stock_catalog_matches({}, [], '  '))
        self.assertFalse(stock_catalog_matches({}, [], '0'))
        self.assertTrue(stock_catalog_matches({'ticker': 0}, [], '000'))
        self.assertTrue(stock_catalog_matches({'ticker': None, 'name': 'Unnamed Company'}, [], 'company'))

    def test_search_does_not_mutate_directory_source(self):
        row = {'ticker': '700', 'stockToken': TENCENT_ADDRESS}
        versions = [{'stockIdentity': {'code': '700', 'nameEn': 'Tencent'}}]
        before = copy.deepcopy((row, versions))
        stock_catalog_matches(row, versions, '腾讯')
        self.assertEqual((row, versions), before)


class StockDirectorySearchIntegration(unittest.TestCase):
    def test_server_search_runs_before_pagination_and_keeps_directory_counts(self):
        from app.api.product_v2 import _stock_directory

        themes = [{'ticker': '700', 'stockToken': TENCENT_ADDRESS, 'stockTokenChain': '56',
                   'theme': {'pairedCount': 1, 'volume': {'value': 100, 'known': 1, 'total': 1}}},
                  {'ticker': 'OTHER', 'stockToken': UNRELATED_ADDRESS, 'stockTokenChain': '56',
                   'theme': {'pairedCount': 0}},
                  {'ticker': '1024', 'theme': {'pairedCount': 0}}]
        payload = {'now': 1_800_000_000_000, 'unified': {'stockThemes': themes, 'stockTokens': [
            {'chainId': '56', 'stockCode': '700', 'tokenContractAddress': TENCENT_ADDRESS},
            {'chainId': '56', 'stockCode': 'OTHER', 'tokenContractAddress': UNRELATED_ADDRESS}]}}
        before = copy.deepcopy(payload)
        result = _stock_directory(payload, 'all', '700', 'ticker', 'all', 1, 0)
        self.assertEqual(result['directory']['total'], 1)
        self.assertEqual(result['directory']['totalThemes'], 3)
        self.assertEqual(result['directory']['pairedThemeCount'], 1)
        self.assertEqual(result['unified']['stockThemes'][0]['ticker'], '700')
        chinese = _stock_directory(payload, 'all', '腾讯', 'ticker', 'all', 20, 0)
        self.assertEqual(chinese['directory']['total'], 1)
        padded = _stock_directory(payload, 'all', '01024', 'ticker', 'all', 20, 0)
        self.assertEqual(padded['unified']['stockThemes'][0]['ticker'], '1024')
        self.assertEqual(_stock_directory(payload, 'all', '01024', 'ticker', 'paired', 20, 0)['directory']['total'], 0)
        self.assertEqual(_stock_directory(payload, 'all', '腾讯', 'ticker', 'all', 1, 1)['unified']['stockThemes'], [])
        self.assertEqual(payload, before)


if __name__ == '__main__':
    unittest.main()
