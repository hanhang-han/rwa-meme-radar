// Runs against a Vite server; all API observations are deterministic fixtures.
import assert from 'node:assert/strict';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const browser=await chromium.launch({headless:true,channel:'chrome'});
try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 page.on('pageerror',e=>{errors.push(e.message);console.error(e.message);});
 const now=Date.now(),barAt=Math.floor(now/300000)*300000;
 const asset={chainId:'196',token:'0xabc',projectionKey:'196:0xabc',symbol:'TEST',name:'Test asset',kind:'candidate',price:1,priceCurrency:'USD',priceScope:'dex',provider:'OKX',volume24h:5,volumeCurrency:'USD',liquidity:10000,holders:2,fieldTimes:{price:now,volume24h:now,liquidity:now,holders:now},dataQuality:{tier:'current',eligible:{marketRanking:true,relationRanking:true}}};
 const stock={chainId:'196',tokenContractAddress:'0xstock',projectionKey:'196:0xstock',stockCode:'AAPL',tokenSymbol:'AAPLx',tokenName:'Apple token',price:10,priceCurrency:'USD',priceScope:'dex',provider:'OKX',stockPrice:100,referenceCurrency:'USD',referenceAt:now,fieldTimes:{price:now},stockIdentity:{id:'AAPL',code:'AAPL',status:'identified',nameZh:'苹果',market:'US'}};
 const relation={id:'p',projectionKey:'196:p',chainId:'196',token:'0xabc',stock:'0xstock',pool:'0xpool',ticker:'AAPL',status:'verified',level:'A',evidenceStatus:'qualified',liquidityUsd:10000,liquidityAt:now,checkedAt:now};
 const snapshot={realtime:{schema:1,cursor:10,revision:1},unified:{snapshotScope:'full',assets:[asset],stockTokens:[stock],relations:[relation],sectors:[],sources:[],metrics:{verifiedPools:1},signals:[],quality:{summary:{total:1,eligible:{}}}}};
 let dashboardCalls=0,registryReady=false,poolMode=false;
 await page.addInitScript(()=>{
  // Observe the real chart instance and its viewport; rendering stays unchanged.
  let chartApi;window.__chartInstances=[];
  Object.defineProperty(window,'LightweightCharts',{configurable:true,get:()=>chartApi,set:api=>{chartApi={...api,createChart(...args){const chart=api.createChart(...args);window.__chartInstances.push(chart);return chart;}};}});
  const original=window.fetch.bind(window);
  window.fetch=(url,options)=>{
   if(String(url).includes('/stream?')){
    window.__streamURL=String(url);window.__streamURLs=[...(window.__streamURLs||[]),String(url)];
    return Promise.resolve(new Response(new ReadableStream({start(controller){const send=(event,data,id)=>controller.enqueue(new TextEncoder().encode(`${id?'id: '+id+'\n':''}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));window.__send=send;send('hello',{cursor:10});options?.signal?.addEventListener('abort',()=>controller.error(new DOMException('Aborted','AbortError')));}}),{headers:{'Content-Type':'text/event-stream'}}));
   }
   return original(url,options);
  };
 });
 await page.route(/\/api\//,async route=>{
  const url=new URL(route.request().url());
  if(!url.pathname.startsWith('/api/'))return route.continue();
  if(url.pathname.endsWith('/dashboard')){dashboardCalls++;return route.fulfill({json:snapshot});}
  if(url.pathname.includes('/token/'))return route.fulfill({json:{asset:{...asset},relations:[relation],trades:[],events:[],samples:[],pools:[],poolMarkets:poolMode?[{poolId:'0xpool',marketId:'0xpool',priceCurrency:'WETH',lastTradeAt:now-1000,liquidityUsd:100}]:[],analysis:{},marketTrades:[]}});
  if(url.pathname.includes('/candles/')&&url.searchParams.get('pool'))return route.fulfill({json:{source:'X Layer native pool',venue:'dex',marketId:'0xpool',pool:'0xpool',priceCurrency:'WETH',volumeCurrency:'WETH',status:'current',marketStatus:'quiet',scanAt:await page.evaluate(()=>Date.now()),lastTradeAt:now-1000,rows:[{t:barAt-300000,o:0.01,h:0.01,l:0.01,c:0.01,v:1,confirmed:true},{t:barAt,o:0.01,h:0.01,l:0.01,c:0.01,v:1,confirmed:false}]}});
  if(url.pathname.includes('/candles/'))return route.fulfill({json:{source:'OKX DEX',lastSuccessfulAt:now,venue:'dex',marketId:'aggregate',priceCurrency:'USD',volumeCurrency:'USD',status:'current',rows:[{t:barAt-300000,o:1,h:2,l:1,c:2,v:10,confirmed:true},{t:barAt,o:2,h:2,l:2,c:2,v:1,confirmed:false}]}});
  if(url.pathname.endsWith('/registry'))return route.fulfill({json:{configured:true,contract:'0xregistry',explorer:'#',pairs:registryReady?[{meme:'0xabc',stock:'0xstock'}]:[]}});
  if(url.pathname.endsWith('/feed'))return route.fulfill({json:{trades:[],relationships:[]}});
  return route.fulfill({json:{status:'scheduled',current:{chainId:'196',token:'0xstock',pairs:[]}}});
 });
 const base=process.env.REALTIME_TEST_URL||'http://127.0.0.1:5186';
 await page.clock.install({time:new Date(now)});
 await page.goto(`${base}/#/meme?filter=all`);
 await page.locator('tr[data-asset="0xabc"]').waitFor({timeout:10000}).catch(async e=>{console.error(await page.locator('body').innerText());throw e;});
 await page.waitForFunction(()=>!!window.__send);
 assert.match(await page.evaluate(()=>window.__streamURL),/after=10/);
 assert.match(await page.evaluate(()=>window.__streamURL),/protocol=1/);
 assert.match(await page.evaluate(()=>window.__streamURL),/candles=none/);
 const updated={...asset,price:2,volume24h:20,holders:50,fieldTimes:{...asset.fieldTimes,price:now+1,volume24h:now+1,holders:now+1}};
 await page.evaluate(d=>window.__send('projection.delta',d,11),{schema:1,revision:2,upserts:{assets:[updated]},removes:{},meta:{metrics:{verifiedPools:2}},invalidations:[]});
 await page.waitForFunction(()=>document.querySelector('tr[data-asset="0xabc"] [data-field="price"]')?.textContent.includes('$2'));
 assert.match(await page.locator('tr[data-asset="0xabc"] [data-field="volume24h"]').innerText(),/20/);
 assert.match(await page.locator('tr[data-asset="0xabc"]').innerText(),/50/);
 await page.evaluate(()=>location.hash='#/stock?q=AAPL');
 await page.locator('.v3-versions').evaluate(el=>el.open=true);
 await page.evaluate(d=>window.__send('projection.delta',d,12),{schema:1,revision:3,upserts:{stockTokens:[{...stock,price:12,stockPrice:102,fieldTimes:{price:now+2}}]},removes:{},meta:{},invalidations:[]});
 await page.waitForFunction(()=>document.querySelector('td[data-field="stockPrice"]')?.textContent.includes('102'));
 assert.match(await page.locator('td[data-field="price"]').innerText(),/12/);
 assert.equal(await page.locator('summary.stock-summary-row').evaluate(el=>getComputedStyle(el,'::after').content),'none','stock row has no trailing plus');
 await page.locator('summary.stock-summary-row').click();
 await page.waitForURL(/#\/stock\/AAPL\?chain=/);
 await page.getByText('股票详情',{exact:true}).waitFor();
 await page.evaluate(()=>location.hash='#/detail/196/0xabc');
 await page.locator('[data-chart-ohlc]').waitFor();
 await page.waitForFunction(()=>new URL(window.__streamURL,location.href).searchParams.get('candles')==='196:0xabc:dex:aggregate:5m');
 await page.evaluate(packet=>window.__send('candle',packet,13),{chainId:'196',token:'0xabc',venue:'dex',marketId:'aggregate',bar:'5m',source:'OKX DEX',priceCurrency:'USD',sourceEventAt:now-100,receivedAt:now+1,row:{t:barAt,o:2,h:99,l:2,c:99,v:99,confirmed:false}});
 await page.waitForTimeout(200);
 assert.match(await page.locator('[data-chart-ohlc]').innerText(),/C 2/);
 await page.evaluate(packet=>window.__send('candle',packet,14),{chainId:'196',token:'0xabc',venue:'dex',marketId:'aggregate',bar:'5m',source:'OKX DEX',priceCurrency:'USD',volumeCurrency:'USD',sourceEventAt:now+3,receivedAt:now+3,row:{t:barAt,o:2,h:4,l:2,c:4,v:9,vu:36,confirmed:false}});
 await page.waitForFunction(()=>document.querySelector('[data-chart-ohlc]')?.textContent.includes('C 4'));
 await page.evaluate(packet=>window.__send('trade',packet,15),{chainId:'196',token:'0xabc',venue:'binance-alpha',marketId:'ALPHA_1U',source:'Binance Alpha',scope:'exchange',id:'trade-1',t:now,price:4,priceCurrency:'U',quoteQuantity:8,volumeCurrency:'U',type:'buy'});
 await page.getByText('市场实时成交',{exact:true}).waitFor();
 assert.match(await page.locator('body').innerText(),/ALPHA_1U/);
 await page.evaluate(packet=>window.__send('trade-remove',packet,16),{chainId:'196',token:'0xabc',venue:'binance-alpha',marketId:'ALPHA_1U',ids:['trade-1']});
 await page.waitForFunction(()=>!document.body.innerText.includes('ALPHA_1U'));
 // A quote replaces the asset object but keeps the market identity. Existing
 // OHLC must remain mounted even while frequent quotes repaint the price line.
 await page.evaluate(()=>{window.__quoteChart=window.__chartInstances.at(-1);window.__quoteChart.timeScale().setVisibleLogicalRange({from:-6,to:1});window.__quoteRange=window.__quoteChart.timeScale().getVisibleLogicalRange();window.__ohlcRemovals=0;window.__ohlcObserver=new MutationObserver(records=>{for(const record of records)for(const node of record.removedNodes)if(node.nodeType===1&&(node.matches('[data-chart-ohlc]')||node.querySelector('[data-chart-ohlc]')))window.__ohlcRemovals++;});window.__ohlcObserver.observe(document.querySelector('.view'),{childList:true,subtree:true});});
 await page.waitForFunction(()=>window.__quoteChart.timeScale().getVisibleLogicalRange()?.from===-6);
 await page.evaluate(()=>window.__quoteRange=window.__quoteChart.timeScale().getVisibleLogicalRange());
 for(let index=0;index<5;index++){
  await page.evaluate(d=>window.__send('projection.delta',d,17+d.revision),{schema:1,revision:4+index,upserts:{assets:[{...updated,price:2.5+index/10,fieldTimes:{...updated.fieldTimes,price:now+100+index}}]},removes:{},meta:{},invalidations:[]});
  await page.waitForTimeout(100);
 }
 assert.equal(await page.evaluate(()=>window.__ohlcRemovals),0,'same-market quotes must not clear displayed OHLC');
 assert.match(await page.locator('[data-chart-ohlc]').innerText(),/C 4/);
 assert.equal(await page.evaluate(()=>window.__chartInstances.at(-1)===window.__quoteChart),true,'quotes must preserve the chart instance');
 assert.deepEqual(await page.evaluate(()=>window.__quoteChart.timeScale().getVisibleLogicalRange()),await page.evaluate(()=>window.__quoteRange),'quotes must preserve the zoom/pan viewport');
 await page.evaluate(()=>window.__ohlcObserver.disconnect());
 registryReady=true;
 await page.evaluate(d=>window.__send('projection.delta',d,30),{schema:1,revision:9,upserts:{},removes:{},meta:{},invalidations:[{kind:'registry'}]});
 await page.locator('.v2-registry').waitFor();
 await page.evaluate(d=>window.__send('projection.delta',d,31),{schema:1,revision:10,upserts:{relations:[{...relation,level:null,evidenceStatus:'unverified'}]},removes:{},meta:{},invalidations:[]});
 await page.waitForFunction(()=>!document.querySelector('.v2-registry'));
 await page.getByRole('button',{name:'EN',exact:true}).click();
 await page.getByText('Price history',{exact:true}).waitFor();
 const before=dashboardCalls;
 // Tick the same browser clock used by its reconciliation interval.
 await page.clock.fastForward(21000);
 assert.equal(dashboardCalls,before,'healthy projection stream must not request a 20s full snapshot');
 poolMode=true;
 await page.reload();
 await page.locator('[data-chart-market]').waitFor();
 await page.waitForFunction(()=>document.querySelector('[data-chart-market]')?.value==='pool:0xpool');
 assert.match(await page.locator('[data-kpi-key="price"] .kpi-value').innerText(),/\$1/);
 assert.match(await page.locator('[data-pool-unit]').innerText(),/WETH/);
 assert.match(await page.locator('[data-chart-status]').innerText(),/Collection healthy/);
 const nativeNow=await page.evaluate(()=>Date.now());
 await page.evaluate(packet=>window.__send('candle',packet,31),{chainId:'196',token:'0xabc',venue:'dex',marketId:'0xpool',poolId:'0xpool',bar:'5m',source:'X Layer native pool',priceCurrency:'WETH',volumeCurrency:'WETH',sourceEventAt:nativeNow,row:{t:barAt,o:0.01,h:0.02,l:0.01,c:0.02,v:2,vu:0.04,confirmed:false}});
 await page.waitForFunction(()=>document.querySelector('[data-chart-ohlc]')?.textContent.includes('C 0.02'));
 assert.match(await page.locator('[data-kpi-key="price"] .kpi-value').innerText(),/\$1/);
 const livePoolStatus=await page.locator('[data-chart-status]').innerText();
 assert.match(livePoolStatus,/Live trades connected; historical data is being backfilled/);
 await page.evaluate(packet=>window.__send('candle',packet,32),{chainId:'196',token:'0xabc',venue:'dex',marketId:'0xpool',poolId:'0xpool',bar:'5m',source:'X Layer native pool',priceCurrency:'WETH',volumeCurrency:'WETH',sourceEventAt:nativeNow-3600000,row:{t:barAt-300000,o:0.01,h:0.015,l:0.01,c:0.015,v:2,vu:0.03,confirmed:true}});
 await page.waitForTimeout(150);
 assert.equal(await page.locator('[data-chart-status]').innerText(),livePoolStatus,'historical correction must preserve latest trade time and live/coverage state');
 assert.match(await page.locator('[data-chart-ohlc]').innerText(),/C 0.02/);
 await page.locator('[data-chart-market]').selectOption('dex');
 await page.waitForFunction(()=>document.querySelector('[data-chart-ohlc]')?.textContent.includes('C 2'));
 assert.equal(await page.locator('[data-pool-unit]').count(),0);
 assert.deepEqual(errors,[]);
 console.log('PASS browser: all fields/DEX stock/reference/candle/trade+reorg/registry/language/cursor/no20s snapshot/native pool default + units + aggregate switch + no quote-induced OHLC clearing + zoom/pan preserved + historical correction preserves live/coverage status');
}finally{await browser.close();}
