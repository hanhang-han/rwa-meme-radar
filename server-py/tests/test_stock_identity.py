import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ['NODE_ENV'] = 'test'

from app.stock_identity import (MANIFEST, RULE_VERSION, assess_pool_relation,
                                classify_derivative, manifest_status, match_name, token_identity)

NOW = 1_790_341_200_000
NATIVE = '0xc845b2894dbddd03858fd2d643b4ef725fe0849d'
WRAPPER = '0xa8ddb5cd96b5222afe198316e9a57caa642850d5'
MEME = '0x' + '1' * 40
POOL = '0x' + '3' * 40


def relation(**changes):
    row = {'id': 'r', 'chainId': '196', 'token': MEME, 'stock': NATIVE,
           'stockSide': WRAPPER, 'pool': POOL, 'token0': MEME,
           'token1': WRAPPER, 'ticker': 'NVDA', 'status': 'verified',
           'liquidityUsd': 1000, 'liquidityAt': NOW - 1000,
           'checkedAt': NOW - 1000}
    row.update(changes)
    return row


class StockIdentityTest(unittest.TestCase):
    def test_unknown_and_malformed_addresses_keep_version_without_recounting_manifest(self):
        version = manifest_status()['version']
        with patch('app.stock_identity.manifest_status', side_effect=AssertionError('catalogue-recount')):
            for address in ('0x' + '9'*40, None, 'malformed'):
                row = token_identity('196', address, 'NVDA')
                self.assertEqual(row['manifestVersion'], version)
                self.assertEqual(row['verificationStatus'], 'unverified')
                self.assertFalse(row['eligibleForPair'])
                self.assertIsNone(row['ticker'])
        with patch('app.stock_identity._manifest', side_effect=OSError('missing')):
            self.assertIsNone(token_identity('196', NATIVE)['manifestVersion'])

    def test_pinned_official_addresses_are_chain_scoped(self):
        self.assertEqual(manifest_status()['status'], 'ready')
        self.assertEqual(manifest_status()['entries'], 4786)
        native = token_identity('196', NATIVE, 'WRONG')
        wrapper = token_identity('196', WRAPPER, 'WRONG')
        self.assertEqual(native['verificationStatus'], 'official')
        self.assertEqual(wrapper['tokenKind'], 'wrapper-current')
        self.assertEqual(native['underlyingId'], wrapper['underlyingId'])
        self.assertEqual(native['ticker'], 'NVDA')
        self.assertEqual(token_identity('4663', NATIVE, 'NVDA')['verificationStatus'], 'unverified')
        self.assertEqual(token_identity('196', '0x' + '9' * 40, 'NVDA')['verificationStatus'], 'unverified')

    def test_crdax_official_ticker_is_crdal_for_exact_deployment_only(self):
        address = '0x71eed272e83fba6194ec22af39bb50040f4a528b'
        for chain in ('196', '56'):
            issuer = token_identity(chain, address, 'CRDA')
            self.assertEqual(issuer['verificationStatus'], 'official')
            self.assertEqual(issuer['underlyingId'], 'xstocks:fe08dada-99dd-4d75-b580-07c63b1eeabd')
            self.assertEqual(issuer['ticker'], 'CRDAL')
            self.assertEqual(issuer['tokenSymbol'], 'CRDAx')
            self.assertEqual(issuer['underlyingIsin'], 'GB00BJFFLV09')
        self.assertEqual(token_identity('4663', address, 'CRDA')['verificationStatus'], 'unverified')

    def test_legacy_wrapper_is_recorded_but_ineligible(self):
        manifest = json.loads(MANIFEST.read_text())
        asset = next(a for a in manifest['assets'] if a['deployments'].get('56', {}).get('wrapperLegacy'))
        deployment = asset['deployments']['56']
        old = token_identity('56', deployment['wrapperLegacy'])
        self.assertEqual(old['verificationStatus'], 'legacy')
        self.assertFalse(old['eligibleForPair'])
        legacy_pair = relation(chainId='56', stock=deployment['native'],
                               stockSide=deployment['wrapperLegacy'], token1=deployment['wrapperLegacy'])
        self.assertEqual(assess_pool_relation(legacy_pair, NOW)['evidenceStatus'], 'legacy-wrapper-ineligible')

    def test_a_requires_both_issuer_deployments_real_pool_and_fresh_threshold(self):
        approved = assess_pool_relation(relation(), NOW)
        self.assertEqual(approved['level'], 'A')
        self.assertEqual(approved['evidenceStatus'], 'qualified')
        self.assertEqual(approved['pairLiquidityUsd'], 1000)
        self.assertEqual(approved['ruleVersion'], RULE_VERSION)
        self.assertEqual(assess_pool_relation(relation(liquidityUsd=999.99), NOW)['evidenceStatus'], 'below-minimum-liquidity')
        self.assertEqual(assess_pool_relation(relation(liquidityUsd=None), NOW)['evidenceStatus'], 'liquidity-unknown')
        self.assertEqual(assess_pool_relation(relation(liquidityAt=NOW - 900001), NOW)['evidenceStatus'], 'liquidity-stale')
        self.assertEqual(assess_pool_relation(relation(liquidityAt=NOW + 1), NOW)['level'], None)
        self.assertEqual(assess_pool_relation(relation(stockSide='0x' + '8' * 40, token1='0x' + '8' * 40), NOW)['evidenceStatus'], 'issuer-deployment-unverified')
        self.assertEqual(assess_pool_relation(relation(stock='0x' + '9' * 40), NOW)['evidenceStatus'], 'issuer-deployment-unverified')
        self.assertEqual(assess_pool_relation(relation(token1='0x' + '9' * 40), NOW)['evidenceStatus'], 'pool-side-mismatch')
        self.assertEqual(assess_pool_relation(relation(pool='unverifiable'), NOW)['evidenceStatus'], 'pool-evidence-incomplete')
        self.assertEqual(assess_pool_relation(relation(status='invalid'), NOW)['level'], None)

    def test_b_keywords_require_complete_latin_word_and_exclude_generic_crypto(self):
        self.assertEqual(match_name('TSLA', 'Tesla rocket')['ticker'], 'TSLA')
        changed = match_name('TSLA', 'Tesla rocket')
        changed['ticker'] = 'incorrect'
        self.assertEqual(match_name('TSLA', 'Tesla rocket')['ticker'], 'TSLA')
        self.assertEqual(match_name('foo', '拉布布币')['ticker'], '9992')
        self.assertEqual(match_name('TSLA', '拉布布币')['ticker'], 'TSLA')
        self.assertEqual(match_name('TSLACAT', 'xTeslaCat'), None)
        self.assertEqual(match_name('BTC', 'Bitcoin'), None)
        self.assertEqual(match_name('MSTR', 'Strategy')['level'], 'B')

    def test_derivative_names_are_separate_and_cannot_become_meme_name_clues(self):
        for symbol, name, kind in (('TSLA3L', 'Tesla 3x Long', 'leverage'),
                                    ('GME', 'GameStop leveraged token', 'leverage'),
                                    ('WNVDA', 'Wrapped NVIDIA', 'wrapper'),
                                    ('TSLA', 'Tesla index token', 'index'),
                                    ('TENCENT', '腾讯指数', 'index')):
            with self.subTest(symbol=symbol, name=name):
                category = classify_derivative(symbol, name)
                self.assertEqual(category['kind'], kind)
                self.assertEqual(category['evidenceStatus'], 'name-only')
                self.assertIsNone(match_name(symbol, name))
        for symbol, name in (('MSTR', 'Strategy'), ('NVDA', 'NVIDIA meme'),
                              ('AMC', 'A MEME CAT'), ('WON', 'Wonder cat'), ('XTSLA', 'xTeslaCat')):
            self.assertIsNone(classify_derivative(symbol, name))
        self.assertIsNone(match_name('CAT', 'A MEME CAT'))
        self.assertIsNone(match_name('AMCAT', 'AMCAT'))


class CollectorQualificationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import ResearchStore
        self.temp = tempfile.TemporaryDirectory()
        self.store = await ResearchStore(self.temp.name + '/research.sqlite', '196').connect()

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    async def test_verified_pool_persists_official_grade_separately_from_status(self):
        from app.collectors import main_round
        stock = {'tokenContractAddress': NATIVE, 'stockCode': 'NVDA'}
        checked = {'token0': MEME, 'token1': WRAPPER, 'relation': {
            'token': MEME, 'stockSide': WRAPPER, 'stock': stock, 'wrapper': True}}
        await self.store.put('asset', MEME, {'token': MEME, 'kind': 'candidate', 'symbol': 'TAPEOUTX'})
        with patch.object(main_round, 'okx_get', AsyncMock(return_value=[{'poolAddress': POOL, 'liquidityUsd': '2500'}])), \
             patch.object(main_round, 'verify_pool', AsyncMock(return_value=checked)), \
             patch.object(main_round, 'rpc_call', AsyncMock(return_value=bytes(32))), \
             patch.object(main_round, 'broadcast'):
            outcome = await main_round.scan_pools(self.store, MEME, [stock], '0x1')
        self.assertEqual(outcome['status'], 'ready')
        saved = (await self.store.all('relation'))[0]
        self.assertEqual(saved['status'], 'verified')
        self.assertEqual(saved['level'], 'A')
        self.assertEqual(saved['pairLiquidityUsd'], 2500)

    async def test_liquidity_rescan_keeps_factory_creation_evidence(self):
        from app.collectors import main_round
        stock = {'tokenContractAddress': NATIVE, 'stockCode': 'NVDA'}
        checked = {'token0': MEME, 'token1': WRAPPER, 'relation': {
            'token': MEME, 'stockSide': WRAPPER, 'stock': stock, 'wrapper': True}}
        evidence = {'poolCreatedAt': NOW - 10_000, 'discoveredAt': NOW,
                    'creationTx': '0x' + 'a' * 64, 'creationBlock': 123,
                    'creationBlockHash': '0x' + 'b' * 64, 'creationLogIndex': 1,
                    'factoryEventId': 'factory:123', 'factory': '0x' + 'c' * 40}
        await self.store.put('asset', MEME, {'token': MEME, 'kind': 'candidate', 'symbol': 'TAPEOUTX'})
        await self.store.put('pool', POOL, {**evidence, 'pool': POOL, 'creationStatus': 'confirmed'})
        await self.store.put('relation', f'196:{POOL}:{MEME}:{NATIVE}', {
            **relation(), **evidence, 'id': f'196:{POOL}:{MEME}:{NATIVE}',
            'confirmationStatus': 'confirmed'})
        with patch.object(main_round, 'okx_get', AsyncMock(return_value=[{'poolAddress': POOL, 'liquidityUsd': '2500'}])), \
             patch.object(main_round, 'verify_pool', AsyncMock(return_value=checked)), \
             patch.object(main_round, 'rpc_call', AsyncMock(return_value=bytes(32))), \
             patch.object(main_round, 'broadcast'):
            await main_round.scan_pools(self.store, MEME, [stock], '0x1')
        pool = await self.store.get('pool', POOL)
        saved = await self.store.get('relation', f'196:{POOL}:{MEME}:{NATIVE}')
        for field in evidence:
            self.assertEqual(pool[field], evidence[field])
            if field != 'factory':
                self.assertEqual(saved[field], evidence[field])
        self.assertEqual(saved['confirmationStatus'], 'confirmed')

    async def test_orphaned_creation_is_not_reverified_by_late_liquidity_scan(self):
        from app.collectors import main_round
        stock = {'tokenContractAddress': NATIVE, 'stockCode': 'NVDA'}
        checked = {'token0': MEME, 'token1': WRAPPER, 'relation': {
            'token': MEME, 'stockSide': WRAPPER, 'stock': stock, 'wrapper': True}}
        relation_id = f'196:{POOL}:{MEME}:{NATIVE}'
        await self.store.put('asset', MEME, {'token': MEME, 'kind': 'candidate', 'symbol': 'TAPEOUTX'})
        await self.store.put('pool', POOL, {'pool': POOL, 'creationStatus': 'orphaned'})
        await self.store.put('relation', relation_id, {
            **relation(), 'id': relation_id, 'status': 'invalid',
            'confirmationStatus': 'orphaned', 'evidenceStatus': 'creation-orphaned'})
        with patch.object(main_round, 'okx_get', AsyncMock(return_value=[{'poolAddress': POOL, 'liquidityUsd': '2500'}])), \
             patch.object(main_round, 'verify_pool', AsyncMock(return_value=checked)), \
             patch.object(main_round, 'broadcast'):
            await main_round.scan_pools(self.store, MEME, [stock], '0x1')
        saved = await self.store.get('relation', relation_id)
        self.assertEqual(saved['status'], 'invalid')
        self.assertEqual(saved['evidenceStatus'], 'creation-orphaned')
