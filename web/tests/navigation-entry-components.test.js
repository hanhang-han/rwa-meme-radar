import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as vueRouter from 'vue-router';
import * as format from '../src/utils/format.js';
import * as navigation from '../src/utils/navigation-context.js';
import * as pageState from '../src/utils/page-navigation-state.js';
import * as chain from '../src/utils/chain-scope.js';
import * as records from '../src/utils/event-records.js';
import * as stock from '../src/utils/stock-theme-model.js';
import * as home from '../src/utils/home-model.js';
import * as homeLive from '../src/utils/home-live-model.js';
import * as theme from '../src/utils/theme-map-model.js';
import * as relations from '../src/utils/relations.js';
import * as memeFilter from '../src/utils/meme-filter-model.js';
import * as productLabels from '../src/utils/product-labels.js';
import * as quote from '../src/utils/quote-status.js';
import * as watchModel from '../src/utils/watch-model.js';
import * as stockRefresh from '../src/utils/stock-directory-refresh.js';
import * as exchangeQuotes from '../src/utils/visible-exchange-quote.js';

const token='0x'+'a'.repeat(40),wallet='0x'+'b'.repeat(40),pool='0x'+'c'.repeat(40),now=Date.now();
const flush=async()=>{for(let i=0;i<8;i++){await Promise.resolve();await vue.nextTick();}};
const submit={preventDefault(){},stopPropagation(){}};
const blank=vue.defineComponent({render:()=>null});

function compile(file,imports){
  imports['../components/AssetAvatar.vue']={render:()=>null};
  imports['./AssetAvatar.vue']={render:()=>null};
  const compiled=compileScript(parse(readFileSync(new URL(`../src/${file}.vue`,import.meta.url),'utf8')).descriptor,{id:file.replaceAll('/','-'),inlineTemplate:true}).content
    .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,bindings,path)=>`const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`)
    .replace(/^import\s+(\w+)\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,name,path)=>`const ${name}=imports[${JSON.stringify(path)}];`)
    .replace('export default','return');
  return new Function('imports',compiled)(imports);
}

function mount(file,{route={path:'/me',fullPath:'/me?chain=all',query:{chain:'all'}},imports={},props={},router}={}){
  const saved={window:globalThis.window,document:globalThis.document,Document:globalThis.Document,ShadowRoot:globalThis.ShadowRoot,localStorage:globalThis.localStorage};
  const doc={activeElement:null,addEventListener(){},removeEventListener(){}};
  globalThis.document=doc;globalThis.window={addEventListener(){},removeEventListener(){}};globalThis.localStorage={getItem:()=>null,setItem(){}};
  globalThis.Document=class {};globalThis.ShadowRoot=class {};
  const component=compile(file,{vue,'vue-router':{useRoute:()=>route,useRouter:()=>({push(){},replace(){}})},'../i18n':{tr:zh=>zh,useI18n:()=>({lang:vue.reactive({lang:'zh'})})},
    '../utils/format':format,'../utils/navigation-context':navigation,'../utils/page-navigation-state':pageState,'../utils/chain-scope':chain,
    '../utils/event-records':records,'../utils/stock-theme-model':stock,'../composables/useMinuteClock':{useMinuteClock:()=>vue.ref(now)},...imports});
  const node=(type,text='')=>({type,tagName:type.toUpperCase(),text,props:{},children:[],parent:null,style:{},ownerDocument:doc,getRootNode:()=>doc,addEventListener(){},removeEventListener(){},setAttribute(){},removeAttribute(){},focus(){},blur(){}});
  const detach=child=>{if(child.parent){const children=child.parent.children,index=children.indexOf(child);if(index>=0)children.splice(index,1);child.parent=null;}};
  const renderer=vue.createRenderer({createElement:tag=>node(tag),createText:text=>node('text',text),createComment:()=>node('comment'),
    insert:(child,parent,anchor)=>{detach(child);child.parent=parent;const at=anchor?parent.children.indexOf(anchor):-1;if(at<0)parent.children.push(child);else parent.children.splice(at,0,child);},
    remove:detach,setText:(target,text)=>target.text=text,setElementText:(target,text)=>{target.text=text;target.children=[];},patchProp:(target,key,_previous,value)=>{target.props[key]=value;if(key==='type')target.type=value;},
    parentNode:target=>target.parent,nextSibling:target=>target.parent?.children[target.parent.children.indexOf(target)+1]??null});
  const app=renderer.createApp({render:()=>vue.h(component,props)});
  if(router)app.use(router);
  if(!router)app.component('RouterLink',{inheritAttrs:false,props:['to'],setup:(props,{slots,attrs})=>()=>vue.h('a',{...attrs,to:props.to},slots.default?.())});
  const root=node('root');app.mount(root);
  const all=()=>{const rows=[];const walk=target=>{rows.push(target);target.children.forEach(walk);};walk(root);return rows;};
  const textOf=node=>node.text+node.children.map(textOf).join('');
  const findClass=name=>all().find(node=>String(node.props.class??'').split(' ').includes(name));
  const button=label=>all().find(node=>node.tagName==='BUTTON'&&textOf(node)===label);
  return {route,all,textOf,findClass,button,unmount:()=>{app.unmount();globalThis.window=saved.window;globalThis.document=saved.document;globalThis.Document=saved.Document;globalThis.ShadowRoot=saved.ShadowRoot;globalThis.localStorage=saved.localStorage;}};
}

const walletProfile={address:wallet,at:now,metrics:{totalValueUsd:100,themeCoverage:1,effectiveThemeCount:2,effectiveHoldingCount:1,themeHHI:.5,riskValueShare:0},summary:[],themes:[{ticker:'700',share:1}],holdings:[{chainId:'56',token,symbol:'真实持仓',quantity:'1',valueUsd:100,share:1}],unvalued:[]};
function walletImports(account,request){return {'../stores/account':{useAccountStore:()=>account},'../api/developer':{productRequest:request}};}

test('wallet child links keep My tab and return context without publishing the wallet address',async()=>{
  pageState.clearPageState('wallet-profile:owner-one');pageState.clearPageState('wallet-profile:guest');
  const account=vue.reactive({session:{user:{id:'owner-one'}}}),calls=[];
  const request=async(path,options)=>{calls.push({path,options});return path==='wallet/profile'?walletProfile:path==='capabilities'?{wallet:{available:true,chains:[]}}:{items:[]};};
  let env=mount('components/WalletProfile',{imports:walletImports(account,request)});
  await flush();env.findClass('wallet-profile').props.onToggle({target:{open:true}});
  env.all().find(node=>node.props.placeholder==='0x…').props['onUpdate:modelValue'](wallet);
  env.all().find(node=>node.tagName==='FORM').props.onSubmit(submit);await flush();
  const link=env.all().find(node=>node.tagName==='A'&&node.props.to?.path===`/asset/56/${token}`).props.to;
  assert.equal(link.query.from,'me');assert.equal(link.query.back,'/me?chain=all');assert.equal(link.query.tab,'overview');assert.equal(JSON.stringify(link).includes(wallet),false);
  env.unmount();env=mount('components/WalletProfile',{imports:walletImports(account,request)});
  try{await flush();assert.equal(env.findClass('wallet-profile').props.open,true);assert.ok(env.findClass('wallet-total'));assert.equal(calls.filter(call=>call.path==='wallet/profile').length,1);
    account.session=null;await flush();assert.equal(env.findClass('wallet-total'),undefined);assert.equal(pageState.readPageState('wallet-profile:owner-one'),null);
  }finally{env.unmount();}
});

test('wallet pending responses cannot populate a different account',async()=>{
  pageState.clearPageState('wallet-profile:owner-one');pageState.clearPageState('wallet-profile:owner-two');
  const account=vue.reactive({session:{user:{id:'owner-one'}}});let resolveProfile;
  const request=path=>path==='wallet/profile'?new Promise(resolve=>{resolveProfile=resolve;}):Promise.resolve(path==='capabilities'?{wallet:{available:true,chains:[]}}:{items:[]});
  const env=mount('components/WalletProfile',{imports:walletImports(account,request)});
  try{await flush();env.all().find(node=>node.props.placeholder==='0x…').props['onUpdate:modelValue'](wallet);env.all().find(node=>node.tagName==='FORM').props.onSubmit(submit);await flush();
    account.session={user:{id:'owner-two'}};await flush();resolveProfile(walletProfile);await flush();assert.equal(env.findClass('wallet-total'),undefined);assert.equal(pageState.readPageState('wallet-profile:owner-two')?.profile??null,null);
  }finally{env.unmount();}
});

test('same-tick account exit and page unmount never cache the old wallet for guests',async()=>{
  pageState.clearPageState('wallet-profile:owner-one');pageState.clearPageState('wallet-profile:guest');
  pageState.writePageState('wallet-profile:owner-one',{profile:walletProfile,address:wallet,expanded:true});
  const account=vue.reactive({session:{user:{id:'owner-one'}}});
  const request=async path=>path==='capabilities'?{wallet:{available:true,chains:[]}}:{items:[]};
  const env=mount('components/WalletProfile',{imports:walletImports(account,request)});
  await flush();assert.ok(env.findClass('wallet-total'));account.session=null;env.unmount();await flush();
  assert.equal(pageState.readPageState('wallet-profile:guest')?.profile??null,null);
});

test('Events return restores loaded pages, older cursor and record expansion',async()=>{
  const route=vue.reactive({path:'/events',fullPath:'/events?chain=56&from=live&back=%2Flive%3Fchain%3D56',query:{chain:'56',from:'live',back:'/live?chain=56'}});
  const key=`events:live:56:${route.query.back}`;pageState.clearPageState(key);
  const event=(id,t)=>({id,chainId:'56',asset:token,symbol:'发现资产',kind:'pair-observed',pool,t});
  const calls=[],feed={relationships:[],load(){}};
  const getEvents=async(_chain,cursor)=>{calls.push(cursor);return cursor?{items:[event('older',now-86400000)],next:'cursor-two'}:{items:[event('newest',now)],next:'cursor-one'};};
  const Row=vue.defineComponent({props:['ev','expanded'],emits:['update:expanded'],setup:(props,{emit})=>()=>vue.h('button',{class:'fixture-event',expanded:props.expanded,onClick:()=>emit('update:expanded',true)},props.ev.id)});
  const imports={'../components/EventRow.vue':Row,'../api/client':{getEvents},'../stores/feed':{useFeedStore:()=>feed}};
  let env=mount('views/EventsView',{route,imports});await flush();env.button('加载更早记录').props.onClick();await flush();env.button('older').props.onClick();await flush();env.unmount();
  env=mount('views/EventsView',{route,imports});
  try{await flush();assert.ok(env.button('older'));assert.equal(env.button('older').props.expanded,true);env.button('加载更早记录').props.onClick();await flush();assert.equal(calls.at(-1),'cursor-two');
  }finally{env.unmount();}
});

test('Events persist the originating context when real Router useRoute changes before unmount',async()=>{
  const router=vueRouter.createRouter({history:vueRouter.createMemoryHistory(),routes:[{path:'/events',name:'events',component:blank},{path:'/asset/:chain/:token',name:'asset',component:blank}]});
  const source='/events?chain=56&from=live&back=%2Flive%3Fchain%3D56';
  await router.push(source);const key='events:live:56:/live?chain=56';pageState.clearPageState(key);
  const event=(id,t)=>({id,chainId:'56',asset:token,symbol:'发现资产',kind:'pair-observed',pool,t});
  const calls=[],getEvents=async(_chain,cursor)=>{calls.push(cursor);return cursor?{items:[event('previous-day',now-86400000)],next:'oldest-cursor'}:{items:[event('today',now)],next:'head-cursor'};};
  const Row=vue.defineComponent({props:['ev','expanded','navigationAnchor'],emits:['update:expanded'],setup:(props,{emit})=>()=>vue.h('button',{'data-navigation-anchor':props.navigationAnchor,expanded:props.expanded,onClick:()=>emit('update:expanded',true)},props.ev.id)});
  const imports={'vue-router':vueRouter,'../components/EventRow.vue':Row,'../api/client':{getEvents},'../stores/feed':{useFeedStore:()=>({relationships:[],load(){}})}};
  let env=mount('views/EventsView',{router,imports});await flush();env.button('加载更早记录').props.onClick();await flush();env.button('previous-day').props.onClick();await flush();
  const anchor=env.button('previous-day').props['data-navigation-anchor'];assert.match(anchor,/event:.*:56:/);
  await router.push(navigation.assetNavigationLink({chainId:'56',token},{route:router.currentRoute.value,scope:'56'}));await flush();env.unmount();
  assert.equal(pageState.readPageState(key).cursors['56'],'oldest-cursor');assert.equal(pageState.readPageState(key).apiItems.length,2);
  await router.push(source);env=mount('views/EventsView',{router,imports});
  try{await flush();assert.equal(env.button('previous-day').props.expanded,true);assert.equal(env.button('previous-day').props['data-navigation-anchor'],anchor);env.button('加载更早记录').props.onClick();await flush();assert.equal(calls.at(-1),'oldest-cursor');
  }finally{env.unmount();}
});

test('same Events instance saves old chain state before switching scope and restores it later',async()=>{
  const route=vue.reactive({path:'/events',fullPath:'/events?chain=56&from=watch',query:{chain:'56',from:'watch'}});
  const key='events:watch:56:';pageState.clearPageState(key);pageState.clearPageState('events:watch:196:');
  const event=(chainId,id,t)=>({id,chainId,asset:token,symbol:'发现资产',kind:'discovered',t});
  const getEvents=async(chainId,cursor)=>({items:[event(chainId,cursor?'earlier-'+chainId:'latest-'+chainId,now-(cursor?86400000:0))],next:cursor?'cursor-two-'+chainId:'cursor-one-'+chainId});
  const Row=vue.defineComponent({props:['ev'],setup:props=>()=>vue.h('span',{},props.ev.id)});
  const env=mount('views/EventsView',{route,imports:{'../components/EventRow.vue':Row,'../api/client':{getEvents},'../stores/feed':{useFeedStore:()=>({relationships:[],load(){}})}}});
  try{await flush();env.button('加载更早记录').props.onClick();await flush();route.query={chain:'196',from:'watch'};route.fullPath='/events?chain=196&from=watch';await flush();assert.equal(pageState.readPageState(key).cursors['56'],'cursor-two-56');route.query={chain:'56',from:'watch'};route.fullPath='/events?chain=56&from=watch';await flush();assert.ok(env.all().some(node=>env.textOf(node)==='earlier-56'));assert.equal(pageState.readPageState(key).cursors['56'],'cursor-two-56');
  }finally{env.unmount();}
});

test('event names open overview and its separate evidence link opens the recorded pool',async()=>{
  const route={path:'/events',fullPath:'/events?chain=56&from=live&back=%2Flive',query:{chain:'56',from:'live',back:'/live'}};
  const env=mount('components/EventRow',{route,props:{ev:{chainId:'56',asset:token,symbol:'资产名',pool,ticker:'700',t:now},scope:'56'}});
  try{await flush();const name=env.findClass('event-asset').props.to;assert.equal(name.query.tab,'overview');assert.equal(name.query.pool,undefined);assert.equal(name.query.from,'live');assert.equal(name.query.back,route.fullPath);
    const evidence=env.all().find(node=>node.tagName==='A'&&node.props.to?.query?.tab==='relation').props.to;assert.equal(evidence.query.pool,pool);assert.equal(evidence.query.from,'live');
  }finally{env.unmount();}
});

test('global search exact asset and theme links preserve the active source and exact parent',async()=>{
  const route=vue.reactive({path:'/watch',fullPath:'/watch?chain=56&filter=risk&item=stock%3A700',query:{chain:'56',filter:'risk',item:'stock:700',q:'腾讯'}});
  const store={stockTokens:[{chainId:'56',stockCode:'700',tokenContractAddress:token,tokenName:'腾讯',stockIdentity:{code:'700',nameZh:'腾讯控股'}}],assets:[]};
  const env=mount('components/GlobalSearch',{route,imports:{'../stores/dashboard':{useDashboardStore:()=>store},'../api/product':{getProductSearch:async()=>({unified:{stockTokens:store.stockTokens,assets:[]}})}}});
  try{await flush();const input=env.all().find(node=>node.props.placeholder==='搜索股票主题、Meme 或合约地址');input.props.onFocus();await flush();
    const theme=env.all().find(node=>node.tagName==='A'&&node.props.to?.path==='/stock/700').props.to;assert.equal(theme.query.from,'watch');assert.equal(theme.query.back,route.fullPath);assert.equal(theme.query.filter,undefined);assert.equal(theme.query.item,undefined);
    input.props['onUpdate:modelValue'](token);await flush();const exact=env.all().find(node=>node.tagName==='A'&&node.props.to?.path===`/asset/56/${token}`).props.to;assert.equal(exact.query.tab,'overview');assert.equal(exact.query.from,'watch');assert.equal(exact.query.back,route.fullPath);
  }finally{env.unmount();}
});

test('watchlist names open contextual overview once; activity selection is a separate exact-identity action',async()=>{
  const route=vue.reactive({path:'/watch',fullPath:'/watch?chain=56',query:{chain:'56'}});
  const account={ready:true,syncState:'local',session:null,watches:[`56:${token}`,'stock:700'],start(){}};
  const asset={chainId:'56',token,name:'关注资产',symbol:'TOKEN',price:1,priceCurrency:'USD',quoteAt:now,fieldTimes:{price:now}};
  const requests=[],replace=next=>{route.query=next.query;route.fullPath='/watch?'+new URLSearchParams(Object.entries(next.query).filter(([,value])=>value!==undefined));};
  const env=mount('views/WatchView',{route,imports:{'vue-router':{useRoute:()=>route,useRouter:()=>({replace})},'../stores/account':{useAccountStore:()=>account},
    '../api/watch':{getWatchSummary:async()=>({items:[{key:`56:${token}`,kind:'asset',chainId:'56',token,asset},{key:'stock:700',kind:'theme',ticker:'700',nameZh:'腾讯控股',asset:null}],directory:{total:2},snapshotAt:now,realtime:{cursor:42}}),getWatchEvents:async body=>{requests.push(body);return {items:[],hasMore:false};}},
    '../composables/useVisibleQuotes':{useVisibleQuotes:()=>({status:vue.reactive({connected:false,state:'idle'})})},
    '../utils/watch-model':watchModel,'../utils/stock-directory-refresh':stockRefresh,'../utils/visible-exchange-quote':exchangeQuotes,
    '../utils/product-labels':productLabels,'../utils/quote-status':quote,'../components/LiveNumber.vue':blank,'../components/LiveDataStatus.vue':blank,'../components/KolTracker.vue':blank,'../components/SignalLegend.vue':blank,'../components/QuoteStatus.vue':blank,'../components/RiskBadge.vue':blank}});
  try{
    await flush();const name=env.findClass('watch-select');assert.equal(name.tagName,'A');assert.equal(name.props.to.path,`/asset/56/${token}`);assert.equal(name.props.to.query.tab,'overview');assert.equal(name.props.to.query.from,'watch');assert.equal(name.props.to.query.back,route.fullPath);
    assert.equal(env.findClass('watch-open'),undefined,'the redundant Open link is removed, leaving one detail destination');
    assert.deepEqual(requests[0].keys,[`56:${token}`,'stock:700'],'activity defaults to all saved identities');
    const activity=env.button('动态');assert.ok(activity);assert.equal(activity.tagName,'BUTTON');activity.props.onClick();await flush();
    assert.equal(route.query.item,`56:${token}`);assert.deepEqual(requests.at(-1).keys,[`56:${token}`],'selecting activity queries the exact asset rather than its whole theme');
    assert.equal(env.findClass('watch-select').props.to.query.back,route.fullPath,'detail return preserves the selected activity context');
    env.button('清除对象筛选').props.onClick();await flush();assert.equal(route.query.item,undefined);assert.deepEqual(requests.at(-1).keys,[`56:${token}`,'stock:700']);
    const themeLink=env.all().find(node=>node.tagName==='A'&&node.props.to?.path==='/stock/700').props.to;assert.equal(themeLink.query.from,'watch');assert.equal(themeLink.query.back,route.fullPath);
  }finally{env.unmount();}
});

test('homepage return restores selected theme and ranking/map view',async()=>{
  pageState.clearPageState('home:all');
  const route={path:'/live',fullPath:'/live?chain=all',query:{chain:'all'}};
  const stockToken={chainId:'56',stockCode:'700',tokenContractAddress:pool,stockIdentity:{code:'700',nameZh:'腾讯控股'}};
  const store={snapshot:{unified:{snapshotScope:'market',hotStocks:[{ticker:'700',stock:stockToken}],stockTokens:[stockToken],relations:[],assets:[]}},assets:[],relations:[],stockTokens:[stockToken],hasProjectionStream:true};
  const Picker=vue.defineComponent({props:['modelValue'],emits:['update:modelValue'],setup:(props,{emit})=>()=>vue.h('button',{class:'fixture-theme-picker',onClick:()=>emit('update:modelValue','700')},'选腾讯')});
  const imports={'../components/SignalLegend.vue':blank,'../components/RiskBadge.vue':blank,'../components/LiveNumber.vue':blank,'../components/ThemeMarketMap.vue':blank,'../components/HomeActivityPanel.vue':blank,'../components/HomeThemeSelector.vue':Picker,
    '../api/product':{getStockTheme:async()=>({unified:{stockTokens:[stockToken],assets:[],relations:[]}})},'../stores/dashboard':{useDashboardStore:()=>store},'../stores/feed':{useFeedStore:()=>({relationships:[],trades:[],load(){}})},
    '../api/client':{getBriefing:async()=>({items:[]}),getDashboard:async()=>store.snapshot},'../utils/home-model':home,'../utils/home-live-model':homeLive,'../utils/relations':relations,'../utils/theme-map-model':theme};
  let env=mount('views/LiveView',{route,imports});await flush();env.button('选腾讯').props.onClick();await flush();env.button('关系图').props.onClick();await flush();env.unmount();
  env=mount('views/LiveView',{route,imports});
  try{await flush();assert.equal(env.button('关系图').props['aria-pressed'],true);assert.ok(env.findClass('home-selected-theme'));const link=env.all().find(node=>node.tagName==='A'&&node.props.to?.path==='/stock/700').props.to;assert.equal(link.query.from,'live');assert.equal(link.query.back,route.fullPath);
  }finally{env.unmount();}
});

test('homepage saves its own scope when real Router switches to a different-chain directory before unmount',async()=>{
  pageState.clearPageState('home:all');pageState.clearPageState('home:56');
  const router=vueRouter.createRouter({history:vueRouter.createMemoryHistory(),routes:[{path:'/live',component:blank},{path:'/meme',component:blank},{path:'/stock',component:blank},{path:'/stock/:ticker',component:blank}]});await router.push('/live?chain=all');
  const stockToken={chainId:'56',stockCode:'700',tokenContractAddress:pool,stockIdentity:{code:'700',nameZh:'腾讯控股'}};
  const store={snapshot:{unified:{snapshotScope:'market',hotStocks:[{ticker:'700',stock:stockToken}],stockTokens:[stockToken],relations:[],assets:[]}},assets:[],relations:[],stockTokens:[stockToken],hasProjectionStream:true};
  const Picker=vue.defineComponent({props:['modelValue'],emits:['update:modelValue'],setup:(_props,{emit})=>()=>vue.h('button',{onClick:()=>emit('update:modelValue','700')},'选择主题')});
  const imports={'vue-router':vueRouter,'../components/SignalLegend.vue':blank,'../components/RiskBadge.vue':blank,'../components/LiveNumber.vue':blank,'../components/ThemeMarketMap.vue':blank,'../components/HomeActivityPanel.vue':blank,'../components/HomeThemeSelector.vue':Picker,
    '../api/product':{getStockTheme:async()=>({unified:{stockTokens:[stockToken],assets:[],relations:[]}})},'../stores/dashboard':{useDashboardStore:()=>store},'../stores/feed':{useFeedStore:()=>({relationships:[],trades:[],load(){}})},
    '../api/client':{getBriefing:async()=>({items:[]}),getDashboard:async()=>store.snapshot},'../utils/home-model':home,'../utils/home-live-model':homeLive,'../utils/relations':relations,'../utils/theme-map-model':theme};
  const env=mount('views/LiveView',{router,imports});await flush();env.button('选择主题').props.onClick();await flush();env.button('关系图').props.onClick();await flush();
  await router.push('/meme?chain=56');await flush();env.unmount();assert.deepEqual(pageState.readPageState('home:all'),{selectedTicker:'700',themeView:'map'});assert.equal(pageState.readPageState('home:56'),null);
});
