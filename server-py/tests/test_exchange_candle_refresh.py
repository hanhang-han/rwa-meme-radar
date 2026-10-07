"""Bounded exchange refresh, exact identity and honest history coverage."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault('NODE_ENV','test')

from app.api import candles
from app.collectors import candles as collector
from app.collectors.market_streams import Market
from app.db import ResearchStore, WriterLock
from app.demand_leases import stop_lease_writer
from test_postgres_core_storage import core_pg


NOW = 1_791_118_500_000
PERIOD = 300_000
TOKEN = '0x'+'6'*40
MARKET = Market('56',TOKEN,'binance-alpha','ALPHA_1108USDT','USDT','DEBIT')


def bar(opened, close=2, confirmed=True):
    return {'t':opened,'o':1,'h':3,'l':1,'c':close,'v':1,'vu':2,'confirmed':confirmed}


def wire(opened, closed_at=None):
    return [opened,'1','3','1','2','1',closed_at or opened+PERIOD-1,'2']


class RefreshContractTests(unittest.TestCase):
    def test_history_limits_cover_recent_corrections_and_cap_long_gap(self):
        self.assertEqual(candles.exchange_history_limit({},'5m',NOW),1000)
        self.assertEqual(candles.exchange_history_limit({'lastHistoryTailAt':NOW},'5m',NOW),3)
        self.assertEqual(candles.exchange_history_limit({'lastHistoryTailAt':NOW-2*PERIOD},'5m',NOW),5)
        self.assertEqual(candles.exchange_history_limit({'lastHistoryTailAt':NOW-7*86_400_000},'5m',NOW),1000)
        for invalid in (None,float('nan'),True,NOW+PERIOD):
            self.assertEqual(candles.exchange_history_limit({'lastHistoryTailAt':invalid},'5m',NOW),1000)

    def test_new_fetch_cannot_make_week_old_history_current(self):
        state=candles.exchange_freshness({'lastHistoryRequestAt':NOW},[bar(NOW-7*86_400_000)],'5m',NOW)
        self.assertTrue(state['stale'])
        self.assertEqual(state['staleReason'],'history-old')
        self.assertNotEqual(state['marketStatus'],'quiet')

    def test_completion_time_cannot_hide_old_request_or_replayed_source_time(self):
        state=candles.exchange_freshness({'lastHistoryRequestAt':NOW-61_000,
            'lastSourceEventAt':NOW-100_000,'lastSuccessfulAt':NOW},[bar(NOW)],'5m',NOW)
        self.assertEqual((state['stale'],state['staleReason']),(True,'collector-overdue'))
        live=candles.exchange_freshness({'lastSourceEventAt':NOW-1000},[bar(NOW)],'5m',NOW)
        self.assertEqual((live['stale'],live['marketStatus']),(False,'live'))


class ExchangeHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=await ResearchStore(':memory:','56').connect()
        await self.store.put('market-registry',MARKET.storage,MARKET.record())
        candles._mem.clear(); candles._meta.clear()

    async def asyncTearDown(self):
        try:
            await stop_lease_writer()
        finally:
            await self.store.close()
            candles._mem.clear(); candles._meta.clear()

    def provider(self, getter):
        client=AsyncMock()
        client.get.side_effect=getter
        factory=MagicMock()
        factory.return_value.__aenter__=AsyncMock(return_value=client)
        factory.return_value.__aexit__=AsyncMock(return_value=None)
        return client,factory

    async def test_warm_refresh_requests_three_bars_and_keeps_confirmed_candle(self):
        await self.store.put_candles(MARKET.storage,'5m',[bar(NOW-PERIOD,9),bar(NOW,2,False)])
        await self.store.put('candle-meta',MARKET.candle_key('5m'),{'lastHistoryTailAt':NOW})
        def get(_url,params):
            response=MagicMock()
            response.json.return_value={'code':'000000','data':[wire(NOW-PERIOD,NOW+1),wire(NOW)]}
            return response
        client,factory=self.provider(get)
        with patch.object(candles.httpx,'AsyncClient',factory),patch.object(candles.time,'time',return_value=NOW/1000):
            result=await candles.candle_series('56',TOKEN,'5m',1000,'binance-alpha',MARKET.market_id,candle_store=self.store)
        self.assertEqual(client.get.call_args.kwargs['params']['limit'],3)
        self.assertEqual(result['rows'][0]['c'],9)
        self.assertEqual(result['nextRefreshAt'],NOW+30_000)
        self.assertFalse(result['stale'])
        self.assertEqual(await self.store.candle_range(TOKEN,'5m'),[])

    async def test_seven_day_gap_uses_bounded_windows_and_survives_live_refresh(self):
        old=NOW-7*86_400_000
        await self.store.put_candles(MARKET.storage,'5m',[bar(old)])
        await self.store.put('candle-meta',MARKET.candle_key('5m'),{'lastHistoryTailAt':old})
        def get(_url,params):
            if 'startTime' in params:
                timestamps=range(params['startTime'],params['endTime']+1,PERIOD)
            else:
                timestamps=range(NOW-(params['limit']-1)*PERIOD,NOW+1,PERIOD)
            response=MagicMock()
            response.json.return_value={'code':'000000','data':[wire(at) for at in timestamps]}
            return response
        client,factory=self.provider(get)
        with patch.object(candles.httpx,'AsyncClient',factory),patch.object(candles.time,'time',return_value=NOW/1000):
            first=await candles.candle_series('56',TOKEN,'5m',1000,'binance-alpha',MARKET.market_id,candle_store=self.store)
        gap=first['historyGap']
        self.assertEqual(first['coverageStatus'],'backfilling')
        self.assertFalse(first['stale'])
        self.assertEqual((gap['to']-gap['from'])//PERIOD+1,16)
        requests=[call.kwargs['params'] for call in client.get.call_args_list]
        self.assertEqual([request['limit'] for request in requests],[1000,1000])
        self.assertEqual(requests[1]['startTime'],old+PERIOD)
        self.assertLessEqual(requests[1]['endTime']-requests[1]['startTime']+1,1000*PERIOD)
        candles._mem.clear()
        with patch.object(candles.httpx,'AsyncClient',factory),patch.object(candles.time,'time',return_value=(NOW+31_000)/1000):
            second=await candles.candle_series('56',TOKEN,'5m',1000,'binance-alpha',MARKET.market_id,candle_store=self.store)
        self.assertIsNone(second['historyGap'])
        self.assertEqual(second['coverageStatus'],'current')
        self.assertEqual([call.kwargs['params']['limit'] for call in client.get.call_args_list],[1000,1000,3,16])
        count=(await self.store.fetchone('SELECT count(*) FROM candles WHERE asset=?',(self.store.key(MARKET.storage),)))[0]
        self.assertEqual(count,2017)
        meta=await self.store.get('candle-meta',MARKET.candle_key('5m'))
        self.assertEqual(meta['lastHistoryTailAt'],NOW)

    async def test_ignored_backfill_bounds_remain_unverified(self):
        await self.store.put('candle-meta',MARKET.candle_key('5m'),{
            'lastHistoryTailAt':NOW,'historyGap':{'from':NOW-10*PERIOD,'to':NOW-5*PERIOD}})
        def get(_url,params):
            response=MagicMock()
            response.json.return_value={'code':'000000','data':[wire(NOW)]}
            return response
        _,factory=self.provider(get)
        with patch.object(candles.httpx,'AsyncClient',factory),patch.object(candles.time,'time',return_value=NOW/1000):
            result=await candles.candle_series('56',TOKEN,'5m',1000,'binance-alpha',MARKET.market_id,candle_store=self.store)
        self.assertFalse(result['stale'])
        self.assertEqual(result['coverageStatus'],'backfilling')
        self.assertEqual(result['historyGap']['from'],NOW-10*PERIOD)
        self.assertEqual(result['historyGap']['error'],'upstream-unavailable')

    async def test_history_batches_release_writer_and_preserve_realtime_newer_tail(self):
        rows=[bar(NOW-i*PERIOD) for i in range(129,0,-1)]
        with patch.object(self.store.db,'commit',wraps=self.store.db.commit) as commit:
            await candles._put_exchange_history(self.store,MARKET.storage,'5m',rows,MARKET.candle_key('5m'),NOW)
        self.assertEqual(commit.await_count,3)
        self.assertEqual(len(await self.store.candle_range(MARKET.storage,'5m',1000)),129)
        await self.store.put('candle-meta',MARKET.candle_key('5m'),{'lastSourceEventAt':NOW+10,'lastHistoryTailAt':NOW-PERIOD})
        await candles._put_exchange_history(self.store,MARKET.storage,'5m',[bar(NOW,7,False)],MARKET.candle_key('5m'),NOW)
        self.assertEqual((await self.store.candle_range(MARKET.storage,'5m'))[-1]['t'],NOW-PERIOD)

    async def test_live_tick_keeps_its_clocks_without_undoing_verified_backfill(self):
        key=MARKET.candle_key('5m')
        gap={'from':NOW-10*PERIOD,'to':NOW-5*PERIOD}
        await self.store.put('candle-meta',key,{'lastHistoryTailAt':NOW,'historyGap':gap})
        async def get(_url,params):
            response=MagicMock()
            if 'startTime' in params:
                # A websocket update arriving during the historical HTTP call
                # still contains the previously pending gap.
                latest=await self.store.get('candle-meta',key)
                await self.store.put('candle-meta',key,{**latest,'historyGap':gap,
                    'lastSourceEventAt':NOW+10,'lastSuccessfulAt':NOW+10,'stale':False})
                response.json.return_value={'code':'000000','data':[
                    wire(at) for at in range(params['startTime'],params['endTime']+1,PERIOD)]}
            else:
                response.json.return_value={'code':'000000','data':[wire(NOW)]}
            return response
        _,factory=self.provider(get)
        with patch.object(candles.httpx,'AsyncClient',factory),patch.object(candles.time,'time',return_value=NOW/1000):
            result=await candles.candle_series('56',TOKEN,'5m',1000,'binance-alpha',MARKET.market_id,candle_store=self.store)
        self.assertIsNone(result['historyGap'])
        self.assertEqual(result['lastSourceEventAt'],NOW+10)
        self.assertEqual(result['lastSuccessfulAt'],NOW+10)
        self.assertEqual(result['coverageStatus'],'current')

    async def test_older_rest_request_cannot_clear_newer_history_gap(self):
        key=MARKET.candle_key('5m')
        gap={'from':NOW-10*PERIOD,'to':NOW-5*PERIOD}
        latest={'lastHistoryRequestAt':NOW+10,'historyGap':gap,'lastSuccessfulAt':NOW+20}
        await self.store.put('candle-meta',key,latest)
        merged=await candles._put_history_meta(self.store,key,{
            'lastHistoryRequestAt':NOW,'historyGap':None,'lastSuccessfulAt':NOW+30},NOW)
        self.assertEqual(merged['historyGap'],gap)
        self.assertEqual(merged['lastHistoryRequestAt'],NOW+10)
        self.assertEqual(merged['lastSuccessfulAt'],NOW+20)


class CollectorLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_exchange_has_own_slots_private_connections_and_failed_job_is_fair(self):
        with tempfile.TemporaryDirectory() as folder:
            write_lock=WriterLock()
            stores={chain:await ResearchStore(folder+'/research.sqlite',chain,write_lock=write_lock).connect()
                    for chain in ('196','56','4663')}
            shared=stores['56']
            collector._attempts.clear()
            leases=[]
            for index in range(30):
                leases.append(('watch'+str(index),{'expiresAt':NOW+120000,'bar':'5m',
                    'address':'0x'+format(index+1,'040x'),'venue':'binance-alpha','market':'ALPHA_'+str(index)+'USDT'}))
            leases.extend(('dex'+str(index),{'expiresAt':NOW+120000,'bar':'5m',
                'address':'0x'+format(index+100,'040x'),'venue':'dex'}) for index in range(3))
            connections=[]; calls=[]; active=0; maximum=0
            async def refresh(_chain,address,_bar,_limit,venue,*args,candle_store=None):
                nonlocal active,maximum
                self.assertIsNot(candle_store,shared)
                self.assertIsNot(candle_store.db,shared.db)
                connections.append(candle_store.db)
                calls.append((address,venue))
                active+=1; maximum=max(maximum,active)
                try:
                    await asyncio.sleep(.02)
                    if address==leases[0][1]['address']:
                        raise RuntimeError('synthetic job failure')
                    return {'rows':[bar(NOW)],'stale':venue=='dex','error':'quota-exhausted' if venue=='dex' else None}
                finally:
                    active-=1
            try:
                with patch.object(collector,'store',AsyncMock(side_effect=lambda chain:stores[chain])), \
                     patch.object(collector,'all_lease_kv',AsyncMock(side_effect=lambda s,_: leases if s.scope=='56' else [])), \
                     patch.object(collector.time,'time',return_value=NOW/1000), \
                     patch.object(candles,'candle_series',side_effect=refresh):
                    result=await collector.refresh_watched_candles()
                self.assertEqual(result,{'requested':14,'accepted':11,'failed':3,'quotaBlocked':2})
                self.assertEqual([venue for _,venue in calls],['binance-alpha']*12+['dex']*2)
                self.assertLessEqual(maximum,4)
                self.assertGreater(maximum,1)
                self.assertEqual(len(set(map(id,connections))),14)
                self.assertTrue(all(connection._connection is None for connection in connections))
                # Failure advanced its scheduling age despite no metadata commit.
                selected=sorted(leases[:30],key=lambda item:collector._attempts.get(('56',item[0]),0))[0]
                self.assertEqual(selected[0],'watch12')
                for tick in (1,2):
                    with patch.object(collector,'store',AsyncMock(side_effect=lambda chain:stores[chain])), \
                         patch.object(collector,'all_lease_kv',AsyncMock(side_effect=lambda s,_: leases if s.scope=='56' else [])), \
                         patch.object(collector.time,'time',return_value=(NOW+tick*5000)/1000), \
                         patch.object(candles,'candle_series',side_effect=refresh):
                        await collector.refresh_watched_candles()
                seen={address for address,venue in calls if venue=='binance-alpha'}
                self.assertEqual(seen,{lease['address'] for _,lease in leases[:30]})
                self.assertTrue(all(connection._connection is None for connection in connections))
            finally:
                collector._attempts.clear()
                for scoped in stores.values():
                    await scoped.close()


def test_postgres_exchange_history_batches_protect_confirmed_bars_and_rollback(core_pg):
    sources,_=core_pg
    async def check():
        scoped=await ResearchStore(str(sources['research']),'56',write_lock=WriterLock()).connect()
        try:
            assert scoped.db.backend=='postgres'
            rows=[bar(NOW-i*PERIOD) for i in range(129,0,-1)]
            await candles._put_exchange_history(scoped,MARKET.storage,'5m',rows,MARKET.candle_key('5m'),NOW)
            assert len(await scoped.candle_range(MARKET.storage,'5m',1000))==129
            await candles._put_exchange_history(scoped,MARKET.storage,'5m',
                [bar(NOW-PERIOD,9,False)],MARKET.candle_key('5m'),NOW+10)
            assert (await scoped.candle_range(MARKET.storage,'5m'))[-1]['c']==2
            metadata=await scoped.get('candle-meta',MARKET.candle_key('5m'))
            outbox=(await scoped.fetchone('SELECT count(*) FROM change_outbox'))[0]
            original=scoped.db.executemany
            async def failing(query,parameters):
                await original(query,parameters)
                raise RuntimeError('synthetic-batch-write-failure')
            with patch.object(scoped.db,'executemany',side_effect=failing):
                with pytest.raises(RuntimeError,match='synthetic-batch-write-failure'):
                    await candles._put_exchange_history(scoped,MARKET.storage,'5m',
                        [bar(NOW-1000*PERIOD)],MARKET.candle_key('5m'),NOW+20)
            assert len(await scoped.candle_range(MARKET.storage,'5m',1000))==129
            assert await scoped.get('candle-meta',MARKET.candle_key('5m'))==metadata
            assert (await scoped.fetchone('SELECT count(*) FROM change_outbox'))[0]==outbox
            key=MARKET.candle_key('5m')
            gap={'from':NOW-10*PERIOD,'to':NOW-5*PERIOD}
            await scoped.put('candle-meta',key,{**metadata,'historyGap':gap,
                'lastSourceEventAt':NOW+30,'lastSuccessfulAt':NOW+30})
            merged=await candles._put_history_meta(scoped,key,{
                'historyGap':None,'lastSuccessfulAt':NOW+40},NOW+10)
            assert merged['historyGap'] is None
            assert merged['lastSourceEventAt']==NOW+30
            assert merged['lastSuccessfulAt']==NOW+30
            await scoped.put('candle-meta',key,{**merged,'historyGap':gap,'lastHistoryRequestAt':NOW+50})
            merged=await candles._put_history_meta(scoped,key,{'historyGap':None},NOW+10)
            assert merged['historyGap']==gap
            assert merged['lastHistoryRequestAt']==NOW+50
        finally:
            await scoped.close()
    asyncio.run(check())


def test_postgres_parallel_exchange_refresh_connections_do_not_mix_markets(core_pg):
    sources,_=core_pg
    async def check():
        guard=WriterLock()
        first=await ResearchStore(str(sources['research']),'56',write_lock=guard).connect()
        second=await ResearchStore(str(sources['research']),'56',write_lock=guard).connect()
        other=Market('56','0x'+'7'*40,'binance-alpha','ALPHA_1110USDT','USDT','OTHER')
        try:
            assert first.db is not second.db and first.db.backend==second.db.backend=='postgres'
            await asyncio.gather(
                candles._put_exchange_history(first,MARKET.storage,'5m',
                    [bar(NOW-i*PERIOD,2) for i in range(130,0,-1)],MARKET.candle_key('5m'),NOW),
                candles._put_exchange_history(second,other.storage,'5m',
                    [bar(NOW-i*PERIOD,5) for i in range(130,0,-1)],other.candle_key('5m'),NOW))
            first_rows=await first.candle_range(MARKET.storage,'5m',1000)
            second_rows=await second.candle_range(other.storage,'5m',1000)
            assert len(first_rows)==len(second_rows)==130
            assert {row['c'] for row in first_rows}=={2}
            assert {row['c'] for row in second_rows}=={5}
            assert (await first.get('candle-meta',MARKET.candle_key('5m')))['lastHistoryTailAt']==NOW-PERIOD
            assert (await second.get('candle-meta',other.candle_key('5m')))['lastHistoryTailAt']==NOW-PERIOD
        finally:
            await first.close(); await second.close()
    asyncio.run(check())


if __name__=='__main__':
    unittest.main()
