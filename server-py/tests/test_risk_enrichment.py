import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.collectors.risk_enrichment import normalize_goplus, refresh_risk_enrichment
from app.db import ResearchStore
from app.risk_assessment import assess_risk

NOW = 1_800_000_000_000
TOKEN = '0x' + 'a' * 40
POOL = '0x' + 'b' * 40
BURN = '0x' + '0' * 36 + 'dead'


def security(**kwargs):
    return {'is_honeypot': '0', 'buy_tax': '0', 'sell_tax': '0.03',
            'is_mintable': '0', 'is_blacklisted': '0', 'transfer_pausable': '0',
            'is_proxy': '0', 'is_open_source': '1', **kwargs}


class RiskNormalizationTest(unittest.TestCase):
    def test_fraction_conversion_unknowns_and_no_holder_or_supply_overwrite(self):
        result = normalize_goplus('56', TOKEN, security(buy_tax='', sell_tax='0.15',
            is_honeypot=None, holder_count='0', total_supply='1'), NOW)
        self.assertEqual(set(result), {'tokenScan', 'securityObservation'})
        self.assertIsNone(result['tokenScan']['buyTaxPct'])
        self.assertIsNone(result['tokenScan']['honeypot'])
        self.assertEqual(result['tokenScan']['sellTaxPct'], 15)
        check = assess_risk(result, now=NOW)['safety']['tax']
        self.assertEqual(check['status'], 'triggered')
        self.assertEqual(check['evidence']['triggers'], ['sellTaxPct'])
        self.assertEqual(assess_risk(result, now=NOW)['safety']['permissions']['status'], 'clear')

    def test_proxy_is_not_honeypot_unknown_does_not_pass(self):
        result = normalize_goplus('196', TOKEN, {'is_proxy': '1', 'is_open_source': '1', 'buy_tax': '', 'sell_tax': ''}, NOW)
        risk = assess_risk(result, now=NOW)
        self.assertEqual(risk['flags'], [])
        self.assertEqual(risk['safety']['tax']['status'], 'unknown')
        self.assertEqual(risk['safety']['permissions']['status'], 'unknown')
        self.assertIs(risk['safety']['permissions']['evidence']['proxy'], True)

    def test_partial_permission_finding_survives_missing_taxes_and_expiration(self):
        result = normalize_goplus('4663', TOKEN, {'is_mintable': '1', 'is_open_source': '0'}, NOW)
        risk = assess_risk(result, now=NOW)
        self.assertIn('contract_risk', risk['flags'])
        self.assertEqual(risk['safety']['tax']['status'], 'unknown')
        self.assertEqual(risk['safety']['permissions']['status'], 'triggered')
        old = assess_risk(result, now=NOW + 86_400_001)
        self.assertNotIn('contract_risk', old['flags'])
        self.assertEqual(old['safety']['permissions']['reason'], 'stale-scan')

    def test_raw_top10_does_not_become_adjusted_concentration(self):
        result = normalize_goplus('56', TOKEN, security(holder_count='2', holders=[
            {'address': POOL, 'percent': '0.8'}, {'address': TOKEN, 'percent': '0.1'}]), NOW)
        check = assess_risk(result, now=NOW)['safety']['concentration']
        self.assertEqual(check['status'], 'unknown')
        self.assertEqual(check['evidence']['top10RawPercent'], 90)
        self.assertFalse(check['evidence']['exclusionsApplied'])
        self.assertNotIn('holderDistribution', result)

    def test_incomplete_and_duplicate_holder_lists_have_no_top10(self):
        holders = [{'address': TOKEN, 'percent': '0.8'}]
        for rows, count in ((holders, '100'), (holders * 2, '2')):
            result = normalize_goplus('56', TOKEN, security(holders=rows, holder_count=count), NOW)
            self.assertIsNone(result['securityObservation']['holders']['top10RawPercent'])

    def test_v2_burn_and_expiring_lock_are_bounds_not_forever_safe(self):
        result = normalize_goplus('56', TOKEN, security(
            dex=[{'pair': POOL, 'liquidity_type': 'UniV2'}], lp_total_supply='100', lp_holders=[
                {'address': BURN, 'percent': '0.5', 'is_locked': '1'},
                {'address': TOKEN, 'percent': '0.5', 'is_locked': '1',
                 'locked_detail': [{'amount': '50', 'end_time': str(NOW // 1000 + 60)}]}]), NOW)
        check = assess_risk(result, now=NOW)['safety']['liquidityLock']
        self.assertEqual(check['status'], 'clear')
        self.assertEqual(check['evidence']['lockedPercentMin'], 50)
        self.assertEqual(check['evidence']['burnedPercent'], 50)
        expired = assess_risk(result, now=NOW + 60_001)['safety']['liquidityLock']
        self.assertEqual(expired['status'], 'unknown')
        self.assertEqual(expired['reason'], 'lp-lock-expired')

    def test_v2_unlocked_flag_and_v3_or_multi_pool_unknown(self):
        values = security(dex=[{'pair': POOL, 'liquidity_type': 'UniV2'}], lp_holders=[
            {'address': TOKEN, 'percent': '0.99', 'is_locked': '0'}])
        self.assertIn('liquidity_unlock', assess_risk(normalize_goplus('56', TOKEN, values, NOW), now=NOW)['flags'])
        for dex in ([{'pair': POOL, 'liquidity_type': 'UniV3'}], values['dex'] * 2):
            risk = assess_risk(normalize_goplus('56', TOKEN, {**values, 'dex': dex}, NOW), now=NOW)
            self.assertEqual(risk['safety']['liquidityLock']['status'], 'unknown')
            self.assertNotIn('liquidity_unlock', risk['flags'])


class RiskCollectorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stores = {}
        for chain in ('system', '196', '56', '4663'):
            self.stores[chain] = await ResearchStore(self.tmp.name + '/research.sqlite', chain).connect()
        for chain in ('196', '56', '4663'):
            await self.stores[chain].put('asset', TOKEN, {'token': TOKEN, 'kind': 'candidate',
                'chainId': chain, 'price': 42, 'holders': 456})
        self.now = patch('app.collectors.risk_enrichment.time.time', return_value=NOW / 1000)
        self.now.start()
        self.lookup = patch('app.collectors.risk_enrichment.store', AsyncMock(side_effect=lambda chain: self.stores[chain]))
        self.lookup.start()
        self.sleep = patch('app.collectors.risk_enrichment.asyncio.sleep', AsyncMock())
        self.sleep.start()
        self.env = patch.dict(os.environ, {'GOPLUS_ROUND_LIMIT': '3', 'GOPLUS_DAILY_BUDGET': '10'})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop(); self.sleep.stop(); self.lookup.stop(); self.now.stop()
        for s in self.stores.values():
            await s.close()
        self.tmp.cleanup()

    async def request(self, client, path, params=None):
        if path == '/supported_chains':
            return [{'id': chain} for chain in ('196', '56', '4663')]
        return {TOKEN: security(holder_count='0')}

    async def test_chain_isolation_ttl_and_quote_preservation(self):
        with patch('app.collectors.risk_enrichment._request', AsyncMock(side_effect=self.request)) as request:
            first = await refresh_risk_enrichment()
            second = await refresh_risk_enrichment()
        self.assertEqual(first['accepted'], 3)
        self.assertEqual(second['requested'], 0)
        self.assertEqual(request.await_count, 4)
        for chain in ('196', '56', '4663'):
            row = await self.stores[chain].get('asset', TOKEN)
            self.assertEqual((row['holders'], row['price']), (456, 42))
            self.assertEqual(row['tokenScan']['chainId'], chain)

    async def test_budget_survives_repeated_round_and_counts_support_lookup(self):
        with patch.dict(os.environ, {'GOPLUS_DAILY_BUDGET': '2'}), patch(
                'app.collectors.risk_enrichment._request', AsyncMock(side_effect=self.request)) as request:
            first = await refresh_risk_enrichment()
            second = await refresh_risk_enrichment()
        self.assertEqual(first['accepted'], 1)
        self.assertEqual(request.await_count, 2)
        self.assertEqual(second['requested'], 0)
        self.assertEqual(second['quotaBlocked'], 1)

    async def test_failed_response_keeps_prior_observation_timestamp(self):
        old = normalize_goplus('56', TOKEN, security(), NOW - 7 * 3600_000)
        await self.stores['56'].patch_fact('asset', TOKEN, old)
        async def request(client, path, params=None):
            if path == '/supported_chains':
                return [{'id': '56'}]
            return {}
        with patch('app.collectors.risk_enrichment._request', AsyncMock(side_effect=request)):
            result = await refresh_risk_enrichment()
            again = await refresh_risk_enrichment()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(again['requested'], 0)
        self.assertEqual((await self.stores['56'].get('asset', TOKEN))['tokenScan']['checkedAt'], NOW - 7 * 3600_000)


if __name__ == '__main__':
    unittest.main()
