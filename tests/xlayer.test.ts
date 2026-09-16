import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ResearchStore } from '../src/lib/research-store';
import { verifyXPool, resolveStock, normalizeTrade, normalizeXAsset, groupCandidates } from '../src/lib/xlayer';
import { unifiedFromXLayer } from '../src/lib/unified-state';
const meme='0x'+'a'.repeat(40), stock='0x'+'b'.repeat(40), wrapper='0x'+'c'.repeat(40), pool='0x'+'d'.repeat(40);
const word=(address:string)=>'0x'+address.slice(2).padStart(64,'0');
const stocks:any[]=[{tokenContractAddress:stock,stockCode:'NVDA'}];

test('pool verification resolves actual wrapper underlying, even for a meme without a stock name',async()=>{
  const result=await verifyXPool(pool,stocks,async(to,data)=>{
    if(to===pool) return data==='0x0dfe1681'?word(meme):word(wrapper);
    if(to===wrapper&&data==='0x38d52e0f')return word(stock);
    throw new Error('method unavailable');
  },'0x123');
  assert.equal(result.relation?.token,meme);
  assert.equal(result.relation?.stock.tokenContractAddress,stock);
  assert.equal(result.relation?.wrapper,true);
});
test('lookalike wrappers and stock-to-stock pools do not establish a meme relation',async()=>{
  assert.equal(await resolveStock(wrapper,stocks,async()=>word(meme)),null);
  await assert.rejects(resolveStock(wrapper,stocks,async()=>{throw new Error('HTTP 429');}),/incomplete/);
  const result=await verifyXPool(pool,[...stocks,{tokenContractAddress:meme}],async(_to,data)=>data==='0x0dfe1681'?word(meme):word(stock));
  assert.equal(result.relation,null);
  await assert.rejects(verifyXPool(pool,stocks,async()=>word(stock)),/invalid/);
});
test('wrong network, missing trade ID and unknown direction cannot contaminate activity',()=>{
  const row={chainIndex:'196',tokenContractAddress:meme,id:'event-1',time:'1234567',type:'buy',volume:'0'};
  assert.equal(normalizeTrade(row,meme)?.volume,0);
  assert.equal(normalizeTrade({...row,chainIndex:'56'},meme),null);
  assert.equal(normalizeTrade({...row,id:''},meme),null);
  assert.equal(normalizeTrade({...row,type:'mint'},meme),null);
  assert.equal(normalizeTrade({...row,volume:''},meme)?.volume,null);
});
test('same names group for display without merging addresses or missing fields into zeros',()=>{
  const a=normalizeXAsset({chainIndex:'196',tokenContractAddress:meme,tokenSymbol:'Apple',price:'',txsBuy:'0'})!;
  const b=normalizeXAsset({chainIndex:'196',tokenContractAddress:wrapper,tokenSymbol:'APPLE',price:'2'})!;
  assert.equal(a.price,null);assert.equal(a.buys24h,0);assert.equal(a.sells24h,null);
  assert.equal(groupCandidates([a,b],[]).length,1);
  assert.equal(groupCandidates([a,b],[])[0].members.length,2);
  assert.equal(normalizeXAsset({chainIndex:'56',tokenContractAddress:meme}),null);
});
test('unified feed retains internal provenance while merging stock-token catalogues',()=>{
  const feed=unifiedFromXLayer({
    status:'ready',updatedAt:10,coverage:{},assets:[{token:meme,chain:'196'}],
    relations:[{id:'r',token:meme}],signals:[{asset:meme}],groups:[],sectors:[],metrics:{},
  } as any,{status:'ready',updatedAt:11,tokens:[{chainIndex:'196',tokenContractAddress:stock}] as any},{status:'ready',updatedAt:12,tokens:[{chainIndex:'4663',tokenContractAddress:wrapper,tokenSymbol:'NVDA',stockCode:'NVDA'}] as any},{status:'ready',updatedAt:13,tokens:[{chainIndex:'56',tokenContractAddress:pool,tokenSymbol:'NVDAB',stockCode:'NVDA'}] as any});
  assert.deepEqual(feed.sources[0],{id:'okx:xlayer:196',provider:'OKX',chainId:'196',chainName:'X Layer',status:'ready',updatedAt:10});
  assert.equal(feed.assets[0].sourceId,'okx:xlayer:196');
  assert.equal(feed.assets[0].chainName,'X Layer');
  assert.equal(feed.relations[0].provider,'OKX');
  assert.equal(feed.stockTokens[0].chainId,'196');
  assert.deepEqual(feed.sources[1],{id:'robinhood:chain:4663',provider:'Robinhood',chainId:'4663',chainName:'Robinhood Chain',status:'ready',updatedAt:12});
  assert.equal(feed.stockTokens[1].tokenContractAddress,wrapper);
  assert.equal(feed.stockTokens[1].chainId,'4663');
  assert.deepEqual(feed.sources[2],{id:'binance:bstocks:56',provider:'Binance',chainId:'56',chainName:'BNB Smart Chain',status:'ready',updatedAt:13});
  assert.equal(feed.stockTokens[2].tokenContractAddress,pool);
});
test('archive survives pagination limits, duplicate replays and database restart',()=>{
  const dir=mkdtempSync(join(tmpdir(),'xlayer-test-')),file=join(dir,'test.sqlite');
  let store=new ResearchStore(file);
  try {
    for(let i=0;i<2100;i++)store.put('asset',String(i),{token:i});
    store.event('pool-1',meme,{label:'verified'},1);
    store.event('pool-1',meme,{label:'verified again'},2);
    const trade={id:'x',t:100,type:'buy',volume:1,user:meme};
    store.trades(meme,[trade]);store.trades(meme,[trade]);
    store.sample(meme,1,100,300000);store.sample(meme,2,200,300001);
    store.close();store=new ResearchStore(file);
    assert.equal(store.all('asset').length,2100);
    assert.deepEqual(store.get('asset','0'),{token:0});
    assert.equal(store.events().length,1);
    assert.equal(store.activity(meme,0).buys,1);
    assert.equal(store.activity(meme,0).volume,1);
    assert.equal(store.samples(meme).length,1);
    assert.equal(store.samples(meme)[0].price,2);
  }finally{store.close();rmSync(dir,{recursive:true,force:true});}
});
