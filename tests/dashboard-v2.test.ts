import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {ResearchStore} from '../src/lib/research-store';
import {normalizeXAsset,inNetwork} from '../src/lib/xlayer';
import {marketPremium,identity,sumKnown} from '../src/lib/dashboard-v2';
const address='0x'+'a'.repeat(40);

test('legacy SQLite migration retains facts, samples, trades and events and isolates identical cross-chain addresses',()=>{
 const dir=mkdtempSync(join(tmpdir(),'radar-migration-')),file=join(dir,'research.sqlite');
 const old=new DatabaseSync(file);
 old.exec(`CREATE TABLE facts(kind TEXT,id TEXT,body TEXT,PRIMARY KEY(kind,id));CREATE TABLE samples(asset TEXT,t INTEGER,price REAL,cap REAL,PRIMARY KEY(asset,t));CREATE TABLE trades(asset TEXT,id TEXT,t INTEGER,body TEXT,PRIMARY KEY(asset,id));CREATE TABLE events(id TEXT PRIMARY KEY,asset TEXT,t INTEGER,body TEXT);`);
 old.prepare('INSERT INTO facts VALUES (?,?,?)').run('asset',address,JSON.stringify({token:address,price:7}));
 old.prepare('INSERT INTO samples VALUES (?,?,?,?)').run(address,300000,7,10);
 old.prepare('INSERT INTO trades VALUES (?,?,?,?)').run(address,'t1',2,JSON.stringify({id:'t1',t:2,type:'buy',volume:5}));
 old.prepare('INSERT INTO events VALUES (?,?,?,?)').run('e1',address,3,JSON.stringify({id:'e1',asset:address,t:3}));old.close();
 const x=new ResearchStore(file),bnb=new ResearchStore(file,'56');
 try{
 assert.equal(x.get<any>('asset',address).price,7);assert.equal(x.samples(address)[0].t,300000);assert.equal(x.activity(address,0).volume,5);assert.equal(x.events().length,1);
 assert.equal(bnb.get('asset',address),null);assert.equal(bnb.events().length,0);
 bnb.put('asset',address,{price:11});bnb.trades(address,[{id:'t1',t:2,type:'sell',volume:4}]);bnb.event('e1',address,{kind:'verified'},3);
 assert.equal(x.get<any>('asset',address).price,7);assert.equal(bnb.get<any>('asset',address).price,11);assert.equal(x.activity(address,0).buys,1);assert.equal(bnb.activity(address,0).sells,1);
 for(let i=0;i<130;i++)x.event('same-time-'+String(i).padStart(3,'0'),address,{kind:'verified'},100);
 const ids=new Set<string>();let cursor:any;do{const p=x.eventsPage(cursor,17);for(const e of p.items){assert.equal(ids.has(e.id),false);ids.add(e.id);}cursor=p.next;}while(cursor);
 assert.equal(ids.size,131);
 }finally{x.close();bnb.close();rmSync(dir,{recursive:true,force:true});}
});
test('partial quote refresh preserves the original timestamp of inherited fields',()=>{
 const a=normalizeXAsset({chainIndex:'196',tokenContractAddress:address,price:1,volume:10,holders:20},null,1000)!;
 const b=normalizeXAsset({chainIndex:'196',tokenContractAddress:address,price:2},a,2000)!;
 assert.equal(b.fieldTimes?.price,2000);assert.equal(b.fieldTimes?.holders,1000);assert.equal(b.holders,20);
 const c=normalizeXAsset({chainIndex:'196',tokenContractAddress:address,holders:''},b,3000)!;assert.equal(c.holders,null);
 const other=inNetwork('56',{tokens:[],updatedAt:1,status:'ready',error:null},()=>normalizeXAsset({chainIndex:'56',tokenContractAddress:address,price:3},null,4000));
 assert.equal(other?.chain,'56');assert.notEqual(identity('196',address),identity('56',address));
});
test('cross-market premium requires independent, positive, aligned prices and an explicit conversion ratio',()=>{
 const now=Date.now();assert.equal(marketPremium(220,100,2,now,now,true,now).value,10.000000000000009);
 assert.equal(marketPremium(100,100,1,now,now,false,now).value,null);
 assert.equal(marketPremium(100,100,null,now,now,true,now).value,null);
 assert.equal(marketPremium(100,0,1,now,now,true,now).value,null);
 assert.equal(marketPremium(100,100,1,now,now-900001,true,now).value,null);
 assert.deepEqual(sumKnown([{v:null},{v:0}],'v'),{value:0,known:1,total:2});assert.equal(sumKnown([{v:null}],'v').value,null);
});

test('stock labels require both numeric code and matching token symbol; unrecognised codes stay unresolved',async()=>{
 const {stockIdentity}=await import('../src/lib/stock-identity');
 assert.equal(stockIdentity({stockCode:'1',tokenSymbol:'CKHUTx'}).nameZh,'长江和记实业');
 assert.equal(stockIdentity({stockCode:'01024',tokenSymbol:'KUAIx'}).id,'XHKG:01024');
 assert.equal(stockIdentity({stockCode:'1038',tokenSymbol:'CKINFx'}).nameEn,'CK Infrastructure Holdings');
 assert.equal(stockIdentity({stockCode:'1',tokenSymbol:'OTHER',assetId:'x'}).status,'unresolved');
 assert.equal(stockIdentity({stockCode:'625',tokenSymbol:'SHEINx',assetId:'x'}).market,null);
});
test('coverage separates confirmed zero, unknown, historical and fresh values, and deduplicates pools',async()=>{
 const {fieldCoverage,dataQuality}=await import('../src/lib/data-quality');const now=2_000_000;
 const rows=[{v:0,t:now},{v:12,t:now-1_000_000},{v:null,t:now},{v:3},{v:NaN,t:now}];
 assert.deepEqual(fieldCoverage(rows,'v',r=>r.t,now),{total:5,available:3,fresh:1,stale:2,missing:2,zero:1});
 const r={chainId:'56',pool:'p',liquidityUsd:0,liquidityAt:now,checkedAt:now,status:'verified'};
 const q=dataQuality([],[],[r,r,{...r,chainId:'196',liquidityAt:1}],now);
 assert.equal(q.verifiedPools,2);assert.equal(q.poolLiquidity.fresh,1);assert.equal(q.poolLiquidity.zero,2);
});
