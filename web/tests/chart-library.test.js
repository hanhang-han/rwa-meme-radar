import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as chartTime from '../src/utils/chart-time.js';
import * as realtime from '../src/utils/realtime.js';
import * as candles from '../src/utils/candles.js';
import * as chartPreferences from '../src/utils/chart-preferences.js';
import { createChartLibraryLoader } from '../src/utils/chart-library.js';
import { liveMarketSnapshotMatches, liveMarketHealthMatches, mergeLiveMarketHealth, liveMarketObservationState } from '../src/utils/live-market-stream.js';

function environment(baseUrl = '/dashboard/') {
  const scripts = [], timers = new Map(), global = {};
  const document = {
    hidden: false,
    createElement: () => ({ remove() { this.removed = true; } }),
    head: { appendChild: script => scripts.push(script) },
  };
  const load = createChartLibraryLoader({document, global, baseUrl,
    setTimer: callback => { const id = Symbol(); timers.set(id, callback); return id; },
    clearTimer: id => timers.delete(id),
  });
  return { load, scripts, timers, global, document };
}

test('simultaneous chart loads share one same-origin script and reuse its global', async () => {
  const env = environment();
  const first = env.load(), second = env.load();
  assert.equal(first, second);
  assert.equal(env.scripts.length, 1);
  assert.equal(env.scripts[0].src, '/dashboard/lightweight-charts.js');
  env.global.LightweightCharts = { createChart() {} };
  env.scripts[0].onload();
  assert.equal(await first, env.global.LightweightCharts);
  assert.equal(await env.load(), env.global.LightweightCharts);
  assert.equal(env.scripts.length, 1);
  assert.equal(env.timers.size, 0);
});

test('an already available vendor makes no request, including the dev base path', async () => {
  const env = environment('/');
  env.global.LightweightCharts = {};
  assert.equal(await env.load(), env.global.LightweightCharts);
  assert.equal(env.scripts.length, 0);
  delete env.global.LightweightCharts;
  const next = env.load();
  assert.equal(env.scripts[0].src, '/lightweight-charts.js');
  env.global.LightweightCharts = {};
  env.scripts[0].onload();
  await next;
});

test('network failure clears the shared attempt and permits an explicit retry', async () => {
  const env = environment();
  const failed = env.load();
  env.scripts[0].onerror();
  await assert.rejects(failed, /chart-library-unavailable/);
  assert.equal(env.scripts[0].removed, true);
  assert.equal(env.timers.size, 0);
  const retry = env.load();
  assert.notEqual(retry, failed);
  assert.equal(env.scripts.length, 2);
  env.global.LightweightCharts = {};
  env.scripts[1].onload();
  await retry;
});

test('a loaded script without the expected global fails and remains retryable', async () => {
  const env = environment();
  const failed = env.load();
  env.scripts[0].onload();
  await assert.rejects(failed, /chart-library-unavailable/);
  const retry = env.load();
  env.global.LightweightCharts = {};
  env.scripts[1].onload();
  await retry;
});

test('a stalled script times out once, stops waiting and can be retried', async () => {
  const env = environment();
  const failed = env.load();
  [...env.timers.values()][0]();
  await assert.rejects(failed, /chart-library-unavailable/);
  assert.equal(env.timers.size, 0);
  assert.equal(env.scripts[0].onload, null);
  const retry = env.load();
  env.global.LightweightCharts = {};
  env.scripts[1].onload();
  await retry;
});

const source = readFileSync(new URL('../src/components/CandleChart.vue', import.meta.url), 'utf8');
const descriptor = parse(source).descriptor;
const compiled = compileScript(descriptor, {id:'chart-library-lifecycle'}).content
  .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (_, bindings, name) =>
    `const {${bindings.replace(/\s+as\s+/g, ':')}} = imports[${JSON.stringify(name)}];`)
  .replace(/export function /g, 'function ')
  .replace('export default', 'return');

function mountedChart(env, getCandles = async () => ({rows:[]}), options = {}) {
  const rendered = [], renderedCandles = [], liveConnections = [], subscriptions = new Set(), resizes = [];
  const streams = vue.reactive({resetVersion:0, version:0, resets:new Map(),
    subscribe:id => subscriptions.add(id), unsubscribe:id => subscriptions.delete(id), matching:() => null,
    receive:() => { streams.version++; }});
  const imports = {
    vue,
    '../api/client': {getCandles},
    '../i18n': {tr:zh => zh, useI18n:() => ({lang:vue.reactive({lang:'zh'})})},
    '../utils/chart-time': chartTime,
    '../utils/realtime': realtime,
    '../utils/format': {priceNumber:value => String(value)},
    '../stores/candles': {useCandleStore:() => streams},
    '../stores/dashboard': {useDashboardStore:() => ({hasProjectionStream:false})},
    '../utils/candles': candles,
    '../utils/chart-library': {loadChartLibrary:env.load},
    '../utils/chart-preferences': chartPreferences,
    '../utils/live-market-stream': {liveMarketSnapshotMatches,liveMarketHealthMatches,mergeLiveMarketHealth,liveMarketObservationState,
      getLiveMarketCandles: options.getLiveMarketCandles ?? (async () => ({liveMarket:false})),
      getLiveMarkets:options.getLiveMarkets ?? (async()=>({markets:[]})),
      openLiveMarketStream: setup => {const connection={setup,closed:false,close(){this.closed=true;}};liveConnections.push(connection);return connection;},
    },
  };
  const component = new Function('imports', compiled)(imports);
  const setup = component.setup;
  let state;
  component.setup = (props, context) => {
    state = setup(props, context);
    return () => vue.h('div', {ref:state.el});
  };
  const renderer = vue.createRenderer({
    createElement:() => ({children:[],clientWidth:400,clientHeight:240}),
    insert:(child,parent) => parent.children.push(child), remove:() => {},
    createText:text => ({text}), createComment:() => ({}),
    setText:() => {}, setElementText:() => {}, patchProp:() => {},
    parentNode:() => null, nextSibling:() => null,
  });
  const app = renderer.createApp(component, {
    asset:{chainId:'56',token:'token',priceScope:'dex',priceCurrency:'USD'},
    samples:[{t:1000,price:1},{t:2000,price:2}],
    ...options.props,
  });
  const vendor = {
    ColorType:{Solid:0}, CrosshairMode:{Normal:0}, LineStyle:{Dashed:0},
    createChart:() => ({remove:() => {},resize:(width,height)=>resizes.push([width,height]),
      addCandlestickSeries:() => ({setData:points => renderedCandles.push(points),update:point=>renderedCandles.push([point]),
        createPriceLine:() => ({applyOptions:() => {}})}),
      addHistogramSeries:() => ({setData:()=>{},priceScale:()=>({applyOptions:()=>{}})}),
      addAreaSeries:() => ({setData:points => rendered.push(points),
        createPriceLine:() => ({applyOptions:() => {}}), applyOptions:() => {}}),
    }),
  };
  env.global.addEventListener = env.global.removeEventListener = () => {};
  env.global.getComputedStyle = () => ({getPropertyValue:() => ''});
  const previousWindow = globalThis.window, previousDocument = globalThis.document;
  const previousAnimationFrame=globalThis.requestAnimationFrame,previousCancelFrame=globalThis.cancelAnimationFrame;
  const frames=new Map();let frameId=0;
  globalThis.requestAnimationFrame=callback=>{const id=++frameId;frames.set(id,callback);return id;};
  globalThis.cancelAnimationFrame=id=>frames.delete(id);
  globalThis.window = env.global;
  globalThis.document = env.document;
  app.mount({children:[]});
  return {state, rendered, renderedCandles, liveConnections, vendor, subscriptions, streams, resizes, unmount:() => app.unmount(),
    restore:() => {globalThis.window = previousWindow;globalThis.document = previousDocument;globalThis.requestAnimationFrame=previousAnimationFrame;globalThis.cancelAnimationFrame=previousCancelFrame;}};
}

const flush = async () => { await Promise.resolve(); await vue.nextTick(); await Promise.resolve(); };

test('the decimal axis suppresses a floating zero tick without changing actual observed prices', () => {
  const env = environment(), mounted = mountedChart(env);
  try {
    const points = [{time:1,value:.0001},{time:2,value:.000008606}];
    const before = JSON.stringify(points), format = mounted.state.linePriceFormat(points);
    assert.equal(format.formatter(1.016e-21),'0');
    assert.equal(format.formatter(-1.016e-21),'0');
    assert.equal(format.formatter(.000008606),String(.000008606));
    assert.equal(JSON.stringify(points),before);
    mounted.state.linePriceFormat([{time:1,value:1e-30},{time:2,value:2e-30}]);
    assert.equal(format.formatter(1e-30),'1e-30','a later genuinely tiny price is never hidden by the earlier scale');
    assert.equal(mounted.state.priceText(1.016e-21),'1.016e-21','OHLC formatting does not use the scale zero tolerance');
  } finally { mounted.unmount(); mounted.restore(); }
});

test('candle-axis zero tolerance follows the smallest actual OHLC value, including subnormal prices', () => {
  const env = environment(), mounted = mountedChart(env);
  try {
    mounted.state.observeAxisPrices([0,.0001,.0000001606,NaN]);
    assert.equal(mounted.state.axisPriceNumber(1.016e-21),'0');
    assert.equal(mounted.state.axisPriceNumber(.0000001606),String(.0000001606));
    mounted.state.observeAxisPrices([Number.MIN_VALUE,0]);
    assert.equal(mounted.state.axisPriceNumber(Number.MIN_VALUE),String(Number.MIN_VALUE));
    mounted.state.observeAxisPrices([]);
    assert.equal(mounted.state.axisPriceNumber(1e-30),'1e-30');
  } finally { mounted.unmount(); mounted.restore(); }
});

test('a chart resizes when its container shrinks after details arrive and disconnects its observer on unmount',async()=>{
  const env=environment();let callback,observed,disconnected=false;
  env.global.ResizeObserver=class{
    constructor(onChange){callback=onChange;}
    observe(element){observed=element;}
    disconnect(){disconnected=true;}
  };
  const mounted=mountedChart(env);
  try{
    env.global.LightweightCharts=mounted.vendor;env.scripts[0].onload();await flush();
    assert.equal(observed,mounted.state.el.value);
    observed.clientWidth=250;callback();
    assert.deepEqual(mounted.resizes,[[250,240]]);
    mounted.unmount();assert.equal(disconnected,true);
    callback();assert.deepEqual(mounted.resizes,[[250,240]]);
  }finally{mounted.restore();}
});

test('a mounted chart automatically retries a transient library failure and cancels pending retries on unmount',async()=>{
  const savedSet=globalThis.setTimeout,savedClear=globalThis.clearTimeout;
  const timers=new Map();
  globalThis.setTimeout=(callback,delay)=>{const id=Symbol();timers.set(id,{callback,delay});return id;};
  globalThis.clearTimeout=id=>timers.delete(id);
  const env=environment(),mounted=mountedChart(env);
  try{
    env.scripts[0].onerror();await flush();
    const [id,timer]=[...timers].find(([,t])=>t.delay===1000);
    timers.delete(id);timer.callback();await flush();
    assert.equal(env.scripts.length,2);
    env.global.LightweightCharts=mounted.vendor;env.scripts[1].onload();await flush();
    assert.equal(mounted.state.libraryState.value,'ready');
    assert.deepEqual(mounted.rendered,[[{time:1,value:1},{time:2,value:2}]]);
    mounted.unmount();assert.equal(timers.size,0);
  }finally{mounted.restore();globalThis.setTimeout=savedSet;globalThis.clearTimeout=savedClear;}
});

test('mount waits for the vendor and then paints observations already present in props', async () => {
  const env = environment(), mounted = mountedChart(env);
  try {
    assert.equal(mounted.rendered.length, 0);
    env.global.LightweightCharts = mounted.vendor;
    env.scripts[0].onload();
    await flush();
    assert.deepEqual(mounted.rendered, [[{time:1,value:1},{time:2,value:2}]]);
    assert.equal(mounted.state.libraryState.value, 'ready');
  } finally {mounted.unmount();mounted.restore();}
});

test('unmount while the script loads prevents history requests and late painting', async () => {
  const env = environment();
  let reads = 0;
  const mounted = mountedChart(env, async () => {reads++;return {rows:[]};});
  try {
    mounted.unmount();
    env.global.LightweightCharts = mounted.vendor;
    env.scripts[0].onload();
    await flush();
    assert.equal(reads, 0);
    assert.equal(mounted.rendered.length, 0);
    assert.equal(mounted.subscriptions.size, 0);
  } finally {mounted.restore();}
});

test('candle packets received before the vendor are painted without another history request', async () => {
  const env = environment();
  let reads = 0;
  const mounted = mountedChart(env, async () => {reads++;return {rows:[]};});
  try {
    const packet = {chainId:'56',token:'token',venue:'dex',bar:'5m',priceCurrency:'USD',
      at:Date.now(), rows:[{t:1000,o:1,h:2,l:1,c:2},{t:2000,o:2,h:3,l:2,c:3}]};
    mounted.streams.matching = () => packet;
    mounted.streams.version++;
    await flush();
    env.global.LightweightCharts = mounted.vendor;
    env.scripts[0].onload();
    await flush();
    assert.equal(reads, 0);
    assert.deepEqual(mounted.rendered, [[{time:1,value:2},{time:2,value:3}]]);
  } finally {mounted.unmount();mounted.restore();}
});

test('a mounted chart shows a library error and its retry paints existing data', async () => {
  const env = environment(), mounted = mountedChart(env);
  try {
    env.scripts[0].onerror();
    await flush();
    assert.equal(mounted.state.libraryState.value, 'unavailable');
    assert.match(mounted.state.chartMessage.value, /请重试/);
    const retry = mounted.state.retryChartLibrary();
    env.global.LightweightCharts = mounted.vendor;
    env.scripts[1].onload();
    await retry;
    assert.equal(mounted.rendered.length, 1);
  } finally {mounted.unmount();mounted.restore();}
});

test('unmount while history is pending prevents painting after its response', async () => {
  const env = environment();
  let finishHistory;
  const mounted = mountedChart(env, () => new Promise(resolve => {finishHistory = resolve;}));
  try {
    env.global.LightweightCharts = mounted.vendor;
    env.scripts[0].onload();
    await flush();
    assert.equal(typeof finishHistory, 'function');
    mounted.unmount();
    finishHistory({rows:[]});
    await flush();
    assert.equal(mounted.rendered.length, 0);
  } finally {mounted.restore();}
});

test('registered live pool shows one real candle immediately and rejects the older journal', async () => {
  const env=environment(),token='0x'+'1'.repeat(40),pool='0x'+'2'.repeat(40),now=Date.now();
  const asset=vue.reactive({chainId:'196',token,priceScope:'dex',priceCurrency:'USD'});
  const frame={chainId:'196',token,venue:'dex',poolId:pool,pool,marketId:pool,bar:'5m',priceCurrency:'WBNB',volumeCurrency:'WBNB'};
  const row={t:now-now%300000,o:1,h:2,l:1,c:2,v:3,vu:5,observedAt:now};
  let legacyReads=0;
  const mounted=mountedChart(env,async()=>{legacyReads++;return {rows:[]};},{
    props:{asset,pool,samples:[]},
    getLiveMarketCandles:async()=>({...frame,liveMarket:true,epoch:'new-db',cursor:3,rows:[row],lastSourceEventAt:now}),
  });
  try{
    env.global.LightweightCharts=mounted.vendor;env.scripts[0].onload();
    for(let i=0;i<5;i++)await flush();
    assert.equal(mounted.state.mode.value,'candle');assert.equal(mounted.state.displayState.value,'ready');
    assert.equal(legacyReads,0);assert.equal(mounted.renderedCandles[0].length,1);
    mounted.streams.matching=()=>({...frame,at:Date.now(),rows:[{...row,c:999}]});mounted.streams.version++;
    await flush();assert.equal(mounted.state.candleInfo.value.rows[0].c,2);
    const live=mounted.liveConnections[0];
    // A late research summary may identify a different canonical quote.
    // The already selected pool owns this chart, so its stream stays open.
    asset.marketId='catalogue-usd-market';asset.price=777;
    for(let i=0;i<3;i++)await flush();
    assert.equal(mounted.liveConnections.length,1);assert.equal(live.closed,false);
    assert.equal(mounted.state.candleInfo.value.rows[0].c,2);
    live.setup.onCandle({...frame,liveMarket:true,row:{...row,c:3,h:3,observedAt:now+10},sourceEventAt:now+10,receivedAt:now+10});
    for(let i=0;i<3;i++)await flush();
    assert.equal(mounted.state.candleInfo.value.rows[0].c,3);
    mounted.unmount();assert.equal(live.closed,true);
  }finally{mounted.restore();}
});

test('registered live snapshot arriving after unmount cannot open a stream',async()=>{
  const env=environment(),token='0x'+'1'.repeat(40),pool='0x'+'2'.repeat(40);
  let finish;
  const mounted=mountedChart(env,async()=>({rows:[]}),{
    props:{asset:{chainId:'196',token,priceScope:'dex'},pool,samples:[]},
    getLiveMarketCandles:()=>new Promise(resolve=>{finish=resolve;}),
  });
  try{
    env.global.LightweightCharts=mounted.vendor;env.scripts[0].onload();await flush();
    mounted.unmount();
    finish({chainId:'196',token,venue:'dex',poolId:pool,pool,marketId:pool,bar:'5m',liveMarket:true,epoch:'new-db',cursor:1,
      priceCurrency:'WBNB',volumeCurrency:'WBNB',rows:[]});
    for(let i=0;i<4;i++)await flush();
    assert.equal(mounted.liveConnections.length,0);
  }finally{mounted.restore();}
});

test('quiet live market rechecks scan on heartbeat without updating candles or source clocks',async()=>{
  const env=environment(),token='0x'+'1'.repeat(40),pool='0x'+'2'.repeat(40),started=Date.now();
  const frame={chainId:'4663',token,venue:'dex',poolId:pool,pool,marketId:pool,bar:'5m',priceCurrency:'WETH',volumeCurrency:'WETH'};
  const row={t:started-16*60000,o:1,h:2,l:1,c:2,v:3,vu:5,observedAt:started-12*60000};
  const initial={...frame,liveMarket:true,epoch:'quiet-db',cursor:3,rows:[row],marketStatus:'quiet',coverageStatus:'current',
    stale:false,scanAt:started,transportStatus:'live',lastTradeAt:started-12*60000,lastSourceEventAt:started-12*60000,lastSuccessfulAt:started-11*60000};
  let reads=0,finish,clock=started;
  const originalDateNow=Date.now;Date.now=()=>clock;
  const mounted=mountedChart(env,async()=>({rows:[]}),{
    props:{asset:{chainId:'4663',token,priceScope:'dex'},pool,samples:[]},getLiveMarketCandles:async()=>initial,
    getLiveMarkets:()=>{reads++;return new Promise(resolve=>{finish=resolve;});},
  });
  try{
    env.global.LightweightCharts=mounted.vendor;env.scripts[0].onload();for(let i=0;i<5;i++)await flush();
    assert.equal(mounted.state.candleStatusLabel.value,'观测范围内暂无新成交');
    clock=started+25000;mounted.state.now.value=clock;await flush();
    assert.equal(mounted.state.candleStatusLabel.value,'实时状态待确认');
    const before=structuredClone(vue.toRaw(mounted.state.candleInfo.value));
    const live=mounted.liveConnections[0];live.setup.onStatus({connected:true,heartbeat:true});live.setup.onStatus({connected:true,heartbeat:true});
    assert.equal(reads,1);
    finish({epoch:'quiet-db',markets:[{...frame,liveMarket:true,stale:false,marketStatus:'quiet',coverageStatus:'current',
      transportStatus:'live',scanAt:clock-1000,scanThroughBlock:999,headBlock:1000,scanBlockTime:clock-2000,lastTradeAt:before.lastTradeAt,
      rows:[{...row,c:999}],lastSuccessfulAt:clock,lastSourceEventAt:clock}]});
    await flush();
    const after=mounted.state.candleInfo.value;
    assert.equal(mounted.state.candleStatusLabel.value,'观测范围内暂无新成交');
    assert.deepEqual(vue.toRaw(after.rows),before.rows);assert.equal(after.lastSuccessfulAt,before.lastSuccessfulAt);
    assert.equal(after.lastSourceEventAt,before.lastSourceEventAt);assert.equal(after.scanThroughBlock,999);
    live.setup.onStatus({connected:true,heartbeat:true});assert.equal(reads,1);
    clock+=25000;mounted.state.now.value=clock;await flush();
    assert.equal(mounted.state.candleStatusLabel.value,'实时状态待确认');
  }finally{mounted.unmount();mounted.restore();Date.now=originalDateNow;}
});

test('health response from wrong market or after unmount cannot update the current chart',async()=>{
  const env=environment(),token='0x'+'1'.repeat(40),pool='0x'+'2'.repeat(40),started=Date.now();
  const frame={chainId:'196',token,venue:'dex',poolId:pool,pool,marketId:pool,bar:'5m',priceCurrency:'WBNB',volumeCurrency:'WBNB'};
  let finish,signal;
  const initial={...frame,liveMarket:true,epoch:'health-db',cursor:1,rows:[{t:started-900000,o:1,h:2,l:1,c:2}],
    stale:false,marketStatus:'quiet',coverageStatus:'current',transportStatus:'live',scanAt:started-30000};
  const mounted=mountedChart(env,async()=>({rows:[]}),{props:{asset:{chainId:'196',token,priceScope:'dex'},pool,samples:[]},
    getLiveMarketCandles:async()=>initial,getLiveMarkets:(_chain,_token,options)=>{signal=options.signal;return new Promise(resolve=>{finish=resolve;});}});
  try{
    env.global.LightweightCharts=mounted.vendor;env.scripts[0].onload();for(let i=0;i<5;i++)await flush();
    mounted.liveConnections[0].setup.onStatus({connected:true,heartbeat:true});
    mounted.unmount();assert.equal(signal.aborted,true);
    finish({epoch:'health-db',markets:[{...frame,liveMarket:true,scanAt:Date.now(),stale:false,marketStatus:'quiet'}]});
    await flush();assert.equal(mounted.state.candleInfo.value.scanAt,initial.scanAt);
  }finally{mounted.restore();}
});
