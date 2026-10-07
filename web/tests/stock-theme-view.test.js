import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as format from '../src/utils/format.js';
import * as stock from '../src/utils/stock-theme-model.js';
import * as presentation from '../src/utils/stock-theme-presentation.js';
import * as navigation from '../src/utils/stock-navigation.js';
import * as pageNavigation from '../src/utils/navigation-context.js';
import * as chart from '../src/utils/theme-chart-model.js';
import * as labels from '../src/utils/product-labels.js';
import * as chain from '../src/utils/chain-scope.js';

const now=Date.now(), address=c=>'0x'+c.repeat(40);
const stockToken={ticker:'700',chainId:'56',tokenContractAddress:address('f'),tokenSymbol:'TENCENTx',price:10,priceCurrency:'USD',provider:'OKX',issuerIdentity:{verificationStatus:'official'},fieldTimes:{price:now}};
const asset=(c,extra={})=>({chainId:'56',token:address(c),symbol:c==='a'?'PAIR-MEME':'NAME-MEME',kind:'candidate',price:1,priceCurrency:'USD',volume24h:9000,volumeCurrency:'USD',fieldTimes:{price:now,volume24h:now},fieldScopes:{volume24h:'token-aggregate'},...extra});
const relation={chainId:'56',token:address('a'),ticker:'700',stock:address('f'),level:'A',status:'verified',pool:address('c'),liquidityUsd:2000,liquidityAt:now,poolMarket:{scope:'pool:'+address('c'),provider:'DexScreener',volumeCurrency:'USD',volume24h:300,updatedAt:now-1000}};
const payload=()=>({now,snapshotAt:now-3000,unified:{stockTokens:[stockToken],assets:[asset('a'),asset('b',{match:{level:'B',ticker:'700',evidenceStatus:'name-only'}})],relations:[relation]},themeMetrics:{at:now,memeCount:1,volume24hUsd:300,coverage:{volumeKnown:1,poolCount:1},contributions:[{chainId:'56',token:address('a'),volume24hUsd:300,share:1}]}});
const flush=async()=>{for(let i=0;i<6;i++){await Promise.resolve();await vue.nextTick();}};
const compiled=compileScript(parse(readFileSync(new URL('../src/views/StockThemeView.vue',import.meta.url),'utf8')).descriptor,{id:'stock-theme-view',inlineTemplate:true}).content
 .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,bindings,path)=>`const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`)
 .replace(/^import\s+(\w+)\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,name,path)=>`const ${name}=imports[${JSON.stringify(path)}];`)
 .replace('export default','return');

function mountView(initial=payload(),query={},pageStates=new Map()){
 const saved={window:globalThis.window,document:globalThis.document,Document:globalThis.Document,ShadowRoot:globalThis.ShadowRoot,setInterval:globalThis.setInterval,clearInterval:globalThis.clearInterval};
 globalThis.window={addEventListener(){},removeEventListener(){}};globalThis.document={hidden:false};globalThis.Document=class{};globalThis.ShadowRoot=class{};
 let interval;globalThis.setInterval=callback=>{interval=callback;return 1;};globalThis.clearInterval=()=>{};
 const language=vue.reactive({lang:'zh'}),clock=vue.ref(now),replacements=[],route=vue.reactive({params:{ticker:'700'},query:{chain:'all',back:'/stock?chain=all&q=700&page=3',...query},path:'/stock/700',get fullPath(){return this.path+'?'+new URLSearchParams(this.query);}});
 let result=initial,chartResult={prices:[],volumes:[]},failure=false,themeCalls=0,chartCalls=0;
 const stub=(name,props=[])=>({props,setup:(props,{slots})=>()=>vue.h(name,{...props},slots.default?.())});
 const live={props:['value','currency','format','label'],setup:props=>()=>vue.h('live-number',{...props},props.value==null?'—':props.currency==='USD'?format.usd(props.value):String(props.value))};
 const imports={vue,'vue-router':{useRoute:()=>route,useRouter:()=>({replace(location){replacements.push(location);route.query={...location.query};return Promise.resolve();}})},'../stores/feed':{useFeedStore:()=>({relationships:[],load(){}})},'../composables/useMinuteClock':{useMinuteClock:()=>clock},'../i18n':{tr:(zh,en)=>language.lang==='en'?en:zh,useI18n:()=>({lang:language})},'../utils/format':format,'../utils/chain-scope':chain,'../utils/stock-theme-model':stock,'../utils/stock-theme-presentation':presentation,'../utils/stock-navigation':navigation,'../utils/navigation-context':pageNavigation,'../utils/theme-chart-model':chart,'../utils/product-labels':labels,'../utils/theme-map-model':{readThemeWatches:()=>new Set(),writeThemeWatches(){}},'../api/product':{getStockTheme:async()=>{themeCalls++;if(failure)throw Error('fixture failure');return result;},getStockChart:async()=>{chartCalls++;return chartResult;}},'../components/ThemeHistoryChart.vue':stub('history-chart',['data','loading','error','compact','range']),'../components/StockEquityMarket.vue':stub('equity-market',['stock','ticker','chain','marketStatus']),'../components/StockTokenLiveChart.vue':stub('token-live-chart',['options']),'../components/LiveNumber.vue':live,'../components/QuoteStatus.vue':stub('quote-status',['row']),'../components/RelationBadge.vue':stub('relation-badge',['relation']),'../components/SignalLegend.vue':stub('signal-legend'),'../components/RiskBadge.vue':stub('risk-badge',['asset'])};
 imports['../utils/page-navigation-state']={readPageState:key=>pageStates.has(key)?structuredClone(pageStates.get(key)):null,writePageState:(key,value)=>{pageStates.set(key,structuredClone(value));return true;}};
 imports['../components/AssetAvatar.vue']={render:()=>null};
  const component=new Function('imports',compiled)(imports);
 const node=(type,text='')=>({type,text,props:{},children:[],parent:null,listeners:new Map(),value:'',addEventListener(key,callback){this.listeners.set(key,callback);},removeEventListener(key){this.listeners.delete(key);},getRootNode(){return {};},get options(){return this.children.filter(child=>child.type==='option');}});
 const detach=child=>{if(child.parent){const children=child.parent.children,at=children.indexOf(child);if(at>=0)children.splice(at,1);child.parent=null;}};
 const renderer=vue.createRenderer({createElement:tag=>node(tag),createText:text=>node('text',text),createComment:()=>node('comment'),insert:(child,parent,anchor)=>{detach(child);child.parent=parent;const at=anchor?parent.children.indexOf(anchor):-1;if(at<0)parent.children.push(child);else parent.children.splice(at,0,child);},remove:detach,setText:(target,text)=>target.text=text,setElementText:(target,text)=>{target.text=text;target.children=[];},patchProp:(target,key,_old,value)=>{target.props[key]=value;if(key==='value'){target.value=value;target._value=value;}},parentNode:target=>target.parent,nextSibling:target=>target.parent?.children[target.parent.children.indexOf(target)+1]??null});
 const app=renderer.createApp(component);app.component('RouterLink',{props:['to','replace'],setup:(props,{attrs,slots})=>()=>vue.h('a',{...attrs,to:props.to,replace:props.replace},slots.default?.())});const root=node('root');app.mount(root);
 const all=()=>{const rows=[];const walk=target=>{rows.push(target);target.children.forEach(walk);};walk(root);return rows;},textOf=node=>node.text+node.children.map(textOf).join('');
 const hasClass=(node,value)=>String(node.props.class??'').split(' ').includes(value),findClass=value=>all().find(node=>hasClass(node,value));
 return {all,textOf,findClass,route,language,clock,replacements,pageStates,setResult:value=>result=value,setChartResult:value=>chartResult=value,setFailure:value=>failure=value,refresh:()=>interval(),calls:()=>({theme:themeCalls,chart:chartCalls}),unmount:()=>{app.unmount();for(const[key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}};
}

test('a new response between clock ticks shows its valid pool amount on first render',async()=>{
 const env=mountView();try{
  env.clock.value=now-10000;await flush();
  assert.ok(env.textOf(env.findClass('theme-metrics')).includes('$300'));
  assert.ok(env.textOf(env.findClass('theme-related-panel')).includes('PAIR-MEME'));
  assert.ok(env.textOf(env.findClass('theme-related-panel')).includes('$300'));
 }finally{env.unmount();}
});

test('stock quote stays visible before compact current summary and one primary related table',async()=>{
 const env=mountView();try{await flush();const all=env.all(),related=env.findClass('theme-related-panel'),reference=env.findClass('theme-reference');
 assert.equal(env.findClass('theme-metrics').children.filter(n=>n.type==='div').length,3);
 assert.ok(all.indexOf(reference)<all.indexOf(related));assert.equal(reference.type,'section');assert.equal(env.findClass('theme-chart-toggle').props['aria-expanded'],true);assert.match(env.textOf(reference),/TENCENTx/);assert.match(env.textOf(reference),/OKX/);assert.match(env.textOf(reference),/报价时间/);assert.match(env.textOf(reference),/USD/);assert.equal(env.findClass('theme-volume-distribution'),undefined);
 assert.equal(all.filter(n=>n.type==='table').length,1);assert.equal(env.calls().chart,1);assert.ok(all.indexOf(all.find(n=>n.type==='equity-market'))<all.indexOf(reference));
 assert.ok(!env.findClass('theme-selected-asset'));assert.ok(!env.findClass('theme-contribution-atlas'));
 const row=all.find(n=>n.type==='tbody').children.find(n=>n.type==='tr');assert.equal(row.props.onClick,undefined);
 const links=all.filter(n=>n.type==='a');const market=links.find(n=>env.textOf(n)==='查看行情 →'),evidence=links.find(n=>env.textOf(n)==='配对依据');
 assert.equal(market.props.to.query.tab,'overview');assert.equal(evidence.props.to.query.tab,'relation');assert.equal(evidence.props.to.query.pool,address('c'));
 assert.equal(market.props.to.query.back,env.route.fullPath);assert.match(env.textOf(row),/\$300/);assert.doesNotMatch(env.textOf(row),/\$9/);assert.doesNotMatch(env.textOf(all.find(n=>n.type==='thead')),/全部市场成交/);
 assert.equal(env.findClass('theme-breadcrumb'),undefined);assert.match(env.textOf(env.findClass('theme-heading')),/腾讯控股/);
 }finally{env.unmount();}
});

test('name matches switch locally without changing the current-pool totals or showing pairing action',async()=>{
 const env=mountView();try{await flush();env.all().find(n=>n.type==='button'&&env.textOf(n).startsWith('名称匹配')).props.onClick();await flush();
 const body=env.all().find(n=>n.type==='tbody');assert.match(env.textOf(body),/NAME-MEME/);assert.doesNotMatch(env.textOf(body),/PAIR-MEME/);assert.doesNotMatch(env.textOf(body),/配对依据/);
 assert.equal(env.calls().theme,1);assert.match(env.textOf(env.findClass('theme-metrics')),/\$300/);
 assert.doesNotMatch(env.textOf(env.all().find(n=>n.type==='thead')),/股票配对池成交/);
 }finally{env.unmount();}
});

test('theme relation choices restore from URL and replace history while keeping the original source',async()=>{
 const source='/asset/56/'+address('a')+'?from=meme&back=%2Fmeme%3Fpage%3D4';
 const env=mountView(payload(),{from:'meme',back:source,relationScope:'name',themeRange:'7d'});
 try{
  await flush();assert.match(env.textOf(env.findClass('theme-related-panel')),/NAME-MEME/);
  assert.equal(env.replacements.length,0);assert.equal(env.findClass('theme-breadcrumb'),undefined);
  env.all().find(n=>n.type==='button'&&env.textOf(n).startsWith('当前同池')).props.onClick();await flush();
  assert.equal(env.replacements.length,1);assert.equal(env.route.query.relationScope,'paired');assert.equal(env.route.query.from,'meme');assert.equal(env.route.query.back,source);
  assert.match(env.textOf(env.findClass('theme-related-panel')),/PAIR-MEME/);
  env.route.query.relationScope='name';await flush();assert.match(env.textOf(env.findClass('theme-related-panel')),/NAME-MEME/);
  assert.equal(env.replacements.length,1);env.refresh();await flush();assert.match(env.textOf(env.findClass('theme-related-panel')),/NAME-MEME/);
 }finally{env.unmount();}
});

test('all relationship records keep the originating tab and return to the exact theme',async()=>{
 const result=payload();result.events=[{id:'event:1',chainId:'56',token:address('a'),ticker:'700',pool:address('c'),type:'relation-observed',t:now}];
 const env=mountView(result,{from:'live',back:'/live?chain=all',relationScope:'paired'});
 try{await flush();const link=env.all().find(n=>n.type==='a'&&env.textOf(n)==='全部记录 →');assert.ok(link);assert.equal(link.props.to.path,'/events');assert.equal(link.props.to.query.from,'live');assert.equal(link.props.to.query.back,env.route.fullPath);}finally{env.unmount();}
});

test('the chosen reference token restores from URL and changes via replace without resetting relations',async()=>{
 const result=payload(),second={...stockToken,tokenContractAddress:address('e'),tokenSymbol:'SECONDx',price:20};
 result.unified.stockTokens.push(second);
 const env=mountView(result,{version:'56:'+address('e'),relationScope:'name'});
 try{
  await flush();assert.match(env.textOf(env.findClass('theme-reference')),/SECONDx/);assert.match(env.textOf(env.findClass('theme-related-panel')),/NAME-MEME/);
  const select=env.findClass('theme-version-select').children.find(n=>n.type==='select');
  select.props.onChange({target:{value:'56:'+address('f')}});await flush();
  assert.equal(env.replacements.length,1);assert.equal(env.route.query.version,'56:'+address('f'));assert.equal(env.route.query.relationScope,'name');
  assert.match(env.textOf(env.findClass('theme-reference')),/TENCENTx/);assert.match(env.textOf(env.findClass('theme-related-panel')),/NAME-MEME/);
  const count=env.replacements.length;env.route.query.version='56:'+address('e');await flush();
  assert.match(env.textOf(env.findClass('theme-reference')),/SECONDx/);assert.equal(env.replacements.length,count);
 }finally{env.unmount();}
});

test('cached refresh failure preserves rows and snapshot time, shows retry, then clears on success',async()=>{
 const env=mountView();try{await flush();env.setFailure(true);env.refresh();await flush();
 const alert=env.all().find(n=>n.props.role==='alert');assert.ok(alert);assert.match(env.textOf(alert),/刷新失败，保留上次结果/);assert.match(env.textOf(alert),/快照时间/);assert.match(env.textOf(alert),new RegExp(format.date(now-3000).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
 assert.match(env.textOf(env.all().find(n=>n.type==='tbody')),/PAIR-MEME/);
 env.setFailure(false);alert.children.find(n=>n.type==='button').props.onClick();await flush();assert.equal(env.all().some(n=>n.props.role==='alert'),false);assert.equal(env.calls().theme,3);
 }finally{env.unmount();}
});

test('reference history loads on entry and can be hidden without losing it',async()=>{
 const env=mountView();try{await flush();const reference=env.findClass('theme-reference');assert.equal(env.calls().chart,1);env.findClass('theme-chart-toggle').props.onClick();await flush();assert.equal(env.findClass('theme-reference-body'),undefined);env.findClass('theme-chart-toggle').props.onClick();await flush();assert.equal(env.calls().chart,1);
 assert.equal(env.all().some(n=>n.type==='history-chart'),false);assert.match(env.textOf(reference),/暂无可用历史行情/);
 env.language.lang='en';await flush();assert.match(env.textOf(env.findClass('theme-related-panel')),/Pool-paired Memes/);assert.match(env.textOf(env.findClass('theme-reference')),/Stock-token market/);
 assert.ok(env.all().some(n=>n.type==='a'&&env.textOf(n)==='Pairing evidence'));
 }finally{env.unmount();}
});

const withoutCurrentPools=()=>{const result=payload();result.unified.assets=[asset('a')];result.unified.relations=[{...relation,liquidityUsd:null,liquidityAt:null,evidenceStatus:'liquidity-unknown'}];result.themeMetrics={at:now,contributions:[]};return result;};

test('unqualified pool keeps real quote and evidence visible without zero cards, tabs or empty table',async()=>{
 const env=mountView(withoutCurrentPools());env.setChartResult({prices:[{t:now-60000,value:9},{t:now,value:10}],volumes:[]});
 try{await flush();assert.equal(env.calls().chart,1);assert.equal(env.findClass('theme-metrics'),undefined);assert.equal(env.findClass('theme-related-panel'),undefined);assert.equal(env.findClass('theme-relation-tabs'),undefined);assert.equal(env.all().some(n=>n.type==='table'),false);
  assert.equal(env.findClass('theme-chart-toggle').props['aria-expanded'],true);assert.equal(env.all().find(n=>n.type==='history-chart').props.compact,'');
  const recorded=env.findClass('theme-recorded-relations');assert.equal(recorded.type,'section');assert.match(env.textOf(recorded),/PAIR-MEME \/ TENCENTx/);assert.match(env.textOf(recorded),/估值待补齐/);assert.doesNotMatch(env.textOf(recorded),/观测已过期/);
  assert.equal(env.findClass('recorded-pool-details').type,'details');assert.equal(env.findClass('recorded-pool-details').props.open,undefined);
  const evidence=env.all().find(n=>n.type==='a'&&env.textOf(n)==='配对依据 →');assert.equal(evidence.props.to.query.pool,address('c'));assert.equal(evidence.props.to.query.tab,'relation');
  env.language.lang='en';await flush();assert.match(env.textOf(recorded),/Valuation pending/);assert.match(env.textOf(env.findClass('theme-reference')),/Quote time/);assert.match(env.textOf(env.findClass('theme-pairing-status')),/No paired pools currently qualify/);
 }finally{env.unmount();}
});

test('name-only state defaults to its nonempty table without zero relationship tabs or pool totals',async()=>{
 const result=payload();result.unified.assets=[asset('b',{match:{level:'B',ticker:'700',evidenceStatus:'name-only'}})];result.unified.relations=[];result.themeMetrics={at:now,contributions:[]};
 const env=mountView(result);try{await flush();assert.match(env.textOf(env.findClass('theme-related-panel')),/NAME-MEME/);assert.match(env.textOf(env.findClass('theme-related-panel')),/尚未确认同池关系/);assert.equal(env.findClass('theme-metrics'),undefined);assert.equal(env.findClass('theme-relation-tabs'),undefined);assert.equal(env.findClass('theme-recorded-relations'),undefined);assert.doesNotMatch(env.textOf(env.all().find(n=>n.type==='thead')),/股票配对池成交/);assert.equal(env.all().some(n=>n.type==='a'&&env.textOf(n)==='配对依据'),false);
 }finally{env.unmount();}
});

test('ordinary responses preserve selected relationship group, quote version and manually hidden chart',async()=>{
 const env=mountView();try{await flush();env.all().find(n=>n.type==='button'&&env.textOf(n).startsWith('名称匹配')).props.onClick();env.findClass('theme-chart-toggle').props.onClick();await flush();
  const result=payload();result.unified.relations=[];result.unified.stockTokens.push({...stockToken,chainId:'196',tokenSymbol:'OTHER-VERSION',fieldTimes:{price:now+1000},quoteAt:now+1000});result.themeMetrics={at:now,contributions:[]};env.setResult(result);env.refresh();await flush();
  assert.match(env.textOf(env.findClass('theme-related-panel')),/NAME-MEME/);assert.equal(env.findClass('theme-chart-toggle').props['aria-expanded'],false);assert.match(env.textOf(env.findClass('theme-reference')),/TENCENTx/);assert.doesNotMatch(env.textOf(env.findClass('theme-reference')),/OTHER-VERSION/);assert.equal(env.calls().chart,1);
 }finally{env.unmount();}
});

test('real zero volume is shown as zero, missing volume stays unknown, neither makes a distribution',async()=>{
 const zero=payload();zero.unified.relations=[{...relation,poolMarket:{...relation.poolMarket,volume24h:0}}];zero.themeMetrics.contributions[0].volume24hUsd=0;
 const env=mountView(zero);try{await flush();assert.match(env.textOf(env.findClass('theme-metrics')),/\$0/);assert.equal(env.findClass('theme-volume-distribution'),undefined);
  const unknown=payload();unknown.unified.relations=[{...relation,poolMarket:{...relation.poolMarket,volume24h:null}}];unknown.themeMetrics.contributions=[];env.setResult(unknown);env.refresh();await flush();assert.match(env.textOf(env.findClass('theme-metrics')),/—/);assert.doesNotMatch(env.textOf(env.findClass('theme-metrics')),/\$0/);assert.equal(env.findClass('theme-volume-distribution'),undefined);
 }finally{env.unmount();}
});

test('no price history does not render an empty chart even when volume rows exist',async()=>{
 const env=mountView(withoutCurrentPools());env.setChartResult({prices:[],volumes:[{t:now,volumeUsd:0}]});try{await flush();assert.equal(env.all().some(n=>n.type==='history-chart'),false);assert.match(env.textOf(env.findClass('theme-reference-body')),/暂无可用历史行情/);assert.equal(env.findClass('theme-chart-toggle').props['aria-expanded'],true);
 }finally{env.unmount();}
});

test('recorded pools show three summaries by default and preserve expansion through refresh',async()=>{
 const result=withoutCurrentPools();result.unified.relations=['c','d','e','f'].map(c=>({...result.unified.relations[0],pool:address(c)}));
 const env=mountView(result);try{await flush();const rows=()=>env.all().filter(n=>n.type==='article'&&String(n.props.class).includes('recorded-pool-row'));assert.equal(rows().length,3);const more=env.all().find(n=>n.type==='button'&&env.textOf(n).startsWith('查看其余'));assert.equal(more.props['aria-expanded'],false);more.props.onClick();await flush();assert.equal(rows().length,4);env.refresh();await flush();assert.equal(rows().length,4);assert.ok(env.all().some(n=>n.type==='button'&&env.textOf(n)==='收起更多交易池'));
 }finally{env.unmount();}
});

const expandedLayoutPayload=()=>{
 const result=withoutCurrentPools();
 result.unified.relations=['c','d','e','f'].map(c=>({...result.unified.relations[0],pool:address(c)}));
 result.events=['c','d','e','f'].map((c,index)=>({id:'layout:'+c,chainId:'56',token:address('a'),ticker:'700',pool:address(c),type:'relation-observed',t:now-index*1000}));
 return result;
};
const poolRowCount=env=>env.all().filter(n=>n.type==='article'&&String(n.props.class).includes('recorded-pool-row')).length;
const eventRowCount=env=>env.findClass('theme-events').children.filter(n=>n.type==='article').length;
function expandLayoutAndHideChart(env){
 env.all().find(n=>n.type==='button'&&env.textOf(n).startsWith('查看其余')).props.onClick();
 env.all().find(n=>n.type==='button'&&env.textOf(n)==='更多记录').props.onClick();
 env.findClass('theme-chart-toggle').props.onClick();
}

test('returning after unmount restores all three disclosure choices but fetches the current quote instead of caching market data',async()=>{
 const states=new Map(),query={from:'live',back:'/live?chain=all'},first=mountView(expandedLayoutPayload(),query,states);
 try{
  await flush();assert.equal(poolRowCount(first),3);assert.equal(eventRowCount(first),3);
  expandLayoutAndHideChart(first);await flush();assert.equal(poolRowCount(first),4);assert.equal(eventRowCount(first),4);
  assert.equal(first.findClass('theme-chart-toggle').props['aria-expanded'],false);
 }finally{first.unmount();}
 assert.equal(states.size,1);
 assert.deepEqual([...states.values()][0],{showAllRecordedPools:true,showAllEvents:true,chartExpanded:false});
 const updated=expandedLayoutPayload();updated.unified.stockTokens[0]={...stockToken,price:77};
 const returned=mountView(updated,query,states);
 try{
  await flush();assert.equal(poolRowCount(returned),4);assert.equal(eventRowCount(returned),4);
  assert.equal(returned.findClass('theme-chart-toggle').props['aria-expanded'],false);assert.equal(returned.findClass('theme-reference-body'),undefined);
  assert.match(returned.textOf(returned.findClass('theme-reference')),/\$77/);assert.equal(returned.calls().theme,1);assert.equal(returned.calls().chart,0);
  returned.refresh();await flush();assert.equal(returned.findClass('theme-chart-toggle').props['aria-expanded'],false);
  assert.equal(poolRowCount(returned),4);assert.equal(eventRowCount(returned),4);assert.equal(returned.calls().chart,0);
 }finally{returned.unmount();}
});

test('disclosure state isolates the same theme by parent entry and network and restores when revisiting the original context',async()=>{
 const env=mountView(expandedLayoutPayload(),{from:'live',back:'/live?chain=all'});
 try{
  await flush();expandLayoutAndHideChart(env);await flush();
  const calls=env.calls().theme;
  env.route.query.back='/watch?chain=all&group=memes';env.route.query.from='watch';await flush();
  assert.equal(poolRowCount(env),3);assert.equal(eventRowCount(env),3);assert.equal(env.findClass('theme-chart-toggle').props['aria-expanded'],true);
  assert.equal(env.calls().theme,calls);
  env.route.query.back='/live?chain=all';env.route.query.from='live';await flush();
  assert.equal(poolRowCount(env),4);assert.equal(eventRowCount(env),4);assert.equal(env.findClass('theme-chart-toggle').props['aria-expanded'],false);
  env.route.query.chain='56';await flush();assert.equal(poolRowCount(env),3);assert.equal(eventRowCount(env),3);
  assert.equal(env.findClass('theme-chart-toggle').props['aria-expanded'],true);
  env.route.query.chain='all';await flush();assert.equal(poolRowCount(env),4);assert.equal(eventRowCount(env),4);
  assert.equal(env.findClass('theme-chart-toggle').props['aria-expanded'],false);
 }finally{env.unmount();}
});

test('unmount saves to the active theme key even after the router has moved to an asset with a new back query',async()=>{
 const states=new Map(),query={from:'live',back:'/live?chain=all'},env=mountView(expandedLayoutPayload(),query,states);
 await flush();expandLayoutAndHideChart(env);await flush();
 const originalKey=[...states.keys()][0],themePath=env.route.fullPath;
 env.route.path='/asset/56/'+address('a');env.route.params={chain:'56',address:address('a')};
 env.route.query={chain:'all',from:'live',back:themePath,tab:'overview'};
 env.unmount();
 assert.equal(states.size,1);assert.equal([...states.keys()][0],originalKey);
 assert.deepEqual(states.get(originalKey),{showAllRecordedPools:true,showAllEvents:true,chartExpanded:false});
 const returned=mountView(expandedLayoutPayload(),query,states);
 try{await flush();assert.equal(poolRowCount(returned),4);assert.equal(eventRowCount(returned),4);assert.equal(returned.findClass('theme-chart-toggle').props['aria-expanded'],false);}finally{returned.unmount();}
});

test('cached disclosure choices do not leak into a different ticker or a different source without a back path',async()=>{
 const states=new Map(),query={from:'watch',back:undefined},first=mountView(expandedLayoutPayload(),query,states);
 try{await flush();expandLayoutAndHideChart(first);await flush();}finally{first.unmount();}
 const otherSource=mountView(expandedLayoutPayload(),{from:'me',back:undefined},states);
 try{await flush();assert.equal(poolRowCount(otherSource),3);assert.equal(eventRowCount(otherSource),3);assert.equal(otherSource.findClass('theme-chart-toggle').props['aria-expanded'],true);}finally{otherSource.unmount();}
 const otherPayload=expandedLayoutPayload();otherPayload.unified.stockTokens[0]={...stockToken,ticker:'1024'};
 otherPayload.unified.relations.forEach(row=>row.ticker='1024');otherPayload.events.forEach(row=>row.ticker='1024');
 const otherTicker=mountView(otherPayload,query,states);otherTicker.route.params.ticker='1024';
 try{await flush();assert.equal(poolRowCount(otherTicker),3);assert.equal(eventRowCount(otherTicker),3);assert.equal(otherTicker.findClass('theme-chart-toggle').props['aria-expanded'],true);}finally{otherTicker.unmount();}
 const original=mountView(expandedLayoutPayload(),query,states);
 try{await flush();assert.equal(poolRowCount(original),4);assert.equal(eventRowCount(original),4);assert.equal(original.findClass('theme-chart-toggle').props['aria-expanded'],false);}finally{original.unmount();}
});

test('theme navigation resets first-load choices while refresh failures retain quote time and relation selection',async()=>{
 const env=mountView();try{await flush();env.all().find(n=>n.type==='button'&&env.textOf(n).startsWith('名称匹配')).props.onClick();env.setFailure(true);env.refresh();await flush();assert.match(env.textOf(env.findClass('theme-related-panel')),/NAME-MEME/);assert.match(env.textOf(env.findClass('theme-reference')),new RegExp(format.date(now).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));assert.ok(env.all().find(n=>n.props.role==='alert'));
  env.setFailure(false);const next=withoutCurrentPools();next.unified.stockTokens[0]={...stockToken,ticker:'1024'};next.unified.relations[0].ticker='1024';env.setResult(next);env.route.params.ticker='1024';await flush();assert.equal(env.findClass('theme-related-panel'),undefined);assert.equal(env.findClass('theme-chart-toggle').props['aria-expanded'],true);
 }finally{env.unmount();}
});

test('expired valuations preserve recorded pairing links and label the valuation as pending in both languages',async()=>{
 const result=payload();result.unified.relations=[{...relation,liquidityAt:now-901000}];result.themeMetrics.contributions=[];
 const env=mountView(result);try{await flush();
  assert.equal(env.findClass('theme-metrics'),undefined);const recorded=env.findClass('theme-recorded-relations');assert.ok(recorded);
  assert.match(env.textOf(recorded),/已记录的配对池/);assert.match(env.textOf(recorded),/估值待更新/);assert.doesNotMatch(env.textOf(recorded),/关联失效|暂无关联/);
  const badge=env.all().find(node=>node.type==='relation-badge'&&node.props.relation.pool===address('c'));assert.ok(badge);
  const evidence=env.all().find(node=>node.type==='a'&&env.textOf(node)==='配对依据 →');assert.equal(evidence.props.to.query.pool,address('c'));assert.equal(evidence.props.to.query.back,env.route.fullPath);
  env.language.lang='en';await flush();assert.match(env.textOf(recorded),/Recorded paired pools/);assert.match(env.textOf(recorded),/Valuation update pending/);
 }finally{env.unmount();}
});

test('a stock-token quote with no usable price shows unavailable without substituting the underlying quote',async()=>{
 const result=payload();result.unified.stockTokens[0]={...stockToken,price:0,change24h:'',stockPrice:90,referenceCurrency:'HKD',referenceProvider:'Exchange',referenceAt:now};
 const env=mountView(result);try{await flush();
  const price=env.all().find(node=>node.type==='live-number'&&node.props.label==='股票代币价格');assert.equal(price.props.value,null);
  const change=env.all().find(node=>node.type==='live-number'&&node.props.label==='24h 价格涨跌');assert.equal(change.props.value,null);
  assert.equal(env.all().find(node=>node.type==='equity-market').props.stock.stockPrice,90);
  assert.equal(env.all().find(node=>node.type==='signal-legend').parent.type,'div');
 }finally{env.unmount();}
});
