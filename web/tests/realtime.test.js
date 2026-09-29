import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createPinia,setActivePinia} from 'pinia';
import {useDashboardStore} from '../src/stores/dashboard.js';
import {useFeedStore} from '../src/stores/feed.js';
test('stock live quote survives older snapshots and never overwrites DEX quote',()=>{
 setActivePinia(createPinia()); const s=useDashboardStore();
 const snapshot=()=>({unified:{assets:[{chainId:'56',token:'a',price:10,fieldTimes:{price:100}}],stockTokens:[{chainId:'56',tokenContractAddress:'a',price:20,fieldTimes:{price:100}}]}});
 s.snapshot=snapshot(); s.applyPrice('56','a',11,200);
 s.applyStockQuote({chainId:'56',token:'a',price:21,marketAt:201,venue:'binance'});
 s.snapshot=snapshot();s._restoreSse();
 assert.equal(s.assets[0].price,11);assert.equal(s.stockTokens[0].price,21);
 s.applyPrice('56','a',9,150);assert.equal(s.assets[0].price,11);
 s.applyPrice('56','a',11,300);assert.equal(s.assets[0].fieldTimes.price,300);
});
test('reading history buffers incoming rows and deduplicates by chain/token/id',()=>{
 setActivePinia(createPinia());const s=useFeedStore();
 const a={chainId:'56',token:'a',id:'1',t:1};
 s.appendTrades([a,a]);assert.equal(s.trades.length,1);
 s.setPaused(true);s.appendTrades([{...a,chainId:'196',t:2}]);
 assert.equal(s.trades.length,1);assert.equal(s.newCount,1);
 s.setPaused(false);assert.equal(s.trades.length,2);assert.equal(s.trades[0].chainId,'196');assert.equal(s.newCount,0);
});
test('feed snapshot arriving after a push preserves that push',async()=>{
 setActivePinia(createPinia());const s=useFeedStore();const old=globalThis.fetch;
 let release;globalThis.fetch=()=>new Promise(r=>release=r);
 try {const p=s.load();s.appendTrades([{chainId:'56',token:'a',id:'new',t:2}]);release({ok:true,json:async()=>({trades:[{chainId:'56',token:'a',id:'old',t:1}]})});await p;assert.deepEqual(s.trades.map(t=>t.id),['new','old']);}
 finally{globalThis.fetch=old;}
});

import {candleTailState,chartTime,chartDateTime,samplePoints} from '../src/utils/chart-time.js';
test('chart axis and crosshair format UTC timestamps in the same local timezone',()=>{
 const t=Date.UTC(2026,8,18,12,15)/1000;
 assert.match(chartTime(t,3,'Asia/Shanghai'),/20:15/);
 assert.match(chartDateTime(t,'Asia/Shanghai'),/20:15/);
 assert.match(chartTime(t,3,'UTC'),/12:15/);
});
test('line samples never move a stale quote to the current clock time',()=>{
 const provenance={scope:'dex',currency:'USD',provider:'OKX'};
 const pts=samplePoints([{t:1000,price:1,provenance},{t:2000,price:2,provenance}],{price:3,provider:'OKX',priceScope:'dex',priceCurrency:'USD',fieldTimes:{price:2000}});
 assert.deepEqual(pts,[{time:1,value:1},{time:2,value:3}]);
});
test('an old candle tail is marked quiet instead of assumed current',()=>{
 const now=Date.UTC(2026,8,21,3,0);
 assert.equal(candleTailState({rows:[{t:now-20*60_000}]},'5m',now),'quiet');
 assert.equal(candleTailState({rows:[{t:now-5*60_000}]},'5m',now),'current');
 assert.equal(candleTailState({rows:[{t:now-5*60_000}],stale:true},'5m',now),'stale');
});


test('legacy or other-market samples are never spliced with a current exchange quote', () => {
 const asset={price:123,priceCurrency:'USDT',priceScope:'exchange',provider:'Binance',quoteAt:3000};
 assert.deepEqual(samplePoints([{t:1000,price:1},{t:2000,price:2}],asset),[{time:1,value:1},{time:2,value:2}]);
 assert.deepEqual(samplePoints([{t:1000,price:1,provenance:{scope:'dex',currency:'USD',provider:'OKX'}}],asset),[]);
});

test('token scope samples with DEX venue retain market provenance', () => {
 const provenance={scope:'token',venue:'dex',currency:'USD',provider:'OKX'};
 const pts=samplePoints([{t:1000,price:1,provenance},{t:2000,price:2,provenance}],{price:3,quoteAt:3000,priceScope:'dex',priceCurrency:'USD',provider:'OKX'});
 assert.deepEqual(pts,[{time:1,value:1},{time:2,value:2},{time:3,value:3}]);
});
