import asyncio
import importlib
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ['NODE_ENV'] = 'test'


def tearDownModule():
    from app.db import close_all
    asyncio.run(close_all())

class RealtimeTest(unittest.TestCase):
    def test_quote_sample_is_atomic_and_older_quote_cannot_win(self):
        from app.db import ResearchStore
        from app.collectors.assets import save_asset
        async def run():
            with tempfile.TemporaryDirectory() as d:
                db = await ResearchStore(d+'/research.sqlite', '196').connect()
                try:
                    token = '0x' + '1' * 40
                    await save_asset(db, {'chainIndex':'196','tokenContractAddress':token,
                                          'tokenSymbol':'NEW','price':'2','time':'2000'})
                    await save_asset(db, {'chainIndex':'196','tokenContractAddress':token,
                                          'tokenSymbol':'OLD','price':'1','time':'1000'})
                    asset = await db.get('asset', token)
                    samples = await db.samples(token)
                    self.assertEqual(asset['price'], 2)
                    self.assertEqual(asset['fieldTimes']['price'], 2000)
                    self.assertTrue(samples)
                finally:
                    await db.close()
        asyncio.run(run())

    def test_event_cursor_with_scoped_id_has_no_same_time_gap(self):
        from app.db import ResearchStore
        async def run():
            with tempfile.TemporaryDirectory() as d:
                db = await ResearchStore(d+'/research.sqlite', '196').connect()
                try:
                    for ident in ('a','b','c'):
                        await db.put_event(ident, '0xtoken', {'kind':'test'}, 1234567890000)
                    rows = []
                    cursor = None
                    while True:
                        page = await db.events_page(cursor, limit=1)
                        if not page: break
                        rows.extend(page)
                        cursor = {'t':page[-1]['t'], 'id':page[-1]['id']}
                    self.assertEqual([r['id'] for r in rows], ['196:c','196:b','196:a'])
                finally:
                    await db.close()
        asyncio.run(run())

    def test_chain_abi_bytes_and_registry_digest_are_exact(self):
        from hexbytes import HexBytes
        from app.collectors.main_round import word_address, uint_words
        from app.registry import evidence_digest, evidence_hash
        address = '11' * 20
        self.assertEqual(word_address(HexBytes(bytes.fromhex('00'*12+address))).lower(), '0x'+address)
        encoded = (7).to_bytes(32,'big') + (9).to_bytes(32,'big')
        self.assertEqual(uint_words(HexBytes(encoded), 2), (7,9))
        relation = {'token':'0x'+'1'*40,'stock':'0x'+'2'*40,'stockSide':'0x'+'2'*40,
                    'pool':'0x'+'3'*40,'chainId':'196','ticker':'T','block':1,'checkedAt':2,'liquidityUsd':3}
        self.assertEqual(len(evidence_digest(relation)), 32)
        self.assertTrue(evidence_hash(relation).startswith('0x'))

    def test_trade_patch_preserves_newer_quote_fields(self):
        from app.db import ResearchStore
        async def run():
            with tempfile.TemporaryDirectory() as d:
                db = await ResearchStore(d+'/research.sqlite', '196').connect()
                try:
                    token='0x'+'1'*40
                    await db.put('asset',token,{'token':token,'price':9,'fieldTimes':{'price':9000}})
                    result=await db.patch_fact('asset',token,{'tradeAt':10000,'tradeCoverage':'window'})
                    self.assertEqual(result['price'],9)
                    self.assertEqual(result['fieldTimes']['price'],9000)
                    self.assertEqual(result['tradeAt'],10000)
                finally: await db.close()
        asyncio.run(run())

    def test_chain_scopes_share_one_process_write_lock(self):
        from app.db import ResearchStore
        first = ResearchStore(':memory:', '196')
        second = ResearchStore(':memory:', '56')
        self.assertIs(first._write_lock, second._write_lock)

    def test_enrichment_is_fieldwise_and_chain_scoped(self):
        from app.market_quotes import enrich_asset
        asset = {'chainId':'56','token':'0xa','price':2,'volume24h':4,'fieldTimes':{'price':100,'volume24h':300}}
        snapshot = {'assets':{'56:0xa':{'DexScreener':{'provider':'DexScreener','price':3,'volume24h':1,'updatedAt':200,'fieldTimes':{'price':200,'volume24h':200}}}}}
        with patch('app.market_quotes._read_snapshot', return_value=snapshot):
            enriched = enrich_asset(asset)
            self.assertEqual(enriched['price'], 3)
            self.assertEqual(enriched['volume24h'], 4)
            self.assertEqual(asset['price'], 2)
            self.assertEqual(enrich_asset({**asset,'chainId':'196'})['price'], 2)

    def test_atomic_budget_never_exceeds_limit(self):
        from concurrent.futures import ThreadPoolExecutor
        from app.request_ledger import shared_usage
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, OKX_LEDGER_PATH=d+'/budget.sqlite'):
            shared_usage('2099-01-01')
            def charge(_):
                try: shared_usage('2099-01-01', 20); return True
                except RuntimeError: return False
            with ThreadPoolExecutor(8) as pool:
                self.assertEqual(sum(pool.map(charge, range(80))), 20)
            self.assertEqual(shared_usage('2099-01-01'), 20)
            self.assertEqual(shared_usage('2099-01-02'), 0)

    def test_node_and_python_share_atomic_budget(self):
        import subprocess
        from app.request_ledger import shared_usage
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, OKX_LEDGER_PATH=d+'/shared.sqlite'):
            shared_usage('2099-02-01')
            js = "import {sharedUsage} from './src/lib/request-ledger.ts';let n=0;for(let i=0;i<40;i++){try{sharedUsage('2099-02-01',30);n++}catch{}}console.log(n)"
            child = subprocess.Popen(['node','--import','tsx','--input-type=module','-e',js],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            own=0
            for _ in range(40):
                try: shared_usage('2099-02-01',30);own+=1
                except RuntimeError: pass
            out,err=child.communicate(timeout=15)
            self.assertEqual(child.returncode,0,err)
            self.assertEqual(own+int(out.strip()),30)
            self.assertEqual(shared_usage('2099-02-01'),30)

    def test_append_events_survive_restart_quotes_do_not_evict(self):
        from app import stream_hub as h
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, STREAM_LEDGER_PATH=d+'/events.sqlite'):
            h._db = None; h._clients.clear()
            h.broadcast('trade', {'fresh': [{'id':'one'}]})
            first=h.cursor()
            for i in range(600): h.broadcast('price', {'chainId':'56','token':'a','price':i})
            h.broadcast('relationship', {'relation':{'id':'two'}})
            h._db.close(); h._db=None; h._latest_price.clear(); h._latest_stock.clear()
            frames=b''.join(h.replay_after(first))
            self.assertIn(b'event: relationship', frames)
            self.assertIn(b'event: price', frames)
            self.assertNotIn(b'event: trade', frames)
            self.assertIn(b'event: reset', b''.join(h.replay_after(999999)))
            q=asyncio.Queue(maxsize=1); h._clients.add(q)
            h.broadcast('heartbeat',{}); h.broadcast('heartbeat',{})
            self.assertIsNone(q.get_nowait())
            self.assertNotIn(q,h.clients())
            h._db.close(); h._db=None

    def test_live_queue_carries_sequence_for_exactly_once_replay_watermark(self):
        from app import stream_hub as h
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, STREAM_LEDGER_PATH=d+'/events.sqlite'):
            h._db = None; h._clients.clear()
            q = asyncio.Queue(maxsize=4); h._clients.add(q)
            h.broadcast('relationship', {'id':'evt','relation':{'id':'rel','token':'0xabc'}})
            seq, frame = q.get_nowait()
            self.assertIsInstance(seq, int)
            self.assertIn(f'id: {seq}'.encode(), frame)
            self.assertEqual(seq, h.cursor())
            h._clients.clear(); h._db.close(); h._db=None

    def test_snapshot_free_stream_still_replays_missed_events(self):
        from app import stream_hub as h
        from app.api.stream import get_stream
        async def run():
            with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, STREAM_LEDGER_PATH=d+'/events.sqlite'):
                h._db=None; h._clients.clear()
                try:
                    h.broadcast('price', {'chainId':'56','token':'a','price':1})
                    await h.flush_legacy()
                    first=h.cursor()
                    response=await get_stream(last_event_id=None,snapshot=False)
                    initial=await anext(response.body_iterator)
                    self.assertIn(b'event: hello',initial)
                    self.assertNotIn(b'event: price',initial)
                    await response.body_iterator.aclose()
                    h.broadcast('discovery',{'chainId':'56','token':'b'})
                    await h.flush_legacy()
                    response=await get_stream(last_event_id=str(first),snapshot=False)
                    replay=await anext(response.body_iterator)
                    self.assertIn(b'event: discovery',replay)
                    self.assertNotIn(b'event: price',replay)
                    await response.body_iterator.aclose()
                finally:
                    h._clients.clear(); h._db.close(); h._db=None
        asyncio.run(run())

    def test_relation_transition_uses_same_event_id_for_history_and_stream(self):
        from unittest.mock import AsyncMock, Mock
        from app.collectors.main_round import emit_relation_event
        relation = {'id':'196:pool:token:stock','chainId':'196','token':'0xtoken',
                    'ticker':'T','pool':'0xpool','checkedAt':123,'status':'invalid'}
        fake = Mock()
        fake.put_event = AsyncMock()
        fake.get = AsyncMock(return_value={'symbol':'MEME'})
        fake.key.side_effect = lambda value: f'196:{value}'
        with patch('app.collectors.main_round.broadcast') as publish:
            event_id = asyncio.run(emit_relation_event(fake, relation, 'invalidated', 'changed'))
        fake.put_event.assert_awaited_once()
        self.assertEqual(fake.put_event.await_args.args[0], '196:pool:token:stock:invalidated:123')
        self.assertEqual(event_id, publish.call_args.args[1]['id'])
        self.assertEqual(publish.call_args.args[1]['asset'], '0xtoken')

    def test_concurrent_snapshot_readers_share_one_reload(self):
        from unittest.mock import AsyncMock
        from app import state
        async def run():
            state._reload_task=None
            gate=asyncio.Event()
            async def reload(): await gate.wait()
            with patch.object(state,'_reload_shared_snapshot',AsyncMock(side_effect=reload)) as spy:
                tasks=[asyncio.create_task(state.reload_data()) for _ in range(10)]
                await asyncio.sleep(0);await asyncio.sleep(0)
                gate.set();await asyncio.gather(*tasks)
                self.assertEqual(spy.await_count,1)
            state._reload_task=None
        asyncio.run(run())

    def test_payload_with_existing_basket(self):
        from unittest.mock import AsyncMock
        from app.state import DashboardData
        from time import time
        async def run():
            data=DashboardData()
            data.assets=[{'chainId':'196','kind':'candidate','token':'a','price':1,'fieldTimes':{'price':int(time()*1000)}}]
            data.baskets={'Tech':{'members':[{'token':'a','baseCap':1}]}}
            fake=AsyncMock(); fake.samples.return_value=[]; fake.get.return_value={}
            with patch('app.state.store',AsyncMock(return_value=fake)), patch('app.market_quotes._read_snapshot',return_value={}):
                payload=await data.payload()
            self.assertEqual(payload['unified']['sectors'][0]['quoteCoverage'],{'fresh':1,'total':1})
        asyncio.run(run())

    def test_baskets_keep_chain_identity_and_same_name(self):
        from unittest.mock import AsyncMock
        from app.state import DashboardData
        from time import time
        async def run():
            data=DashboardData()
            now=int(time()*1000)
            data.assets=[
                {'chainId':'196','kind':'candidate','token':'same','symbol':'XL','price':1,'fieldTimes':{'price':now}},
                {'chainId':'56','kind':'candidate','token':'same','symbol':'BSC','price':2,'fieldTimes':{'price':now}},
            ]
            data.baskets={
                ('196','Tech'):{'members':[{'token':'same','baseCap':1}],'baseAt':1},
                ('56','Tech'):{'members':[{'token':'same','baseCap':2}],'baseAt':2},
            }
            fake=AsyncMock(); fake.samples.return_value=[]
            with patch('app.state.store',AsyncMock(return_value=fake)), patch('app.market_quotes._read_snapshot',return_value={}):
                sectors=await data._sectors()
            self.assertEqual([(x['chainId'],x['components'][0]['symbol']) for x in sectors],
                             [('196','XL'),('56','BSC')])
            self.assertEqual(sectors[1]['basketVersion'],2)
        asyncio.run(run())

    def test_exchange_candles_never_share_dex_storage(self):
        from unittest.mock import AsyncMock, MagicMock
        from app.api import candles
        from app.collectors.binance import BSTOCKS
        async def run():
            response=MagicMock();response.json.return_value=[[1000,'1','2','0.5','1.5','3',1999,'4']]
            client=AsyncMock();client.get.return_value=response
            fake=AsyncMock();fake.candle_range.return_value=[]
            address=BSTOCKS[0]['addr'].lower()
            candles._mem.clear()
            with patch.object(candles.httpx,'AsyncClient') as factory, patch.object(candles,'store',AsyncMock(return_value=fake)), patch.object(candles,'okx_get',AsyncMock()) as dex:
                factory.return_value.__aenter__=AsyncMock(return_value=client)
                factory.return_value.__aexit__=AsyncMock(return_value=None)
                await candles.candle_series('56',address,'1D',100,'binance')
                dex.assert_not_awaited()
                self.assertEqual(fake.put_candles.call_args.args[0],'binance:'+address)
                self.assertEqual(client.get.call_args.kwargs['params']['interval'],'1d')
        asyncio.run(run())

    def test_candle_quota_failure_uses_isolated_fallback(self):
        from unittest.mock import AsyncMock
        from app.api import candles
        from app.okx_client import QuotaExceeded
        async def run():
            fake=AsyncMock();fake.get.return_value=None
            rows=[{'t':1000,'o':1,'h':2,'l':1,'c':2,'vu':10}]
            fake.candle_range.return_value=rows
            candles._mem.clear();candles._meta.clear()
            with patch.object(candles,'store',AsyncMock(return_value=fake)), patch.object(candles,'okx_get',AsyncMock(side_effect=QuotaExceeded())), patch.object(candles,'gecko_candles',AsyncMock(return_value=(rows,{'source':'GeckoTerminal','pool':'p','storage':'gecko:p:a'}))):
                result=await candles.candle_series('56','a','5m',30)
                self.assertFalse(result['stale']);self.assertEqual(result['source'],'GeckoTerminal')
                self.assertEqual(result['lastCandleAt'],1000)
                self.assertEqual(result['fallbackReason'],'quota-exhausted')
                self.assertEqual(fake.put_candles.call_args.args[0],'gecko:p:a')
        asyncio.run(run())

    def test_candle_both_sources_failed_retains_stale_provenance(self):
        from unittest.mock import AsyncMock
        from app.api import candles
        from app.okx_client import QuotaExceeded
        async def run():
            fake=AsyncMock();fake.get.return_value={'source':'GeckoTerminal','storage':'gecko:p:a','lastSuccessfulAt':123}
            fake.candle_range.return_value=[{'t':1000}]
            candles._mem.clear();candles._meta.clear()
            with patch.object(candles,'store',AsyncMock(return_value=fake)), patch.object(candles,'okx_get',AsyncMock(side_effect=QuotaExceeded())), patch.object(candles,'gecko_candles',AsyncMock(side_effect=RuntimeError())):
                result=await candles.candle_series('56','a','5m',30)
                self.assertTrue(result['stale']);self.assertEqual(result['error'],'quota-exhausted')
                self.assertEqual(result['lastSuccessfulAt'],123)
                self.assertEqual(result['source'],'GeckoTerminal')
        asyncio.run(run())

    def test_daily_candle_tail_and_boundary(self):
        from app.api.candles import cache_ttl
        with patch('app.api.candles.time.time',return_value=100000):
            self.assertEqual(cache_ttl('1D',[{'t':86400000}],99990),30)
            self.assertEqual(cache_ttl('1m',[{'t':99900000}],99900),0)

    def test_nested_allowance_charges_each_parent(self):
        from app.okx_client import request_allowance, charge, QuotaExceeded, USAGE
        USAGE.daily=0; USAGE.round=0
        with request_allowance(1):
            with request_allowance(10):
                charge(True)
                with self.assertRaises(QuotaExceeded): charge(True)

    def test_priority_reserve_keeps_critical_requests_available(self):
        from app.okx_client import charge, QuotaExceeded, USAGE
        with patch.dict(os.environ, OKX_DAILY_REQUEST_LIMIT='100', OKX_BACKGROUND_RESERVE='20', OKX_CRITICAL_RESERVE='5'):
            USAGE.rollover(); USAGE.daily=80; USAGE.round=0
            with self.assertRaises(QuotaExceeded):
                charge(True, 'background')
            charge(True, 'interactive')
            self.assertEqual(USAGE.daily, 81)
            USAGE.daily=95
            with self.assertRaises(QuotaExceeded):
                charge(True, 'interactive')
            charge(True, 'critical')
            self.assertEqual(USAGE.daily, 96)

    def test_trade_buckets_write_observed_zero_only_inside_coverage(self):
        from app.db import ResearchStore
        async def run():
            with tempfile.TemporaryDirectory() as d:
                db = await ResearchStore(d+'/research.sqlite', '196').connect()
                try:
                    token='0x'+'3'*40
                    start=1_800_000_000_000
                    await db.put_trades(token,[{'id':'one','t':start+60_000,'type':'buy','volume':12.5,'user':'u'}])
                    await db.record_trade_observation(token,start,start+600_000)
                    latest=await db.rebuild_trade_buckets(token,start+600_001)
                    rows=await db.trade_buckets(token,'5m',10)
                    self.assertEqual(len(rows),2)
                    self.assertEqual(rows[0]['volumeUsd'],12.5)
                    self.assertTrue(rows[0]['complete'])
                    self.assertEqual(rows[1]['volumeUsd'],0.0)
                    self.assertTrue(rows[1]['complete'])
                    self.assertEqual(latest['5m']['openTime'],start+300_000)
                finally: await db.close()
        asyncio.run(run())

    def test_quality_rules_separate_history_from_rankings_and_alerts(self):
        from app.data_quality import evaluate_asset
        now=1_800_000_000_000
        relation={'status':'verified','liquidityUsd':2000,'liquidityAt':now}
        current=evaluate_asset({'price':1,'volume24h':10,'fieldTimes':{'price':now,'volume24h':now}},[relation],now)
        self.assertTrue(current['eligible']['relationRanking'])
        self.assertFalse(current['eligible']['marketAlert'])
        live=evaluate_asset({'price':1,'volume24h':10,'observedBucket5mComplete':True,'priceProvenance':{'timeKind':'market'},
                             'fieldTimes':{'price':now,'volume24h':now,'observedVolume5m':now}},[relation],now)
        self.assertEqual(live['tier'],'realtime')
        self.assertTrue(live['eligible']['marketAlert'])
        old=evaluate_asset({'price':1,'volume24h':10,'fieldTimes':{'price':now-20*60_000,'volume24h':now}},[relation],now)
        self.assertEqual(old['tier'],'historical')
        self.assertFalse(old['eligible']['marketRanking'])

    def test_structural_verification_without_current_official_evidence_does_not_enter_metrics(self):
        from app.state import DashboardData
        data=DashboardData()
        data.relations=[{'chainId':'196','token':'0xa','status':'verified','checkedAt':1,'liquidityUsd':5}]
        metrics=data.metrics()
        self.assertEqual(metrics['verifiedPools'],0)
        self.assertEqual(metrics['freshVerifiedPools'],0)

    def test_more_than_50_watchers_keep_unattempted_priority(self):
        from unittest.mock import AsyncMock
        from app.collectors import live_quotes as q
        async def run():
            q._watched.clear()
            tokens=['0x'+format(i,'040x') for i in range(1,76)]
            queue={};calls=[];clock=[10_000_000]
            stores={cid:AsyncMock() for cid in q.CHAINS}
            async def rows(kind):
                if kind=='asset':return [{'token':t,'kind':'candidate'} for t in tokens]
                if kind=='watch':return [{'token':t,'expiresAt':99_000_000} for t in tokens]
                if kind=='collector-job':return list(queue.values())
                return []
            for cid in q.CHAINS:stores[cid].all.return_value=[]
            stores['56'].all.side_effect=rows
            async def scoped(cid):return stores[cid]
            async def fetch(chain,selected,lane,priority='background'):
                calls.extend(selected)
                for t in selected:queue[t]={'domain':'quote','key':t,'lastAttemptAt':clock[0],'lastSuccessAt':clock[0]}
                return {'accepted':len(selected)}
            with patch.object(q,'_fetch_and_merge',fetch), patch.object(q,'store',scoped), patch.object(q,'now_ms',lambda:clock[0]):
                for _ in range(3):
                    await q.refresh_watched();clock[0]+=300_000
            self.assertEqual(len(set(calls)),75)
            self.assertEqual(len(calls),90) # bounded 30 BSC core slots per 5m
            q._watched.clear()
        asyncio.run(run())

    def test_hot_trade_lane_excludes_base_quote_assets(self):
        from unittest.mock import AsyncMock
        from app.collectors import trades
        async def run():
            quote='0x'+'1'*40; meme='0x'+'2'*40
            fake=AsyncMock()
            async def all_rows(kind):
                if kind == 'asset':
                    return [
                        {'token':quote,'symbol':'WBNB','kind':'candidate','volume24h':10_000,'tradeAt':0},
                        {'token':meme,'symbol':'MEME','kind':'candidate','volume24h':100,'tradeAt':0},
                    ]
                if kind == 'relation':
                    return [{'token':quote,'status':'verified'},{'token':meme,'status':'verified'}]
                return []
            fake.all.side_effect=all_rows
            trades._hot_turn=0
            with patch.object(trades,'store',AsyncMock(return_value=fake)), patch.object(trades,'refresh_asset_on_demand',AsyncMock()) as refresh:
                await trades.refresh_hot_trades()
            self.assertEqual(refresh.await_args.args[1],meme)
        asyncio.run(run())

if __name__ == '__main__': unittest.main()
