import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withRequestAllowance,okxGet,startCollection,collectionStatus} from '../src/lib/okx-client';
test('stage budgets reserve work for siblings without bypassing network or global caps',async()=>{
 const saved=globalThis.fetch,vars={...process.env};let sent=0;
 Object.assign(process.env,{OKX_API_KEY:'test',OKX_SECRET_KEY:'test',OKX_PASSPHRASE:'test',OKX_DAILY_REQUEST_LIMIT:'100',OKX_ROUND_REQUEST_LIMIT:'5'});
 globalThis.fetch=async()=>{sent++;return new Response(JSON.stringify({code:'0',data:[]}));};
 try{
 startCollection();
 await withRequestAllowance(2,async()=>{
  await withRequestAllowance(1,async()=>{await okxGet('/api/v6/dex/test',{});await assert.rejects(okxGet('/api/v6/dex/test',{}),/allowance exhausted/);});
  await withRequestAllowance(10,async()=>{await okxGet('/api/v6/dex/test',{});await assert.rejects(okxGet('/api/v6/dex/test',{}),/allowance exhausted/);});
 });
 assert.equal(sent,2);assert.equal(collectionStatus().round,2);
 }finally{globalThis.fetch=saved;for(const k of ['OKX_API_KEY','OKX_SECRET_KEY','OKX_PASSPHRASE','OKX_DAILY_REQUEST_LIMIT','OKX_ROUND_REQUEST_LIMIT'])vars[k]===undefined?delete process.env[k]:process.env[k]=vars[k];}
});
