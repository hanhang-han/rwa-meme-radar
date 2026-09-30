import test from 'node:test';
import assert from 'node:assert/strict';
import {createPinia,setActivePinia} from 'pinia';
import {useDetailStore} from '../src/stores/detail.js';
const summary=(revision,price=5,at=100)=>({revision,asset:{chainId:'56',token:'a',price,fieldTimes:{price:at},primaryQuote:{price,provider:`source-${revision}`}},relations:[{id:'preview'}]});
test('a higher revision may change canonical source to an earlier observed price',async()=>{
 setActivePinia(createPinia());const store=useDetailStore(),old=global.fetch;let next=summary(1,5,500);
 global.fetch=async()=>({ok:true,json:async()=>next});
 try{await store.fetch('56','a');next=summary(2,7,400);await store.fetch('56','a',{force:true});assert.equal(store.currentData,null);assert.equal(store.cache.get('56:a').data.asset.price,7);next=summary(1,6,900);await store.fetch('56','a',{force:true});assert.equal(store.cache.get('56:a').data.asset.price,7);assert.equal(store.cache.get('56:a').data.asset.primaryQuote.provider,'source-2');}finally{global.fetch=old;}
});
test('summary refresh preserves the entry and a concurrent completed section',async()=>{
 setActivePinia(createPinia());const store=useDetailStore(),old=global.fetch;let resolve;
 global.fetch=async url=>url.includes('section=relations')?new Promise(done=>{resolve=()=>done({ok:true,json:async()=>({revision:1,relations:[{id:'one'},{id:'two'}],next:null})});}):({ok:true,json:async()=>summary(1)});
 try{await store.fetch('56','a');const entry=store.cache.get('56:a');const request=store.fetchSection('56','a','relations');await store.fetch('56','a',{force:true});resolve();await request;assert.equal(store.cache.get('56:a'),entry);assert.equal(entry.data.relations.length,2);await store.fetch('56','a',{force:true});assert.equal(entry.data.relations.length,2);assert.ok(entry.sections.relations);}finally{global.fetch=old;}
});
test('market sections update selectors without changing the headline quote',async()=>{
 setActivePinia(createPinia());const store=useDetailStore(),old=global.fetch;
 global.fetch=async url=>({ok:true,json:async()=>url.includes('section=markets')?{asset:{price:999,exchangeMarkets:[{venue:'binance',marketId:'AX'}]},markets:[{venue:'binance',marketId:'AX'}],marketTrades:[]}:summary(1)});
 try{await store.fetch('56','a');await store.fetchSection('56','a','markets');assert.equal(store.cache.get('56:a').data.asset.price,5);assert.equal(store.cache.get('56:a').data.asset.exchangeMarkets[0].marketId,'AX');}finally{global.fetch=old;}
});
