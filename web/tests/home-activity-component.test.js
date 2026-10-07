import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as format from '../src/utils/format.js';
import * as theme from '../src/utils/theme-map-model.js';
import * as home from '../src/utils/home-model.js';
import * as live from '../src/utils/home-live-model.js';
import * as navigation from '../src/utils/navigation-context.js';
import * as pageState from '../src/utils/page-navigation-state.js';
import { tradeKey } from '../src/stores/feed.js';

const amountSource=parse(readFileSync(new URL('../src/components/TradeAmount.vue',import.meta.url),'utf8')).descriptor.script.content;
const {describeTradeAmount}=await import('data:text/javascript;base64,'+Buffer.from(amountSource).toString('base64'));
const now=Date.now(),token='0x'+'a'.repeat(40),other='0x'+'b'.repeat(40),pool='0x'+'c'.repeat(40);
const relation={chainId:'56',token,pool,ticker:'700',level:'A',status:'verified'};
const trade=(id,extra={})=>({id,chainId:'56',token,venue:'dex',hash:`0x${id}`,type:'buy',t:now-60000,volume:20,volumeCurrency:'USD',...extra});
const flush=async()=>{for(let i=0;i<6;i++){await Promise.resolve();await vue.nextTick();}};

function compile(file,imports){
  const compiled=compileScript(parse(readFileSync(new URL(`../src/components/${file}.vue`,import.meta.url),'utf8')).descriptor,{id:file,inlineTemplate:true}).content
    .replace(/^import\s+(\w+)\s*,\s*\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,name,bindings,path)=>`const ${name}=imports[${JSON.stringify(path)}].default;const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`)
    .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,bindings,path)=>`const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`)
    .replace(/^import\s+(\w+)\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,name,path)=>`const ${name}=imports[${JSON.stringify(path)}];`)
    .replace('export default','return');
  return new Function('imports',compiled)(imports);
}

function mountActivity(overrides={}, {restore=false,route={path:'/live',fullPath:'/live?chain=all',query:{chain:'all'}}}={}){
  if(!restore)pageState.clearPageState(`home-activity:${overrides.scope??'all'}`);
  const saved={window:globalThis.window,localStorage:globalThis.localStorage};
  const stored=new Map(),listeners=new Map();
  globalThis.localStorage={getItem:key=>stored.get(key)??null,setItem:(key,value)=>stored.set(key,value)};
  globalThis.window={addEventListener:(key,listener)=>listeners.set(key,listener),removeEventListener:key=>listeners.delete(key),dispatchEvent:()=>{}};
  const language=vue.ref('zh'),props=vue.reactive({trades:[trade('one')],changes:{items:[]},observations:[],unified:{relations:[relation],assets:[]},assets:[{chainId:'56',token,name:'真实资产'}],scope:'all',ticker:'',...overrides});
  const imports={vue,'vue-router':{useRoute:()=>route},'../utils/navigation-context':navigation,'../utils/page-navigation-state':pageState,'../i18n':{tr:(zh,en)=>language.value==='en'?en:zh},'../utils/format':format,
    '../composables/useMinuteClock':{useMinuteClock:()=>vue.ref(now)},'../utils/home-model':home,
    '../utils/home-live-model':live,'../utils/theme-map-model':theme,'../stores/feed':{tradeKey}};
  const important=compile('ImportantChanges',imports);
  const amount=vue.defineComponent({props:['trade'],setup:props=>()=>vue.h('b',{},describeTradeAmount(props.trade).value??'—')});
  const component=compile('HomeActivityPanel',{...imports,'./ImportantChanges.vue':important,'./TradeAmount.vue':{default:amount,describeTradeAmount}});
  const node=(type,text='')=>({type,text,props:{},style:{},children:[],parent:null});
  const detach=child=>{if(child.parent){const children=child.parent.children;const index=children.indexOf(child);if(index>=0)children.splice(index,1);child.parent=null;}};
  const renderer=vue.createRenderer({createElement:tag=>node(tag),createText:text=>node('text',text),createComment:()=>node('comment'),
    insert:(child,parent,anchor)=>{detach(child);child.parent=parent;const at=anchor?parent.children.indexOf(anchor):-1;if(at<0)parent.children.push(child);else parent.children.splice(at,0,child);},
    remove:detach,setText:(target,text)=>target.text=text,setElementText:(target,text)=>{target.text=text;target.children=[];},patchProp:(target,key,_old,value)=>target.props[key]=value,
    parentNode:target=>target.parent,nextSibling:target=>target.parent?.children[target.parent.children.indexOf(target)+1]??null});
  const app=renderer.createApp({render:()=>vue.h(component,props)});
  app.component('RouterLink',{inheritAttrs:false,props:['to'],setup:(props,{slots,attrs})=>()=>vue.h('a',{...attrs,to:props.to},slots.default?.())});
  const root=node('root');app.mount(root);
  const all=()=>{const rows=[];const walk=target=>{rows.push(target);for(const child of target.children)walk(child);};walk(root);return rows;};
  const textOf=node=>node.text+node.children.map(textOf).join('');
  const findClass=className=>all().find(node=>String(node.props.class??'').split(' ').includes(className));
  const findButton=label=>all().find(node=>node.type==='button'&&textOf(node)===label);
  const tradeLinks=()=>all().filter(node=>node.type==='a'&&node.props.to?.query?.tab==='trades');
  return {props,language,all,textOf,findClass,findButton,tradeLinks,listeners,stored,unmount:()=>{app.unmount();globalThis.window=saved.window;globalThis.localStorage=saved.localStorage;}};
}

test('home trades are compact DEX-only links into each asset trades tab, with exact theme identity',async()=>{
  const env=mountActivity({ticker:'700',trades:[trade('one',{pool}),trade('same-address-other-chain',{chainId:'196'}),trade('other-theme',{token:other}),trade('exchange',{venue:'binance'})]});
  try{
    await flush();env.findButton('成交').props.onClick();await flush();
    assert.equal(env.tradeLinks().length,1);assert.equal(env.tradeLinks()[0].props.to.path,`/asset/56/${token}`);
    assert.equal(env.tradeLinks()[0].props.to.query.tab,'trades');
    assert.equal(env.tradeLinks()[0].props.to.query.pool,pool);
    assert.equal(env.tradeLinks()[0].props.to.query.from,'live');
    assert.equal(env.tradeLinks()[0].props.to.query.back,'/live?chain=all');
    env.props.ticker='1024';await flush();assert.equal(env.tradeLinks().length,0);
    assert.ok(env.all().some(node=>env.textOf(node)==='当前范围暂无已收录链上成交。'));
  }finally{env.unmount();}
});

test('mouse, focus and touch activity keep applying new trades without a resume gate',async()=>{
  const env=mountActivity({trades:[trade('one')]});
  try{
    await flush();env.findButton('成交').props.onClick();await flush();
    assert.equal(env.findClass('activity-new'),undefined);
    for(const [index,handler] of ['onMouseenter','onFocusin','onTouchstartPassive'].entries()){
      env.findClass('activity-content').props[handler]?.();await flush();
      const fresh=trade('new-'+index,{t:now});
      env.props.trades=[fresh,...env.props.trades];await flush();
      assert.equal(env.tradeLinks().length,index+2);
      assert.equal(env.tradeLinks()[0].props.title.includes(fresh.hash),true);
      assert.equal(env.findClass('activity-new'),undefined);
    }
    assert.ok(env.all().some(node=>String(node.props.class??'').includes('activity-trade is-added')));
  }finally{env.unmount();}
});

test('reading discoveries displays new records immediately and keeps duplicate pools grouped',async()=>{
  const observation=(id,address,poolId)=>({id,chainId:'56',asset:`56:${address}`,kind:'pair-observed',ticker:'700',pool:poolId,t:now-1000});
  const first=observation('first',token,pool),fresh=observation('fresh',other,'0x'+'d'.repeat(40));
  const env=mountActivity({observations:[first]});
  try{
    await flush();
    const entries=()=>env.all().filter(node=>String(node.props.class??'').split(' ').includes('important-item'));
    assert.equal(entries().length,1);
    const content=env.findClass('activity-content');
    for(const handler of ['onMouseenter','onFocusin','onTouchstartPassive'])content.props[handler]?.();
    env.props.observations=[fresh,fresh,first];await flush();
    assert.equal(entries().length,2);
    assert.equal(env.findClass('activity-new'),undefined);
    assert.ok(entries().some(node=>String(node.props.class).includes('is-added')));
  }finally{env.unmount();}
});

test('amount gaps stay unavailable and observed pool events keep their evidence label',async()=>{
  const observation={id:'observed',chainId:'56',asset:`56:${token}`,kind:'pair-observed',ticker:'700',pool,t:now-120000};
  const env=mountActivity({ticker:'700',unified:{relations:[],assets:[]},observations:[observation],trades:[trade('missing',{volume:null,quoteQuantity:null})]});
  try{
    await flush();
    assert.ok(env.all().some(node=>node.props.class?.includes('important-status')&&env.textOf(node)==='池关系观测'));
    assert.equal(env.all().some(node=>node.props.class?.includes('important-status')&&env.textOf(node)==='同池记录'),false);
    env.props.ticker='';await flush();env.findButton('成交').props.onClick();await flush();
    assert.ok(env.all().some(node=>env.textOf(node)==='金额待补'));
    env.language.value='en';await flush();assert.ok(env.findButton('Trades'));assert.ok(env.all().some(node=>env.textOf(node)==='Amount unavailable'));
  }finally{env.unmount();}
});

test('following updates locally without pausing the shared market feed',async()=>{
  const env=mountActivity();
  try{
    await flush();env.findButton('成交').props.onClick();env.findButton('已关注').props.onClick();await flush();
    assert.equal(env.tradeLinks().length,0);
    env.stored.set(theme.THEME_WATCH_KEY,JSON.stringify([`56:${token}`]));env.listeners.get('theme-watch-change')();await flush();
    assert.equal(env.tradeLinks().length,1);
    env.findButton('暂停').props.onClick();await flush();env.props.trades=[trade('new'),trade('one')];await flush();assert.equal(env.tradeLinks().length,1);
    assert.match(env.textOf(env.findClass('activity-new')),/新增 1 条/);
    env.findButton('继续').props.onClick();await flush();assert.equal(env.tradeLinks().length,2);assert.equal(env.findClass('activity-new'),undefined);
  }finally{env.unmount();}
});

test('one pool shown by two feeds is a single discovery and dated liquidity stays historical',async()=>{
  const observed={id:'observed',chainId:'56',asset:`56:${token}`,kind:'pair-observed',ticker:'700',pool,t:now-10000};
  const verified={id:'verified',chainId:'56',token,ticker:'700',type:'relation-verified',verifiedAt:now-20000,relation:{pool,liquidityUsd:2000,liquidityAt:now-1800000}};
  const env=mountActivity({ticker:'700',changes:{items:[verified]},observations:[observed]});
  try{
    await flush();
    const entries=env.all().filter(node=>String(node.props.class??'').split(' ').includes('important-item'));
    assert.equal(entries.length,1);
    assert.ok(env.all().some(node=>node.props.class?.includes('important-status')&&env.textOf(node)==='同池记录'));
    assert.ok(env.all().some(node=>env.textOf(node)==='历史池流动性 '+format.usd(2000)));
    assert.equal(env.all().some(node=>String(node.props.class??'').split(' ').includes('important-tab-switch')),false);
  }finally{env.unmount();}
});

test('returning to the homepage retains activity mode and following filter',async()=>{
  let env=mountActivity();
  await flush();env.findButton('成交').props.onClick();env.findButton('已关注').props.onClick();await flush();env.unmount();
  env=mountActivity({}, {restore:true});
  try{await flush();assert.equal(env.findButton('成交').props['class'].includes('active'),true);assert.equal(env.findButton('已关注').props['aria-pressed'],true);}finally{env.unmount();}
});

test('activity links preserve the originating tab rather than assuming homepage',async()=>{
  const route={path:'/events',fullPath:'/events?chain=56&from=watch&back=%2Fwatch%3Fchain%3D56',query:{chain:'56',from:'watch',back:'/watch?chain=56'}};
  const env=mountActivity({scope:'56',ticker:'700',trades:[trade('one',{pool})]}, {route});
  try{await flush();env.findButton('成交').props.onClick();await flush();const link=env.tradeLinks()[0].props.to;assert.equal(link.query.from,'watch');assert.equal(link.query.back,route.fullPath);assert.equal(link.query.pool,pool);assert.equal(link.query.chain,'56');}finally{env.unmount();}
});
