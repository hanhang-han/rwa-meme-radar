import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { ResearchStore } from '../src/lib/research-store';
import { closePostgresStoragePools, postgresStorage } from '../src/lib/postgres-storage';
import { reserveRequestSlot, sharedUsage } from '../src/lib/request-ledger';
import { inNetwork, xLayerState } from '../src/lib/xlayer';

const settings=process.env.CLIPERX_NODE_PG_TEST_SETTINGS;
if(settings)process.env.ARCHITECTURE_SETTINGS=settings;
const options={skip:!settings};
const file=process.env.RESEARCH_DB||'data/research.sqlite';
after(async()=>{await closePostgresStoragePools();});

test('PostgreSQL Node storage retains timestamps, evidence boundaries, cross-chain identities and duplicate history',options,async()=>{
  const token='node-test-'+randomUUID(),store=new ResearchStore(file,'196'),other=new ResearchStore(file,'56');
  const pg=postgresStorage('research',file)!;
  try{
    assert.ok((store as any).postgres);
    await store.put('asset',token,{token,symbol:'中文🙂',price:2,holders:0,updatedAt:200,tradeAt:200,
      tradeGaps:['retained'],fieldTimes:{price:200,holders:100},fieldSources:{price:'Python'}});
    await store.put('asset',token,{token,price:1,holders:null,updatedAt:100,tradeAt:100,tradeGaps:[],
      fieldTimes:{price:100,holders:300},fieldSources:{price:'stale',holders:'Node'}});
    const asset=await store.get<any>('asset',token);
    assert.equal(asset.symbol,'中文🙂');assert.equal(asset.price,2);assert.equal(asset.holders,null);
    assert.equal(asset.fieldTimes.price,200);assert.equal(asset.fieldTimes.holders,300);
    assert.equal(asset.updatedAt,200);assert.deepEqual(asset.tradeGaps,['retained']);
    assert.equal(await other.get('asset',token),null);
    await store.sample(token,1,0,300001);
    await pg.transaction(async client=>{
      await client.query(`INSERT INTO ${pg.table('sample_evidence')}(asset,t,body) VALUES ($1,$2,$3)`,['196:'+token,300000,'{"verified":true}']);
    });
    await store.sample(token,2,null,300002);
    assert.deepEqual(await store.samples(token),[{t:300000,price:2,cap:null}]);
    assert.equal((await pg.query(`SELECT body FROM ${pg.table('sample_evidence')} WHERE asset=$1`,['196:'+token])).length,0);
    const trade={id:'trade',t:300001,type:'buy',volume:0,user:'wallet'};
    await store.trades(token,[trade,trade]);
    assert.equal(await store.hasTrade(token,'trade'),true);
    assert.equal(await store.hasTrade(token,'missing'),false);
    assert.deepEqual(await store.activity(token,0),{buys:1,sells:0,volume:0,count:1,traders:1});
    assert.deepEqual(await store.recentTrades(token),[trade]);
    const candle={t:300000,o:1,h:2,l:.5,c:1.5,v:null,vu:0,confirmed:true};
    await store.candles(token,'5m',[candle]);
    assert.deepEqual(await store.candleRange(token,'5m'),[candle]);
    for(let i=0;i<12;i++)await store.event(token+':'+String(i).padStart(2,'0'),token,{kind:'verified'},100);
    await store.event(token+':00',token,{kind:'duplicate'},200);
    assert.equal((await store.events(token)).length,12);
    const ids=new Set<string>();let before:{t:number;id:string}|undefined;
    do{
      const page=await store.eventsPage(before,5);
      for(const event of page.items.filter(row=>row.asset===token)){
        assert.equal(ids.has(event.id),false);ids.add(event.id);
      }
      before=page.next??undefined;
    }while(before);
    assert.equal(ids.size,12);
    const reader=new ResearchStore(file,'196',{readOnly:true});
    try{await assert.rejects(reader.put('asset',token,{price:10}),/read-only/);assert.equal((await reader.get<any>('asset',token)).price,2);}
    finally{await reader.close();}
    await assert.rejects(pg.transaction(async client=>{
      await client.query(`INSERT INTO ${pg.table('facts')}(kind,id,body) VALUES ($1,$2,$3)`,['196:rollback-test',token,'{}']);
      throw new Error('abort probe');
    }),/abort probe/);
    assert.equal(await store.get('rollback-test',token),null);
  }finally{await store.close();await other.close();}
});

test('PostgreSQL quotas and provider start slots remain atomic with concurrent Node workers',options,async()=>{
  const day='node-budget-'+randomUUID();
  const outcomes=await Promise.all(Array.from({length:80},()=>sharedUsage(day,20,'node-test',20).then(()=>true,error=>{
    if(!/^OKX (local request budget exhausted|node-test daily allowance exhausted)$/.test(error?.message??''))throw error;
    return false;
  })));
  assert.equal(outcomes.filter(Boolean).length,20);
  assert.equal(await sharedUsage(day),20);
  const laneDay='node-lane-'+randomUUID();
  assert.equal(await sharedUsage(laneDay,100,'candles',1),1);
  await assert.rejects(sharedUsage(laneDay,100,'candles',1),/allowance exhausted/);
  assert.equal(await sharedUsage(laneDay),1);
  assert.equal(await sharedUsage(laneDay,100,'discovery',2),2);
  const now=Date.now()+10_000;
  const slots=(await Promise.all(Array.from({length:12},()=>reserveRequestSlot(500,now)))).sort((a,b)=>a-b);
  assert.deepEqual(slots,Array.from({length:12},(_,index)=>index*500));
});

test('PostgreSQL legacy state reads quote-only assets with absent or null metadata unchanged',options,async()=>{
  const token='0x'+randomUUID().replaceAll('-','').padEnd(40,'a');
  const other='0x'+randomUUID().replaceAll('-','').padEnd(40,'b');
  const store=new ResearchStore(file,'196');
  const pg=postgresStorage('research',file)!;
  const rows=[{token,chain:'196',kind:'candidate',price:0,updatedAt:123,fieldTimes:{price:123}},
    {token:other,chain:'196',kind:'candidate',symbol:null,name:null,price:2.5,updatedAt:456,fieldTimes:{price:456}}];
  try{
    for(const row of rows)await store.put('asset',row.token,row);
    const state=await inNetwork('196',{tokens:[],updatedAt:null,status:'ready',error:null},xLayerState);
    for(const row of rows){
      assert.deepEqual(state.assets.find(asset=>asset.token===row.token),row);
      const group=state.groups.find(group=>group.members.some(asset=>asset.token===row.token));
      assert.equal(group?.symbol,null);assert.equal(group?.count,1);
    }
    assert.doesNotThrow(()=>JSON.stringify(state));
  }finally{
    // These two identities were created only by this isolated fixture.
    await pg.transaction(async client=>{
      await client.query(`DELETE FROM ${pg.table('facts')} WHERE kind=$1 AND id=ANY($2::text[])`,['196:asset',[token,other]]);
    });
    await store.close();
  }
});
