import test from 'node:test';
import assert from 'node:assert/strict';
import {observedField,recordedPool,tradeClocks} from '../src/utils/detail-presentation.js';
import {riskDisplayCheck} from '../src/utils/risk-presentation.js';
import {getJSON} from '../src/api/client.js';
const now=1_800_000_000_000,token='0x'+'a'.repeat(40),pool='0x'+'b'.repeat(40);

test('a stale incomplete zero and old buys cannot become current detail numbers',()=>{
 const asset={totalLiquidityUsd:0,totalLiquidityAt:now-86_400_000,totalLiquidityStatus:'stale',totalLiquidityCoverage:{complete:false},buys24h:66,fieldTimes:{buys24h:now-6*86_400_000}};
 assert.equal(observedField(asset,'totalLiquidityUsd',now).value,null);
 assert.equal(observedField(asset,'buys24h',now).value,null);
 assert.equal(observedField({...asset,totalLiquidityAt:now,totalLiquidityStatus:'current'},'totalLiquidityUsd',now).value,null);
 assert.equal(observedField({...asset,totalLiquidityAt:now,totalLiquidityStatus:'current',totalLiquidityCoverage:{complete:true}},'totalLiquidityUsd',now).value,0);
});
test('recorded pool identity survives stale qualification and receipt delay differs from idle age',()=>{
 assert.equal(recordedPool({pool,stock:token,status:'verified',level:null}),true);
 assert.equal(recordedPool({pool,stock:token,status:'name-match',level:'B'}),false);
 assert.deepEqual(tradeClocks({t:now-120_000,receivedAt:now-119_000,delayMs:120_000},now),{ageMs:120_000,receiptDelayMs:1000,confirmed:false});
});
test('expired clear becomes unknown and prior triggered risk retains historical severity',()=>{
 const old={status:'clear',checkedAt:now-21_600_001,provider:'GoPlus'};
 assert.equal(riskDisplayCheck(old,now).status,'unknown');
 const flag=riskDisplayCheck({...old,status:'triggered',severity:'red'},now);
 assert.equal(flag.status,'triggered');assert.equal(flag.historical,true);assert.equal(flag.severity,'critical');
 assert.equal(riskDisplayCheck({status:'clear'},now).status,'unknown');
});
test('identical concurrent HTTP reads share transport but isolate consumers and allow retries',async()=>{
 const saved=globalThis.fetch;let calls=0,release;
 try{
  globalThis.fetch=()=>{calls++;return new Promise(resolve=>release=resolve);};
  const a=getJSON('/test-integrity'),b=getJSON('test-integrity');
  release({ok:true,json:async()=>({rows:[{value:1}]})});
  const [first,second]=await Promise.all([a,b]);assert.equal(calls,1);first.rows[0].value=9;assert.equal(second.rows[0].value,1);
  globalThis.fetch=async()=>{calls++;return {ok:false,status:503};};
  await assert.rejects(getJSON('test-integrity'),/HTTP 503/);
  globalThis.fetch=async()=>{calls++;return {ok:true,json:async()=>({ok:true})};};
  assert.equal((await getJSON('test-integrity')).ok,true);assert.equal(calls,3);
 }finally{globalThis.fetch=saved;}
});
