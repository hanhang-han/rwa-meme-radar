import unittest
from unittest.mock import patch

from app.market_quotes import enrich_asset, enrich_relation
from app.stock_quotes import apply_stock_overlays, binance_live, eodhd_quotes, valid_reference

NOW = 1_790_261_400_000
TOKEN = '0x' + 'a' * 40


class DataContractRepairs(unittest.TestCase):
    def overlays(self, rows, binance=None, robinhood=None, eod=None):
        with patch('app.stock_quotes.binance_live', return_value=binance or {}), patch('app.stock_quotes.robinhood_live', return_value=robinhood or {}), patch('app.stock_quotes.eodhd_quotes', return_value=eod or {}), patch('app.stock_quotes.eodhd_status', return_value={}), patch('app.stock_quotes._now', return_value=NOW):
            return apply_stock_overlays(rows)

    def test_separate_api_process_reads_worker_snapshot(self):
        with patch('app.collectors.binance.state', {'tokens': []}), patch('app.collectors.binance.status_snapshot', return_value={'tokens': [{'tokenContractAddress': TOKEN.upper(), 'price': 123}]}):
            self.assertEqual(binance_live()[TOKEN]['price'], 123)

    def test_venue_switch_keeps_dex_observation_and_does_not_relabel_dex_volume(self):
        row = self.overlays([{'chainId': '56', 'tokenContractAddress': TOKEN, 'price': 12, 'volume24h': 99, 'fieldTimes': {'price': NOW-60_000}}], binance={TOKEN: {'price': 13, 'quoteAt': NOW}})[0]
        self.assertEqual(row['priceScope'], 'exchange')
        self.assertEqual(row['priceCurrency'], 'USDT')
        self.assertIsNone(row['volume24h'])
        self.assertEqual(row['marketQuotes']['dex']['volume24h'], 99)
        self.assertEqual(row['priceProvenance']['timeKind'], 'market')

    def test_legacy_identity_collisions_are_quarantined_at_read(self):
        snapshot = {'stocks': {'SLV': {'id': 'SLV', 'symbol': 'SLV.WAR', 'currency': 'PLN', 'price': 22.5}, 'QQQ': {'id': 'QQQ', 'symbol': 'QQQ.BA', 'currency': 'ARS', 'price': 59850}, 'AAPL': {'id': 'AAPL', 'symbol': 'AAPL.US', 'currency': 'USD', 'price': 200}}}
        with patch('app.stock_quotes._read_snapshot', return_value=snapshot):
            quotes = eodhd_quotes()
        self.assertNotIn('SLV', quotes)
        self.assertNotIn('QQQ', quotes)
        self.assertEqual(quotes['AAPL']['delayMs'], 1_200_000)
        self.assertFalse(quotes['AAPL']['realtime'])
        self.assertFalse(valid_reference(quotes['AAPL'], {'code': 'NVDA'}))

    def test_old_eod_never_overwrites_fresh_robinhood_reference(self):
        row = self.overlays([{'chainId': '4663', 'tokenContractAddress': TOKEN, 'stockCode': 'AMZN', 'price': 244, 'fieldTimes': {'price': NOW-1000}}], robinhood={TOKEN: {'stockPrice': 246.865, 'price': 246.865, 'quoteAt': NOW, 'tokenToAssetRatio': 1}}, eod={'AMZN': {'symbol': 'AMZN.US', 'currency': 'USD', 'price': 254.98, 'marketAt': NOW-86400_000}})[0]
        self.assertEqual(row['stockPrice'], 246.865)
        self.assertEqual(row['referenceAt'], NOW)
        self.assertEqual(row['referenceProvider'], 'Robinhood')
        self.assertEqual(row['referenceCurrency'], 'USD')
        self.assertEqual(row['referenceScope'], 'issuer-reference')
        self.assertEqual(row['priceScope'], 'dex')
        self.assertEqual(row['price'], 244)
        self.assertEqual(row['referenceObservations']['equity']['price'], 254.98)
        self.assertIsNone(row.get('ratioSource'))

    def test_legacy_holder_update_cannot_rejuvenate_price(self):
        asset = {'chainId': '56', 'token': TOKEN, 'price': 1, 'fieldTimes': {'price': NOW-86400_000}}
        snapshot = {'assets': {f'56:{TOKEN}': {'CoinGecko': {'provider': 'CoinGecko', 'price': 2, 'holders': 100, 'holderTop10': 65, 'updatedAt': NOW, 'fieldTimes': {'holders': NOW, 'holderTop10': NOW}}}}}
        with patch('app.market_quotes._read_snapshot', return_value=snapshot):
            row = enrich_asset(asset)
            missing = enrich_asset({'chainId': '56', 'token': TOKEN})
        self.assertEqual(row['price'], 1)
        self.assertEqual(row['fieldTimes']['price'], NOW-86400_000)
        self.assertEqual(row['holders'], 100)
        self.assertEqual(row['risk']['top10'], 65)
        self.assertEqual(missing['price'], 2)
        self.assertIsNone(missing['quoteAt'])
        self.assertEqual(missing['quoteStatus'], 'unknown')

    def test_fresh_pool_enrichment_reaches_python_projection_including_valid_zero(self):
        snapshot = {'pools': {f'56:{TOKEN}': {'CoinGecko': {'provider': 'CoinGecko', 'updatedAt': NOW, 'liquidityUsd': 0, 'volume24h': 0}}}}
        with patch('app.market_quotes._read_snapshot', return_value=snapshot):
            row = enrich_relation({'chainId': '56', 'pool': TOKEN, 'liquidityUsd': 20, 'liquidityAt': NOW-86400_000})
        self.assertEqual(row['liquidityUsd'], 0)
        self.assertEqual(row['liquidityAt'], NOW)
        self.assertEqual(row['liquidityProvider'], 'CoinGecko')
        self.assertEqual(row['poolMarket']['scope'], 'pool:' + TOKEN)

    def test_recent_verification_does_not_make_old_liquidity_current(self):
        from app.data_quality import evaluate_asset
        asset = {'price': 1, 'volume24h': 2, 'fieldTimes': {'price': NOW, 'volume24h': NOW}}
        quality = evaluate_asset(asset, [{'status': 'verified', 'liquidityUsd': 1, 'checkedAt': NOW}], NOW)
        self.assertFalse(quality['fresh']['poolLiquidity'])
        self.assertFalse(quality['eligible']['theme'])

    def test_new_sample_without_evidence_cannot_inherit_old_market_identity(self):
        import asyncio
        import tempfile
        from app.db import ResearchStore

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                store = await ResearchStore(directory + '/quotes.sqlite', '56').connect()
                try:
                    await store.sample(TOKEN, 1, None, NOW, metadata={
                        'provider': 'Binance', 'currency': 'USDT', 'scope': 'exchange', 'marketAt': NOW})
                    await store.sample(TOKEN, 2, None, NOW + 1000)
                    latest = (await store.samples(TOKEN))[-1]
                    self.assertEqual(latest['price'], 2)
                    self.assertIsNone(latest['provenance'])

                    await store.merge_asset_observation('other', {'token': 'other', 'fieldObservations': {
                        'price': {'provider': 'OKX', 'timeKind': 'market', 'marketAt': NOW}}},
                        {'price': (1, NOW)}, (1, None, NOW))
                    await store.merge_asset_observation('other', {'token': 'other'},
                        {'price': (2, NOW + 1000)}, (2, None, NOW + 1000))
                    latest = (await store.samples('other'))[-1]
                    self.assertEqual(latest['price'], 2)
                    self.assertIsNone(latest['provenance'])
                    self.assertNotIn('price', (await store.get('asset', 'other')).get('fieldObservations', {}))
                finally:
                    await store.close()
        asyncio.run(run())

    def test_robinhood_ratio_requires_matching_deployment_evidence_and_expires(self):
        from app.stock_quotes import robinhood_ratio_evidence
        observation = {
            'stockCode': 'TEST', 'sourceAssetId': 'equity-a', 'tokenToAssetRatio': .25, 'currentMultiplier': .25,
            'ratioSource': 'https://api.robinhood.com/rhj/assets', 'ratioAt': NOW, 'ratioTimeKind': 'observed',
            'ratioVersion': f'rh:equity-a:4663:{TOKEN}:0.25', 'ratioValidUntil': NOW+600_000, 'ratioVerified': True,
            'ratioEvidence': {'sourceAssetId': 'equity-a', 'chainId': '4663', 'tokenContractAddress': TOKEN,
                              'stockCode': 'TEST', 'currentMultiplier': .25, 'observedAt': NOW}}
        self.assertTrue(robinhood_ratio_evidence(observation, TOKEN, NOW)['ratioVerified'])
        self.assertFalse(robinhood_ratio_evidence(observation, TOKEN, NOW+600_001)['ratioVerified'])
        self.assertFalse(robinhood_ratio_evidence({**observation, 'tokenToAssetRatio': .5}, TOKEN, NOW)['ratioVerified'])
        self.assertFalse(robinhood_ratio_evidence(observation, '0x'+'b'*40, NOW)['ratioVerified'])
        self.assertFalse(robinhood_ratio_evidence({**observation, 'ratioEvidence': None}, TOKEN, NOW)['ratioVerified'])
        row = self.overlays([{'chainId': '4663', 'tokenContractAddress': TOKEN, 'tokenToAssetRatio': 1,
                              'ratioVerified': True, 'ratioVersion': 'old'}], robinhood={TOKEN: observation})[0]
        self.assertTrue(row['ratioVerified'])
        self.assertEqual(row['tokenToAssetRatio'], .25)
        self.assertEqual(row['ratioVersion'], observation['ratioVersion'])
        legacy = self.overlays([{'chainId': '4663', 'tokenContractAddress': TOKEN, 'tokenToAssetRatio': 1,
                                 'ratioVerified': True, 'ratioVersion': 'old'}], robinhood={TOKEN: {'tokenToAssetRatio': 1}})[0]
        self.assertFalse(legacy['ratioVerified'])
        self.assertIsNone(legacy['ratioVersion'])

    def test_missing_reference_clears_all_legacy_reference_identity_fields(self):
        row = self.overlays([{'stockCode': 'SLV', 'referenceProvider': 'EODHD', 'referenceSymbol': 'SLV.WAR',
                              'referenceScope': 'equity-exchange', 'referenceIdentityVerified': True,
                              'referenceAdjustmentVersion': 'bad', 'referenceCurrency': 'PLN',
                              'referenceDelayMs': 1200000, 'stockPrice': 22.5}])[0]
        self.assertIsNone(row['referenceSymbol'])
        self.assertIsNone(row['referenceAdjustmentVersion'])
        self.assertIsNone(row['referenceScope'])
        self.assertFalse(row['referenceIdentityVerified'])
        self.assertIsNone(row['stockPrice'])

    def test_real_robinhood_normalizer_output_is_accepted_by_python_overlay(self):
        import json
        import subprocess
        from pathlib import Path
        catalogue = {'assets': [{'id': 'source-equity', 'tokenSymbol': 'AAPL', 'currentMultiplier': '0.125',
                                'deployments': [{'chainId': 4663, 'contractAddress': TOKEN}]}]}
        quotes = {'quotes': [{'tokenSymbol': 'AAPL', 'bid': '199', 'ask': '201',
                              'generatedAt': '2026-09-24T14:49:00.000Z'}]}
        source = ("import {normalizeRobinhoodSnapshot} from './src/lib/robinhood.ts';"
                  "console.log(JSON.stringify(normalizeRobinhoodSnapshot(" + json.dumps(catalogue) + "," +
                  json.dumps(quotes) + "," + str(NOW) + ")[0]));")
        normalized = json.loads(subprocess.check_output(['node', '--import', 'tsx', '--input-type=module', '-e', source],
                                                       cwd=Path(__file__).resolve().parents[2], text=True))
        row = self.overlays([{'chainId': '4663', 'tokenContractAddress': TOKEN, 'stockCode': 'AAPL'}],
                            robinhood={TOKEN: normalized})[0]
        self.assertTrue(row['ratioVerified'])
        self.assertEqual(row['tokenToAssetRatio'], .125)
        self.assertEqual(row['ratioAt'], NOW)
        self.assertEqual(row['ratioEvidence']['tokenContractAddress'], TOKEN)
        self.assertNotEqual(row['ratioAt'], row['referenceAt'])
        self.assertIsNone(row['referenceAdjustmentVersion'])
