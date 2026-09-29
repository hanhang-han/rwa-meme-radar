import test from 'node:test';
import assert from 'node:assert/strict';
import {createPinia,setActivePinia} from 'pinia';
import {selectDetailMarket} from '../src/utils/market-selection.js';
import {applyQuote,mergeEntity} from '../src/utils/realtime.js';
import {useDashboardStore} from '../src/stores/dashboard.js';
import {useDetailStore} from '../src/stores/detail.js';
import {useCandleStore} from '../src/stores/candles.js';
import {mergeCandleRows,candlePacketMatches,snapshotCandleRows,candleResponseMatches,candleStreamState} from '../src/utils/candles.js';
import {handleStreamEvent} from '../src/composables/useStream.js';
const setup=()=>{setActivePinia(createPinia());return useDashboardStore();};
const row=(token='a',fields={})=>({chainId:'196',token,price:1,fieldTimes:{price:100},...fields});
const snapshot=(revision=1,assets=[row()],stocks=[])=>({realtime:{schema:1,cursor:revision,revision},unified:{snapshotScope:'full',assets,stockTokens:stocks,relations:[],sectors:[],metrics:{total:1}}});
const delta=(revision,upserts={},removes={},meta={})=>({schema:1,revision,upserts,removes,meta});

test('DEX stock quote updates stock directory and open detail without a candidate row',async()=>{
 const dash=setup();const stock={chainId:'196',tokenContractAddress:'s',price:1,fieldTimes:{price:100},priceScope:'dex'};
 const boot=snapshot(1,[],[stock]);delete boot.realtime;dash.acceptSnapshot(boot);
 const detail=useDetailStore();detail.cache.set('196:s',{at:0,data:{asset:row('s'),stock:{...stock}}});
 assert.equal(await handleStreamEvent('price',JSON.stringify({chainId:'196',token:'s',price:2,at:200,venue:'dex',timeKind:'market'})),true);
 assert.equal(dash.stockTokens[0].price,2);assert.equal(detail.cache.get('196:s').data.asset.price,2);
});

test('each quote field uses its own observation time and accepts explicit null',()=>{
 const a={price:1,volume24h:100,fieldTimes:{price:100,volume24h:500}};
 applyQuote(a,{price:2,volume24h:20,marketAt:200});
 assert.equal(a.price,2);assert.equal(a.volume24h,100);assert.equal(a.fieldTimes.volume24h,500);
 applyQuote(a,{volume24h:null,marketAt:600});
 assert.equal(a.volume24h,null);assert.equal(a.fieldTimes.volume24h,600);
});

test('same-price new volume updates its metadata while received time stays distinct',()=>{
 const a=row();applyQuote(a,{price:1,volume24h:5,at:200,receivedAt:200,timeKind:'received',marketAt:null,provider:'OKX',priceScope:'dex'});
 assert.equal(a.volume24h,5);assert.equal(a.fieldTimes.volume24h,200);assert.equal(a.fieldSources.volume24h,'OKX');assert.equal(a.priceProvenance.timeKind,'received');assert.equal(a.priceProvenance.marketAt,null);
});

test('field revision permits same-time correction and rejects older revision',()=>{
 const a={price:1,fieldTimes:{price:100},fieldRevisions:{price:8}};
 applyQuote(a,{price:2,marketAt:100,fieldRevisions:{price:9}});
 applyQuote(a,{price:99,marketAt:200,fieldRevisions:{price:7}});
 assert.equal(a.price,2);assert.equal(a.fieldRevisions.price,9);
});

test('complete projection changes all displayed field families without replacing canonical objects',()=>{
 const dash=setup();dash.acceptSnapshot(snapshot());const original=dash.assets[0];
 dash.applyProjection(delta(2,{assets:[row('a',{price:2,fieldTimes:{price:200,volume24h:200},volume24h:9,holders:7,marketCap:88,liquidity:66,risk:{top10:20},dataQuality:{tier:'current'}})],sectors:[{chainId:'196',sector:'芯片',value:102}]},{},{metrics:{total:2},sources:[{provider:'OKX',status:'ready'}],quality:{known:2}}));
 assert.equal(dash.assets[0],original);assert.equal(original.holders,7);assert.equal(original.risk.top10,20);assert.equal(dash.snapshot.unified.sectors[0].value,102);assert.equal(dash.metrics.total,2);assert.equal(dash.sources[0].status,'ready');
});

test('projection deletion and explicit invalidation survive an older snapshot response',()=>{
 const dash=setup();dash.acceptSnapshot(snapshot(1,[row('a'),row('b')]));
 dash.applyProjection(delta(3,{assets:[row('a',{price:null,fieldTimes:{price:null}})]},{assets:['196:b']}));
 dash.acceptSnapshot(snapshot(2,[row('a'),row('b')]));
 assert.equal(dash.assets.length,1);assert.equal(dash.assets[0].price,null);assert.equal(dash.revision,3);
});

test('delta arriving before full bootstrap replays after its matching snapshot cursor',()=>{
 const dash=setup();dash.applyProjection(delta(11,{assets:[row('new',{holders:5})]}, {}, {totals:{assets:2}}));
 dash.acceptSnapshot(snapshot(10));
 assert.equal(dash.assets.length,2);assert.equal(dash.assetIndex.get('196:new').holders,5);assert.equal(dash.snapshot.unified.totals.assets,2);
});

test('duplicate/out-of-order projection never resurrects removed entities',()=>{
 const dash=setup();dash.acceptSnapshot(snapshot());dash.applyProjection(delta(3,{}, {assets:['196:a']}));
 dash.applyProjection(delta(2,{assets:[row()]}));dash.applyProjection(delta(3,{assets:[row()]}));
 assert.equal(dash.assets.length,0);
});

test('projectionKey addresses relation removal without cross-chain collisions',()=>{
 const dash=setup();const data=snapshot();data.unified.relations=[{id:'p',chainId:'196',projectionKey:'196:p'},{id:'p',chainId:'56',projectionKey:'56:p'}];dash.acceptSnapshot(data);
 dash.applyProjection(delta(2,{}, {relations:['56:p']}));assert.deepEqual(dash.relations.map(r=>r.chainId),['196']);
});

test('detail facts are refreshed from projection, including reference and relation removal',async()=>{
 const dash=setup();dash.acceptSnapshot(snapshot());const details=useDetailStore();details.cache.set('196:a',{at:0,data:{asset:row('a',{risk:{top10:5}}),relations:[{id:'old'}]}});
 await handleStreamEvent('projection.delta',JSON.stringify(delta(2,{assets:[row('a',{holders:50})]})));
 const data=details.cache.get('196:a').data;assert.equal(data.asset.holders,50);assert.equal(data.asset.risk.top10,5);assert.deepEqual(data.relations,[]);
});

test('an old detail HTTP response retains quote observations received while request was in flight',async()=>{
 setup();const details=useDetailStore();details.cache.set('196:a',{data:{asset:row(),trades:[]},at:0});
 const old=globalThis.fetch;let release;globalThis.fetch=()=>new Promise(r=>release=r);
 try{const loading=details.fetch('196','a',{force:true});details.applyQuote({chainId:'196',token:'a',price:4,at:400});release({ok:true,json:async()=>({asset:row(),trades:[]})});await loading;assert.equal(details.cache.get('196:a').data.asset.price,4);}finally{globalThis.fetch=old;}
});

test('higher projection revision invalidates an old value instead of timestamp resurrection',()=>{
 const a={price:4,fieldTimes:{price:400},_revision:2};mergeEntity(a,{price:null,fieldTimes:{price:null}},3,true);assert.equal(a.price,null);assert.equal(a._revision,3);
});
const candle=(t,c=2,extra={})=>({t,o:1,h:Math.max(2,c),l:1,c,v:10,confirmed:false,...extra});
test('candle event changes current OHLCV, appends next candle and keeps history',()=>{
 setup();const store=useCandleStore();const packet={chainId:'56',token:'a',venue:'binance',marketId:'AUSDT',bar:'1m',source:'Binance',priceCurrency:'USDT'};
 store.receive({...packet,row:candle(60000),sourceEventAt:100});store.receive({...packet,row:candle(60000,3,{v:20}),sourceEventAt:200});store.receive({...packet,row:candle(120000,4),sourceEventAt:300});
 const rows=store.matching(packet).rows;assert.equal(rows.length,2);assert.equal(rows[0].c,3);assert.equal(rows[0].v,20);assert.equal(rows[1].t,120000);
});

test('late candle correction updates history and older duplicate cannot unconfirm a closed bar',()=>{
 const rows=mergeCandleRows([candle(60000,2,{confirmed:true,sourceEventAt:200}),candle(120000)], [candle(60000,1,{sourceEventAt:100})]);assert.equal(rows[0].c,2);assert.equal(rows[0].confirmed,true);
 const corrected=mergeCandleRows(rows,[candle(60000,3,{confirmed:true,sourceEventAt:300})]);assert.equal(corrected[0].c,3);assert.equal(corrected[1].t,120000);
});

test('native pool candles never match USD aggregated candles or a different pool',()=>{
 const p={chainId:'196',token:'a',venue:'dex',poolId:'pool-a',marketId:'pool-a',bar:'1m'};
 assert.equal(candlePacketMatches(p,{...p,poolId:'pool-b'}),false);assert.equal(candlePacketMatches(p,{chainId:'196',token:'a',venue:'dex',bar:'1m'}),false);assert.equal(candlePacketMatches(p,p),true);
});

test('Alpha markets stay isolated by venue and actual trading quote symbol',()=>{
 const p={chainId:'56',token:'a',venue:'binance-alpha',marketId:'ALPHA_1U',bar:'1m'};
 assert.equal(candlePacketMatches(p,{...p,venue:'binance'}),false);assert.equal(candlePacketMatches(p,{...p,marketId:'ALPHA_1USDT'}),false);
});

test('unsupported projection schema is rejected without corrupting state',()=>{
 const dash=setup();dash.acceptSnapshot(snapshot());assert.equal(dash.applyProjection({...delta(9),schema:9}),false);assert.equal(dash.revision,1);
});

test('authoritative projection changes selected market in both list and detail even with an older source timestamp',async()=>{
 const dash=setup();dash.acceptSnapshot(snapshot(1,[row('a',{price:8,venue:'old',fieldTimes:{price:800}})]));
 const details=useDetailStore();details.cache.set('196:a',{at:0,data:{asset:row('a',{price:8,venue:'old',fieldTimes:{price:800},risk:{status:'checked'}})}});
 await handleStreamEvent('projection.delta',JSON.stringify(delta(2,{assets:[row('a',{price:4,venue:'new',fieldTimes:{price:400}})]})));
 assert.equal(dash.assets[0].price,4);assert.equal(details.currentData,null);
 assert.equal(details.cache.get('196:a').data.asset.price,4);assert.equal(details.cache.get('196:a').data.asset.venue,'new');
 assert.equal(details.cache.get('196:a').data.asset.risk.status,'checked');
 await handleStreamEvent('price',JSON.stringify({chainId:'196',token:'a',price:99,at:900,venue:'old'}));
 assert.equal(dash.assets[0].price,4);assert.equal(details.cache.get('196:a').data.asset.price,4);
});

test('candle reset invalidates one exact market and retains another market history',async()=>{
 setup();const store=useCandleStore(),packet={chainId:'196',token:'a',venue:'dex',poolId:'p1',marketId:'p1',bar:'1m'};
 store.receive({...packet,row:candle(60000)});store.receive({...packet,poolId:'p2',marketId:'p2',row:candle(60000,3)});
 await handleStreamEvent('candle-reset',JSON.stringify({...packet,reason:'chain-reorg'}));
 assert.equal(store.matching(packet),null);assert.equal(store.matching({...packet,poolId:'p2',marketId:'p2'}).rows[0].c,3);
 assert.equal(store.resetVersion,1);assert.equal([...store.resets.values()][0].resetVersion,1);
});

test('visible charts union subscriptions with canonical market keys and release all on unmount',()=>{
 setup();const store=useCandleStore();assert.equal(store.subscriptionScope,'none');
 store.subscribe('first',{chainId:'56',token:'0xABC',venue:'binance',marketId:'NVDAUSDT',bar:'1H'});
 assert.equal(store.subscriptionScope,'56:0xabc:binance:nvdausdt:1H');
 store.subscribe('second',{chainId:'196',token:'b',venue:'dex',bar:'5m'});assert.equal(store.subscriptionScope,'all');
 store.subscribe('second',{chainId:'196',token:'b',venue:'dex',poolId:'POOL',bar:'5m'});
 assert.equal(store.subscriptionScope,'196:b:dex:pool:5m,56:0xabc:binance:nvdausdt:1H');
 store.unsubscribe('second');store.unsubscribe('first');assert.equal(store.subscriptionScope,'none');
});

test('exchange trade is isolated from DEX metrics, and reorg tombstone survives detail history reload',async()=>{
 const dash=setup();dash.acceptSnapshot(snapshot());const details=useDetailStore();details.cache.set('196:a',{at:0,data:{asset:row('a',{buys24h:3}),trades:[],marketTrades:[]}});
 const trade={chainId:'196',token:'a',venue:'binance-alpha',marketId:'ALPHA_1U',id:'t',t:100,price:5,receivedAt:123};
 await handleStreamEvent('trade',JSON.stringify(trade));assert.equal(details.cache.get('196:a').data.marketTrades.length,1);
 assert.equal(details.cache.get('196:a').data.trades.length,0);assert.equal(details.cache.get('196:a').data.asset.buys24h,3);
 await handleStreamEvent('trade-remove',JSON.stringify({...trade,ids:['t']}));
 const old=globalThis.fetch;globalThis.fetch=async()=>({ok:true,json:async()=>({asset:row(),marketTrades:[trade]})});
 try{await details.fetch('196','a',{force:true});assert.equal(details.cache.get('196:a').data.marketTrades.length,0);await handleStreamEvent('trade',JSON.stringify(trade));assert.equal(details.cache.get('196:a').data.marketTrades.length,0);}finally{globalThis.fetch=old;}
});


test('default native pool requires real trades, prefers liquidity and never mutates asset quote',()=>{
 const base={price:5,priceCurrency:'USD',priceScope:'dex'};
 const pools=[{poolId:'empty',liquidityUsd:999,lastTradeAt:null},{poolId:'small',liquidityUsd:10,lastTradeAt:200},{poolId:'large',liquidityUsd:20,lastTradeAt:100,priceCurrency:'WETH'}];
 const selected=selectDetailMarket(base,[],pools);assert.equal(selected.id,'pool:large');assert.equal(selected.pool.priceCurrency,'WETH');assert.equal(base.price,5);assert.equal(base.priceCurrency,'USD');
 assert.equal(selectDetailMarket(base,[],pools,'dex').kind,'base');assert.equal(selectDetailMarket(base,[],pools,'pool:small').id,'pool:small');
 assert.equal(selectDetailMarket(base,[],[pools[0]]).kind,'base');
});

test('Alpha market and existing exchange base keep default priority over native pools',()=>{
 const pools=[{poolId:'p',lastTradeAt:100,liquidityUsd:1}],exchanges=[{venue:'binance-alpha',marketId:'ALPHA_1U'}];
 assert.equal(selectDetailMarket({priceScope:'dex'},exchanges,pools).id,'binance-alpha:ALPHA_1U');
 assert.equal(selectDetailMarket({priceScope:'exchange'},[],pools).kind,'base');
 assert.equal(selectDetailMarket({priceScope:'dex'},exchanges,pools,'pool:p').kind,'pool');
});


test('late streamed open candle cannot roll back newer HTTP history observation',()=>{
 const history=snapshotCandleRows({lastSuccessfulAt:500,rows:[candle(60000,4,{h:5,v:50,vu:100})]});
 const stale=candle(60000,3,{h:3,v:30,vu:60,sourceEventAt:400});
 const merged=mergeCandleRows(history,[stale]);assert.equal(merged[0].c,4);assert.equal(merged[0].h,5);assert.equal(merged[0].vu,100);
 const fresh=candle(60000,5,{h:6,v:70,vu:140,sourceEventAt:600});
 assert.equal(mergeCandleRows(merged,[fresh])[0].c,5);
});

test('history reconciliation respects server source watermark while retaining a newer live candle',()=>{
 const history=snapshotCandleRows({lastSourceEventAt:400,lastSuccessfulAt:900,rows:[candle(60000,2)]});
 const live=candle(60000,4,{sourceEventAt:500});
 assert.equal(mergeCandleRows(history,[live])[0].c,4);
 const next=snapshotCandleRows({lastSourceEventAt:600,rows:[candle(60000,3)]});
 assert.equal(mergeCandleRows(next,[live])[0].c,3);
});


test('explicit per-candle observation watermark beats response and late source packets',()=>{
 const history=snapshotCandleRows({lastObservationAt:900,rows:[candle(60000,4,{observedAt:600})]});
 assert.equal(history[0]._snapshotAt,undefined);
 assert.equal(mergeCandleRows(history,[candle(60000,2,{sourceEventAt:500,receivedAt:1000})])[0].c,4);
 assert.equal(mergeCandleRows(history,[candle(60000,5,{sourceEventAt:700,receivedAt:1100})])[0].c,5);
});


test('HTTP candle history rejects a different pool, venue, bar, or exchange market',()=>{
 const selected={chainId:'196',token:'a',venue:'dex',bar:'5m'};
 assert.equal(candleResponseMatches({rows:[],priceCurrency:'USD'},selected),true);
 assert.equal(candleResponseMatches({poolId:'pool-a',marketId:'pool-a',priceCurrency:'WETH'},selected),false);
 assert.equal(candleResponseMatches({pool:'pool-a',priceCurrency:'WETH'},{...selected,poolId:'pool-b'}),false);
 assert.equal(candleResponseMatches({marketId:'POOL-A',priceCurrency:'WETH'},{...selected,poolId:'pool-a'}),true);
 const exchange={...selected,venue:'binance-alpha',marketId:'ALPHA_1USDT'};
 assert.equal(candleResponseMatches({venue:'binance-alpha',marketId:'ALPHA_1U'},exchange),false);
 assert.equal(candleResponseMatches({venue:'binance',marketId:'ALPHA_1USDT'},exchange),false);
 assert.equal(candleResponseMatches({bar:'1m'},exchange),false);
 assert.equal(candleResponseMatches({venue:'binance-alpha',marketId:'alpha_1usdt',bar:'5m'},exchange),true);
});


test('historical native candle does not claim live freshness or move last trade backwards',()=>{
 const now=1_800_000_000_000,prior={stale:true,status:'stale',marketStatus:'recovering',coverageStatus:'backfilling',lastTradeAt:now-60000,lastSourceEventAt:now-60000};
 const historical={poolId:'p',sourceEventAt:now-3600000,receivedAt:now,row:{t:now}};
 const state=candleStreamState(prior,historical,now);
 assert.equal(state.stale,true);assert.equal(state.status,'stale');assert.equal(state.marketStatus,'recovering');
 assert.equal(state.lastTradeAt,now-60000);assert.equal(state.lastSourceEventAt,now-60000);
 const first=candleStreamState(null,historical,now);assert.equal(first.stale,true);assert.equal(first.lastTradeAt,now-3600000);
 const missing=candleStreamState(null,{poolId:'p',receivedAt:now,row:{t:now}},now);assert.equal(missing.stale,true);assert.equal(missing.lastTradeAt,null);
});

test('fresh native trade is current while historical coverage is still backfilling',()=>{
 const now=1_800_000_000_000,packet={poolId:'p',sourceEventAt:now-1000};
 const live=candleStreamState({stale:true,status:'stale',coverageStatus:'backfilling'},packet,now);
 assert.equal(live.stale,false);assert.equal(live.status,'current');assert.equal(live.marketStatus,'live');assert.equal(live.coverageStatus,'backfilling');
 const correction=candleStreamState(live,{poolId:'p',sourceEventAt:now-600000},now+1000);
 assert.deepEqual(correction,live);
});

test('old correction preserves a scanned quiet market and cannot make stale exchange data current',()=>{
 const now=1_800_000_000_000,quiet={stale:false,status:'current',marketStatus:'quiet',coverageStatus:'current',lastTradeAt:now-120000,lastSourceEventAt:now-120000};
 const state=candleStreamState(quiet,{poolId:'p',sourceEventAt:now-3600000},now);
 assert.equal(state.marketStatus,'quiet');assert.equal(state.coverageStatus,'current');assert.equal(state.lastTradeAt,quiet.lastTradeAt);assert.equal(state.stale,false);
 assert.equal(candleStreamState({stale:true,status:'stale'},{sourceEventAt:now-60000,receivedAt:now},now).stale,true);
 assert.equal(candleStreamState({stale:true,status:'stale'},{sourceEventAt:now-1000},now).stale,false);
});
