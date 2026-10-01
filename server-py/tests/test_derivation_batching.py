import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock,patch

from app.db import ResearchStore
from app.state import DashboardData
from app.comparison_service import ComparisonBatch,new_history_rows,refresh_comparisons
from app.collectors.baskets import refresh_baskets
from tests.test_comparisons import stock,NOW


class DerivationBatchingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.s=await ResearchStore(str(Path(self.temp.name)/'db.sqlite'),'196').connect()

    async def asyncTearDown(self):
        await self.s.close()
        self.temp.cleanup()

    async def test_subject_alert_packet_event_and_outbox_roll_back_together(self):
        batch=ComparisonBatch(self.s,{'comparison-alert-state':{},'comparison':{}})
        packet={'token':'token','premium':{'value':3}}
        await batch.put('comparison-alert-state','token:premium',{'active':True})
        await batch.put('comparison','token',packet)
        await batch.put_event('alert-1','token',{'kind':'spread-expanded','value':3},NOW)
        batch.finish_subject(packet)
        execute=self.s.db.executemany
        async def fail_event(query,args):
            if query.startswith('INSERT OR IGNORE INTO events'):raise RuntimeError('event-write-failed')
            return await execute(query,args)
        with patch.object(self.s.db,'executemany',side_effect=fail_event),patch('app.comparison_service.broadcast') as broadcast:
            with self.assertRaisesRegex(RuntimeError,'event-write-failed'):await batch.flush()
            broadcast.assert_not_called()
        self.assertIsNone(await self.s.get('comparison','token'))
        self.assertIsNone(await self.s.get('comparison-alert-state','token:premium'))
        self.assertEqual((await self.s.fetchone('SELECT COUNT(*) FROM change_outbox'))[0],0)
        with patch('app.comparison_service.broadcast') as broadcast:
            await batch.flush()
            broadcast.assert_called_once_with('comparison',packet)
        self.assertEqual((await self.s.get('comparison','token'))['premium']['value'],3)
        self.assertTrue((await self.s.get('comparison-alert-state','token:premium'))['active'])
        self.assertEqual(len(await self.s.events('token')),1)
        self.assertEqual((await self.s.fetchone('SELECT COUNT(*) FROM change_outbox'))[0],3)

    async def test_subject_commit_retries_busy_without_duplicate_alert(self):
        batch=ComparisonBatch(self.s,{'comparison-alert-state':{},'comparison':{}})
        packet={'token':'token','premium':{'value':3}}
        await batch.put('comparison-alert-state','token:premium',{'active':True})
        await batch.put('comparison','token',packet)
        await batch.put_event('alert-1','token',{'kind':'spread-expanded','value':3},NOW)
        batch.finish_subject(packet)
        execute=self.s.db.execute
        attempts=0
        async def busy_once(query,*args):
            nonlocal attempts
            if query=='BEGIN IMMEDIATE' and attempts==0:
                attempts+=1
                raise sqlite3.OperationalError('database is locked')
            return await execute(query,*args)
        with patch.object(self.s.db,'execute',side_effect=busy_once), \
             patch('app.comparison_service.broadcast') as broadcast, patch('builtins.print'):
            await batch.flush()
        self.assertEqual(attempts,1)
        broadcast.assert_called_once_with('comparison',packet)
        self.assertEqual((await self.s.get('comparison','token'))['premium']['value'],3)
        self.assertEqual(len(await self.s.events('token')),1)
        self.assertEqual((await self.s.fetchone('SELECT COUNT(*) FROM change_outbox'))[0],3)

    async def test_single_dirty_token_only_builds_its_dependency_view(self):
        from app.market_quotes import enrich_asset
        data=DashboardData()
        data.stock_tokens=[stock(tokenContractAddress=f'0xstock{i}') for i in range(300)]
        data.assets=[{'chainId':'196','token':f'0xstock{i}','price':102,'fieldTimes':{'price':NOW}}for i in range(300)]
        data.assets.append({'chainId':'196','token':'0xmeme','price':1,'fieldTimes':{'price':NOW}})
        data.relations=[{'chainId':'196','token':'0xmeme','stock':'0xstock0','stockSide':'0xstock0','pool':'0xpool'}]
        observed=[]
        def views(view,include_comparisons=True,enriched=None):
            observed.append((len(view.stock_tokens),set(enriched)))
            return list(view.stock_tokens)
        with patch('app.comparison_service.store',AsyncMock(return_value=self.s)),patch.object(DashboardData,'stock_views',views),patch('app.comparison_service.enrich_asset',wraps=enrich_asset)as enrich,patch('app.comparison_service.broadcast'),patch('app.comparison_service.time.time',return_value=NOW/1000):
            result=await refresh_comparisons(affected={('196','0xmeme')},data=data)
        self.assertEqual(result['requested'],1)
        self.assertEqual(enrich.call_count,2)
        self.assertEqual(observed,[(1,{('196','0xstock0'),('196','0xmeme')})])
        self.assertEqual(len(data.stock_tokens),300)
        self.assertEqual(len(data.assets),301)

    async def test_unchanged_comparisons_report_completed_without_new_observations(self):
        from app.collectors.scheduler import apply_result
        class Data:
            assets=[]
            relations=[]
            def stock_views(self, include_comparisons=True):
                return [stock(price=None, stockPrice=None)]
        await self.s.put('comparison','0xstock',{'method':'previous-version','premium':{'value':1}})
        with patch('app.comparison_service.store',AsyncMock(return_value=self.s)), \
             patch('app.comparison_service.time.time',return_value=NOW/1000), \
             patch('app.comparison_service.broadcast'):
            first=await refresh_comparisons(data=Data())
            second=await refresh_comparisons(data=Data())
        self.assertEqual((first['processed'], first['changed'], first['unavailable']), (1, 1, 1))
        self.assertEqual((second['requested'], second['processed'], second['changed'], second['accepted']), (1, 1, 0, 0))
        self.assertTrue(second['noChange'])
        task={}
        apply_result(task,second,NOW)
        self.assertEqual((task['status'],task['outcome'],task['lastSuccessAt']),('waiting','no-change',NOW))
        self.assertNotIn('lastDataAt',task)

    async def test_replayed_observation_does_not_count_as_accepted(self):
        row=('subject','fingerprint',NOW,{'at':NOW,'value':5})
        self.assertEqual(await self.s.comparison_samples_batch([row]),1)
        self.assertEqual(await self.s.comparison_samples_batch([row]),0)

    async def test_history_prefilter_keeps_only_missing_fingerprints(self):
        existing=('subject','existing',NOW,{'at':NOW,'value':5})
        fresh=('subject','fresh',NOW+1,{'at':NOW+1,'value':6})
        other=('other','existing',NOW+1,{'at':NOW+1,'value':7})
        await self.s.comparison_samples_batch([existing])
        batch=ComparisonBatch(self.s,{})
        self.assertEqual(await new_history_rows(batch,[existing,fresh,fresh,other]),[fresh,other])
        self.assertEqual(await self.s.comparison_samples_batch(
            await new_history_rows(batch,[existing,fresh,fresh,other])),2)
        self.assertEqual(await new_history_rows(batch,[existing,fresh,other]),[])
        many=[('bulk',str(i),NOW+i,{'at':NOW+i}) for i in range(401)]
        self.assertEqual(await new_history_rows(batch,many),many)
        self.assertEqual(await self.s.comparison_samples_batch(many),401)
        self.assertEqual(await new_history_rows(batch,many),[])

    async def test_history_prefilter_does_not_trust_uncommitted_row(self):
        row=('subject','pending',NOW,{'at':NOW,'value':5})
        batch=ComparisonBatch(self.s,{})
        async with self.s._guard_write():
            await self.s.db.execute('BEGIN IMMEDIATE')
            await self.s.db.execute('INSERT INTO comparison_samples VALUES (?,?,?,?,?)',
                                    (self.s.scope,row[0],row[1],row[2],json.dumps(row[3])))
            # A separate read connection must not see this pending insert.
            self.assertEqual(await new_history_rows(batch,[row]),[row])
            await self.s.db.rollback()
        self.assertEqual(await new_history_rows(batch,[row]),[row])

    async def test_memory_store_keeps_idempotent_insert_fallback(self):
        memory=await ResearchStore(':memory:','196').connect()
        row=('subject','fingerprint',NOW,{'at':NOW})
        try:
            self.assertEqual(await new_history_rows(memory,[row]),[row])
            self.assertEqual(await memory.comparison_samples_batch([row]),1)
            self.assertEqual(await new_history_rows(memory,[row]),[row])
            self.assertEqual(await memory.comparison_samples_batch([row]),0)
        finally:
            await memory.close()

    async def test_unchanged_full_refresh_does_not_request_history_writer(self):
        class Data:
            assets=[]
            relations=[]
            def stock_views(self,include_comparisons=True):
                return [stock()]
        with patch('app.comparison_service.store',AsyncMock(return_value=self.s)), \
             patch('app.comparison_service.time.time',return_value=NOW/1000), \
             patch('app.comparison_service.broadcast'), \
             patch.object(self.s,'comparison_samples_batch',wraps=self.s.comparison_samples_batch) as write:
            first=await refresh_comparisons(data=Data())
            initial_calls=write.await_count
            second=await refresh_comparisons(data=Data())
        self.assertGreater(first['observations'],0)
        self.assertEqual(second['observations'],0)
        self.assertEqual(write.await_count,initial_calls)

    async def test_comparison_history_chunks_release_writer_and_replay_safely(self):
        rows=[('subject',str(i),NOW+i,{'at':NOW+i,'value':i})for i in range(520)]
        execute=self.s.db.executemany
        sizes=[]
        async def record_chunks(query,args):
            if query.startswith('INSERT OR IGNORE INTO comparison_samples'):
                sizes.append(len(args))
            return await execute(query,args)
        with patch.object(self.s.db,'executemany',side_effect=record_chunks):
            self.assertEqual(await self.s.comparison_samples_batch(rows),520)
            self.assertEqual(await self.s.comparison_samples_batch(rows),0)
        self.assertEqual(sizes,[250,250,20,250,250,20])
        self.assertEqual((await self.s.fetchone('SELECT COUNT(*) FROM comparison_samples'))[0],520)

    async def test_shared_source_put_and_history_batch_retry_real_sqlite_writer(self):
        await self.s.db.execute('PRAGMA busy_timeout=10')
        with closing(sqlite3.connect(self.s.path)) as external, patch('builtins.print'):
            external.execute('BEGIN IMMEDIATE')
            external.execute('INSERT INTO facts VALUES (?,?,?)',('196:marker','external','{}'))
            task=asyncio.create_task(self.s.put('shared-source','snapshot',{'signature':'new'}))
            await asyncio.sleep(.08)
            external.commit()
            await task
        self.assertEqual((await self.s.get('shared-source','snapshot'))['signature'],'new')
        with closing(sqlite3.connect(self.s.path)) as external, patch('builtins.print'):
            external.execute('BEGIN IMMEDIATE')
            external.execute('INSERT INTO facts VALUES (?,?,?)',('196:marker','external2','{}'))
            task=asyncio.create_task(self.s.comparison_samples_batch([('subject','fp',NOW,{'at':NOW})]))
            await asyncio.sleep(.08)
            external.commit()
            self.assertEqual(await task,1)
        self.assertEqual((await self.s.fetchone('SELECT COUNT(*) FROM comparison_samples'))[0],1)

    async def test_large_comparison_batch_does_not_commit_per_token(self):
        class Data:
            assets=[]
            relations=[]
            def stock_views(self,include_comparisons=True):
                return [stock(tokenContractAddress=f'0xstock{i}')for i in range(100)]
        with patch('app.comparison_service.store',AsyncMock(return_value=self.s)),patch('app.comparison_service.broadcast')as broadcast,patch('app.comparison_service.time.time',return_value=NOW/1000),patch.object(self.s.db,'commit',wraps=self.s.db.commit)as commit:
            await refresh_comparisons(data=Data())
        self.assertLessEqual(commit.await_count,4)
        self.assertEqual(broadcast.call_count,100)
        self.assertEqual(len(await self.s.all('comparison')),100)
        self.assertEqual(len(await self.s.all('comparison-alert-state')),100)

    async def test_unchanged_basket_observation_does_not_rewrite_baseline_sample_or_view(self):
        official_nvda = '0xc845b2894dbddd03858fd2d643b4ef725fe0849d'
        meme_tokens = ['0x' + f'{i + 1:x}' * 40 for i in range(3)]
        data=DashboardData()
        data.assets=[{'chainId':'196','token':token,'kind':'candidate','symbol':f'M{i}','price':i+1,'marketCap':100,
                     'fieldTimes':{'price':NOW,'marketCap':NOW}}for i,token in enumerate(meme_tokens)]
        data.relations=[{'chainId':'196','token':token,'stock':official_nvda,'stockSide':official_nvda,
                         'token0':token,'token1':official_nvda,'pool':'0x' + f'{i + 5:x}' * 40,
                         'ticker':'NVDA','status':'verified','checkedAt':NOW,
                         'liquidityUsd':2000,'liquidityAt':NOW}for i,token in enumerate(meme_tokens)]
        with patch('app.collectors.baskets.store',AsyncMock(return_value=self.s)),patch('app.collectors.baskets.now_ms',return_value=NOW),patch('app.market_quotes._read_snapshot',return_value={}):
            with patch.object(self.s.db,'commit',wraps=self.s.db.commit)as commit:
                await refresh_baskets(affected={('196',meme_tokens[0])},data=data)
            self.assertEqual(commit.await_count,1)
            base=await self.s.get('basket','芯片')
            with patch.object(self.s.db,'commit',wraps=self.s.db.commit)as commit:
                await refresh_baskets(affected={('196',meme_tokens[0])},data=data)
            self.assertEqual(commit.await_count,0)
        self.assertEqual(await self.s.get('basket','芯片'),base)
        view=await self.s.get('basket-view','芯片')
        self.assertEqual(len(view['history']),1)
        self.assertEqual(len(await self.s.samples(base['sampleKey'])),1)
        self.assertAlmostEqual(view['value'],100)
