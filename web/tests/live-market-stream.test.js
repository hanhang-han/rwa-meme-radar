import test from 'node:test';
import assert from 'node:assert/strict';
import { openLiveMarketStream, getLiveMarkets, liveMarketSnapshotMatches, liveMarketHealthMatches, mergeLiveMarketHealth, liveMarketObservationState } from '../src/utils/live-market-stream.js';
import { selectDetailMarket } from '../src/utils/market-selection.js';

const token='0x'+'1'.repeat(40),pool='0x'+'2'.repeat(40);
const selection={chainId:'196',token,venue:'dex',poolId:pool,marketId:pool,bar:'5m'};
const snapshot={...selection,pool,liveMarket:true,epoch:'small-db-uuid',cursor:7,priceCurrency:'WBNB',volumeCurrency:'WBNB',rows:[]};
class FakeEventSource{
 static instances=[];
 constructor(url){this.url=url;this.listeners=new Map();FakeEventSource.instances.push(this);}
 addEventListener(name,listener){this.listeners.set(name,listener);}
 emit(name,data,id){this.listeners.get(name)?.({data:JSON.stringify(data),lastEventId:String(id)});}
 close(){this.closed=true;}
}

test('only an explicit registered native snapshot with exact market identity opens live stream',()=>{
 FakeEventSource.instances=[];
 for(const changes of [{liveMarket:false},{epoch:null},{poolId:null,pool:null},{chainId:'56'},{token:'bad'},{bar:'1m'},{venue:'binance'},{priceCurrency:null}]){
  const modified={...snapshot,...changes};
  assert.equal(!!liveMarketSnapshotMatches(modified,selection),false);
  openLiveMarketStream({selection,snapshot:modified,EventSourceImpl:FakeEventSource});
 }
 assert.equal(FakeEventSource.instances.length,0);
});

test('live connection uses its own cursor and accepts neither another unit nor old-journal packets',()=>{
 FakeEventSource.instances=[];const scheduled=[],received=[];
 const stream=openLiveMarketStream({selection:{...selection,lastEventId:99999999},snapshot,onCandle:p=>received.push(p),
  EventSourceImpl:FakeEventSource,apiBase:'/dashboard/api/',schedule:callback=>{scheduled.push(callback);return scheduled.length;},cancel:()=>{}});
 const first=FakeEventSource.instances[0],query=new URL(first.url,'https://local.test').searchParams;
 assert.equal(query.get('after'),'7');assert.equal(query.get('epoch'),'small-db-uuid');
 const packet={...selection,pool,priceCurrency:'WBNB',volumeCurrency:'WBNB',liveMarket:true,row:{t:1,o:1,h:2,l:1,c:2}};
 first.emit('market.candle',{...packet,liveMarket:false},20);
 first.emit('market.candle',{...packet,priceCurrency:'USD'},21);
 first.emit('market.candle',{...packet,poolId:'0x'+'3'.repeat(40)},22);
 assert.equal(received.length,0);
 first.emit('market.candle',packet,8);assert.equal(received.length,1);
 first.onerror();scheduled[0]();
 const second=FakeEventSource.instances[1];assert.equal(new URL(second.url,'https://local.test').searchParams.get('after'),'8');
 // Late callbacks on a replaced connection cannot mutate the new stream.
 first.emit('market.candle',packet,90);first.onerror();assert.equal(received.length,1);assert.equal(second.closed,undefined);
 second.emit('heartbeat',{liveMarket:true,epoch:'small-db-uuid'},100);
 second.onerror();scheduled[1]();
 assert.equal(new URL(FakeEventSource.instances[2].url,'https://local.test').searchParams.get('after'),'100');
 stream.close();
});

test('reorg resets only the matching series and realm reset requests a new HTTP snapshot',()=>{
 FakeEventSource.instances=[];const resets=[];
 const stream=openLiveMarketStream({selection,snapshot,onReset:p=>resets.push(p),EventSourceImpl:FakeEventSource});
 const connection=FakeEventSource.instances[0];
 connection.emit('market.reset',{...selection,liveMarket:true,priceCurrency:'USD',volumeCurrency:'USD'},8);
 assert.equal(resets.length,0);
 connection.emit('market.reset',{...selection,liveMarket:true,priceCurrency:'WBNB',volumeCurrency:'WBNB',reason:'chain-reorg'},9);
 assert.equal(resets[0].reason,'chain-reorg');
 connection.emit('reset',{liveMarket:true,epoch:'replaced-db',cursor:3,reason:'epoch-changed'},3);
 assert.equal(resets[1].reload,true);assert.equal(connection.closed,true);
 stream.close();
});

test('healthy dedicated live pool is the initial chart; every explicit user selection wins',()=>{
 const now=1800000000000,base={price:1,priceCurrency:'USD',priceScope:'dex'};
 const live={poolId:pool,venue:'dex',quoteType:'pool',liveMarket:true,status:'current',marketStatus:'live',stale:false,
  priceCurrency:'WBNB',lastTradeAt:now-1000,scanAt:now};
 assert.equal(selectDetailMarket(base,[],[live],null,now).id,`pool:${pool}`);
 assert.equal(selectDetailMarket(base,[],[live],'dex',now).kind,'base');
 assert.equal(selectDetailMarket(base,[],[{...live,stale:true}],null,now).kind,'base');
 assert.equal(selectDetailMarket(base,[],[{...live,lastTradeAt:now+5000,scanAt:null}],null,now).kind,'base');
 assert.equal(selectDetailMarket(base,[],[{...live,marketStatus:'quiet',lastTradeAt:now-100000}],null,now).kind,'pool');
 assert.equal(selectDetailMarket(base,[],[{...live,liveMarket:false}],null,now).kind,'base');
 assert.equal(base.priceCurrency,'USD');assert.equal(base.price,1);
});

test('quiet scan evidence expires without reporting candle age as delay or heartbeat as fresh proof',()=>{
 const now=1800000000000;
 const info={...snapshot,marketStatus:'quiet',coverageStatus:'current',transportStatus:'live',stale:false,scanAt:now,
  lastTradeAt:now-600000,lastSourceEventAt:now-600000,lastSuccessfulAt:now-500000,rows:[{t:now-900000,c:1}]};
 assert.equal(liveMarketObservationState(info,now+15000),'quiet');
 assert.equal(liveMarketObservationState(info,now+25000),'unverified');
 assert.equal(liveMarketObservationState({...info,stale:true},now),'unverified');
 assert.equal(liveMarketObservationState({...info,scanAt:now+5000},now),'unverified');
 const updated=mergeLiveMarketHealth(info,{scanAt:now+24000,marketStatus:'quiet',stale:false,coverageStatus:'current',
  transportStatus:'live',lastSourceEventAt:now+24000,lastSuccessfulAt:now+24000,rows:[{t:now,c:99}]});
 assert.equal(liveMarketObservationState(updated,now+25000),'quiet');
 assert.equal(updated.lastSourceEventAt,info.lastSourceEventAt);assert.equal(updated.lastSuccessfulAt,info.lastSuccessfulAt);
 assert.deepEqual(updated.rows,info.rows);
 const late=mergeLiveMarketHealth(updated,{scanAt:now+10000,stale:true,marketStatus:'recovering'});
 assert.equal(late,updated);
 assert.equal(mergeLiveMarketHealth(updated,{scanAt:now+25000,lastTradeAt:now-700000}).lastTradeAt,updated.lastTradeAt);
});

test('health metadata matches exact pool, token, chain and both units',()=>{
 const health={...snapshot};assert.equal(liveMarketHealthMatches(health,selection,snapshot),true);
 for(const changes of [{chainId:'56'},{token:'other'},{poolId:'other'},{priceCurrency:'USD'},{volumeCurrency:'USD'},{venue:'binance'},{liveMarket:false}])
  assert.equal(liveMarketHealthMatches({...health,...changes},selection,snapshot),false);
});

test('a fresh completed scan of the exact pool proves quiet while websocket and history recover',()=>{
 const now=1800000000000;
 const info={...snapshot,stale:false,coverageStatus:'current',marketStatus:'quiet',scanAt:now,
  transportStatus:'reconnecting',historicalCoverageStatus:'backfilling',lastTradeAt:now-600000,
  recentCoverageStatus:'current',nearTipPool:pool,nearTipVerifiedAt:now,
  nearTipBlockTime:now-1000,nearTipFromBlock:100,nearTipThroughBlock:110};
 assert.equal(liveMarketObservationState(info,now),'quiet');
 for(const change of [{nearTipPool:'other'},{nearTipFromBlock:111},{nearTipVerifiedAt:now-21000},
  {nearTipBlockTime:now-31000},{nearTipVerifiedAt:now+5000},{recentCoverageStatus:'unverified'},
  {coverageStatus:'backfilling'},{stale:true}])
  assert.equal(liveMarketObservationState({...info,...change},now),'unverified');
 const updated=mergeLiveMarketHealth(info,{scanAt:now+1000,historicalCoverageStatus:'current',
  nearTipVerifiedAt:now+1000,nearTipThroughBlock:113,nearTipBlockTime:now+500});
 assert.equal(updated.historicalCoverageStatus,'current');
 assert.equal(liveMarketObservationState(updated,now+1000),'quiet');
 assert.equal(updated.lastTradeAt,info.lastTradeAt);assert.deepEqual(updated.rows,info.rows);
});

test('only independent valid heartbeat marks status as a heartbeat, without any market clock',()=>{
 FakeEventSource.instances=[];const states=[];
 const stream=openLiveMarketStream({selection,snapshot,onStatus:status=>states.push(status),EventSourceImpl:FakeEventSource});
 const source=FakeEventSource.instances[0];source.emit('heartbeat',{liveMarket:true,epoch:'old-journal'},10);
 assert.equal(states.some(s=>s.heartbeat),false);
 source.emit('heartbeat',{liveMarket:true,epoch:'small-db-uuid'},11);
 assert.deepEqual(states.at(-1),{connected:true,heartbeat:true});stream.close();
});

test('aborting a chart health waiter leaves another component shared HTTP read working',async()=>{
 const originalFetch=globalThis.fetch;let complete,reads=0;
 globalThis.fetch=()=>{reads++;return new Promise(resolve=>{complete=resolve;});};
 const controller=new AbortController();
 try{
  const first=getLiveMarkets('196','abort-health-test',{signal:controller.signal});
  const second=getLiveMarkets('196','abort-health-test');
  controller.abort();await assert.rejects(first,error=>error.name==='AbortError');
  assert.equal(reads,1);
  complete({ok:true,json:async()=>({epoch:'matching-db',markets:[]})});
  assert.deepEqual(await second,{epoch:'matching-db',markets:[]});
 }finally{globalThis.fetch=originalFetch;}
});

test('market discovery sends exact optional pool and bar parameters and keeps requests for different pools separate',async()=>{
 const originalFetch=globalThis.fetch,urls=[],finish=[];
 globalThis.fetch=url=>{urls.push(url);return new Promise(resolve=>finish.push(()=>resolve({ok:true,json:async()=>({markets:[]})})));};
 try{
  const first=getLiveMarkets('56','pool-discovery-test',{pool,bar:'5m'});
  const shared=getLiveMarkets('56','pool-discovery-test',{pool,bar:'5m'});
  const other=getLiveMarkets('56','pool-discovery-test',{pool:'0x'+'3'.repeat(40),bar:'1m'});
  const withoutPool=getLiveMarkets('56','pool-discovery-test');
  assert.equal(urls.length,3);
  const firstQuery=new URL(urls[0],'https://test.local').searchParams;
  assert.equal(firstQuery.get('pool'),pool);assert.equal(firstQuery.get('bar'),'5m');
  const secondQuery=new URL(urls[1],'https://test.local').searchParams;
  assert.equal(secondQuery.get('pool'),'0x'+'3'.repeat(40));assert.equal(secondQuery.get('bar'),'1m');
  assert.equal(new URL(urls[2],'https://test.local').search,'');
  for(const complete of finish)complete();await Promise.all([first,shared,other,withoutPool]);
 }finally{globalThis.fetch=originalFetch;}
});
