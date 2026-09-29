import test from 'node:test';
import assert from 'node:assert/strict';
import {createPinia,setActivePinia} from 'pinia';
import {useDashboardStore} from '../src/stores/dashboard.js';
import {getJSON} from '../src/api/client.js';
const snapshot=(scope)=>({unified:{snapshotScope:scope,assets:[],stockTokens:[],relations:[],totals:{assets:100}}});

test('full response wins over a late overview and both views share one request each',async()=>{
 setActivePinia(createPinia());const store=useDashboardStore();const original=globalThis.fetch;const resolve={};const calls=[];
 globalThis.fetch=(url)=>{calls.push(url);return new Promise(done=>{resolve[url]=()=>done({ok:true,json:async()=>snapshot(url.includes('overview')?'overview':'full')});});};
 try{
  const overview=store.poll({view:'overview'}),full=store.poll({view:'full'});
  const duplicate=store.poll({view:'full'});
  resolve['/api/dashboard?view=full']();await full;await duplicate;
  resolve['/api/dashboard?view=overview']();await overview;
  assert.equal(store.snapshot.unified.snapshotScope,'full');assert.equal(calls.length,2);
 }finally{globalThis.fetch=original;}
});

test('failed full list loading preserves useful overview totals',async()=>{
 setActivePinia(createPinia());const store=useDashboardStore();store.snapshot=snapshot('overview');const old=globalThis.fetch;
 globalThis.fetch=async()=>{throw new Error('offline');};
 try{await store.poll({view:'full'});assert.equal(store.snapshot.unified.totals.assets,100);assert.equal(store.snapshot.unified.snapshotScope,'overview');assert.equal(store.error,'offline');}
 finally{globalThis.fetch=old;}
});

test('stalled request has a bounded timeout instead of freezing polling forever',async()=>{
 const old=globalThis.fetch;
 globalThis.fetch=(_,options)=>new Promise((_,reject)=>{options.signal.addEventListener('abort',()=>reject(new Error('aborted')));});
 try{await assert.rejects(()=>getJSON('dashboard',5),/request-timeout/);}
 finally{globalThis.fetch=old;}
});

test('interactive dashboard polling uses the compact market view by default',async()=>{
 setActivePinia(createPinia());const store=useDashboardStore();const old=globalThis.fetch;const calls=[];
 globalThis.fetch=async url=>{calls.push(url);return {ok:true,json:async()=>snapshot('market')};};
 try{await store.poll();assert.equal(calls[0],'/api/dashboard?view=market');assert.equal(store.snapshot.unified.snapshotScope,'market');}
 finally{globalThis.fetch=old;}
});
