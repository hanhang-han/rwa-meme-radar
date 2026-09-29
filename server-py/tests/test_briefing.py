import asyncio
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from app import ai, briefing
from app.db import ResearchStore

class BriefingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = await ResearchStore(self.tmp.name + '/test.sqlite', 'ai').connect()
        self.patches = [patch.object(briefing, 'store', AsyncMock(return_value=self.db)),
                        patch.object(briefing, 'briefing_base', AsyncMock(return_value={
                            'moverIds': [], 'items': [], 'snapshotId': 'sample',
                            'coverage': {'assets': 1, 'relations': 0}, 'dataAsOf': 1000,
                        })),
                        patch.object(briefing, 'update_streaks')]
        for p in self.patches: p.start()
        briefing._refresh_lock = asyncio.Lock()
        briefing._errors.clear()
        briefing._generating.clear()
        briefing._retry_after.clear()

    async def asyncTearDown(self):
        for p in self.patches: p.stop()
        await self.db.close()
        self.tmp.cleanup()

    async def test_reads_never_generate_including_empty_and_stale(self):
        self.assertIsNone((await briefing.briefing_endpoint('zh'))['text'])
        await self.db.put('briefing', 'zh', {'text': 'old', 'at': 1000})
        result = await briefing.briefing_endpoint('zh')
        self.assertEqual(result['text'], 'old')
        self.assertTrue(result['stale'])
        self.patches[1].new.assert_not_awaited()

    async def test_languages_persist_and_fresh_restart_skips_generation(self):
        await briefing.refresh_briefing()
        await self.db.close()
        await self.db.connect()
        await briefing.refresh_briefing()
        self.assertEqual(self.patches[1].new.await_count, 1)
        zh = await briefing.briefing_endpoint('zh')
        en = await briefing.briefing_endpoint('en')
        self.assertEqual(zh['generationId'], en['generationId'])
        self.assertEqual(zh['snapshotId'], en['snapshotId'])
        self.assertEqual(zh['promptVersion'], 3)
        self.assertEqual(zh['model'], 'template-v3')
        published = await self.db.get('briefing', 'latest')
        self.assertEqual(published['zh']['generationId'], published['en']['generationId'])
        self.assertIsNone(await self.db.get('briefing', 'zh'))
        self.assertIsNone(await self.db.get('briefing', 'en'))

    async def test_language_reads_keep_previous_pair_until_atomic_publish(self):
        await briefing.refresh_briefing()
        published = await self.db.get('briefing', 'latest')
        original_id = published['zh']['generationId']
        for language in ('zh', 'en'):
            published[language]['at'] = 1000
        await self.db.put('briefing', 'latest', published)
        self.patches[1].new.return_value['snapshotId'] = 'next-snapshot'

        started, release = asyncio.Event(), asyncio.Event()
        original_put = self.db.put

        async def pause_before_publish(kind, id, value):
            if kind == 'briefing' and id == 'latest':
                started.set()
                await release.wait()
            await original_put(kind, id, value)

        with patch.object(self.db, 'put', side_effect=pause_before_publish):
            task = asyncio.create_task(briefing.refresh_briefing())
            await started.wait()
            before = [await briefing.briefing_endpoint(lang) for lang in ('zh', 'en')]
            self.assertEqual([row['generationId'] for row in before], [original_id, original_id])
            release.set()
            await task

        after = [await briefing.briefing_endpoint(lang) for lang in ('zh', 'en')]
        self.assertEqual(after[0]['generationId'], after[1]['generationId'])
        self.assertNotEqual(after[0]['generationId'], original_id)
        self.assertEqual(after[0]['snapshotId'], 'next-snapshot')

    async def test_failure_retains_last_success_and_backs_off(self):
        await self.db.put('briefing', 'zh', {'text': 'previous', 'at': 1000})
        with patch.object(briefing, 'briefing_base', AsyncMock(side_effect=RuntimeError('broken'))) as build:
            await briefing.refresh_briefing()
            await briefing.refresh_briefing()
            self.assertEqual(build.await_count, 1)
            result = await briefing.briefing_endpoint('zh')
            self.assertEqual(result['text'], 'previous')
            self.assertEqual(result['status'], 'upstream_failed')

    async def test_overlapping_round_and_page_reads_do_not_duplicate(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def generate():
            started.set()
            await release.wait()
            return {'moverIds': [], 'items': [], 'snapshotId': 'sample',
                    'coverage': {'assets': 1, 'relations': 0}, 'dataAsOf': 1000}
        with patch.object(briefing, 'briefing_base', AsyncMock(side_effect=generate)) as build:
            task = asyncio.create_task(briefing.refresh_briefing())
            await started.wait()
            result = await briefing.briefing_endpoint('zh')
            self.assertIsNone(result['text'])
            await briefing.refresh_briefing()
            release.set()
            await task
            self.assertEqual(build.await_count, 1)

    async def test_ai_cache_uses_milliseconds_and_separate_languages(self):
        with patch.object(ai, 'CACHE', ai._Cache()):
            ai.CACHE.set('briefing', '中文', 'zh', 1800000)
            ai.CACHE.set('briefing', 'English', 'en', 1800000)
            with patch.dict('os.environ', {}, clear=True):
                zh = await ai.ai_narrate('briefing', 'zh', 1800000, {}, '')
                en = await ai.ai_narrate('briefing', 'en', 1800000, {}, '')
            self.assertEqual(zh['text'], '中文')
            self.assertEqual(en['text'], 'English')
            self.assertGreater(zh['at'], 1_000_000_000_000)

    async def test_ai_balance_error_has_long_backoff_without_immediate_retry(self):
        response = MagicMock(status_code=402, text='insufficient balance')
        client = AsyncMock()
        client.post.return_value = response
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)
        ai.LAST_FAILURE.clear()
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'test-key'}), \
             patch.object(ai.httpx, 'AsyncClient', return_value=context):
            result = await ai.ai_narrate('briefing', 'zh', 1000, {}, 'test', force=True)
        self.assertIsNone(result)
        self.assertEqual(client.post.await_count, 1)
        failure = ai.ai_failure('briefing', 'zh')
        self.assertEqual(failure['reason'], 'insufficient_balance')
        self.assertGreater(failure['retryAt'], time.time() * 1000 + 5 * 3_600_000)

    async def test_briefing_base_reads_published_projection_and_counts_real_name_leads(self):
        # The report must refer to the exact dashboard revision already
        # published, rather than rebuilding an independent state on page load.
        self.patches[1].stop()
        payload = {'now': 123456, 'realtime': {'revision': 7, 'cursor': 42}, 'unified': {
            'relations': [
                {'chainId':'196','token':'a','ticker':'AAA','status':'lead','firstSeen':time.time()*1000},
                {'chainId':'56','token':'b','ticker':'AAA','status':'lead','firstSeen':time.time()*1000},
            ],
            'assets': [], 'stockTokens': [], 'distribution': [],
            'metrics': {'verifiedPools':0,'verifiedAssets':0,'newRelations24h':2},
        }}
        with patch.object(briefing, 'read_projection', AsyncMock(return_value=payload)) as read:
            result = await briefing.briefing_base()
        read.assert_awaited_once()
        self.assertEqual(result['snapshotId'], '7:42')
        self.assertEqual(result['dataAsOf'], 123456)
        self.assertEqual(result['newNameLeads24h']['total'], 2)
        self.assertEqual(result['newNameLeads24h']['byTicker'], [{'ticker':'AAA','count':2}])

    async def test_structured_items_use_official_relation_and_exact_contract(self):
        first = '0x' + 'a' * 40
        second = '0x' + 'b' * 40
        row = lambda token, volume, flags: {
            'chainId': '56', 'token': token, 'symbol': 'SAME',
            'volume24h': volume, 'totalLiquidityUsd': 1000,
            'dataQuality': {'eligible': {'marketRanking': True}},
            'riskFlags': flags,
            'riskAssessment': {'checks': {'wash_suspect': {
                'evidence': {'volumeLiquidityRatio': 61}}}},
        }
        unified = {
            'assets': [row(first, 61000, ['wash_suspect']), row(second, 1_000_000, [])],
            'relations': [
                {'chainId': '56', 'token': first, 'status': 'verified', 'level': 'A'},
                {'chainId': '56', 'token': second, 'status': 'verified', 'level': 'B'},
            ],
        }
        items = briefing.briefing_items(unified, 'fixed-revision')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['asset']['address'], first)
        self.assertEqual(items[0]['asset']['chain'], 'BNB Smart Chain')
        self.assertEqual(items[0]['snapshotId'], 'fixed-revision')
        self.assertEqual(items[0]['fields']['volumeLiquidityRatio'], 61)
        self.assertIn('61 倍', briefing.render_briefing(items, 'zh'))
        self.assertNotIn('newNameLeads24h', briefing.render_briefing(items, 'zh'))

    async def test_no_risk_claim_without_assessment(self):
        token = '0x' + 'c' * 40
        items = briefing.briefing_items({
            'assets': [{'chainId': '196', 'token': token, 'symbol': 'LARGE',
                        'volume24h': 1_000_000, 'totalLiquidityUsd': 1000,
                        'dataQuality': {'eligible': {'marketRanking': True}},
                        'riskFlags': []}],
            'relations': [{'chainId': '196', 'token': token, 'level': 'A'}],
        }, 'revision')
        self.assertEqual(items[0]['type'], 'market_activity')
        self.assertNotIn('疑似刷量', briefing.render_briefing(items, 'zh'))

    async def test_unknown_total_liquidity_is_not_filled_from_pair_pool(self):
        token = '0x' + 'd' * 40
        items = briefing.briefing_items({
            'assets': [{'chainId': '196', 'token': token, 'symbol': 'SAMPLE',
                        'volume24h': 5000, 'totalLiquidityUsd': None,
                        'pairLiquidityUsd': 1200,
                        'dataQuality': {'eligible': {'marketRanking': True}}}],
            'relations': [{'chainId': '196', 'token': token, 'level': 'A'}],
        }, 'revision')
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]['fields']['totalLiquidityUsd'])
        self.assertIn('总流动性暂无可核对数据', briefing.render_briefing(items, 'zh'))
        self.assertNotIn('$1.2K', briefing.render_briefing(items, 'zh'))
