import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parse,compileScript} from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as format from '../src/utils/format.js';
import * as presentation from '../src/utils/detail-presentation.js';
import * as disclosure from '../src/utils/detail-disclosure-presentation.js';
import {selectDetailMarket} from '../src/utils/market-selection.js';
import {tradeKey} from '../src/stores/feed.js';
import * as stockThemeModel from '../src/utils/stock-theme-model.js';
import * as stockNavigation from '../src/utils/stock-navigation.js';

const token='0x'+'a'.repeat(40),nextToken='0x'+'c'.repeat(40),pool='0x'+'b'.repeat(40),otherPool='0x'+'d'.repeat(40);
const source=readFileSync(new URL('../src/views/DetailView.vue',import.meta.url),'utf8');
const compiled=compileScript(parse(source).descriptor,{id:'detail-live-startup',inlineTemplate:true}).content
  .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,bindings,path)=>`const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`)
  .replace(/^import\s+(\w+)\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,name,path)=>`const ${name}=imports[${JSON.stringify(path)}];`)
  .replace('export default','return');
const deferred=()=>{let resolve,reject;const promise=new Promise((done,fail)=>{resolve=done;reject=fail;});return {promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<5;i++){await Promise.resolve();await vue.nextTick();}};
const livePool=(overrides={})=>({chainId:'56',token,poolId:pool,pool,marketId:pool,liveMarket:true,
  venue:'dex',quoteType:'pool',status:'current',marketStatus:'quiet',stale:false,
  scanAt:Date.now(),lastTradeAt:Date.now()-60000,priceCurrency:'WBNB',volumeCurrency:'WBNB',...overrides});
const summary=(address=token)=>({asset:{chainId:'56',token:address,name:'Actual asset',price:5,priceCurrency:'USD',
  priceScope:'dex',fieldTimes:{price:Date.now()},primaryQuote:{price:5,provider:'actual-quote'}},relations:[]});

function mountDetail(query={}){
  const saved={window:globalThis.window,document:globalThis.document,setTimeout:globalThis.setTimeout,
    clearTimeout:globalThis.clearTimeout,setInterval:globalThis.setInterval,clearInterval:globalThis.clearInterval};
  const timers=new Map(),intervals=new Map(),listeners=new Map(),liveRequests=[],summaryRequests=[],chartMounts=[],replacements=[];
  globalThis.setTimeout=(callback,delay)=>{const id=Symbol();timers.set(id,{callback,delay});return id;};
  globalThis.clearTimeout=id=>timers.delete(id);
  globalThis.setInterval=(callback,delay)=>{const id=Symbol();intervals.set(id,{callback,delay});return id;};
  globalThis.clearInterval=id=>intervals.delete(id);
  globalThis.window={matchMedia:()=>({matches:false}),addEventListener:(name,callback)=>listeners.set(name,callback),
    removeEventListener:name=>listeners.delete(name),history:{state:{}}};
  globalThis.document={hidden:false};
  const route=vue.reactive({path:'/asset/56/'+token,query:{...query},get fullPath(){return this.path+'?'+new URLSearchParams(this.query);}}),identity=vue.reactive({chain:'56',address:token}),language=vue.ref('zh');
  const detail=vue.reactive({cache:new Map(),watch(){},unwatch(){},activate:()=>()=>{},fetchSection:async()=>{}});
  detail.fetch=(chain,address)=>{
    const request={chain,address,...deferred()};summaryRequests.push(request);
    return request.promise.then(data=>{detail.cache.set(`${chain}:${address.toLowerCase()}`,{data,at:Date.now()});return data;});
  };
  const chart=vue.defineComponent({props:['asset','samples','pool','nativePool'],setup(props){
    const instance={props,unmounted:false};chartMounts.push(instance);vue.onBeforeUnmount(()=>instance.unmounted=true);
    return()=>vue.h('div',{'data-chart':props.pool});
  }});
  const stub=name=>vue.defineComponent({setup:()=>()=>vue.h('span',{'data-component':name})});
  const imports={vue,'vue-router':{useRoute:()=>route,useRouter:()=>({replace(location){replacements.push(location);route.query={...location.query};return Promise.resolve();}})},
    '../stores/detail':{useDetailStore:()=>detail},'../stores/dashboard':{useDashboardStore:()=>({hasProjectionStream:true})},
    '../stores/account':{useAccountStore:()=>({watches:[],toggle(){}})},'../stores/comparisons':{useComparisonStore:()=>({latest:new Map()})},
    '../i18n':{tr:(zh,en)=>language.value==='en'?en:zh},'../utils/format':format,'../utils/market-selection':{selectDetailMarket},
    '../utils/detail-presentation':presentation,'../utils/detail-disclosure-presentation':disclosure,'../stores/feed':{tradeKey},
    '../utils/stock-theme-model':stockThemeModel,'../utils/stock-navigation':stockNavigation,
    '../utils/live-market-stream':{getLiveMarkets:(chain,address,options)=>{
      const request={chain,address,signal:options.signal,pool:options.pool,bar:options.bar,...deferred()};liveRequests.push(request);return request.promise;
    }},
  };
  for(const name of ['AssetMetrics','LiveNumber','QuoteStatus','RiskSummary','SpreadPanel','AssetInsight','TradeAmount','TradeActivity','HolderComposition','DetailRelationshipRecord'])
    imports[`../components/${name}.vue`]=stub(name);
  imports['../components/CandleChart.vue']=chart;
  imports['../components/AssetAvatar.vue']={render:()=>null};
  const component=new Function('imports',compiled)(imports);
  const node=(type,text='')=>({type,text,style:{},props:{},children:[],parent:null});
  const detach=child=>{if(child.parent){const children=child.parent.children;const index=children.indexOf(child);if(index>=0)children.splice(index,1);child.parent=null;}};
  const renderer=vue.createRenderer({createElement:tag=>node(tag),createText:text=>node('text',text),createComment:()=>node('comment'),
    insert:(child,parent,anchor)=>{detach(child);child.parent=parent;const at=anchor?parent.children.indexOf(anchor):-1;if(at<0)parent.children.push(child);else parent.children.splice(at,0,child);},
    remove:detach,setText:(target,text)=>target.text=text,setElementText:(target,text)=>{target.text=text;target.children=[];},
    patchProp:(target,key,_previous,value)=>target.props[key]=value,parentNode:target=>target.parent,
    nextSibling:target=>target.parent?.children[target.parent.children.indexOf(target)+1]??null});
  const app=renderer.createApp({render:()=>vue.h(component,identity)});
  app.component('RouterLink',{inheritAttrs:false,props:{to:{},custom:Boolean,replace:Boolean},setup:(props,{attrs,slots})=>()=>props.custom?slots.default?.({href:'/',navigate(){}}):vue.h('a',{...attrs,to:props.to,replace:props.replace},slots.default?.())});
  const root=node('root');app.mount(root);
  const all=()=>{const rows=[];const walk=target=>{rows.push(target);for(const child of target.children)walk(child);};walk(root);return rows;};
  const find=attr=>all().find(target=>Object.hasOwn(target.props,attr));
  const runTimer=delay=>{const entry=[...timers].find(([,timer])=>timer.delay===delay);assert.ok(entry,`timer ${delay} exists`);timers.delete(entry[0]);entry[1].callback();};
  return {route,identity,language,detail,liveRequests,summaryRequests,chartMounts,timers,intervals,all,find,runTimer,replacements,
    unmount:()=>app.unmount(),restore:()=>{for(const [key,value]of Object.entries(saved))globalThis[key]=value;},
    async live(markets,index=liveRequests.length-1){liveRequests[index].resolve({markets});await flush();},
    async summary(data,index=summaryRequests.length-1){summaryRequests[index].resolve(data);await flush();}};
}

test('detail keeps the real asset identity and bilingual business theme link without duplicating shared navigation',async()=>{
  const directory='/stock?chain=all&q=700&page=4',theme='/stock/700?chain=all&back='+encodeURIComponent(directory);
  const env=mountDetail({from:'stock',ticker:'700',chain:'all',back:theme});
  const textOf=node=>node.text+node.children.map(textOf).join('');
  try{
    await env.summary({...summary(),asset:{...summary().asset,match:{ticker:'700',level:'B'}}});
    assert.equal(env.all().some(node=>['asset-breadcrumb','asset-back'].includes(node.props.class)),false);
    assert.match(textOf(env.all().find(node=>node.type==='h2')),/Actual asset/);
    const themeLink=env.all().find(node=>node.props.class==='asset-relation-pill');
    assert.ok(themeLink);assert.match(textOf(themeLink),/腾讯控股/);
    env.language.value='en';await flush();
    assert.match(textOf(env.all().find(node=>node.props.class==='asset-relation-pill')),/Tencent/);
    env.route.query={from:'meme',chain:'all',back:'/meme?chain=all&page=2'};await flush();
    assert.equal(env.all().some(node=>['asset-breadcrumb','asset-back'].includes(node.props.class)),false);
    assert.match(textOf(env.all().find(node=>node.type==='h2')),/Actual asset/);
  }finally{env.unmount();env.restore();}
});

test('primary quote and source time remain fixed across sections while the same chart keeps its market',async()=>{
  const env=mountDetail();
  const textOf=node=>node.text+node.children.map(textOf).join('');
  try{
    const quoteAt=Date.now()-60_000;
    await env.summary({...summary(),snapshotAt:quoteAt+50_000,asset:{...summary().asset,fieldTimes:{price:quoteAt}}});
    await env.live([livePool()]);
    const headline=env.find('data-detail-headline'),quoteTime=env.find('data-detail-quote-time'),chart=env.chartMounts[0];
    assert.match(textOf(quoteTime),/actual-quote/);assert.ok(textOf(quoteTime).includes(format.date(quoteAt)));
    assert.equal(env.all().filter(node=>Object.hasOwn(node.props,'data-detail-headline')).length,1);
    for(const tab of ['trades','holders','relation','overview']){
      env.route.query.tab=tab;await flush();
      assert.equal(env.find('data-detail-headline'),headline);assert.equal(env.find('data-detail-quote-time'),quoteTime);
      assert.equal(env.find('data-detail-market-context').type,'section');
      assert.equal(env.chartMounts.length,1);assert.equal(chart.unmounted,false);assert.equal(chart.props.pool,pool);
      assert.equal(chart.props.asset.price,5);
    }
    env.language.value='en';await flush();assert.match(textOf(headline),/Primary asset quote/);
  }finally{env.unmount();env.restore();}
});

test('section links replace the current history entry and market choices keep source context in the URL',async()=>{
  const source='/watch?chain=all&group=memes',env=mountDetail({from:'watch',chain:'all',back:source});
  try{
    await env.summary(summary());await env.live([livePool(),livePool({poolId:otherPool,pool:otherPool,marketId:otherPool})]);
    const tabLinks=env.all().filter(node=>node.type==='a'&&node.props.to?.query?.tab);
    assert.ok(tabLinks.length>=4);assert.ok(tabLinks.every(node=>node.props.replace===true));
    const requestCount=env.liveRequests.length;
    env.find('data-chart-market').props.onChange({target:{value:'pool:'+otherPool}});await flush();
    assert.equal(env.replacements.length,1);assert.equal(env.route.query.pool,otherPool);assert.equal(env.route.query.market,undefined);
    assert.equal(env.route.query.back,source);assert.equal(env.route.query.from,'watch');assert.equal(env.route.query.chain,'all');
    assert.equal(env.liveRequests.length,requestCount+1);assert.equal(env.chartMounts[0].props.pool,otherPool);
    env.find('data-chart-market').props.onChange({target:{value:'dex'}});await flush();
    assert.equal(env.route.query.pool,undefined);assert.equal(env.route.query.market,'dex');assert.equal(env.route.query.back,source);
    assert.equal(env.chartMounts.length,1);assert.equal(env.chartMounts[0].props.pool,'');
    const replaceCount=env.replacements.length;
    env.route.query.pool=pool;delete env.route.query.market;await flush();
    assert.equal(env.chartMounts[0].props.pool,pool);assert.equal(env.replacements.length,replaceCount);
  }finally{env.unmount();env.restore();}
});

test('a shared exchange choice restores its market without changing the primary asset quote',async()=>{
  const env=mountDetail({market:'binance:ALPHA_USDT',from:'meme',back:'/meme?chain=all&page=3'});
  try{
    await env.summary({...summary(),asset:{...summary().asset,exchangeMarkets:[{venue:'binance',marketId:'ALPHA_USDT',price:6,priceCurrency:'USDT'}]}});
    assert.equal(env.chartMounts[0].props.pool,'');assert.equal(env.chartMounts[0].props.asset.marketId,'ALPHA_USDT');
    assert.equal(env.chartMounts[0].props.asset.priceCurrency,'USDT');assert.equal(env.detail.cache.get('56:'+token).data.asset.price,5);
    assert.equal(env.replacements.length,0);
  }finally{env.unmount();env.restore();}
});

test('real zero and historical liquidity stay visible outside mobile disclosures; all unknown extras remain absent',async()=>{
  const env=mountDetail();const textOf=node=>node.text+node.children.map(textOf).join('');
  try{
    const at=Date.now()-1_000,oldAt=at-2_000_000;
    await env.summary({...summary(),asset:{...summary().asset,volume24h:0,volumeCurrency:'WBNB',holders:0,
      fieldTimes:{price:at,volume24h:at,holders:at},totalLiquidityUsd:123,totalLiquidityAt:oldAt,totalLiquidityStatus:'stale',
      fieldAvailability:{totalLiquidityUsd:{value:null,historicalValue:123,at:oldAt,status:'stale',source:'actual-pools'}},
      productMetrics:{fdvUsd:{value:null,status:'unknown'},exitImpact1k:{valuePercent:null,status:'unsupported'}},
      riskFlags:['wash_suspect'],riskAssessment:{checks:{wash_suspect:{status:'triggered',checkedAt:at}}}}});
    const strip=env.find('data-detail-key-metrics');assert.equal(strip.type,'section');
    assert.match(textOf(strip),/WBNB/);assert.match(textOf(strip),/历史数据/);assert.match(textOf(strip),/actual-pools/);
    assert.ok(env.find('data-detail-risk-flags'));assert.match(textOf(env.find('data-detail-risk-flags')),/成交异常/);
    assert.equal(env.all().some(node=>node.props['data-component']==='AssetMetrics'),false);
  }finally{env.unmount();env.restore();}
});

test('the open trades tab applies new records, corrections and removals immediately without duplicates',async()=>{
  const env=mountDetail({tab:'trades'});
  const trade=id=>({id,venue:'dex',marketId:pool,pool,t:Date.now()-60_000,type:'buy',priceCurrency:'WBNB',price:0,volume:0,volumeCurrency:'WBNB'});
  try{
    await env.summary({...summary(),trades:[trade('one'),trade('two')]});
    const ids=()=>env.all().filter(node=>node.type==='tr'&&String(node.props.id).startsWith('trade-')).map(node=>node.props.id);
    const original=ids();assert.equal(original.length,2);
    const fresh=trade('fresh'),entry=env.detail.cache.get(`56:${token}`);
    entry.data.trades=[fresh,fresh,...entry.data.trades];await flush();
    assert.equal(ids().length,3);assert.equal(env.find('data-detail-new-trades'),undefined);
    assert.ok(ids().includes('trade-'+encodeURIComponent(tradeKey(fresh))));
    entry.data.trades=[fresh,{...trade('one'),price:1}];await flush();
    assert.equal(ids().length,2);assert.equal(ids().includes(original[1]),false);
    const corrected=env.all().find(node=>node.props.id===original[0]);
    assert.equal(corrected.children[2].text,format.money(1,'WBNB'));
    env.route.query.pool=pool;await flush();
    const ctx=env.find('data-detail-market-context');assert.ok(ctx);assert.equal(env.find('data-chart-market').props.value,'pool:'+pool);
  }finally{env.unmount();env.restore();}
});

test('history pagination keeps its cursor and loaded older trades while live records keep arriving',async()=>{
  const env=mountDetail({tab:'trades',pool});
  const trade=(id,at,extra={})=>({id,chainId:'56',token,venue:'dex',marketId:pool,pool,t:at,type:'buy',priceCurrency:'WBNB',price:1,...extra});
  try{
    const at=Date.now()-60000;
    await env.live([livePool()]);
    await env.summary({...summary(),trades:[trade('one',at)],sectionNext:{trades:150}});
    const entry=env.detail.cache.get(`56:${token}`),calls=[];
    env.detail.fetchSection=async(chain,address,section,options)=>{
      calls.push({chain,address,section,options});
      entry.data.trades=[...entry.data.trades,trade('old',at-60000)];entry.data.sectionNext.trades=null;
    };
    const textOf=node=>node.text+node.children.map(textOf).join('');
    const loadMore=env.all().find(node=>node.type==='button'&&textOf(node)==='加载更多');
    await loadMore.props.onClick();await flush();
    assert.equal(calls.length,1);assert.equal(calls[0].section,'trades');assert.equal(calls[0].options.offset,150);
    entry.data.trades=[trade('fresh',at+1000),trade('other-pool',at+2000,{pool:otherPool,marketId:otherPool}),...entry.data.trades];await flush();
    const ids=env.all().filter(node=>node.type==='tr'&&String(node.props.id).startsWith('trade-')).map(node=>node.props.id);
    assert.deepEqual(ids,['fresh','one','old'].map(id=>'trade-'+encodeURIComponent(tradeKey(trade(id,at)))));
    assert.equal(env.find('data-detail-new-trades'),undefined);
  }finally{env.unmount();env.restore();}
});

test('a verified pool mounts before a pending summary and survives summary failure and retry without a second chart',async()=>{
  const env=mountDetail();
  try{
    await env.live([livePool()]);
    assert.equal(env.chartMounts.length,1);assert.equal(env.find('data-chart').props['data-chart'],pool);
    assert.equal(env.chartMounts[0].props.asset.price,undefined);assert.equal(env.chartMounts[0].props.asset.priceCurrency,undefined);
    assert.equal(env.all().some(n=>n.props['data-component']==='LiveNumber'||n.props['data-component']==='RiskSummary'),false);
    assert.ok(env.find('data-summary-loading'));
    env.summaryRequests[0].reject(new Error('request-timeout'));await flush();
    assert.ok(env.find('data-summary-error'));assert.equal(env.chartMounts[0].unmounted,false);
    env.find('data-summary-error').children.find(n=>n.type==='button').props.onClick();await env.summary(summary());
    assert.equal(env.chartMounts.length,1);assert.equal(env.chartMounts[0].unmounted,false);
    assert.equal(env.chartMounts[0].props.asset.price,5);assert.equal(env.chartMounts[0].props.asset.priceCurrency,'USD');
    assert.equal(env.chartMounts[0].props.pool,pool);
    assert.equal(env.all().filter(n=>n.props['data-chart']===pool).length,1);
  }finally{env.unmount();env.restore();}
});

test('an explicit pool wins before summary and an unsupported explicit pool never silently mounts another market',async()=>{
  const env=mountDetail({pool:otherPool});
  try{
    await env.live([livePool(),livePool({poolId:otherPool,pool:otherPool,marketId:otherPool})]);
    assert.equal(env.chartMounts[0].props.pool,otherPool);
    env.route.query.pool='0x'+'e'.repeat(40);await flush();
    assert.equal(env.chartMounts[0].unmounted,true);assert.equal(env.chartMounts.length,1);
    await env.summary(summary());assert.ok(env.find('data-chart-market-unavailable'));
    assert.equal(env.chartMounts.length,1);assert.equal(env.find('data-chart'),undefined);
  }finally{env.unmount();env.restore();}
});

test('changing identity clears the old pool, aborts its waiter and rejects late metadata or summary identity',async()=>{
  const env=mountDetail();
  try{
    const old=env.liveRequests[0];env.identity.address=nextToken;await flush();assert.equal(old.signal.aborted,true);
    await env.live([livePool()],0);assert.equal(env.chartMounts.length,0);
    await env.live([livePool(),livePool({chainId:'196',token:nextToken}),livePool({token:nextToken})],1);
    assert.equal(env.chartMounts.length,1);assert.equal(env.chartMounts[0].props.asset.token,nextToken);
    await env.summary(summary(),0);assert.equal(env.chartMounts[0].props.asset.price,undefined);
    env.identity.chain='196';await flush();assert.equal(env.chartMounts[0].unmounted,true);assert.equal(env.find('data-chart'),undefined);
    const pending=env.liveRequests.at(-1);env.unmount();assert.equal(pending.signal.aborted,true);
    pending.resolve({markets:[livePool({chainId:'196',token:nextToken})]});await flush();assert.equal(env.chartMounts.length,1);
  }finally{env.unmount();env.restore();}
});

test('a first 503 retries after one second, merges overlapping reads and returns to the normal refresh when ready',async()=>{
  const env=mountDetail();
  try{
    env.liveRequests[0].reject(new Error('HTTP 503'));await flush();assert.ok(env.find('data-live-market-error'));
    env.runTimer(1000);const pending=env.liveRequests[1];
    const retry=env.find('data-live-market-error').children.find(n=>n.type==='button').props.onClick;
    retry();retry();assert.equal(env.liveRequests.length,2);
    pending.resolve({markets:[livePool()]});await flush();assert.equal(env.chartMounts.length,1);
    assert.equal(env.find('data-live-market-error'),undefined);assert.equal(env.timers.size,0);
    [...env.intervals.values()].find(timer=>timer.delay===20000).callback();assert.equal(env.liveRequests.length,3);
    env.unmount();assert.equal(env.liveRequests[2].signal.aborted,true);
  }finally{env.unmount();env.restore();}
});

test('startup retry backoff is bounded and pending retry is cancelled on an identity change or unmount',async()=>{
  const env=mountDetail();
  try{
    for(const delay of [1000,2000,4000,8000,15000]){env.liveRequests.at(-1).reject(new Error('request-timeout'));await flush();env.runTimer(delay);}
    env.liveRequests.at(-1).reject(new Error('request-timeout'));await flush();assert.equal(env.timers.size,0);
    env.identity.address=nextToken;await flush();env.liveRequests.at(-1).reject(new Error('HTTP 503'));await flush();
    assert.equal(env.timers.size,1);env.identity.chain='196';await flush();assert.equal(env.timers.size,0);
    env.liveRequests.at(-1).reject(new Error('HTTP 503'));await flush();assert.equal(env.timers.size,1);
    env.unmount();assert.equal(env.timers.size,0);assert.equal(env.intervals.size,0);
  }finally{env.unmount();env.restore();}
});

test('a pending explicit pool requests that pool, waits honestly and mounts only when it is selected',async()=>{
  const env=mountDetail({pool:otherPool});
  try{
    assert.equal(env.liveRequests[0].pool,otherPool);assert.equal(env.liveRequests[0].bar,'5m');
    env.liveRequests[0].resolve({liveMarket:false,selectionStatus:'pending',watchRequested:true,watchPool:otherPool,markets:[livePool()]});await flush();
    assert.ok(env.find('data-live-market-pending'));assert.equal(env.chartMounts.length,0);
    assert.equal(env.all().some(n=>n.props['data-component']==='LiveNumber'||n.props['data-component']==='RiskSummary'),false);
    env.runTimer(1000);assert.equal(env.liveRequests[1].pool,otherPool);
    env.liveRequests[1].resolve({liveMarket:false,selectionStatus:'pending',watchRequested:true,watchPool:otherPool,markets:[livePool()]});await flush();
    // A different selected pool must not reset the requested pool's backoff.
    env.runTimer(2000);assert.equal(env.chartMounts.length,0);
    env.liveRequests[2].reject(new Error('HTTP 503'));await flush();assert.ok(env.find('data-live-market-error'));
    env.runTimer(4000);assert.equal(env.liveRequests[3].pool,otherPool);
    env.liveRequests[3].resolve({liveMarket:true,selectionStatus:'selected',watchRequested:true,watchPool:otherPool,
      markets:[livePool(),livePool({poolId:otherPool,pool:otherPool,marketId:otherPool})]});await flush();
    assert.equal(env.chartMounts.length,1);assert.equal(env.chartMounts[0].props.pool,otherPool);
    assert.equal(env.find('data-live-market-pending'),undefined);assert.equal(env.timers.size,0);
    assert.equal(env.chartMounts[0].props.asset.price,undefined);
  }finally{env.unmount();env.restore();}
});

test('a default request can wake a known pool, while an unregistered response does not claim pending or spin',async()=>{
  const env=mountDetail();
  try{
    assert.equal(env.liveRequests[0].pool,undefined);
    env.liveRequests[0].resolve({selectionStatus:'pending',watchRequested:true,watchPool:pool,markets:[]});await flush();
    assert.ok(env.find('data-live-market-pending'));assert.equal(env.chartMounts.length,0);env.runTimer(1000);
    env.liveRequests[1].resolve({selectionStatus:'selected',watchRequested:true,watchPool:pool,markets:[livePool()]});await flush();
    assert.equal(env.chartMounts[0].props.pool,pool);
    [...env.intervals.values()].find(timer=>timer.delay===20000).callback();assert.equal(env.liveRequests[2].pool,pool);
    env.liveRequests[2].resolve({selectionStatus:'unregistered',watchRequested:false,markets:[]});await flush();
    assert.equal(env.find('data-live-market-pending'),undefined);assert.equal(env.timers.size,0);
  }finally{env.unmount();env.restore();}
});

test('changing a pending pool cancels its retry and rejects its late response before showing the newly requested pool',async()=>{
  const env=mountDetail({pool});
  try{
    env.liveRequests[0].resolve({selectionStatus:'pending',watchRequested:true,watchPool:pool,markets:[]});await flush();
    env.runTimer(1000);const old=env.liveRequests[1];env.route.query.pool=otherPool;await flush();
    assert.equal(old.signal.aborted,true);assert.equal(env.liveRequests[2].pool,otherPool);assert.equal(env.timers.size,0);
    old.resolve({selectionStatus:'pending',watchRequested:true,watchPool:pool,markets:[]});await flush();assert.equal(env.find('data-live-market-pending'),undefined);
    await env.live([livePool({poolId:otherPool,pool:otherPool,marketId:otherPool})],2);
    assert.equal(env.chartMounts[0].props.pool,otherPool);
  }finally{env.unmount();env.restore();}
});
