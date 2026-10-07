import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createPinia,defineStore} from 'pinia';
import {createWatchOutbox,validWatchKey} from '../src/utils/watch-outbox.js';
const source=readFileSync(new URL('../src/stores/account.js',import.meta.url),'utf8').replace(/^import .*;\s*$/gm,'').replace('export const useAccountStore','const useAccountStore')+'\nreturn useAccountStore;';
const factory=new Function('defineStore','developerRequest','getDeveloperSession','logoutDeveloper','THEME_WATCH_KEY','readThemeWatches','writeThemeWatches','createWatchOutbox','validWatchKey','window','document','localStorage','setInterval','clearInterval',source);
const OWNER={user:{id:'owner'},csrfToken:'csrf'},ONE='56:0x'+'a'.repeat(40),TWO='196:0x'+'b'.repeat(40),THREE='stock:NVDA',PEER='stock:TSLA';
const tick=()=>new Promise(r=>setTimeout(r,0));
async function until(fn){for(let i=0;i<100&&!fn();i++)await tick();assert.ok(fn(),'tabs settled');}
function browser(server){
 const data=new Map(),tabs=[];
 function tab(id){
  const bus=new EventTarget();
  const storage={get length(){return data.size;},key:i=>[...data.keys()][i]??null,getItem:key=>data.get(key)??null,
   setItem(key,value){const old=data.get(key)??null;data.set(key,String(value));if(old!==String(value))emit(key,old,String(value));},
   removeItem(key){const old=data.get(key)??null;data.delete(key);if(old!=null)emit(key,old,null);}};
  function emit(key,oldValue,newValue){for(const peer of tabs)if(peer.bus!==bus&&!peer.closed)queueMicrotask(()=>{if(peer.closed)return;const e=new Event('storage');Object.assign(e,{key,oldValue,newValue});peer.bus.dispatchEvent(e);});}
  const read=()=>{const owner=storage.getItem('cliperx-watch-account'),key='cliperx-theme-watch-v1'+(owner?':'+owner:'');return new Set(JSON.parse(storage.getItem(key)||'[]'));};
  const write=(items,silent=false)=>{const owner=storage.getItem('cliperx-watch-account'),key='cliperx-theme-watch-v1'+(owner?':'+owner:''),before=[...read()],after=[...items];storage.setItem(key,JSON.stringify(after));bus.dispatchEvent(new CustomEvent('theme-watch-change',{detail:{before,after,silent}}));};
  const useStore=factory(defineStore,(path,options)=>server(id,path,options),async()=>OWNER,async()=>{},'cliperx-theme-watch-v1',read,write,()=>createWatchOutbox({storage:()=>storage,tabId:id}),validWatchKey,bus,{hidden:false},storage,()=>1,()=>{});
  const account=useStore(createPinia()),context={bus,account,closed:false,close(){this.closed=true;account.stop();}};tabs.push(context);return context;
 }
 return {tab,data};
}

test('independent account-store tabs replay all incremental edits after interleaved failures and both tabs close',async()=>{
 let failed=true;const remote=new Set([PEER]),flights=[],requests=[];
 const env=browser(async(tab,path,options)=>{
  if(options?.method!=='PATCH')return {userId:OWNER.user.id,items:[...remote]};
  const delta=structuredClone(options.body);requests.push({tab,delta});
  if(failed)return new Promise((_resolve,reject)=>flights.push(()=>reject(Object.assign(new Error('offline'),{status:503}))));
  for(const key of delta.add)remote.add(key);for(const key of delta.remove)remote.delete(key);return {userId:OWNER.user.id,items:[...remote]};
 });
 const left=env.tab('left'),right=env.tab('right');let reopened;
 try{
  await left.account.start();await right.account.start();left.account.toggle(ONE);right.account.toggle(TWO);left.account.toggle(ONE);right.account.toggle(THREE);
  await until(()=>flights.length===2);for(const fail of flights)fail();await until(()=>left.account.syncState==='error'&&right.account.syncState==='error');
  left.close();right.close();failed=false;reopened=env.tab('reopened');await reopened.account.start();await until(()=>reopened.account.syncState==='synced');
  assert.deepEqual(remote,new Set([PEER,TWO,THREE]));assert.deepEqual(new Set(reopened.account.watches),remote);
  const replay=requests.filter(r=>r.tab==='reopened');assert.equal(replay.length,1);assert.deepEqual(new Set(replay[0].delta.add),new Set([TWO,THREE]));assert.deepEqual(replay[0].delta.remove,[ONE]);
 }finally{left.close();right.close();reopened?.close();}
});

test('two active stores converge when an old add completes after the other tab saved a remove',async()=>{
 const remote=new Set([PEER]),flights=[],requests=[];
 function apply(delta){for(const key of delta.add)remote.add(key);for(const key of delta.remove)remote.delete(key);return {userId:OWNER.user.id,items:[...remote]};}
 const env=browser(async(tab,path,options)=>{
  if(options?.method!=='PATCH')return {userId:OWNER.user.id,items:[...remote]};
  const delta=structuredClone(options.body);requests.push({tab,delta});
  if(requests.length<=2)return new Promise(resolve=>flights.push(()=>resolve(apply(delta))));
  return apply(delta);
 });
 const left=env.tab('left'),right=env.tab('right');
 try{await left.account.start();await right.account.start();left.account.toggle(ONE);right.account.toggle(ONE);await until(()=>flights.length===2);
  flights[1]();await until(()=>right.account.syncState==='synced');flights[0]();await until(()=>requests.length>=3&&!remote.has(ONE)&&left.account.syncState==='synced'&&right.account.syncState==='synced');
  assert.deepEqual(remote,new Set([PEER]));assert.deepEqual(new Set(left.account.watches),remote);assert.deepEqual(new Set(right.account.watches),remote);assert.ok(requests.slice(2).every(r=>r.delta.remove.includes(ONE)&&!r.delta.add.includes(ONE)));
 }finally{left.close();right.close();}
});

test('an older successful PATCH response cannot hide another tab\'s newer confirmed asset',async()=>{
 const remote=new Set(),requests=[];let finishOld;
 const env=browser(async(tab,path,options)=>{
  if(options?.method!=='PATCH')return {userId:OWNER.user.id,items:[...remote]};
  const delta=structuredClone(options.body);requests.push({tab,delta});
  for(const key of delta.add)remote.add(key);for(const key of delta.remove)remote.delete(key);
  const result={userId:OWNER.user.id,items:[...remote]};
  if(tab==='left'&&requests.length===1)return new Promise(resolve=>{finishOld=()=>resolve(result);});
  return result;
 });
 const left=env.tab('left'),right=env.tab('right');
 try{
  await left.account.start();await right.account.start();left.account.toggle(THREE);await until(()=>!!finishOld);
  right.account.toggle(PEER);await until(()=>right.account.syncState==='synced'&&right.account.watches.includes(PEER));
  const beforeOldResponse=requests.length;finishOld();await until(()=>left.account.syncState==='synced');await tick();
  assert.deepEqual(remote,new Set([THREE,PEER]));assert.deepEqual(new Set(left.account.watches),remote);assert.deepEqual(new Set(right.account.watches),remote);
  assert.deepEqual(new Set(JSON.parse(env.data.get('cliperx-theme-watch-v1:owner'))),remote);
  assert.equal(requests.length,beforeOldResponse,'no extra retry loop after both mutations succeeded');
 }finally{left.close();right.close();}
});
