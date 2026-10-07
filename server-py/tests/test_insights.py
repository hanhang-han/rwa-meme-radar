import copy
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import insights
from app.asset_facts import build_fact_packet, fact_hash, template_lines, validate_model_text
from app.db import ResearchStore
from app.demand_leases import all_lease_kv, flush_lease_writer


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

    async def test_immediate_three_lines_from_real_scope_and_null_missing_fields(self):
        with patch('app.ai.ai_narrate', AsyncMock()) as model:
            result = await insights.request_insight('56', self.token, 'zh')
        model.assert_not_awaited()
        self.assertEqual((result['status'], result['source'], result['stale']),
                         ('ready', 'template', False))
        self.assertEqual(len(result['lines']), 3)
        self.assertTrue(all(len(line) <= 180 for line in result['lines']))
        self.assertEqual(result['facts']['volumeLiquidityRatio'], 70.03)
        self.assertIn('同池成交', result['lines'][1])
        self.assertEqual(result['facts']['top10AdjustedPercent'], 52.5)
        self.assertIsNone(result['facts']['uniqueTraderAddresses'])
        self.assertIsNone(result['facts']['creatorHoldingPercent'])
        self.assertIsNotNone(result['facts']['dataAsOf'])
        self.assertIn('前十持仓 52.5%', result['lines'][2])
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
        invented[1] = invented[1].replace('$2.0M', '$999.9M')
        self.assertIsNone(validate_model_text('\n'.join(invented), facts, 'zh'))
        advised = copy.copy(base)
        advised[1] = '行情：建议买入。'
        self.assertIsNone(validate_model_text('\n'.join(advised), facts, 'zh'))
        fake_risk = copy.copy(base)
        fake_risk[2] = '风险：全部安全。'
        self.assertIsNone(validate_model_text('\n'.join(fake_risk), facts, 'zh'))

    async def test_extreme_real_values_preserve_three_fact_sentences(self):
        self.snapshot['asset']['symbol'] = 'SUPERCALIFRAGILISTIC'
        self.snapshot['asset']['change24h'] = 123456789.12345
        self.snapshot['relations'][0]['ticker'] = 'LONGSTOCKTICKER123456'
        self.snapshot['relations'][0]['stockIdentity']['ticker'] = 'LONGSTOCKTICKER123456'
        self.snapshot['relations'][0]['poolMarket']['volume24h'] = 1e18
        facts = build_fact_packet(self.snapshot, '56', self.token)
        for lang in ('zh', 'en'):
            lines = template_lines(facts, lang)
            self.assertEqual(len(lines), 3)
            self.assertTrue(all(len(line) <= 180 for line in lines))
            self.assertEqual(validate_model_text('\n'.join(lines), facts, lang), lines)

    async def test_env_cannot_enable_model_rewriting_or_paid_demand(self):
        with patch.dict('os.environ', {'INSIGHT_MODEL_PUBLISH': '1', 'DEEPSEEK_API_KEY': 'test'}), \
             patch('app.ai.ai_narrate', AsyncMock()) as model:
            first = await insights.request_insight('56', self.token, 'en')
            await insights.refresh_insights()
            await flush_lease_writer()
        self.assertFalse(insights._model_publication_enabled())
        self.assertEqual(first['source'], 'template')
        self.assertEqual(dict(await all_lease_kv(self.db, 'insight-demand')), {})
        model.assert_not_awaited()

    async def test_old_model_cache_is_never_served(self):
        facts = build_fact_packet(self.snapshot, '56', self.token)
        await self.db.put('insight', self.token + ':zh', {
            'text': 'Model-written cached claim', 'inputHash': fact_hash(facts),
            'promptVersion': insights.PROMPT_VERSION, 'source': 'model'})
        with patch.dict('os.environ', {'INSIGHT_MODEL_PUBLISH': '1'}):
            result = await insights.request_insight('56', self.token, 'zh')
        self.assertEqual(result['source'], 'template')
        self.assertEqual(result['lines'], template_lines(facts, 'zh'))
        self.assertNotIn('Model-written', result['text'])

    async def test_fact_change_immediately_updates_template_and_hash(self):
        first = await insights.request_insight('56', self.token, 'en')
        self.snapshot['relations'][0]['poolMarket']['volume24h'] = 1_000_000
        revised = await insights.request_insight('56', self.token, 'en')
        self.assertEqual(revised['source'], 'template')
        self.assertNotEqual(revised['factHash'], first['factHash'])
        self.assertNotEqual(revised['text'], first['text'])
