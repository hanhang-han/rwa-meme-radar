import test from 'node:test';
import assert from 'node:assert/strict';
import { createPinia, setActivePinia } from 'pinia';
import { useAccountStore } from '../src/stores/account.js';
import { readThemeWatches, THEME_WATCH_KEY } from '../src/utils/theme-map-model.js';

const A={user:{id:'account-a',email:'a@example.com'},csrfToken:'csrf-a'};
const B={user:{id:'account-b',email:'b@example.com'},csrfToken:'csrf-b'};
const ONE='56:0x'+'a'.repeat(40), TWO='196:0x'+'b'.repeat(40), PEER='stock:NVDA';
const response=(body,status=200)=>new Response(JSON.stringify(body),{status});
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
async function until(fn){for(let i=0;i<100&&!fn();i++)await tick();assert.ok(fn(),'condition completed');}
function setup(handler){
  const saved={window:global.window,document:global.document,localStorage:global.localStorage,fetch:global.fetch};
  const values=new Map();
  global.window=new EventTarget();global.document={hidden:false};
  global.localStorage={get length(){return values.size;},key:index=>[...values.keys()][index]??null,getItem:key=>values.get(key)??null,setItem:(key,v)=>values.set(key,String(v)),removeItem:key=>values.delete(key)};
  global.fetch=handler;setActivePinia(createPinia());const account=useAccountStore();
  return {account,values,cleanup(){account.stop();for(const [key,value] of Object.entries(saved)){if(value===undefined)delete global[key];else global[key]=value;}}};
}

test('route mounts do not reset an authenticated session or pending changes',async()=>{
  const patch=deferred();let requests=0;
  const env=setup(async(url,options={})=>{
    ++requests;
    if(url.endsWith('/me'))return response({detail:'login-required'},401);
    if(options.method==='PATCH')return patch.promise;
    return response({userId:A.user.id,items:[]});
  });
  try{
    await env.account.start();await env.account.useSession(A);env.account.toggle(ONE);
    const before=requests;await env.account.start();assert.equal(requests,before);
    patch.resolve(response({userId:A.user.id,items:[ONE]}));await until(()=>env.account.syncState==='synced');
    assert.deepEqual([...readThemeWatches()],[ONE]);
  }finally{env.cleanup();}
});

test('a stale refresh cannot roll back a successfully saved edit',async()=>{
  let hold=false;const old=deferred();
  const env=setup(async(url,options={})=>{
    if(url.endsWith('/me'))return response({},401);
    if(options.method==='PATCH')return response({userId:A.user.id,items:[ONE]});
    return hold?old.promise:response({userId:A.user.id,items:[]});
  });
  try{
    await env.account.start();await env.account.useSession(A);hold=true;
    const refreshing=env.account.refresh();env.account.toggle(ONE);await until(()=>env.account.syncState==='synced');
    old.resolve(response({userId:A.user.id,items:[]}));await refreshing;
    assert.deepEqual([...readThemeWatches()],[ONE]);
  }finally{env.cleanup();}
});

test('a late response from account A cannot overwrite account B cache',async()=>{
  const first=deferred();let reads=0;
  const env=setup(async(url)=>{
    if(url.endsWith('/me'))return response({},401);
    return ++reads===1?first.promise:response({userId:B.user.id,items:[TWO]});
  });
  try{
    await env.account.start();const loadingA=env.account.useSession(A);
    await env.account.useSession(B);first.resolve(response({userId:A.user.id,items:[ONE]}));await loadingA;
    assert.equal(env.account.session.user.id,B.user.id);
    assert.deepEqual([...readThemeWatches()],[TWO]);
    assert.equal(global.localStorage.getItem(`${THEME_WATCH_KEY}:${B.user.id}`),JSON.stringify([TWO]));
  }finally{env.cleanup();}
});

test('in-flight add then remove preserves peer additions and sends incremental intent',async()=>{
  const first=deferred();const sent=[];
  const env=setup(async(url,options={})=>{
    if(url.endsWith('/me'))return response({},401);
    if(options.method!=='PATCH')return response({userId:A.user.id,items:[]});
    sent.push(JSON.parse(options.body));return sent.length===1?first.promise:response({userId:A.user.id,items:[PEER,TWO]});
  });
  try{
    await env.account.start();await env.account.useSession(A);env.account.toggle(ONE);
    env.account.toggle(ONE);env.account.toggle(TWO);
    first.resolve(response({userId:A.user.id,items:[ONE,PEER]}));await until(()=>sent.length===2&&env.account.syncState==='synced');
    assert.deepEqual(sent,[{add:[ONE],remove:[]},{add:[TWO],remove:[ONE]}]);
    assert.deepEqual(new Set(readThemeWatches()),new Set([PEER,TWO]));
  }finally{env.cleanup();}
});

test('failed edits survive reload and replay only their delta, not a stale full list',async()=>{
  let fail=true;const sent=[];
  const env=setup(async(url,options={})=>{
    if(url.endsWith('/me'))return response(A);
    if(options.method==='PATCH'){
      sent.push(JSON.parse(options.body));return fail?response({detail:'unavailable'},503):response({userId:A.user.id,items:[PEER,ONE]});
    }
    return response({userId:A.user.id,items:[PEER]});
  });
  let resumed;
  try{
    await env.account.start();env.account.toggle(ONE);await until(()=>env.account.syncState==='error');
    assert.ok([...env.values].some(([key,value])=>key.startsWith('cliperx-watch-outbox-v2:')&&JSON.parse(value).key===ONE&&JSON.parse(value).add));
    env.account.stop();fail=false;setActivePinia(createPinia());resumed=useAccountStore();await resumed.start();
    assert.deepEqual(sent,[{add:[ONE],remove:[]},{add:[ONE],remove:[]}]);
    assert.deepEqual(new Set(readThemeWatches()),new Set([PEER,ONE]));
  }finally{resumed?.stop();env.cleanup();}
});

test('a session network failure is unknown rather than a confirmed guest',async()=>{
 const env=setup(async()=>{throw new Error('network-down');});
 try{await env.account.start();assert.equal(env.account.sessionState,'error');assert.equal(env.account.sessionError,'unavailable');assert.equal(env.account.syncState,'local');}finally{env.cleanup();}
});
test('a successful session check with a failed follow read remains authenticated',async()=>{
 const env=setup(async url=>url.endsWith('/me')?response(A):response({detail:'unavailable'},503));
 try{await env.account.start();assert.equal(env.account.session.user.id,A.user.id);assert.equal(env.account.sessionState,'authenticated');assert.equal(env.account.syncState,'error');assert.equal(env.account.sessionError,null);}finally{env.cleanup();}
});
test('a storage peer edit invalidates an in-flight old refresh and preserves its durable delta',async()=>{
 let hold=false;const old=deferred();
 const env=setup(async(url,options={})=>url.endsWith('/me')?response(A):options.method==='PATCH'?response({detail:'unavailable'},503):hold?old.promise:response({userId:A.user.id,items:[PEER]}));
 try{await env.account.start();hold=true;const refresh=env.account.refresh();const {createWatchOutbox}=await import('../src/utils/watch-outbox.js');const peer=createWatchOutbox({tabId:'peer',storage:()=>global.localStorage});peer.record(A.user.id,ONE,true);const key=[...env.values.keys()].find(key=>key.includes(':peer-'));
   const event=new Event('storage');event.key=key;global.window.dispatchEvent(event);await until(()=>env.account.syncState==='error');old.resolve(response({userId:A.user.id,items:[]}));await refresh;
   assert.ok(env.account.watches.includes(ONE));assert.ok(env.account.watches.includes(PEER));assert.deepEqual(peer.pending(A.user.id).add,[ONE]);
 }finally{env.cleanup();}
});

test('a stale tab cannot toggle into another account cache before its storage event is processed',async()=>{
 const waiting=deferred();let sessionChecks=0;
 const env=setup(async(url)=>url.endsWith('/me')?(++sessionChecks>1?waiting.promise:response(A)):response({userId:A.user.id,items:[]}));
 try{await env.account.start();env.values.set('cliperx-watch-account',B.user.id);env.values.set(`${THEME_WATCH_KEY}:${B.user.id}`,JSON.stringify([TWO]));env.account.toggle(ONE);
  assert.equal(env.values.get(`${THEME_WATCH_KEY}:${B.user.id}`),JSON.stringify([TWO]));assert.equal(env.account.watches.includes(ONE),false);assert.equal(sessionChecks,2);
  waiting.resolve(response({},401));await tick();
 }finally{env.cleanup();}
});

test('late A refresh, initial list and PATCH responses cannot write B before a storage event arrives',async()=>{
 for(const phase of ['refresh','initial','patch']){
  const delayed=deferred();let late=false,owner=A;
  const env=setup(async(url,options={})=>{
   if(url.endsWith('/me'))return response(owner);
   if(phase==='patch'&&options.method==='PATCH')return delayed.promise;
   if(phase==='initial'&&owner===A)return delayed.promise;
   if(late&&options.method!=='PATCH')return delayed.promise;
   return response({userId:owner.user.id,items:owner===A?[PEER]:[TWO]});
  });
  try{
   let waiting;
   if(phase==='initial')waiting=env.account.start();
   else{await env.account.start();if(phase==='refresh'){late=true;waiting=env.account.refresh();}else{env.account.toggle(ONE);waiting=tick();}}
   owner=B;late=false;env.values.set('cliperx-watch-account',B.user.id);env.values.set(`${THEME_WATCH_KEY}:${B.user.id}`,JSON.stringify([TWO]));
   delayed.resolve(response({userId:A.user.id,items:[PEER,ONE]}));await waiting;await until(()=>env.account.session?.user.id===B.user.id&&env.account.syncState==='synced');
   assert.equal(env.values.get(`${THEME_WATCH_KEY}:${B.user.id}`),JSON.stringify([TWO]),phase);assert.deepEqual(env.account.watches,[TWO],phase);
  }finally{env.cleanup();}
 }
});
