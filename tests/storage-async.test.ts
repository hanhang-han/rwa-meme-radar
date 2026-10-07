import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ResearchStore } from '../src/lib/research-store';
import { refreshTrades, inNetwork } from '../src/lib/xlayer';
import { PostgresStorage, postgresResearchEnabled, postgresStorage, storageWriterLock } from '../src/lib/postgres-storage';

const delay=(ms:number)=>new Promise(resolve=>setTimeout(resolve,ms));

test('trade refresh awaits duplicate checks and durable writes before publishing its checkpoint',async()=>{
  const savedFetch=globalThis.fetch;
  const environment={...process.env};
  const original={hasTrade:ResearchStore.prototype.hasTrade,trades:ResearchStore.prototype.trades,put:ResearchStore.prototype.put};
  const token='0x'+'a'.repeat(40);
  let tradesStored=false,checkpointStored=false;
  Object.assign(process.env,{RESEARCH_DB:':memory:',OKX_API_KEY:'test',OKX_SECRET_KEY:'test',OKX_PASSPHRASE:'test'});
  ResearchStore.prototype.hasTrade=async(_token,id)=>{await delay(5);return id==='old';};
  ResearchStore.prototype.trades=async()=>{await delay(5);tradesStored=true;};
  ResearchStore.prototype.put=async()=>{await delay(5);assert.ok(tradesStored);checkpointStored=true;};
  globalThis.fetch=async()=>new Response(JSON.stringify({code:'0',data:['old','new'].map(id=>({
    id,chainIndex:'196',tokenContractAddress:token,time:Date.now(),type:'buy',price:'1',volume:'2',
  }))}));
  try{
    const asset:any={token,tradeAt:0};
    const fresh=await inNetwork('196',{tokens:[],updatedAt:null,status:'ready',error:null},()=>refreshTrades(asset));
    assert.deepEqual(fresh.map(row=>row.id),['new']);
    assert.ok(tradesStored&&checkpointStored);
    assert.equal(asset.tradeCoverage,'observed');
  }finally{
    globalThis.fetch=savedFetch;
    Object.assign(ResearchStore.prototype,original);
    for(const key of ['RESEARCH_DB','OKX_API_KEY','OKX_SECRET_KEY','OKX_PASSPHRASE']){
      if(environment[key]===undefined)delete process.env[key];else process.env[key]=environment[key];
    }
  }
});

test('an enabled invalid PostgreSQL switch fails closed instead of reading stale SQLite',()=>{
  const directory=mkdtempSync(join(tmpdir(),'node-storage-settings-'));
  const path=join(directory,'runtime.json'),before=process.env.ARCHITECTURE_SETTINGS;
  process.env.ARCHITECTURE_SETTINGS=path;
  try{
    writeFileSync(path,JSON.stringify({storage:{enabled:true,backend:'unsupported'}}));
    assert.throws(()=>postgresResearchEnabled(),/Invalid PostgreSQL storage switch/);
    assert.throws(()=>new ResearchStore(join(directory,'research.sqlite')),/Invalid PostgreSQL storage switch/);
    writeFileSync(path,JSON.stringify({storage:{enabled:false}}));
    assert.equal(postgresStorage('research','data/research.sqlite'),null);
    assert.equal(storageWriterLock('cliperx_storage_budget'),'8142821920835652560');
  }finally{
    if(before===undefined)delete process.env.ARCHITECTURE_SETTINGS;else process.env.ARCHITECTURE_SETTINGS=before;
    rmSync(directory,{recursive:true,force:true});
  }
});

test('libpq and malformed connection strings fail before a Node pool can interpret them as a hostname',()=>{
  const secret='do-not-print-this-password';
  for(const dsn of [`dbname=base user=test password=${secret} host=127.0.0.1`,
    `https://test:${secret}@example.com/base`,`postgresql://test:${secret}@127.0.0.1:invalid/base`]){
    assert.throws(()=>new PostgresStorage('cliperx_storage_budget',dsn),(error:Error)=>{
      assert.match(error.message,/PostgreSQL storage (DSN must use|connection URI is invalid)/);
      assert.equal(error.message.includes(secret),false);
      return true;
    });
  }
});
