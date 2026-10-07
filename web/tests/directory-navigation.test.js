import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as vueRouter from 'vue-router';
import * as format from '../src/utils/format.js';
import * as themeModel from '../src/utils/stock-theme-model.js';
import * as chainScope from '../src/utils/chain-scope.js';
import * as filters from '../src/utils/meme-filter-model.js';
import * as directoryQuery from '../src/utils/meme-directory-query.js';
import * as directoryPresentation from '../src/utils/meme-directory-presentation.js';
import * as relations from '../src/utils/relations.js';
import * as productLabels from '../src/utils/product-labels.js';
import * as themeLabels from '../src/utils/theme-labels.js';
import * as stockPresentation from '../src/utils/stock-directory-presentation.js';
import * as stockQuery from '../src/utils/stock-directory-query.js';
import * as navigation from '../src/utils/navigation-context.js';
import * as pageState from '../src/utils/page-navigation-state.js';
import * as visibleQuotes from '../src/utils/visible-quotes.js';
import * as exchangeQuotes from '../src/utils/visible-exchange-quote.js';
import * as stockRefresh from '../src/utils/stock-directory-refresh.js';
import * as memeSignals from '../src/utils/meme-scan-signals.js';
import * as stockSignals from '../src/utils/stock-signal-presentation.js';
import * as rowMetrics from '../src/utils/meme-row-metrics.js';

const token = '0x'+'a'.repeat(40), pool = '0x'+'c'.repeat(40);
const flush = async () => { for(let i=0;i<5;i++){await Promise.resolve();await vue.nextTick();} await new Promise(resolve=>setTimeout(resolve,0));await vue.nextTick(); };
function compile(path, imports) {
  imports['../components/AssetAvatar.vue']={render:()=>null};
  imports['./AssetAvatar.vue']={render:()=>null};
  const source = compileScript(parse(readFileSync(new URL('../src/'+path,import.meta.url),'utf8')).descriptor,{id:path,inlineTemplate:true}).content
    .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,bindings,path)=>`const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`)
    .replace(/^import\s+(\w+)\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,name,path)=>`const ${name}=imports[${JSON.stringify(path)}];`)
    .replace('export default','return');
  return new Function('imports',source)(imports);
}
async function mount(path, location, {store, props={}, api={}}={}) {
  const globals = {Document:globalThis.Document,ShadowRoot:globalThis.ShadowRoot,document:globalThis.document};
  globalThis.Document ??= class Document{};globalThis.ShadowRoot ??= class ShadowRoot{};
  globalThis.document ??= {hidden:false,addEventListener(){},removeEventListener(){}};
  const router=vueRouter.createRouter({history:vueRouter.createMemoryHistory(),routes:['/meme','/stock','/stock/:ticker','/asset/:chain/:address'].map(path=>({path,component:{render:()=>null}}))});
  await router.push(location);
  const language=vue.reactive({lang:'zh'}),stub={render:()=>vue.h('span')};
  const account=vue.reactive({watches:[],calls:[],toggle(key){this.calls.push(key);this.watches=this.watches.includes(key)?this.watches.filter(item=>item!==key):[...this.watches,key];}});
  const liveNumber={props:['value','currency','format'],setup:props=>()=>vue.h('live-number',{...props},props.value==null?'—':String(props.value))};
  const relationBadge={props:['relation'],setup:props=>()=>vue.h('relation-badge',{relation:props.relation})};
  let quoteOptions;
  const filterStub={props:['query'],emits:['apply'],setup(_props,{emit}){return()=>vue.h('button',{onClick:()=>emit('apply',{q:undefined,catalog:'all',sort:'volume24h',page:undefined})},'Apply volume');}};
  const imports={vue,'vue-router':vueRouter,'../i18n':{tr:(zh,en)=>language.lang==='en'?en:zh,useI18n:()=>({lang:language})},
    '../utils/format':format,'../utils/stock-theme-model':themeModel,'../utils/stock-theme-presentation':{},'../utils/chain-scope':chainScope,
    '../utils/meme-filter-model':filters,'../utils/meme-directory-query':directoryQuery,'../utils/meme-directory-presentation':directoryPresentation,
    '../utils/meme-scan-signals':memeSignals,'../utils/stock-signal-presentation':stockSignals,
    '../utils/meme-row-metrics':rowMetrics,
    '../utils/relations':relations,'../utils/product-labels':productLabels,'../utils/theme-labels':themeLabels,
    '../utils/stock-directory-presentation':stockPresentation,'../utils/stock-directory-query':stockQuery,
    '../utils/navigation-context':navigation,'../utils/page-navigation-state':pageState,
    '../utils/visible-quotes':visibleQuotes,'../utils/visible-quotes.js':visibleQuotes,
    '../utils/visible-exchange-quote':exchangeQuotes,'../utils/visible-exchange-quote.js':exchangeQuotes,
    '../utils/stock-directory-refresh.js':stockRefresh,'../utils/stock-directory-refresh':stockRefresh,
    '../composables/useVisibleQuotes':{useVisibleQuotes:options=>{quoteOptions=options;return {status:vue.reactive({connected:false,state:'connecting'}),lastQuoteAt:vue.ref(0)};}},
    '../composables/useMinuteClock':{useMinuteClock:()=>vue.ref(Date.now())},'../stores/meme-directory':{useMemeDirectoryStore:()=>store},'../api/product':api,
    '../stores/account':{useAccountStore:()=>account},
    '../components/MemeDirectoryRow.vue':stub,'./PoolListView.vue':stub,'./LiveNumber.vue':liveNumber,'./RiskBadge.vue':stub,'./RelationBadge.vue':relationBadge,
    '../components/SignalLegend.vue':stub,
    '../components/LiveNumber.vue':stub,'../components/LiveDataStatus.vue':stub,'../components/StockDirectoryFilters.vue':filterStub,'../components/ThemeSparkline.vue':stub};
  const component=compile(path,imports);
  const node=(type,text='')=>({type,text,props:{},style:{},children:[],parent:null,listeners:new Map(),value:'',selected:false,
    addEventListener(key,callback){this.listeners.set(key,callback);},removeEventListener(key){this.listeners.delete(key);},getRootNode(){return {};},get options(){return this.children.filter(child=>child.type==='option');}});
  const detach=child=>{if(child.parent){const at=child.parent.children.indexOf(child);if(at>=0)child.parent.children.splice(at,1);child.parent=null;}};
  const renderer=vue.createRenderer({createElement:tag=>node(tag),createText:text=>node('text',text),createComment:()=>node('comment'),
    insert:(child,parent,anchor)=>{detach(child);child.parent=parent;const at=anchor?parent.children.indexOf(anchor):-1;if(at<0)parent.children.push(child);else parent.children.splice(at,0,child);},remove:detach,
    setText:(target,text)=>target.text=text,setElementText:(target,text)=>{target.text=text;target.children=[];},
    patchProp:(target,key,_old,value)=>{target.props[key]=value;if(key==='value'){target.value=value;target._value=value;}},parentNode:target=>target.parent,nextSibling:target=>target.parent?.children[target.parent.children.indexOf(target)+1]??null});
  const root=node('root'),app=renderer.createApp({render:()=>vue.h(component,props)});app.use(router);app.mount(root);await flush();
  const textOf=target=>target.text+target.children.map(textOf).join('');
  const all=()=>{const out=[];const visit=target=>{out.push(target);target.children.forEach(visit);};visit(root);return out;};
  const find=(type,label)=>all().find(target=>target.type===type&&textOf(target)===label);
  async function click(type,label){const target=find(type,label);assert.ok(target,`${type}: ${label}`);await target.props.onClick({button:0,defaultPrevented:false,preventDefault(){}});await flush();}
  return {router,language,account,all,textOf,find,click,quoteRows:()=>quoteOptions.rows.value,quoteEnabled:()=>quoteOptions.enabled.value,unmount(){app.unmount();for(const key of Object.keys(globals)){if(globals[key]===undefined)delete globalThis[key];else globalThis[key]=globals[key];}}};
}
function coinStore(){return vue.reactive({query:null,snapshot:null,ready:false,groups:[],members:{},relations:[],directory:{catalogTotal:400,filteredTotal:400,groupTotal:400},summary:{},calls:[],
  setQuery(query){this.query=query;this.calls.push(query);this.snapshot={snapshotAt:Date.now(),chart:{points:[]}};this.groups=query.view==='coin'?[{symbol:'COIN',memberCount:1,representative:{chainId:'56',token,name:'Coin',symbol:'COIN'}}]:[];this.ready=true;return Promise.resolve(this.snapshot);},start(){},stop(){},loadGroup(){},setChartVisible(){}});}

test('Meme modes preserve separate filters and pages while sharing the current browsing chain',async()=>{
  pageState.writePageState('meme-mode-query:coin',{});pageState.writePageState('meme-mode-query:pool',{});
  const store=coinStore(),env=await mount('views/MemeView.vue',{path:'/meme',query:{chain:'all',q:'COIN',rel:'B',fresh:'1',qualified:'1',category:'meme',sort:'holders',page:'3'}},{store});
  try{
    await env.click('button','按池');
    assert.equal(env.router.currentRoute.value.query.chain,'all');assert.equal(env.router.currentRoute.value.query.qualified,undefined);assert.equal(env.router.currentRoute.value.query.rel,undefined);assert.equal(env.router.currentRoute.value.query.page,undefined);
    await env.router.push({path:'/meme',query:{chain:'56',view:'pool',q:'POOL',qualified:'1',sort:'createdAt',page:'2'}});await flush();
    await env.click('button','按币');
    const coin=env.router.currentRoute.value.query;assert.equal(coin.chain,'56');assert.equal(coin.q,'COIN');assert.equal(coin.rel,'B');assert.equal(coin.category,'meme');assert.equal(coin.sort,'holders');assert.equal(coin.page,'3');
    await env.click('button','按池');
    const pools=env.router.currentRoute.value.query;assert.equal(pools.chain,'56');assert.equal(pools.q,'POOL');assert.equal(pools.sort,'createdAt');assert.equal(pools.page,'2');assert.equal(pools.category,undefined);
  }finally{env.unmount();}
});

test('directory quote and stock links preserve source URL, origin tab and the browsing scope',async()=>{
  const location={path:'/meme',query:{chain:'all',q:'COIN',page:'3'}},asset={chainId:'56',token,name:'Coin',symbol:'COIN'};
  const relation={chainId:'56',token,ticker:'700',level:'B'};
  const env=await mount('components/MemeDirectoryRow.vue',location,{props:{asset,relations:[relation],scope:'all'}});
  try{
    const origin=env.router.currentRoute.value.fullPath;
    assert.ok(env.all().some(node=>node.props['data-navigation-anchor']===`56:${token}`));
    await env.click('a','查看行情 →');
    assert.equal(env.router.currentRoute.value.path,`/asset/56/${token}`);assert.equal(env.router.currentRoute.value.query.chain,'all');assert.equal(env.router.currentRoute.value.query.back,origin);assert.equal(env.router.currentRoute.value.query.from,'meme');
    await env.router.push(location);await flush();await env.click('a','腾讯控股');
    assert.equal(env.router.currentRoute.value.path,'/stock/700');assert.equal(env.router.currentRoute.value.query.from,'meme');assert.equal(env.router.currentRoute.value.query.back,origin);
  }finally{env.unmount();}
});

test('pool name, market action and evidence preserve one selected pool and source directory',async()=>{
  const row={key:`196:${pool}`,chainId:'196',asset:{symbol:'Coin',name:'Coin'},relation:{pool,token,ticker:'700',liquidityAt:Date.now(),protocol:'DEX'},volume24h:10,liquidity:1000,liquidityCurrent:true,volumeCurrent:true,volumeAt:Date.now()};
  const store=vue.reactive({ready:true,query:{view:'pool'},snapshot:{snapshotAt:Date.now()},groups:[{items:[row]}],directory:{totalPools:1,groupTotal:1},summary:{},loading:false,error:null});
  const location={path:'/meme',query:{view:'pool',chain:'all',q:'Coin',sort:'createdAt'}};
  const env=await mount('views/PoolListView.vue',location,{store,props:{scope:'all'}});
  try{
    const origin=env.router.currentRoute.value.fullPath;
    for(const [label,tab] of [['Coin','overview'],['查看行情 →','overview'],['配对依据 →','relation']]){
      await env.router.push(location);await flush();await env.click('a',label);
      const destination=env.router.currentRoute.value;assert.equal(destination.query.pool,pool);assert.equal(destination.query.tab,tab);assert.equal(destination.query.chain,'all');assert.equal(destination.query.from,'meme');assert.equal(destination.query.back,origin);
    }
  }finally{env.unmount();}
});

test('stock requested sort changes query immediately on Apply and display mode changes keep the page without another request',async()=>{
  const calls=[],packet={directory:{total:400,totalThemes:400},unified:{stockThemes:[],sectors:[]}};
  const api={peekStockDirectory:()=>null,getStockDirectory:async(chain,options)=>{calls.push({chain,options});return packet;}};
  const env=await mount('views/StockView.vue',{path:'/stock',query:{chain:'all',page:'3'}},{api});
  try{
    assert.equal(calls.length,1);assert.equal(calls[0].options.sort,'related');assert.equal(calls[0].options.offset,60);
    await env.click('button','Apply volume');
    assert.equal(calls.length,2);assert.equal(calls[1].options.sort,'volume24h');assert.equal(calls[1].options.offset,0);assert.equal(env.router.currentRoute.value.query.page,undefined);
    await env.router.push({path:'/stock',query:{chain:'all',sort:'volume24h',page:'2'}});await flush();const before=calls.length;
    await env.click('button','对比列表');assert.equal(env.router.currentRoute.value.query.page,'2');assert.equal(calls.length,before,'display mode is presentation, not a directory request');
  }finally{env.unmount();}
});

test('returning to a cached Meme directory restores open member segments from fresh requests and remembers columns',async()=>{
  const location={path:'/meme',query:{chain:'all',q:'members',page:'2'}},query=directoryQuery.memeDirectoryQuery(location.query);
  const key=`meme-directory:${query.view}:${directoryQuery.memeFilterKey(query)}:${query.offset}`;
  const distant='56:0x'+'b'.repeat(40),first=`56:${token}`;
  pageState.writePageState(key,{groups:['COIN'],memberFocus:{COIN:[first,distant]},holders:true,windows:true});
  const store=coinStore(),setQuery=store.setQuery,requests=[];
  store.setQuery=function(query){const result=setQuery.call(this,query);this.groups[0].memberCount=200;return result;};
  store.loadGroup=async function(symbol,options={}){
    requests.push({symbol,...options});
    const entry=this.members[symbol]??{rows:[],segments:{},error:null};
    const offset=options.focus===distant?150:0;
    const address=offset?'0x'+'b'.repeat(40):token;
    const row={chainId:'56',token:address,symbol:'COIN',price:987,_directoryMemberIndex:offset};
    if(!entry.rows.some(existing=>existing.token===address))entry.rows.push(row);
    entry.segments[offset]=50;this.members[symbol]=entry;
    return entry;
  };
  const env=await mount('views/MemeView.vue',location,{store});
  try{
    assert.deepEqual(requests,[{symbol:'COIN'},{symbol:'COIN',focus:distant}]);
    assert.ok(env.find('th','持币地址'));assert.ok(env.find('th','其他涨跌'));
    assert.ok(store.members.COIN.rows.some(row=>directoryPresentation.directoryAssetKey(row)===distant));
  }finally{env.unmount();}
  const saved=pageState.readPageState(key);
  assert.deepEqual(saved.groups,['COIN']);assert.ok(saved.memberFocus.COIN.includes(distant));
  assert.equal(JSON.stringify(saved).includes('987'),false,'page context must not cache quote data');
  pageState.clearPageState(key);
});

test('leaving a directory while its member request is pending cancels additional focus restoration',async()=>{
  const location={path:'/meme',query:{chain:'all',q:'slow-members'}},query=directoryQuery.memeDirectoryQuery(location.query);
  const key=`meme-directory:${query.view}:${directoryQuery.memeFilterKey(query)}:${query.offset}`;
  pageState.writePageState(key,{groups:['COIN'],memberFocus:{COIN:['56:0x'+'b'.repeat(40)]}});
  let resolveGroup;
  const requests=[],store=coinStore();store.loadGroup=(symbol,options={})=>{requests.push({symbol,...options});return new Promise(resolve=>{resolveGroup=resolve;});};
  const env=await mount('views/MemeView.vue',location,{store});
  try{
    assert.equal(requests.length,1);
    await env.router.push({path:'/meme',query:{chain:'all',q:'different-directory'}});await flush();
    resolveGroup();await flush();
    assert.deepEqual(requests,[{symbol:'COIN'}],'a previous context cannot enqueue requests after navigation');
  }finally{env.unmount();pageState.clearPageState(key);}
});

test('the scan row preserves independent sources, units and observation times through a live price update',async()=>{
  const now=Date.now(),asset=vue.reactive({chainId:'56',token,name:'Coin',symbol:'COIN',price:.53,priceCurrency:'USD',priceScope:'dex',provider:'OKX',
    quoteAt:now-1000,change24h:2,volume24h:160000000,volumeCurrency:'USD',volumeScope:'token',totalLiquidityUsd:50000,totalLiquidityStatus:'current',totalLiquidityAt:now-1800001,
    totalLiquidityCoverage:{provider:'DexScreener',complete:false},fieldTimes:{price:now-1000,change24h:now-900001,volume24h:now-900001},fieldSources:{price:'OKX',change24h:'OKX',volume24h:'OKX'}});
  exchangeQuotes.applyVisibleExchangeQuote(asset,{chainId:'56',token,priceScope:'exchange',priceCurrency:'USDT',venue:'binance-alpha',provider:'Binance Alpha',marketId:'ALPHA_1228USDT',price:.528,marketAt:now-100,change24h:-13,statisticsAt:now-1100},now);
  const env=await mount('components/MemeDirectoryRow.vue',{path:'/meme',query:{chain:'all',q:'Coin',page:'3'}},{props:{asset,scope:'all'}});
  const cell=field=>env.all().find(node=>node.type==='td'&&node.props['data-field']===field);
  try{
    const values=()=>env.all().filter(node=>node.type==='live-number'&&node.props.format==='price');
    assert.deepEqual(values().map(node=>[node.props.value,node.props.currency]),[[.53,'USD'],[.528,'USDT']]);
    assert.match(cell('price').props.title,/OKX/);assert.match(cell('price').props.title,/USD/);
    assert.match(cell('volume24h').props.title,/历史/);assert.match(cell('volume24h').props.title,/资产市场范围/);
    assert.ok(cell('volume24h').props.title.includes(format.date(now-900001)));
    assert.match(cell('totalLiquidityUsd').props.title,/部分覆盖/);assert.match(cell('totalLiquidityUsd').props.title,/历史/);
    const beforeVolume=cell('volume24h').props.title,beforeLiquidity=cell('totalLiquidityUsd').props.title;
    asset.price=.55;asset.quoteAt=now;asset.fieldTimes.price=now;await flush();
    assert.deepEqual(values().map(node=>[node.props.value,node.props.currency]),[[.55,'USD'],[.528,'USDT']]);
    assert.equal(cell('volume24h').props.title,beforeVolume,'a quote packet cannot renew turnover');
    assert.equal(cell('totalLiquidityUsd').props.title,beforeLiquidity,'a quote packet cannot renew liquidity');
    const change=env.all().find(node=>node.props['data-field']==='change24h');
    assert.equal(String(change.props.class).includes('up'),false,'historical gains keep neutral styling');
    env.language.lang='en';await flush();
    assert.ok(env.find('a','View market →'));assert.match(env.textOf(cell('volume24h')),/Historical/);
  }finally{env.unmount();}
});

test('the list follow action uses the exact contract and leaves the browsing context intact',async()=>{
  const asset={chainId:'56',token:token.toUpperCase(),name:'Coin',symbol:'COIN'},location={path:'/meme',query:{chain:'all',q:'Coin',rel:'B',page:'3'}};
  const env=await mount('components/MemeDirectoryRow.vue',location,{props:{asset,scope:'all'}});
  try{
    const origin=env.router.currentRoute.value.fullPath;
    await env.click('button','☆ 关注');
    assert.deepEqual(env.account.calls,[`56:${token}`]);assert.deepEqual(env.account.watches,[`56:${token}`]);
    assert.equal(env.find('button','★ 已关注').props['aria-pressed'],true);
    assert.equal(env.router.currentRoute.value.fullPath,origin);
    await env.click('button','★ 已关注');assert.deepEqual(env.account.watches,[]);
    assert.equal(env.router.currentRoute.value.fullPath,origin);
  }finally{env.unmount();}
});

test('missing volume units and pool scope stay explicit instead of inheriting the main quote currency',async()=>{
  const now=Date.now(),asset={chainId:'56',token,name:'Coin',price:2,priceCurrency:'USD',provider:'OKX',quoteAt:now-1000,volume24h:13,volumeScope:'pool',fieldTimes:{volume24h:now-1000},fieldSources:{volume24h:'Pool provider'}};
  const env=await mount('components/MemeDirectoryRow.vue',{path:'/meme',query:{chain:'all'}},{props:{asset}});
  try{
    const volume=env.all().find(node=>node.type==='td'&&node.props['data-field']==='volume24h');
    assert.match(env.textOf(volume),/币种待确认/);assert.match(env.textOf(volume),/成交范围待核实/);
    const number=volume.children.find(node=>node.type==='strong').children.find(node=>node.type==='live-number');
    assert.equal(number.props.currency,'');assert.equal(number.props.value,13);assert.match(volume.props.title,/pool/);
    assert.equal(volume.props.title.includes('资产市场范围'),false);
  }finally{env.unmount();}
});

test('visible quotes follow automatic list changes while expanded contracts and mode navigation stay intact',async()=>{
  const location={path:'/meme',query:{chain:'all',q:'scan-subscription'}},query=directoryQuery.memeDirectoryQuery(location.query);
  const key=`meme-directory:${query.view}:${directoryQuery.memeFilterKey(query)}:${query.offset}`;
  pageState.writePageState(key,{groups:['COIN']});
  const store=coinStore(),setQuery=store.setQuery,other='0x'+'b'.repeat(40);
  store.setQuery=function(query){const result=setQuery.call(this,query);if(query.view==='coin'){this.groups[0].memberCount=3;this.members.COIN={rows:[{chainId:'56',token:token.toUpperCase(),symbol:'COIN'},{chainId:'56',token:other,symbol:'COIN'},{chainId:'56',token:other,symbol:'COIN'}]};}return result;};
  store.setReading=()=>assert.fail('browsing interactions must not suspend directory updates');
  const env=await mount('views/MemeView.vue',location,{store});
  try{
    assert.deepEqual(env.quoteRows().map(row=>directoryPresentation.directoryAssetKey(row)),[`56:${token}`,`56:${other}`]);
    assert.equal(env.quoteEnabled(),true);
    const panel=env.all().find(node=>String(node.props.class).includes('meme-results'));
    for(const event of ['onPointerenter','onPointerleave','onFocusin','onFocusout','onScrollCapture','onTouchstartPassive'])assert.equal(panel.props[event],undefined);
    const newest='0x'+'c'.repeat(40),first='0x'+'d'.repeat(40);
    store.members.COIN.rows.push({chainId:'56',token:newest,symbol:'COIN'});
    store.groups=[{symbol:'NEW',memberCount:1,representative:{chainId:'56',token:first,symbol:'NEW'}},...store.groups];
    store.snapshot={snapshotAt:Date.now(),chart:{points:[]}};await flush();
    assert.deepEqual(env.quoteRows().map(row=>directoryPresentation.directoryAssetKey(row)),[`56:${first}`,`56:${token}`,`56:${other}`,`56:${newest}`]);
    assert.equal(env.quoteEnabled(),true);
    await env.click('button','按池');assert.equal(env.quoteEnabled(),false);
  }finally{env.unmount();pageState.clearPageState(key);}
});

test('legacy scan rows also use published field availability and keep unknown volume units independent',async()=>{
  const now=Date.now(),a={chainId:'56',token,name:'Coin',price:99,priceCurrency:'USD',volume24h:12,volumeScope:'token',quoteAt:now-1000,
    fieldTimes:{volume24h:now-1000},fieldAvailability:{price:{status:'current',value:2,at:now-1000,source:'Published quote'}}};
  const env=await mount('components/MemeRow.vue',{path:'/meme',query:{chain:'all',q:'Coin'}},{props:{a,store:{diffCells:()=>false}}});
  try{
    const price=env.all().find(node=>node.type==='td'&&node.props['data-field']==='price'),volume=env.all().find(node=>node.type==='td'&&node.props['data-field']==='volume24h');
    assert.equal(price.children.find(node=>node.type==='live-number').props.value,2);
    assert.equal(volume.children.find(node=>node.type==='live-number').props.currency,'');
    assert.match(volume.props.title,/币种待确认/);
  }finally{env.unmount();}
});
