"""Official Binance payload fixtures plus real SQLite commit/replay semantics."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

os.environ.setdefault('NODE_ENV','test')

from app.collectors.market_streams import (Market,MarketStreamProcessor,trade_to_candle,
    normalize_binance_trade,normalize_binance_candle,merge_quote)
from app.collectors.binance_alpha import map_markets
from app.collectors.exchange_stream import BinanceMarketFeed
from app.db import ResearchStore

TOKEN='0xeec6574eabba52bac3f0277f2cd5ac7e67197886'
MARKET=Market('56',TOKEN,'binance-alpha','ALPHA_1088USDT','USDT','KII')
# Wire schema and values captured from the official public Alpha stream.
TICK={'e':'24hrTicker','E':1790317781817,'s':'ALPHA_1088USDT',
      'c':'0.07945151','o':'0.08000000','q':'10000.12','n':1234,'P':'-0.68'}
TRADE={'e':'aggTrade','E':1790317782019,'T':1790317782018,'s':'ALPHA_1088USDT',
       'a':12345,'f':12345,'l':12345,'m':False,'p':'0.07945151','q':'100.00000000'}
KLINE={'e':'kline','E':1790317781818,'s':'ALPHA_1088USDT','k':{
       't':1790317740000,'T':1790317799999,'s':'ALPHA_1088USDT','i':'1m',
       'o':'0.07940886','h':'0.07947448','l':'0.07938742','c':'0.07945151',
       'v':'1400','q':'111.20','x':False}}


class MappingTests(unittest.TestCase):
    def test_pool_identity_survives_symbol_enrichment(self):
        identity={'chain_id':'56','token':TOKEN,'venue':'dex','market_id':'0x'+'a'*40,
                  'pool_id':'0x'+'a'*40,'quote_token':'0x'+'b'*40}
        original=Market(**identity,quote_currency='0x'+'b'*40)
        enriched=Market(**identity,quote_currency='WBNB')
        self.assertEqual(original.storage,enriched.storage)
        self.assertEqual(enriched.frame()['priceCurrency'],'WBNB')

    def test_quiet_pool_uses_scanner_health_not_trade_age(self):
        from app.api.candles import pool_freshness
        now=1790317783000
        health={'status':'live','updatedAt':now-1000,'lastHead':1000}
        cursor={'block':997,'updatedAt':now-1000}
        meta={'lastSourceEventAt':now-3600000}
        current=pool_freshness(health,cursor,meta,now)
        self.assertFalse(current['stale'])
        self.assertEqual(current['marketStatus'],'quiet')
        self.assertEqual(current['lastTradeAt'],now-3600000)
        self.assertTrue(pool_freshness(health,{**cursor,'updatedAt':now-120000},meta,now)['stale'])
        self.assertTrue(pool_freshness(health,{**cursor,'block':10},meta,now)['stale'])
        self.assertFalse(pool_freshness(health,{**cursor,'block':900,'blockTime':now-5000},meta,now)['stale'])
        self.assertTrue(pool_freshness(health,{**cursor,'blockTime':now-60000},meta,now)['stale'])

    def test_current_pool_trade_is_distinct_from_backfilled_history(self):
        from app.api.candles import pool_freshness
        now=1790317783000
        health={'status':'catching-up','updatedAt':now-500,'lastHead':1000,
                'lastReceivedAt':now-1000,'lastProcessedAt':now-400,'sourceLagMs':1000,'queueDepth':1}
        cursor={'block':100,'blockTime':now-600000,'updatedAt':now-1000}
        recent={'lastSourceEventAt':now-1000,'lastSuccessfulAt':now-400}
        current=pool_freshness(health,cursor,recent,now)
        self.assertFalse(current['stale'])
        self.assertEqual(current['marketStatus'],'live')
        self.assertEqual(current['coverageStatus'],'backfilling')
        old=pool_freshness(health,cursor,{'lastSourceEventAt':now-600000},now)
        self.assertTrue(old['stale'])
        self.assertEqual(old['marketStatus'],'recovering')
        queued=pool_freshness({**health,'queueDepth':500,'sourceLagMs':120000},cursor,recent,now)
        self.assertFalse(queued['stale'])
        self.assertEqual(queued['marketStatus'],'live')
        late=pool_freshness(health,cursor,{'lastSourceEventAt':now-1000,
                                          'lastSuccessfulAt':now-15000},now)
        self.assertTrue(late['stale'])

    def test_identity_currency_and_trading_gate(self):
        token={'chainId':'4663','contractAddress':TOKEN,'alphaId':'ALPHA_1088','symbol':'SAME'}
        pairs={'symbols':[{'status':'TRADING','baseAsset':'ALPHA_1088','quoteAsset':'U','symbol':'ALPHA_1088U'},
                          {'status':'BREAK','baseAsset':'ALPHA_1088','quoteAsset':'USDT','symbol':'ALPHA_1088USDT'},
                          {'status':'TRADING','baseAsset':'SAME','quoteAsset':'USDT','symbol':'SAMEUSDT'}]}
        markets,counts=map_markets([token],pairs)
        self.assertEqual([(m.chain_id,m.market_id,m.quote_currency) for m in markets],[('4663','ALPHA_1088U','U')])
        self.assertEqual(counts['mappedAssets'],1)
        ambiguous={**token,'contractAddress':'0x'+'1'*40}
        self.assertEqual(map_markets([token,ambiguous],pairs)[0],[])

    def test_numeric_order_decimal_and_no_synthetic_bar(self):
        row=None
        for ident,price in [(100,'0.2'),(99,'0.1'),(101,'0.3')]:
            row=trade_to_candle(row,{'id':f'trade:{ident}','t':1790317782000,
                                    'price':price,'quantity':'0.1'},60000)
        self.assertEqual((row['o'],row['c'],row['h'],row['l']),(0.1,0.3,0.3,0.1))
        self.assertEqual(row['v'],0.3)
        self.assertEqual(row['_vExact'],'0.3')
        self.assertIsNone(trade_to_candle(None,{'t':0,'price':1},60000))
        row=None
        for log,price in [('0x10',2),('0x2',1)]:
            row=trade_to_candle(row,{'id':log,'t':1790317782000,'price':price,'quantity':1,
                      'blockNumber':'0x123','logIndex':log},60000)
        self.assertEqual((row['o'],row['c']),(1,2))

    def test_quote_independent_statistics_clock(self):
        result=merge_quote({'price':2,'sourceEventAt':200,'statisticsAt':50,'volume24h':1},
                           {'price':1,'sourceEventAt':100,'statisticsAt':100,'volume24h':2})
        self.assertEqual((result['price'],result['sourceEventAt'],result['volume24h']),(2,200,2))


class StreamPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=await ResearchStore(str(Path(self.temp.name)/'research.sqlite'),'56').connect()
        self.patches=[patch('app.collectors.market_streams.store',AsyncMock(return_value=self.store)),
                      patch.object(MarketStreamProcessor,'_writer_store',AsyncMock(return_value=self.store)),
                      patch('app.collectors.exchange_stream.store',AsyncMock(return_value=self.store)),
                      patch('app.collectors.market_streams.now_ms',return_value=1790317783000),
                      patch('app.collectors.exchange_stream.now_ms',return_value=1790317783000)]
        for item in self.patches:item.start()
        self.processor=MarketStreamProcessor()

    async def asyncTearDown(self):
        for item in self.patches:item.stop()
        await self.store.close()
        self.temp.cleanup()

    async def events(self,event):
        cursor=await self.store.db.execute('SELECT body FROM realtime_events WHERE event=? ORDER BY id',(event,))
        return [json.loads(row[0]) for row in await cursor.fetchall()]

    async def test_reconnect_trade_dedup_and_atomic_outbox(self):
        trade=normalize_binance_trade(TRADE)
        await self.processor.flush([('trade',MARKET,trade),('trade',MARKET,trade)])
        # A fresh processor simulates a worker restart; memory dedup is insufficient.
        await MarketStreamProcessor().flush([('trade',MARKET,trade)])
        self.assertEqual(len(await self.store.recent_trades(MARKET.storage)),1)
        self.assertEqual(len(await self.events('trade')),1)
        self.assertEqual(await self.store.recent_trades(TOKEN),[])
        event=(await self.events('trade'))[0]
        self.assertEqual((event['venue'],event['volumeCurrency'],event['volume']),('binance-alpha','USDT',None))
        with patch('app.realtime_schema.enqueue_events',AsyncMock(side_effect=RuntimeError('failed'))):
            with self.assertRaises(RuntimeError):
                await self.processor.flush([('trade',MARKET,{**trade,'id':'agg:99999'})])
        self.assertFalse(await self.store.has_trade(MARKET.storage,'agg:99999'))

    async def test_stream_batch_inserts_only_new_trades_and_events(self):
        trade=normalize_binance_trade(TRADE)
        messages=[('trade',MARKET,{**trade,'id':f'agg:{index}','sourceId':index}) for index in range(16)]
        with patch.object(self.store.db,'execute_fetchall',wraps=self.store.db.execute_fetchall) as write, \
             patch.object(self.store.db,'executemany',wraps=self.store.db.executemany) as bulk:
            await self.processor.flush(messages)
        trade_sql=[call.args[0] for call in write.call_args_list if 'trades' in call.args[0]]
        self.assertEqual(len(trade_sql),1)
        self.assertIn('RETURNING asset,id',trade_sql[0])
        self.assertNotIn('SELECT asset,id',trade_sql[0])
        self.assertEqual(sum('INSERT INTO realtime_events' in call.args[0] for call in bulk.call_args_list),1)
        self.assertEqual(len(await self.events('trade')),16)
        await self.processor.flush(messages)
        self.assertEqual(len(await self.events('trade')),16)

    async def test_same_price_volume_updates_and_partial_trade_quote(self):
        await self.processor.flush([('quote',MARKET,{'price':1,'volume24h':10,'exchangeTrades24h':5,'statisticsAt':100,'sourceEventAt':100})])
        await self.processor.flush([('quote',MARKET,{'price':1,'volume24h':11,'exchangeTrades24h':6,'statisticsAt':101,'sourceEventAt':101}),
                                    ('quote',MARKET,{'price':1,'sourceEventAt':102})])
        events=await self.events('price')
        self.assertEqual(len(events),2)
        self.assertEqual(events[-1]['volume24h'],11)
        self.assertEqual(events[-1]['exchangeTrades24h'],6)
        latest=await self.store.get('market-quote',MARKET.storage)
        self.assertEqual(latest['sourceEventAt'],102)

    async def test_official_open_candle_close_and_old_correction(self):
        bar,row=normalize_binance_candle(KLINE)
        await self.processor.flush([('candle',MARKET,{'bar':bar,'row':row,'sourceEventAt':200})])
        await self.processor.flush([('candle',MARKET,{'bar':bar,'row':{**row,'confirmed':True},'sourceEventAt':201})])
        await self.processor.flush([('candle',MARKET,{'bar':bar,'row':row,'sourceEventAt':202})])
        older={**row,'t':row['t']-60000,'confirmed':True}
        await self.processor.flush([('candle',MARKET,{'bar':bar,'row':older,'sourceEventAt':100})])
        rows=await self.store.candle_range(MARKET.storage,bar)
        self.assertEqual(len(rows),2)
        self.assertTrue(rows[-1]['confirmed'])
        self.assertEqual((await self.store.get('market-candle',MARKET.storage+':'+bar))['row']['t'],row['t'])
        self.assertEqual((await self.store.get('candle-meta',MARKET.candle_key(bar)))['lastSourceEventAt'],201)

    async def test_subscription_all_markets_independent_of_demand(self):
        feed=BinanceMarketFeed('binance-alpha','unused',AsyncMock(return_value=[MARKET]))
        streams=await feed._streams([MARKET])
        self.assertEqual(streams,{'alpha_1088usdt@ticker','alpha_1088usdt@aggTrade','alpha_1088usdt@kline_1m'})
        await self.store.put('candle-watch','watch',{'venue':'binance-alpha','address':TOKEN,'market':'ALPHA_1088USDT',
             'expiresAt':9999999999999,'bar':'5m'})
        self.assertIn('alpha_1088usdt@kline_5m',await feed._streams([MARKET]))

    async def test_dynamic_demand_keeps_market_and_timeframe_identity(self):
        feed=BinanceMarketFeed('binance-alpha','unused',AsyncMock(return_value=[MARKET]))
        await self.store.put('candle-watch','live',{'venue':'binance-alpha','address':TOKEN.upper(),
            'market':'alpha_1088usdt','expiresAt':9999999999999,'bar':'1H'})
        await self.store.put('candle-watch','other-unit',{'venue':'binance-alpha','address':TOKEN,
            'market':'ALPHA_1088U','expiresAt':9999999999999,'bar':'5m'})
        await self.store.put('candle-watch','expired',{'venue':'binance-alpha','address':TOKEN,
            'market':'ALPHA_1088USDT','expiresAt':1,'bar':'15m'})
        streams=await feed._streams([MARKET])
        self.assertIn('alpha_1088usdt@kline_1h',streams)
        self.assertNotIn('alpha_1088usdt@kline_5m',streams)
        self.assertNotIn('alpha_1088usdt@kline_15m',streams)
        await self.store.put('candle-watch','live',{'venue':'binance-alpha','address':TOKEN,
            'market':'ALPHA_1088USDT','expiresAt':1,'bar':'1H'})
        self.assertNotIn('alpha_1088usdt@kline_1h',await feed._streams([MARKET]))

    async def failed_connections(self,markets):
        feed=BinanceMarketFeed('binance-alpha','unused',AsyncMock(return_value=markets))
        attempts=0
        async def status(**values):
            nonlocal attempts
            if values.get('status')=='reconnecting':
                attempts+=1
                if attempts==2:
                    raise asyncio.CancelledError
        with patch('app.collectors.exchange_stream.websockets.connect',side_effect=OSError('offline')) as connect, \
             patch('app.collectors.exchange_stream.asyncio.sleep',new_callable=AsyncMock), \
             patch.object(feed,'_status',side_effect=status), \
             patch.object(self.store,'all_kv',wraps=self.store.all_kv) as all_kv, \
             patch.object(self.store,'put',wraps=self.store.put) as put:
            with self.assertRaises(asyncio.CancelledError):
                await feed._connection(markets)
        self.assertEqual(connect.call_count,2)
        self.assertEqual([call.args[0] for call in all_kv.await_args_list],['market-registry']*2)
        return [call for call in put.await_args_list if call.args[0]=='market-registry']

    async def test_unchanged_registry_reconnect_does_not_write(self):
        await self.store.put('market-registry',MARKET.storage,MARKET.record())
        self.assertEqual(await self.failed_connections([MARKET]),[])
        self.assertEqual(await self.store.get('market-registry',MARKET.storage),MARKET.record())

    async def test_reconnect_writes_changed_and_new_market_definitions_once(self):
        await self.store.put('market-registry',MARKET.storage,MARKET.record())
        changed=Market('56',TOKEN,'binance-alpha','ALPHA_1088USDT','USDT','RENAMED')
        added=Market('56','0x'+'a'*40,'binance-alpha','ALPHA_2000USDT','USDT','NEW')
        writes=await self.failed_connections([changed,added])
        self.assertEqual([(call.args[1],call.args[2]) for call in writes],
                         [(changed.storage,changed.record()),(added.storage,added.record())])
        self.assertEqual(await self.store.get('market-registry',changed.storage),changed.record())
        self.assertEqual(await self.store.get('market-registry',added.storage),added.record())

    async def test_reconnect_captures_replay_checkpoint_before_socket_opens(self):
        await self.store.put('market-registry',MARKET.storage,MARKET.record())
        old_gap={'status':'retrying','fromSourceId':100}
        await self.store.put('market-gap',MARKET.storage,old_gap)
        await self.store.put_trades(MARKET.storage,[{'id':'agg:100','sourceId':100,'t':100}])
        feed=BinanceMarketFeed('binance-alpha','unused',AsyncMock(return_value=[MARKET]))
        feed.live_ids[MARKET.storage]=999
        recovered=asyncio.Event()
        captured={}
        async def open_socket():
            # Live writes after opening the socket must not move the replay floor.
            await self.store.put('market-gap',MARKET.storage,{'status':'recovering','fromSourceId':200})
            await self.store.put_trades(MARKET.storage,[{'id':'agg:200','sourceId':200,'t':200}])
            return socket
        async def recover(markets,checkpoints):
            captured.update(checkpoints)
            recovered.set()
        async def recv():
            await recovered.wait()
            raise OSError('socket closed')
        async def status(**values):
            if values.get('status')=='reconnecting':
                raise asyncio.CancelledError
        socket=AsyncMock()
        socket.recv.side_effect=recv
        context=MagicMock()
        context.__aenter__=AsyncMock(side_effect=open_socket)
        context.__aexit__=AsyncMock(return_value=None)
        with patch('app.collectors.exchange_stream.websockets.connect',return_value=context), \
             patch.object(feed,'_streams',AsyncMock(return_value=set())), \
             patch.object(feed,'_control',AsyncMock()), \
             patch.object(feed,'_status',side_effect=status), \
             patch.object(feed,'_recover',side_effect=recover) as replay:
            with self.assertRaises(asyncio.CancelledError):
                await feed._connection([MARKET])
        replay.assert_awaited_once()
        self.assertEqual(captured[MARKET.storage][0],old_gap)
        self.assertEqual(captured[MARKET.storage][1]['sourceId'],100)
        self.assertNotIn(MARKET.storage,feed.live_ids)

    async def test_wire_payloads_accepted(self):
        feed=BinanceMarketFeed('binance-alpha','unused',AsyncMock(return_value=[MARKET]))
        for data in (TICK,TRADE,KLINE):
            await feed.handle(MARKET,data)
        pending=[]
        while not feed.processor.queue.empty():pending.append(feed.processor.queue.get_nowait())
        await feed.processor.flush(pending)
        self.assertEqual(len(await self.events('trade')),1)
        self.assertEqual(len(await self.events('candle')),1)
        self.assertEqual((await self.events('price'))[-1]['exchangeTrades24h'],1234)

    async def test_spot_paginated_recovery_persists_before_checkpoint(self):
        market=Market('56',TOKEN,'binance','NVDABUSDT','USDT','NVDAB',underlying='NVDA')
        feed=BinanceMarketFeed('binance','unused',AsyncMock(return_value=[market]))
        feed.live_ids[market.storage]=1103
        rows=[{'id':i,'time':1790317780000+i,'price':'1.2','qty':'3','isBuyerMaker':False}
              for i in range(101,1104)]
        responses=[]
        for page in (rows[:1000],rows[1000:]):
            response=MagicMock();response.json.return_value=page
            responses.append(response)
        client=AsyncMock();client.get.side_effect=responses
        with patch('app.collectors.exchange_stream.httpx.AsyncClient') as factory, patch('app.api.candles.candle_series',AsyncMock(return_value={'rows':[]})):
            factory.return_value.__aenter__=AsyncMock(return_value=client)
            factory.return_value.__aexit__=AsyncMock(return_value=None)
            await feed.recover_market(market,{}, {'sourceId':100})
        self.assertEqual([call.kwargs['params']['fromId'] for call in client.get.call_args_list],[101,1101])
        self.assertTrue(client.get.call_args_list[0].args[0].endswith('/historicalTrades'))
        self.assertEqual((await self.store.get('market-gap',market.storage))['status'],'complete')
        self.assertEqual(len(await self.store.recent_trades(market.storage,2000)),1003)
        self.assertEqual(len(await self.events('trade')),1003)

    async def test_alpha_history_keeps_non_usd_currency_and_market(self):
        from app.api import candles
        market=Market('56',TOKEN,'binance-alpha','ALPHA_1088U','U','KII')
        await self.store.put('market-registry',market.storage,market.record())
        response=MagicMock();response.json.return_value={'code':'000000','data':[
            [1790317740000,'1','2','.5','1.5','3',1790317799999,'4']]}
        client=AsyncMock();client.get.return_value=response
        candles._mem.clear();candles._meta.clear()
        with patch.object(candles,'store',AsyncMock(return_value=self.store)),patch.object(candles.httpx,'AsyncClient') as factory:
            factory.return_value.__aenter__=AsyncMock(return_value=client)
            factory.return_value.__aexit__=AsyncMock(return_value=None)
            result=await candles.candle_series('56',TOKEN,'1m',100,'binance-alpha','ALPHA_1088U')
        self.assertEqual(client.get.call_args.kwargs['params']['symbol'],'ALPHA_1088U')
        self.assertEqual((result['priceCurrency'],result['volumeCurrency']),('U','U'))
        self.assertEqual(len(await self.store.candle_range(market.storage,'1m')),1)
        self.assertEqual(await self.store.candle_range(TOKEN,'1m'),[])

    async def test_history_transaction_does_not_overwrite_newer_live_tail(self):
        from app.api.candles import _put_exchange_history,_put_history_meta
        bar,row=normalize_binance_candle(KLINE)
        await self.processor.flush([('candle',MARKET,{'bar':bar,'row':row,'sourceEventAt':200})])
        await _put_exchange_history(self.store,MARKET.storage,bar,[{**row,'c':1}],MARKET.candle_key(bar),100)
        await _put_history_meta(self.store,MARKET.candle_key(bar),{'source':'REST','lastSuccessfulAt':100,'stale':True},100)
        self.assertEqual((await self.store.candle_range(MARKET.storage,bar))[-1]['c'],row['c'])
        meta=await self.store.get('candle-meta',MARKET.candle_key(bar))
        self.assertEqual(meta['transport'],'websocket')
        self.assertFalse(meta['stale'])

    async def test_late_live_candle_cannot_regress_newer_history_snapshot(self):
        from app.api.candles import _put_exchange_history,_put_history_meta,observation_rows
        bar,row=normalize_binance_candle(KLINE)
        await self.processor.flush([('candle',MARKET,{'bar':bar,'row':row,'sourceEventAt':200})])
        snapshot={**row,'h':row['h']+1,'vu':row['vu']+100}
        await _put_exchange_history(self.store,MARKET.storage,bar,[snapshot],MARKET.candle_key(bar),300)
        await _put_history_meta(self.store,MARKET.candle_key(bar),{'source':'REST','lastSuccessfulAt':350,'stale':False},300)
        await self.processor.flush([('candle',MARKET,{'bar':bar,'row':row,'sourceEventAt':250})])
        saved=(await self.store.candle_range(MARKET.storage,bar))[-1]
        self.assertEqual((saved['h'],saved['vu']),(snapshot['h'],snapshot['vu']))
        meta=await self.store.get('candle-meta',MARKET.candle_key(bar))
        self.assertEqual(meta['lastHistoryRequestAt'],300)
        self.assertEqual(meta['lastSourceEventAt'],200)
        self.assertEqual(observation_rows([saved],meta)[0]['observedAt'],300)
        future={**snapshot,'h':snapshot['h']+1}
        await self.processor.flush([('candle',MARKET,{'bar':bar,'row':future,'sourceEventAt':400})])
        saved=(await self.store.candle_range(MARKET.storage,bar))[-1]
        meta=await self.store.get('candle-meta',MARKET.candle_key(bar))
        self.assertEqual(saved['h'],future['h'])
        self.assertEqual(observation_rows([saved],meta)[0]['observedAt'],400)
        self.assertEqual(meta['lastHistoryRequestAt'],300)
        await _put_exchange_history(self.store,MARKET.storage,bar,[snapshot],MARKET.candle_key(bar),299)
        self.assertEqual((await self.store.candle_range(MARKET.storage,bar))[-1]['h'],future['h'])
        self.assertEqual(observation_rows([saved],{'rowObservedAt':{str(saved['t']):500}})[0]['observedAt'],500)

    async def test_replay_commits_bounded_batches_and_keeps_trade_event_atomic(self):
        trade=normalize_binance_trade(TRADE)
        messages=[('trade',MARKET,{**trade,'id':f'agg:{index}','sourceId':index}) for index in range(300)]
        with patch.object(self.store.db,'commit',wraps=self.store.db.commit) as commit:
            await self.processor.flush(messages)
            self.assertGreaterEqual(commit.await_count,19)
        count=await (await self.store.db.execute('SELECT COUNT(*) FROM trades')).fetchone()
        events=await (await self.store.db.execute("SELECT COUNT(*) FROM realtime_events WHERE event='trade'")).fetchone()
        self.assertEqual((count[0],events[0]),(300,300))
        await self.processor.flush(messages)
        events=await (await self.store.db.execute("SELECT COUNT(*) FROM realtime_events WHERE event='trade'")).fetchone()
        self.assertEqual(events[0],300)

    async def test_pool_history_never_calls_token_usd_provider(self):
        from app.api import candles
        market=Market('56',TOKEN,'dex','0x'+'a'*40,'WBNB','KII',pool_id='0x'+'a'*40)
        await self.store.put('market-registry',market.storage,market.record())
        bar,row=normalize_binance_candle(KLINE)
        await self.processor.flush([('candle',market,{'bar':bar,'row':row,'sourceEventAt':200})])
        with patch.object(candles,'store',AsyncMock(return_value=self.store)),patch.object(candles,'okx_get',AsyncMock()) as okx:
            result=await candles.candle_series('56',TOKEN,bar,100,'dex',pool=market.pool_id)
        okx.assert_not_awaited()
        self.assertEqual(result['priceCurrency'],'WBNB')
        self.assertEqual(result['storage'],market.storage)

    async def test_aggregate_candles_do_not_inherit_native_pool_or_other_market_metadata(self):
        from app.api import candles
        from app.demand_leases import flush_lease_writer
        key=f'dex:56:{TOKEN}:5m'
        pool=Market('56',TOKEN,'dex','0x'+'a'*40,'wSPCXx','KII',pool_id='0x'+'a'*40)
        usd={'t':1790317500000,'o':1,'h':2,'l':1,'c':2,'v':3,'vu':4}
        await self.store.put_candles(TOKEN,'5m',[usd])
        await self.store.put_candles(pool.storage,'5m',[{**usd,'c':999}])
        await self.store.put('candle-meta',key+':'+pool.market_id,{
            **pool.frame(),'source':'On-chain pool','storage':pool.storage,'lastSuccessfulAt':9999999999999})
        # Even a numeric old-limit key cannot carry an unrelated native market.
        await self.store.put('candle-meta',key+':1000',{
            **pool.frame(),'storage':pool.storage,'lastSuccessfulAt':9999999999999})
        await self.store.put('candle-meta',key+':180',{
            'source':'OKX DEX','storage':TOKEN,'priceCurrency':'USD','lastSuccessfulAt':1790317783000})
        with patch.object(candles,'store',AsyncMock(return_value=self.store)):
            response=await candles.get_candles('56',TOKEN,'5m',30,'dex')
            await flush_lease_writer()
        self.assertEqual(response['priceCurrency'],'USD')
        self.assertEqual(response['source'],'OKX DEX')
        self.assertEqual(response['rows'][0]['c'],2)
        self.assertNotIn('marketId',response)
        self.assertNotIn('pool',response)

    async def test_alpha_market_resolution_accepts_wire_identity_case(self):
        from app.api import candles
        await self.store.put('market-registry',MARKET.storage,MARKET.record())
        with patch.object(candles,'store',AsyncMock(return_value=self.store)):
            resolved=await candles.resolve_market('56',TOKEN,'binance-alpha',MARKET.market_id.lower())
            other=await candles.resolve_market('56',TOKEN,'binance-alpha','ALPHA_1088U')
        self.assertEqual(resolved,MARKET)
        self.assertIsNone(other)

    async def test_market_tape_same_millisecond_is_numeric_before_limit(self):
        from app.market_history import market_trades
        await self.store.put('market-registry',MARKET.storage,MARKET.record())
        await self.store.put_trades(MARKET.storage,[
            {'id':f'agg:{i}','sourceId':i,'t':1790317782000,'venue':'binance-alpha',
             'marketId':MARKET.market_id,'token':TOKEN} for i in (9,99,100,1000)])
        self.assertEqual([r['sourceId'] for r in await market_trades(self.store,TOKEN,2)],[1000,100])
        self.assertEqual([r['sourceId'] for r in await market_trades(self.store,limit=2)],[1000,100])
        # Legacy exchange entries without sourceId still use their numeric id.
        await self.store.put_trades(MARKET.storage,[{'id':'trade:99','t':1790317783000},
                                                   {'id':'trade:100','t':1790317783000}])
        self.assertEqual([r['id'] for r in await market_trades(self.store,TOKEN,2)],['trade:100','trade:99'])

    async def test_pool_tape_chain_order_and_global_time_index(self):
        from app.market_history import market_trades,TRADE_ORDER
        market=Market('56',TOKEN,'dex','0x'+'a'*40,'WBNB',pool_id='0x'+'a'*40,quote_token='0x'+'b'*40)
        await self.store.put('market-registry',market.storage,market.record())
        # Deliberately choose transaction hashes in the reverse lexical order.
        await self.store.put_trades(market.storage,[
            {'id':'chain:zzz:0x2','t':1790317782000,'blockNumber':10,'transactionIndex':0,'logIndex':2},
            {'id':'chain:aaa:0x10','t':1790317782000,'blockNumber':10,'transactionIndex':0,'logIndex':16}])
        self.assertEqual([r['logIndex'] for r in await market_trades(self.store,TOKEN,2)],[16,2])
        cursor=await self.store.db.execute("EXPLAIN QUERY PLAN SELECT body FROM trades INDEXED BY trades_scope_time WHERE substr(asset,1,instr(asset,':')-1)=? AND (asset LIKE ? OR asset LIKE ? OR asset LIKE ?) ORDER BY "+TRADE_ORDER+' LIMIT ?',
                         ['56','56:binance:%','56:binance-alpha:%','56:dex:%',100])
        plan=' '.join(str(tuple(row)) for row in await cursor.fetchall())
        self.assertIn('trades_scope_time',plan)
        self.assertIn('SEARCH trades',plan)

    async def test_detail_pool_markets_keep_native_unit_out_of_usd_kpis(self):
        from app.market_history import attach_market_detail
        market=Market('56',TOKEN,'dex','0x'+'a'*40,'WBNB',pool_id='0x'+'a'*40,quote_token='0x'+'b'*40)
        await self.store.put('market-registry',market.storage,market.record())
        await self.store.put('pool',market.pool_id,{'pool':market.pool_id,'liquidityUsd':100,'updatedAt':100})
        await self.store.put('candle-meta',market.candle_key('1m'),{'lastSourceEventAt':200})
        payload={'asset':{'token':TOKEN,'price':10,'priceCurrency':'USD','volume24h':2000},
                 'relations':[{'pool':market.pool_id,'liquidityUsd':150,'liquidityAt':150}]}
        result=await attach_market_detail(payload,self.store)
        self.assertEqual(result['asset']['price'],10)
        self.assertEqual(result['asset']['priceCurrency'],'USD')
        self.assertEqual(result['asset']['volume24h'],2000)
        self.assertEqual(result['poolMarkets'][0]['liquidityUsd'],150)
        self.assertEqual(result['poolMarkets'][0]['lastTradeAt'],200)
        self.assertEqual(result['poolMarkets'][0]['priceCurrency'],'WBNB')
        self.assertEqual(result['poolMarkets'],result['asset']['poolMarkets'])


class StreamWriterIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_connection_slow_read_does_not_hold_stream_write_lock(self):
        temp=tempfile.TemporaryDirectory()
        shared=await ResearchStore(str(Path(temp.name)/'research.sqlite'),'56').connect()
        processor=MarketStreamProcessor()
        entered, release=threading.Event(), threading.Event()
        def hold():
            entered.set()
            release.wait(5)
            return 1
        try:
            with patch('app.collectors.market_streams.store',AsyncMock(return_value=shared)), \
                 patch('app.collectors.market_streams.now_ms',return_value=1790317783000):
                writer=await processor._writer_store('56')
                self.assertIsNot(writer.db,shared.db)
                await shared.db.create_function('hold',0,hold)
                read=asyncio.create_task(shared.db.execute_fetchall('SELECT hold()'))
                await asyncio.wait_for(asyncio.to_thread(entered.wait),1)
                try:
                    trade=normalize_binance_trade(TRADE)
                    await asyncio.wait_for(processor.flush([('trade',MARKET,trade)]),2)
                finally:
                    release.set()
                await read
                self.assertTrue(await shared.has_trade(MARKET.storage,trade['id']))
        finally:
            release.set()
            await processor.stop()
            await shared.close()
            temp.cleanup()


if __name__=='__main__':
    unittest.main()
