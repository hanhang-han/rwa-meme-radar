import copy
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import insights
from app.asset_facts import build_fact_packet, fact_hash, template_lines, validate_model_text
from app.db import ResearchStore
from app.demand_leases import flush_lease_writer


class InsightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = await ResearchStore(self.tmp.name + '/test.sqlite', '56').connect()
        self.token = '0x' + '1' * 40
        self.pool = '0x' + '2' * 40
        self.now = int(time.time() * 1000)
        observed = self.now - 10_000
        self.snapshot = {
            'snapshotAt': self.now,
            'asset': {
                'chainId': '56', 'token': self.token, 'symbol': 'Q', 'name': 'Q币',
                'price': 1.5, 'priceCurrency': 'USD', 'change24h': 125.5,
                'volume24h': 2_010_000, 'volumeCurrency': 'USD',
                'fieldTimes': {'price': observed, 'change24h': observed, 'volume24h': observed},
                'fieldScopes': {'price': 'token', 'change24h': 'token', 'volume24h': 'token'},
                'fieldSources': {'price': 'OKX', 'change24h': 'OKX', 'volume24h': 'OKX'},
                'riskAssessment': {'safety': {
                    'tax': {'status': 'triggered', 'provider': 'GoPlus',
                            'evidence': {'buyTaxPct': 12}},
                    'permissions': {'status': 'unknown'},
                    'concentration': {'status': 'triggered', 'provider': 'VerifiedHolderAPI',
                                      'evidence': {'top10AdjustedPercent': 52.5}},
                }},
            },
            'relations': [{
                'chainId': '56', 'token': self.token, 'pool': self.pool,
                'status': 'verified', 'level': 'A', 'ticker': '700',
                'stockIdentity': {'ticker': '700'},
                'liquidityUsd': 28_700, 'liquidityAt': observed,
                'poolCreatedAt': self.now - 3600_000,
                'poolMarket': {'volume24h': 2_010_000, 'volumeCurrency': 'USD',
                               'updatedAt': observed, 'provider': 'OKX',
                               'scope': 'pool:' + self.pool},
            }],
        }
        self.patches = [
            patch.object(insights, 'store', AsyncMock(return_value=self.db)),
            patch.object(insights, 'read_token_projection', AsyncMock(return_value=self.snapshot)),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        await flush_lease_writer()
        for p in self.patches:
            p.stop()
        await self.db.close()
        self.tmp.cleanup()

    async def test_immediate_four_lines_from_real_scope_and_null_missing_fields(self):
        with patch.object(insights, 'ai_narrate', AsyncMock()) as model:
            result = await insights.request_insight('56', self.token, 'zh')
        model.assert_not_awaited()
        self.assertEqual((result['status'], result['source'], result['stale']),
                         ('ready', 'template', False))
        self.assertEqual(len(result['lines']), 4)
        self.assertTrue(all(len(line) <= 40 for line in result['lines']))
        self.assertEqual(result['facts']['volumeLiquidityRatio'], 70.03)
        self.assertIn('该池成交', result['lines'][1])
        self.assertEqual(result['facts']['top10AdjustedPercent'], 52.5)
        self.assertIsNone(result['facts']['uniqueTraderAddresses'])
        self.assertIsNone(result['facts']['creatorHoldingPercent'])
        self.assertIn('交易地址、创建者持仓等暂无数据', result['lines'][3])
        self.assertIn('税费、前 10 持仓需留意', result['lines'][2])
        self.assertNotIn('股票池净买入', result['text'])

    async def test_b_grade_and_missing_pool_never_get_verified_readout(self):
        for relation in self.snapshot['relations']:
            relation['level'] = 'B'
        self.assertEqual((await insights.request_insight('56', self.token, 'zh'))['status'],
                         'no_verified_pool')
        self.snapshot['relations'][0]['level'] = 'A'
        self.snapshot['relations'][0]['pool'] = None
        self.assertEqual((await insights.request_insight('56', self.token, 'zh'))['status'],
                         'no_verified_pool')

    async def test_unmatched_pool_scope_cannot_create_turnover_ratio(self):
        self.snapshot['relations'][0]['poolMarket']['scope'] = 'pool:' + '0x' + '3' * 40
        result = await insights.request_insight('56', self.token, 'zh')
        self.assertIsNone(result['facts']['poolVolume24hUsd'])
        self.assertIsNone(result['facts']['volumeLiquidityRatio'])
        self.assertNotIn('同池周转', result['lines'][1])

    async def test_stale_markets_force_template_even_when_model_cache_matches(self):
        self.snapshot['snapshotAt'] = self.now - 4 * 60_000
        for key in self.snapshot['asset']['fieldTimes']:
            self.snapshot['asset']['fieldTimes'][key] = self.now - 4 * 60_000
        relation = self.snapshot['relations'][0]
        relation['liquidityAt'] = self.now - 4 * 60_000
        relation['poolMarket']['updatedAt'] = self.now - 4 * 60_000
        facts = build_fact_packet(self.snapshot, '56', self.token)
        digest = fact_hash(facts)
        await self.db.put('insight', self.token + ':zh', {
            'text': '\n'.join(template_lines(facts, 'zh')), 'inputHash': digest,
            'promptVersion': insights.PROMPT_VERSION, 'source': 'model',
        })
        result = await insights.request_insight('56', self.token, 'zh')
        self.assertEqual((result['status'], result['stale'], result['source']),
                         ('stale', True, 'template'))

    async def test_validation_rejects_invented_numbers_advice_and_unsupported_risk(self):
        facts = build_fact_packet(self.snapshot, '56', self.token)
        base = template_lines(facts, 'zh')
        self.assertEqual(validate_model_text('\n'.join(base), facts, 'zh'), base)
        invented = copy.copy(base)
        invented[1] = invented[1].replace('+125.5%', '+999.9%')
        self.assertIsNone(validate_model_text('\n'.join(invented), facts, 'zh'))
        advised = copy.copy(base)
        advised[1] = '行情：建议买入。'
        self.assertIsNone(validate_model_text('\n'.join(advised), facts, 'zh'))
        fake_risk = copy.copy(base)
        fake_risk[2] = '风险：全部安全。'
        self.assertIsNone(validate_model_text('\n'.join(fake_risk), facts, 'zh'))

    async def test_extreme_real_values_still_fit_four_line_limit(self):
        self.snapshot['asset']['symbol'] = 'SUPERCALIFRAGILISTIC'
        self.snapshot['asset']['change24h'] = 123456789.12345
        self.snapshot['relations'][0]['ticker'] = 'LONGSTOCKTICKER123456'
        self.snapshot['relations'][0]['stockIdentity']['ticker'] = 'LONGSTOCKTICKER123456'
        self.snapshot['relations'][0]['poolMarket']['volume24h'] = 1e18
        facts = build_fact_packet(self.snapshot, '56', self.token)
        for lang in ('zh', 'en'):
            lines = template_lines(facts, lang)
            self.assertEqual(len(lines), 4)
            self.assertTrue(all(len(line) <= 40 for line in lines))
            self.assertEqual(validate_model_text('\n'.join(lines), facts, lang), lines)

    async def test_worker_only_uses_exact_hash_and_one_validated_generation(self):
        with patch.dict('os.environ', {'INSIGHT_MODEL_PUBLISH': '1'}), \
             patch.object(insights, 'ai_enabled', return_value=True), \
             patch.object(insights, 'ai_narrate', AsyncMock()) as model:
            first = await insights.request_insight('56', self.token, 'en')
            self.assertEqual(first['source'], 'template')
            model.assert_not_awaited()
            await flush_lease_writer()
            self.assertEqual((await self.db.get('insight-demand', self.token + ':en'))['factHash'],
                             first['factHash'])
            model.return_value = {'text': first['text'], 'at': self.now}
            with patch.object(insights, 'CHAINS', ('56',)):
                await insights.refresh_insights()
                await insights.refresh_insights()
            self.assertEqual(model.await_count, 1)
            result = await insights.request_insight('56', self.token, 'en')
            self.assertEqual(result['source'], 'model')
            self.snapshot['asset']['change24h'] = 127.5
            revised = await insights.request_insight('56', self.token, 'en')
            self.assertEqual(revised['source'], 'template')
            self.assertNotEqual(revised['factHash'], first['factHash'])

    async def test_model_key_alone_does_not_publish_before_manual_review_gate(self):
        facts = build_fact_packet(self.snapshot, '56', self.token)
        await self.db.put('insight', self.token + ':zh', {
            'text': '\n'.join(template_lines(facts, 'zh')),
            'inputHash': fact_hash(facts),
            'promptVersion': insights.PROMPT_VERSION, 'source': 'model',
        })
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'test',
                                    'INSIGHT_MODEL_PUBLISH': ''}), \
             patch.object(insights, 'ai_enabled', return_value=True), \
             patch.object(insights, 'ai_narrate', AsyncMock()) as model:
            result = await insights.request_insight('56', self.token, 'zh')
            with patch.object(insights, 'CHAINS', ('56',)):
                await insights.refresh_insights()
        self.assertEqual(result['source'], 'template')
        self.assertIsNone(await self.db.get('insight-demand', self.token + ':zh'))
        model.assert_not_awaited()

    async def test_rejected_model_output_keeps_template(self):
        with patch.dict('os.environ', {'INSIGHT_MODEL_PUBLISH': '1'}), \
             patch.object(insights, 'ai_enabled', return_value=True), \
             patch.object(insights, 'ai_narrate', AsyncMock(return_value={
                 'text': 'Pair: fabricated 999%\nMarket: 999%\nRisk: clear\nData: now',
                 'at': self.now,
             })):
            await insights.request_insight('56', self.token, 'en')
            await flush_lease_writer()
            with patch.object(insights, 'CHAINS', ('56',)):
                await insights.refresh_insights()
            result = await insights.request_insight('56', self.token, 'en')
        self.assertEqual(result['source'], 'template')
        job = await self.db.get('insight-job', self.token + ':en')
        self.assertEqual(job['status'], 'invalid_output')
