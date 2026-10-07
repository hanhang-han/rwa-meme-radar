import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parse,compileScript} from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as vueRouter from 'vue-router';
import * as presentation from '../src/utils/detail-presentation.js';
import * as chainScope from '../src/utils/chain-scope.js';
import * as navigation from '../src/utils/navigation-context.js';

const address=c=>'0x'+c.repeat(40),stock=address('a'),token=address('b'),pool=address('c');
const relation={chainId:'56',stock,token,pool,ticker:'700'};
const flush=async()=>{await new Promise(resolve=>setImmediate(resolve));for(let i=0;i<3;i++){await Promise.resolve();await vue.nextTick();}};
const compiled=compileScript(parse(readFileSync(new URL('../src/views/PairView.vue',import.meta.url),'utf8')).descriptor,{id:'pair-navigation',inlineTemplate:true}).content
 .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,bindings,path)=>`const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`)
 .replace('export default','return');

async function mountPair({query={},source='/meme?chain=all&page=3',relations=[relation],read=async()=>({relations:[]}),alias=false}={}){
 const calls=[],changes=[],history=vueRouter.createMemoryHistory();
 for(const method of ['push','replace']){const original=history[method];history[method]=(...args)=>{changes.push({method,path:args[0]});return original.apply(history,args);};}
 const imports={vue,'vue-router':vueRouter,'../stores/dashboard':{useDashboardStore:()=>({relations})},
  '../api/client':{getDetail:async(...args)=>{calls.push(args);return read(...args);}},'../utils/detail-presentation':presentation,
  '../utils/chain-scope':chainScope,'../utils/navigation-context':navigation,'../i18n':{tr:zh=>zh}};
 const component=new Function('imports',compiled)(imports),empty={render:()=>vue.h('span')};
 const router=vueRouter.createRouter({history,routes:[
  {path:'/pair/:chain/:stock',alias:'/pools/:chain/:stock',component,props:true},
  ...['/asset/:chain/:address','/meme','/live','/stock','/stock/:ticker','/watch','/events','/me'].map(path=>({path,component:empty})),
 ]});
 await router.push(source);await router.push({path:`/${alias?'pools':'pair'}/56/${stock}`,query:{chain:'all',pool,...query}});changes.length=0;
 const node=(type,text='')=>({type,text,props:{},children:[],parent:null});
 const detach=child=>{if(child.parent){const siblings=child.parent.children,index=siblings.indexOf(child);if(index>=0)siblings.splice(index,1);child.parent=null;}};
 const renderer=vue.createRenderer({createElement:tag=>node(tag),createText:text=>node('text',text),createComment:()=>node('comment'),
  insert:(child,parent,anchor)=>{detach(child);child.parent=parent;const index=anchor?parent.children.indexOf(anchor):-1;if(index<0)parent.children.push(child);else parent.children.splice(index,0,child);},
  remove:detach,setText:(target,text)=>target.text=text,setElementText:(target,text)=>{target.text=text;target.children=[];},
  patchProp:(target,key,_old,value)=>target.props[key]=value,parentNode:target=>target.parent,nextSibling:target=>target.parent?.children[target.parent.children.indexOf(target)+1]??null});
 const app=renderer.createApp({render:()=>vue.h(vueRouter.RouterView)}),root=node('root');app.use(router);app.mount(root);await flush();
 const all=()=>{const result=[];const visit=n=>{result.push(n);n.children.forEach(visit);};visit(root);return result;};
 const textOf=n=>n.text+n.children.map(textOf).join('');
 return {router,changes,calls,all,textOf,unmount:()=>app.unmount()};
}

test('resolving a pair replaces the bridge and preserves every main-tab source rather than assigning stock',async()=>{
 const theme='/stock/700?chain=all&from=stock&back='+encodeURIComponent('/stock?chain=all&page=3');
 for(const [from,back,expected]of [['live','/live?chain=all','live'],['meme','/meme?chain=all&page=4','meme'],['watch','/watch?chain=all&group=memes','watch'],['me','/me?section=alerts','me'],['stock',theme,'stock'],['events','/events?from=live&back=%2Flive%3Fchain%3Dall','live']]){
  const env=await mountPair({query:{from,back}});
  try{
   const current=env.router.currentRoute.value;
   assert.equal(current.path,'/asset/56/'+token);assert.equal(current.query.from,expected);assert.equal(current.query.back,back);
   assert.equal(current.query.pool,pool);assert.equal(current.query.tab,'relation');assert.equal(current.query.ticker,'700');
   assert.deepEqual(env.changes.map(change=>change.method),['replace']);
   env.router.back();await flush();assert.equal(env.router.currentRoute.value.path,'/meme');
  }finally{env.unmount();}
 }
});

test('direct, unsafe and self-referencing pair links fall back to the pool list with no redirect loop',async()=>{
 const own=`/pair/56/${stock}?pool=${pool}`;
 for(const query of [{},{from:'stock'},{from:'me',back:'https://example.invalid/me'},{from:'watch',back:own},{back:own.replace('/pair/','/pools/')}]){
  const env=await mountPair({query,alias:true});
  try{
   const current=env.router.currentRoute.value;
   assert.equal(current.path,'/asset/56/'+token);assert.equal(current.query.from,'meme');
   const parent=navigation.navigationRoute(current.query.back);
   assert.equal(parent.path,'/meme');assert.equal(parent.query.view,'pool');assert.equal(parent.query.q,stock);assert.equal(parent.query.chain,'all');
  }finally{env.unmount();}
 }
});

test('a pair matches the complete chain, stock contract and pool across paginated relations',async()=>{
 const env=await mountPair({query:{from:'watch',back:'/watch?group=pools'},relations:[{...relation,chainId:'196'},{...relation,stock:address('d')},{...relation,pool:address('e')}],
  read:async(_chain,_stock,{offset})=>offset===0?{relations:[{...relation,pool:address('e')}],next:100}:{relations:[relation]}});
 try{
  assert.deepEqual(env.calls.map(call=>call[2].offset),[0,100]);assert.equal(env.router.currentRoute.value.path,'/asset/56/'+token);
  assert.equal(env.router.currentRoute.value.query.from,'watch');assert.equal(env.router.currentRoute.value.query.back,'/watch?group=pools');
 }finally{env.unmount();}
});

test('temporary request failure stays distinct from a missing pair and retry resolves without losing its source',async()=>{
 let attempts=0;
 const env=await mountPair({query:{from:'me',back:'/me?section=alerts'},relations:[],read:async()=>{if(++attempts===1)throw Error('temporary');return {relations:[relation]};}});
 try{
  assert.match(env.textOf(env.all().find(n=>n.type==='h2')),/暂时无法加载/);assert.match(env.router.currentRoute.value.path,/^\/pair\//);
  env.all().find(n=>n.type==='button').props.onClick();await flush();
  assert.equal(env.router.currentRoute.value.query.from,'me');assert.equal(env.router.currentRoute.value.query.back,'/me?section=alerts');
 }finally{env.unmount();}
 const missing=await mountPair({relations:[]});
 try{assert.match(missing.textOf(missing.all().find(n=>n.type==='h2')),/未找到/);assert.match(missing.router.currentRoute.value.path,/^\/pair\//);}finally{missing.unmount();}
});

test('a late response for an old pool cannot replace the newer deep link or its return context',async()=>{
 const pending=[],nextPool=address('d'),nextToken=address('e');
 const env=await mountPair({relations:[],read:()=>new Promise(resolve=>pending.push(resolve)),query:{from:'live',back:'/live?chain=all'}});
 try{
  await env.router.push({path:`/pair/56/${stock}`,query:{chain:'all',pool:nextPool,from:'watch',back:'/watch?group=pools'}});await flush();
  assert.equal(pending.length,2);
  pending[0]({relations:[relation]});await flush();assert.match(env.router.currentRoute.value.path,/^\/pair\//);
  pending[1]({relations:[{...relation,pool:nextPool,token:nextToken}]});await flush();
  assert.equal(env.router.currentRoute.value.path,'/asset/56/'+nextToken);assert.equal(env.router.currentRoute.value.query.from,'watch');
  assert.equal(env.router.currentRoute.value.query.back,'/watch?group=pools');assert.equal(env.router.currentRoute.value.query.pool,nextPool);
 }finally{env.unmount();}
});
