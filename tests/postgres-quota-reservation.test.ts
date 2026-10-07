import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { Pool } from 'pg';
import { closePostgresStoragePools } from '../src/lib/postgres-storage';
import { collectionStatus, okxPost, startCollection, withRequestAllowance } from '../src/lib/okx-client';

test('concurrent urgent calls reserve their stage allowance while the PostgreSQL quota commit is pending',async()=>{
  const directory=mkdtempSync(join(tmpdir(),'quota-pending-'));
  const file=join(directory,'runtime.json');
  const savedEnvironment={...process.env},savedFetch=globalThis.fetch;
  const prototype:any=Pool.prototype,originalQuery=prototype.query,originalConnect=prototype.connect;
  let charged=0,sent=0;
  writeFileSync(file,JSON.stringify({managedBy:'cliperx-architecture-v1',dsn:'postgresql://test@localhost/never_connected',
    storage:{enabled:true,backend:'postgres',domainSchemas:Object.fromEntries(
      ['research','market','accounts','budget','leases','stream_archive'].map(domain=>[domain,'cliperx_storage_'+domain]))}}));
  Object.assign(process.env,{ARCHITECTURE_SETTINGS:file,NODE_ENV:'production',OKX_API_KEY:'test',
    OKX_SECRET_KEY:'test',OKX_PASSPHRASE:'test',OKX_DAILY_REQUEST_LIMIT:'100',OKX_ROUND_REQUEST_LIMIT:'5'});
  prototype.query=async(sql:string)=>{
    if(sql.includes('_cliperx_migration_owner'))return {rows:[{source_id:'a'.repeat(64),source_schema_hash:'b'.repeat(64)}]};
    if(sql.includes('_cliperx_migration_state'))return {rows:[{total:3,pending:0}]};
    if(sql.includes('to_regprocedure'))return {rows:[{helper:'installed'}]};
    if(sql.includes('SELECT used'))return {rows:[{used:charged}]};
    throw new Error('Unexpected storage status query');
  };
  prototype.connect=async()=>({
    async query(sql:string){
      if(sql.includes('pg_advisory_xact_lock'))await new Promise(resolve=>setTimeout(resolve,15));
      if(sql.includes('SELECT used'))return {rows:[{used:charged}]};
      if(sql.includes('SET used=used+1'))charged++;
      return {rows:[]};
    },release(){},
  });
  globalThis.fetch=async()=>{sent++;return new Response(JSON.stringify({code:'0',data:[]}));};
  try{
    startCollection();
    const outcomes=await withRequestAllowance(1,()=>Promise.allSettled([
      okxPost('/api/v6/dex/test',[],{urgent:true}),okxPost('/api/v6/dex/test',[],{urgent:true}),
    ]));
    assert.equal(outcomes.filter(result=>result.status==='fulfilled').length,1);
    const failed=outcomes.find(result=>result.status==='rejected') as PromiseRejectedResult;
    assert.match(String(failed.reason),/allowance exhausted/);
    assert.equal(charged,1);assert.equal(sent,1);
    assert.equal((await collectionStatus()).round,1);
  }finally{
    prototype.query=originalQuery;prototype.connect=originalConnect;
    globalThis.fetch=savedFetch;
    for(const key of ['ARCHITECTURE_SETTINGS','NODE_ENV','OKX_API_KEY','OKX_SECRET_KEY','OKX_PASSPHRASE','OKX_DAILY_REQUEST_LIMIT','OKX_ROUND_REQUEST_LIMIT']){
      if(savedEnvironment[key]===undefined)delete process.env[key];else process.env[key]=savedEnvironment[key];
    }
    await closePostgresStoragePools();
    rmSync(directory,{recursive:true,force:true});
  }
});
