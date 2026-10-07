import test from 'node:test';
import assert from 'node:assert/strict';
import { ResearchStore } from '../src/lib/research-store';

test('Node stale asset writes preserve Python quote metadata and trade checkpoints',async()=>{
  const s=new ResearchStore(':memory:');
  try{
    (await s.put('asset','a',{token:'a',price:2,holders:10,tradeAt:200,tradeGap:{from:1,to:5},fieldTimes:{price:200,holders:100},fieldSources:{price:'OKX'}}));
    (await s.put('asset','a',{token:'a',price:1,holders:20,tradeAt:100,tradeGap:null,fieldTimes:{price:100,holders:300},fieldSources:{price:'old',holders:'new'}}));
    const row=(await s.get<any>('asset','a'))!;
    assert.equal(row.price,2);assert.equal(row.fieldTimes.price,200);assert.equal(row.fieldSources.price,'OKX');
    assert.equal(row.holders,20);assert.equal(row.fieldTimes.holders,300);assert.equal(row.tradeAt,200);
    assert.deepEqual(row.tradeGap,{from:1,to:5});
    (await s.put('asset','a',{token:'a',kind:'stock'}));
    assert.equal((await s.get<any>('asset','a'))!.price,2);
  }finally{(await s.close());}
});


test('Node sample replacement cannot inherit Python verified evidence from the same bucket',async()=>{
  const s=new ResearchStore(':memory:');
  // Inspect the physical shared schema; Python stores evidence under these
  // same composite keys, and unrelated chains must remain untouched.
  const db=(s as any).db;
  try{
    (await s.sample('a',1,null,300001));
    const evidence=JSON.stringify({provider:'Binance',verified:true,marketAt:300001});
    db.prepare('INSERT INTO sample_evidence VALUES (?,?,?)').run('196:a',300000,evidence);
    db.prepare('INSERT INTO sample_evidence VALUES (?,?,?)').run('56:a',300000,evidence);
    (await s.sample('a',2,null,300002));
    assert.equal((await s.samples('a'))[0].price,2);
    assert.equal(db.prepare('SELECT body FROM sample_evidence WHERE asset=? AND t=?').get('196:a',300000),undefined);
    assert.ok(db.prepare('SELECT body FROM sample_evidence WHERE asset=? AND t=?').get('56:a',300000));
  }finally{(await s.close());}
});
