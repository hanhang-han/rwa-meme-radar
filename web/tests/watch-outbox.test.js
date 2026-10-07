import test from 'node:test';
import assert from 'node:assert/strict';
import { createWatchOutbox } from '../src/utils/watch-outbox.js';
const ONE='56:0x'+'a'.repeat(40),TWO='196:0x'+'b'.repeat(40),THREE='stock:NVDA';
function sharedStorage(){const data=new Map();return {data,get length(){return data.size;},key:i=>[...data.keys()][i]??null,getItem:key=>data.get(key)??null,setItem:(key,value)=>data.set(key,String(value)),removeItem:key=>data.delete(key)};}
const client=(storage,tabId)=>createWatchOutbox({storage:()=>storage,tabId,now:()=>1000});

test('two independent tabs retain interleaved failed edits across both tabs closing and reopening',()=>{
  const storage=sharedStorage(),left=client(storage,'left'),right=client(storage,'right');
  left.record('account-a',ONE,true);const firstFlight=left.pending('account-a');
  right.record('account-a',TWO,true);const secondFlight=right.pending('account-a');
  left.record('account-a',ONE,false);right.record('account-a',THREE,true);
  // Both PATCH requests fail. Their claims are deliberately not acknowledged.
  assert.deepEqual(firstFlight.add,[ONE]);assert.equal(secondFlight.claims.length,2);
  const reopened=client(storage,'reopened');
  const pending=reopened.pending('account-a');assert.deepEqual(new Set(pending.add),new Set([TWO,THREE]));assert.deepEqual(pending.remove,[ONE]);
  const remote=[ONE,'stock:TSLA'];const final=reopened.apply('account-a',remote);
  assert.deepEqual(final,new Set([TWO,THREE,'stock:TSLA']));
  reopened.acknowledge('account-a',pending.claims);assert.equal(reopened.pending('account-a').claims.length,0);
  assert.equal(client(storage,'another-reload').pending('account-a').claims.length,0);
});

test('a successful newer remove followed by a late older add replays the final remove',()=>{
  const storage=sharedStorage(),left=client(storage,'left'),right=client(storage,'right');
  left.record('a',ONE,true);const oldFlight=left.pending('a');
  right.record('a',ONE,false);const newerFlight=right.pending('a');
  right.acknowledge('a',newerFlight.claims);assert.equal(right.pending('a').claims.length,0);
  left.acknowledge('a',oldFlight.claims);
  assert.deepEqual(right.pending('a').remove,[ONE]);assert.deepEqual([...right.apply('a',[ONE,TWO])],[TWO]);
  right.acknowledge('a',right.pending('a').claims);assert.equal(left.pending('a').claims.length,0);
});

test('acknowledging one tab does not clear another unsent asset, and account journals are isolated',()=>{
  const storage=sharedStorage(),left=client(storage,'left'),right=client(storage,'right');
  left.record('a',ONE,true);const flight=left.pending('a');right.record('a',TWO,true);right.record('b',THREE,true);
  left.acknowledge('a',flight.claims);
  assert.deepEqual(right.pending('a').add,[TWO]);assert.deepEqual(left.pending('b').add,[THREE]);assert.deepEqual([...left.apply('b',[])],[THREE]);
  assert.equal([...left.apply('b',[])].includes(ONE),false);
});

test('legacy failed incremental intents migrate once, then journal compaction remains bounded per asset',()=>{
  const storage=sharedStorage();storage.setItem('cliperx-watch-pending:a',JSON.stringify({add:[ONE],remove:[TWO]}));
  const tab=client(storage,'tab');tab.migrate('a');assert.equal(storage.getItem('cliperx-watch-pending:a'),null);
  assert.deepEqual(tab.pending('a').add,[ONE]);assert.deepEqual(tab.pending('a').remove,[TWO]);
  for(let i=0;i<100;i++){tab.record('a',ONE,i%2===0);tab.acknowledge('a',tab.pending('a').claims);}
  assert.ok(storage.length<=4);assert.equal(tab.pending('a').claims.length,0);
});

test('compaction never deletes a peer intent written after its candidate snapshot',()=>{
 const shared=sharedStorage(),left=client(shared,'left'),right=client(shared,'right');
 left.record('a',ONE,true);left.record('a',ONE,false);right.record('other-account',THREE,true);
 let armed=true,reads=0;const initialLength=shared.length;
 Object.defineProperty(shared,'length',{get(){const size=shared.data.size;if(armed&&++reads===initialLength+1){armed=false;right.record('a',TWO,true);}return size;}});
 // The final loop condition returns the old length, then another tab writes.
 // A second enumeration would see TWO, while the first keep set cannot.
 left.compact('a');assert.equal(armed,false);
 const reload=client(shared,'reopened');assert.deepEqual(reload.pending('a').add,[TWO]);assert.deepEqual(reload.pending('a').remove,[ONE]);assert.deepEqual(reload.pending('other-account').add,[THREE]);
});

test('a late old add acknowledgement cannot acknowledge a newer add after a completed remove',()=>{
 const storage=sharedStorage(),left=client(storage,'left'),right=client(storage,'right');
 left.record('a',ONE,true);const oldAdd=left.pending('a');
 right.record('a',ONE,false);right.acknowledge('a',right.pending('a').claims);
 right.record('a',ONE,true);const finalAdd=right.pending('a').claims[0];
 left.acknowledge('a',oldAdd.claims);
 const pending=right.pending('a');assert.deepEqual(pending.add,[ONE]);assert.equal(pending.claims[0].id,finalAdd.id);assert.deepEqual([...right.apply('a',[])],[ONE]);
 right.acknowledge('a',pending.claims);assert.equal(left.pending('a').claims.length,0);
});
test('concurrent legacy migrations are idempotent and never outrank a real edit in another tab',()=>{
 const storage=sharedStorage(),left=client(storage,'left'),right=client(storage,'right');
 const payload=JSON.stringify({add:[ONE,TWO],remove:[]});storage.setItem('cliperx-watch-pending:a',payload);
 let armed=true;const original=storage.setItem;
 storage.setItem=(key,value)=>{if(armed&&key.includes('legacy-')){armed=false;right.record('a',ONE,false);right.migrate('a');}original(key,value);};
 left.migrate('a');left.migrate('a');right.migrate('a');
 assert.deepEqual(left.pending('a').remove,[ONE]);assert.deepEqual(left.pending('a').add,[TWO]);
 const intents=[...storage.data].filter(([key,value])=>JSON.parse(value).kind==='intent'&&JSON.parse(value).key===TWO);assert.equal(intents.length,1);
});

test('request checkpoints preserve newer confirmed edits without a permanent local overlay',()=>{
 const storage=sharedStorage(),left=client(storage,'left'),right=client(storage,'right');
 left.record('a',ONE,true);const checkpoint=left.checkpoint('a'),flight=left.pending('a');
 right.record('a',TWO,true);right.acknowledge('a',right.pending('a').claims);
 right.record('a',THREE,false);right.acknowledge('a',right.pending('a').claims);
 left.acknowledge('a',flight.claims);
 assert.deepEqual(left.apply('a',[ONE,THREE],checkpoint),new Set([ONE,TWO]));
 assert.equal(left.pending('a').claims.length,0);
 // A later fresh server response may legitimately exclude an ACKed local item.
 assert.deepEqual(left.apply('a',[],left.checkpoint('a')),new Set());
 assert.deepEqual(left.apply('other-account',[],left.checkpoint('other-account')),new Set());
});
