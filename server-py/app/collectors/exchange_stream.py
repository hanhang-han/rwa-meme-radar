"""Bounded Binance-protocol subscriptions shared by bStocks and Alpha."""
import asyncio
import json
import math
import random

import httpx
import websockets

from ..db import store
from ..demand_leases import all_lease_kv
from .market_streams import (BAR_MS, MarketStreamProcessor, finite, now_ms,
                             normalize_binance_candle, normalize_binance_trade)


class BinanceMarketFeed:
    def __init__(self, venue, endpoint, markets, *, source='Binance', on_quote=None):
        self.venue, self.endpoint, self.markets = venue, endpoint, markets
        self.source, self.on_quote = source, on_quote
        self.processor=MarketStreamProcessor(source)
        self.processor.on_quote=on_quote
        self.task=None
        self.connections={}
        self.state={'status':'starting','activeConnections':0,'mappedMarkets':0,'error':None}
        self.rest_gate=asyncio.Semaphore(2)
        self.rest_rate_lock=asyncio.Lock()
        self._next_rest=0.0
        self._last_status=0
        self.live_ids={}

    async def _get_replay(self,client,endpoint,params):
        # Independent from viewer count: at most four replay requests/second
        # per provider, with only two in flight. Historical Spot weight is 25.
        async with self.rest_rate_lock:
            loop=asyncio.get_running_loop()
            await asyncio.sleep(max(0,self._next_rest-loop.time()))
            self._next_rest=loop.time()+.25
        return await client.get(endpoint,params=params)

    def start(self):
        if self.task is None or self.task.done():
            self.processor.start()
            self.task=asyncio.create_task(self._supervise(),name=self.venue+'-streams')
        return self.task

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
            self.task=None
        await self.processor.stop()

    async def _status(self, **values):
        self.state.update(values)
        self.state['updatedAt']=now_ms()
        self.state['queueDepth']=self.processor.queue.qsize()
        self.state['writerError']=self.processor.error
        self.state['lastPersistedAt']=self.processor.last_persisted_at
        # Persistent health, including gaps; this does not manufacture market ticks.
        if now_ms()-self._last_status>2000 or values.get('status') in ('reconnecting','error'):
            # Health persistence is observational: a failed write must not
            # escape the recovery handler and terminate every socket. Throttle
            # failed attempts too, so each incoming tick cannot retry the DB.
            self._last_status=now_ms()
            try:
                self.state['healthWriteError']=None
                await (await store('56')).put('market-stream-status',self.venue,dict(self.state))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.state.update(healthWriteError=type(error).__name__[:80],
                                  healthWriteFailedAt=now_ms())

    async def _supervise(self):
        jobs={}
        try:
            while True:
                try:
                    markets=await self.markets()
                    # 100 markets => 300 baseline streams, well below Spot's
                    # 1,024 stream limit. No asset is selected by page popularity.
                    groups=[markets[i:i+100] for i in range(0,len(markets),100)]
                    self.state.update(desiredMarkets=len(markets),maximumMarkets=1200)
                    # Hard capacity must be observable rather than silently drop.
                    if len(groups)>12:
                        raise RuntimeError('market-subscription-capacity-exceeded')
                    desired={tuple((m.chain_id,m.storage) for m in group):group for group in groups}
                    removed=[]
                    for key in list(jobs):
                        if key not in desired or jobs[key].done():
                            old=jobs.pop(key)
                            old.cancel()
                            removed.append(old)
                    if removed:
                        await asyncio.gather(*removed,return_exceptions=True)
                    for key,group in desired.items():
                        if key not in jobs:
                            jobs[key]=asyncio.create_task(self._connection(group),name=self.venue+'-socket')
                    await self._status(mappedMarkets=len(markets),connections=len(jobs))
                except Exception as error:
                    await self._status(status='error',error=type(error).__name__)
                await asyncio.sleep(20)
        finally:
            for task in jobs.values():
                task.cancel()
            await asyncio.gather(*jobs.values(),return_exceptions=True)

    async def _streams(self,markets):
        trade='aggTrade' if self.venue=='binance-alpha' else 'trade'
        streams={m.market_id.lower()+suffix for m in markets for suffix in ('@ticker','@'+trade,'@kline_1m')}
        by_asset={(m.chain_id,m.token.lower(),m.market_id):m for m in markets}
        now=now_ms()
        for chain in {m.chain_id for m in markets}:
            for _,lease in await all_lease_kv(await store(chain), 'candle-watch'):
                if lease.get('venue')!=self.venue or lease.get('expiresAt',0)<=now or lease.get('bar') not in BAR_MS:
                    continue
                for (c,token,market_id),market in by_asset.items():
                    if c==chain and token==str(lease.get('address') or '').lower() and (not lease.get('market') or str(lease['market']).upper()==market_id):
                        streams.add(market_id.lower()+'@kline_'+lease['bar'].lower())
        return streams

    async def _control(self,ws,method,streams):
        streams=sorted(streams)
        for offset in range(0,len(streams),150):
            await ws.send(json.dumps({'method':method,'params':streams[offset:offset+150],'id':now_ms()}))
            await asyncio.sleep(.3)

    async def _connection(self,markets):
        by_symbol={m.market_id:m for m in markets}
        retry=0
        while True:
            recovery=None
            connected=False
            try:
                # Captured before opening WS so live writes cannot advance the
                # replay lower bound while recovery is still running.
                checkpoints={}
                stores={chain:await store(chain) for chain in {m.chain_id for m in markets}}
                registry={chain:dict(await s.all_kv('market-registry'))
                          for chain,s in stores.items()}
                for market in markets:
                    s=stores[market.chain_id]
                    record=market.record()
                    if registry[market.chain_id].get(market.storage)!=record:
                        await s.put('market-registry',market.storage,record)
                        registry[market.chain_id][market.storage]=record
                    gap=await s.get('market-gap',market.storage) or {}
                    recent=await s.recent_trades(market.storage,1)
                    checkpoints[market.storage]=(gap,recent[0] if recent else None)
                    self.live_ids.pop(market.storage,None)
                async with websockets.connect(self.endpoint, ping_interval=20,ping_timeout=20,
                        open_timeout=20,max_queue=128,max_size=2**21,proxy=None) as ws:
                    connected=True
                    self.state['activeConnections']=self.state.get('activeConnections',0)+1
                    subscribed=await self._streams(markets)
                    await self._control(ws,'SUBSCRIBE',subscribed)
                    await self._status(status='connected',error=None)
                    recovery=asyncio.create_task(self._recover(markets,checkpoints))
                    last_control=now_ms()
                    while True:
                        # A newly opened chart should not wait a full 20-second
                        # catalogue cycle for its timeframe subscription.
                        if now_ms()-last_control>=5000:
                            desired=await self._streams(markets)
                            if desired-subscribed:
                                await self._control(ws,'SUBSCRIBE',desired-subscribed)
                            if subscribed-desired:
                                await self._control(ws,'UNSUBSCRIBE',subscribed-desired)
                            subscribed=desired
                            last_control=now_ms()
                        try:
                            # Also wake the control loop for a quiet socket;
                            # lack of trades must not postpone a new watch.
                            timeout=max(.1,min(10,(last_control+5000-now_ms())/1000))
                            message=await asyncio.wait_for(ws.recv(),timeout=timeout)
                        except asyncio.TimeoutError:
                            # Quiet markets are legitimate; protocol ping/pong
                            # alone determines transport liveness.
                            continue
                        envelope=json.loads(message)
                        data=envelope.get('data',envelope)
                        if envelope.get('code') is not None:
                            raise RuntimeError('subscription-rejected:'+str(envelope['code']))
                        if not isinstance(data,dict):
                            continue
                        market=by_symbol.get(str(data.get('s') or (data.get('k') or {}).get('s') or '').upper())
                        if not market:
                            continue
                        await self.handle(market,data)
                        retry=0
                        await self._status(status='live',lastMessageAt=now_ms(),error=None)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._status(status='reconnecting',error=type(error).__name__)
            finally:
                if connected:
                    self.state['activeConnections']=max(0,self.state.get('activeConnections',1)-1)
                if recovery:
                    recovery.cancel()
                    await asyncio.gather(recovery,return_exceptions=True)
            retry=min(retry+1,6)
            await asyncio.sleep(min(60,2**retry)+random.random())

    async def handle(self,market,data):
        event=data.get('e')
        received=now_ms()
        if event in ('24hrTicker','24hrMiniTicker'):
            price=finite(data.get('c'),positive=True)
            if price is None:
                return
            open24=finite(data.get('o'),positive=True)
            change=None
            try:
                change=float(data['P']) if data.get('P') is not None else ((price/open24-1)*100 if open24 else None)
                if change is not None and not math.isfinite(change):
                    change=None
            except (ValueError,TypeError):
                pass
            quote={'price':price,'change24h':change,'volume24h':finite(data.get('q')),
                   'marketAt':int(data.get('E') or received),'sourceEventAt':int(data.get('E') or received),
                   'statisticsAt':int(data.get('E') or received),
                   'receivedAt':received,'exchangeTrades24h':int(data['n']) if data.get('n') is not None else None}
            await self.processor.quote(market,quote)
        elif event in ('trade','aggTrade'):
            trade=normalize_binance_trade(data)
            if trade:
                self.live_ids[market.storage]=max(self.live_ids.get(market.storage,0),trade['sourceId'])
                await self.processor.trade(market,{**trade,'receivedAt':received})
                # The latest transaction is a price observation even before the
                # slower rolling-24h ticker arrives. It does not reset volume.
                quote={'price':trade['price'],'marketAt':trade['t'],'sourceEventAt':trade['sourceEventAt'],'receivedAt':received}
                await self.processor.quote(market,quote)
        elif event=='kline':
            candle=normalize_binance_candle(data)
            if candle:
                await self.processor.candle(market,*candle,source_at=int(data.get('E') or received),received_at=received)

    async def _recover(self,markets,checkpoints):
        async def one(market):
            async with self.rest_gate:
                gap,last=checkpoints[market.storage]
                try:
                    await self.recover_market(market,gap,last)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    await (await store(market.chain_id)).put('market-gap',market.storage,{
                        **(await (await store(market.chain_id)).get('market-gap',market.storage) or gap),
                        'status':'retrying','error':type(error).__name__,'lastAttemptAt':now_ms()})
                marker=await (await store(market.chain_id)).get('market-gap',market.storage) or {}
                if marker.get('status') in ('retrying','incomplete'):
                    checkpoints[market.storage]=(marker,last)
                    return market
        # A bounded number of coroutines runs REST concurrently; per connection
        # queues are finite (<=100 markets), never one request per browser.
        pending=markets
        while pending:
            results=await asyncio.gather(*(one(m) for m in pending))
            pending=[market for market in results if market is not None]
            if pending:
                await asyncio.sleep(20)

    async def recover_market(self,market,gap,last):
        s=await store(market.chain_id)
        recovered_to=now_ms()
        # Baseline starts from current history; no assertion about the token's
        # lifetime tape. Keep failed replay lower bounds across worker restarts.
        lower=gap.get('fromSourceId') if gap.get('status') not in ('complete','baseline') else None
        if lower is None and last:
            lower=last.get('sourceId')
        await s.put('market-gap',market.storage,{'status':'recovering','fromSourceId':lower,'lastAttemptAt':recovered_to})
        async with httpx.AsyncClient(timeout=15,headers={'User-Agent':'CliperX/1.0'}) as client:
            alpha=self.venue=='binance-alpha'
            endpoint=('https://www.binance.com/bapi/defi/v1/public/alpha-trade/agg-trades'
                      if alpha else 'https://api.binance.com/api/v3/historicalTrades')
            cursor=int(lower)+1 if lower is not None else None
            complete=False
            # Each pass is limited to 4,000 trades. Incomplete cursors remain
            # durable and continue in the background, independently of views.
            for _ in range(4):
                page_limit=1000 if cursor is not None else 100
                params={'symbol':market.market_id,'limit':page_limit}
                if cursor is not None:
                    params['fromId']=cursor
                response=await self._get_replay(client,endpoint,params)
                response.raise_for_status()
                body=response.json()
                rows=body.get('data') if alpha and isinstance(body,dict) else body
                if not isinstance(rows,list):
                    raise ValueError('invalid trade replay')
                recovered=[]
                for data in rows:
                    payload=({**data,'e':'aggTrade'} if alpha else {
                        'e':'trade','t':data.get('id'),'T':data.get('time'),
                        'p':data.get('price'),'q':data.get('qty'),'m':data.get('isBuyerMaker')})
                    trade=normalize_binance_trade(payload)
                    if trade:
                        recovered.append(('trade',market,{**trade,'receivedAt':now_ms(),'replayed':True}))
                if rows and not recovered:
                    raise ValueError('nonempty replay contains no valid trades')
                await self.processor.flush(recovered)
                replay_high=max((item[2]['sourceId'] for item in recovered),default=(cursor-1 if cursor is not None else 0))
                if rows and cursor is not None and replay_high<cursor:
                    raise ValueError('provider replay cursor did not advance')
                if recovered:
                    cursor=replay_high+1
                    await s.put('market-gap',market.storage,{'status':'recovering','fromSourceId':replay_high,
                        'lastAttemptAt':now_ms()})
                reached_time=any(item[2]['t']>=recovered_to for item in recovered)
                if len(rows)<page_limit or reached_time or lower is None:
                    target=self.live_ids.get(market.storage)
                    complete=target is None or replay_high>=target-1
                    break
                await asyncio.sleep(.3)
            marker={'status':('complete' if lower is not None else 'baseline') if complete else 'incomplete',
                    'fromSourceId':None if complete else (cursor-1 if cursor is not None else lower),
                    'recoveredUntil':recovered_to if complete else None}
            # Backfill official candles separately; trade replay limits must not
            # silently make the displayed price history incomplete.
            from ..api.candles import candle_series
            await candle_series(market.chain_id,market.token,'1m',1000,self.venue,market.market_id)
        # Ensure replay transactions are committed before the durable checkpoint.
        await s.put('market-gap',market.storage,{**marker,'lastAttemptAt':now_ms()})
