// Read-only production acceptance. No route interception, fixtures, credentials,
// clock changes, account actions, or manual-refresh clicks. Run AFTER deployment.
// PLAYWRIGHT_MODULE=/absolute/path/playwright/index.mjs node web/tests/production-realtime-browser.mjs
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=(process.env.PROD_TEST_URL||'https://cliperx.com/dashboard/').replace(/\/?$/,'/');
const windows=new Set((process.env.PROD_WINDOWS||'stock,meme,detail,native').split(','));
const seconds=Math.min(300,Math.max(10,Number(process.env.PROD_OBSERVE_SECONDS)||45));
const output=resolve(process.env.PROD_REPORT_DIR||`/tmp/cliperx-production-${new Date().toISOString().replace(/[:.]/g,'-')}`);
await mkdir(output,{recursive:true});
const report={startedAt:new Date().toISOString(),base,observationSecondsPerMarket:seconds,output,tabs:[],observations:[],pageErrors:[],consoleErrors:[],httpErrors:[],dashboardRequests:[],candleResponses:[],streamResponseHeaders:[],requestErrors:[],expectedStreamAborts:0,notes:[]};
const browser=await chromium.launch({headless:process.env.PROD_HEADFUL!=='1',channel:'chrome'});
let page;
try{
 page=await browser.newPage({viewport:{width:1440,height:1100}});
 page.setDefaultTimeout(30000);
 page.on('pageerror',error=>report.pageErrors.push(error.message));
 page.on('console',message=>{if(message.type()==='error')report.consoleErrors.push(message.text().slice(0,500));});
 page.on('response',response=>{if(/\/api\/candles\//.test(response.url()))response.json().then(data=>report.candleResponses.push({at:Date.now(),url:response.url(),httpStatus:response.status(),venue:data.venue,marketId:data.marketId,poolId:data.poolId,pool:data.pool,priceCurrency:data.priceCurrency,volumeCurrency:data.volumeCurrency,status:data.status,stale:data.stale,marketStatus:data.marketStatus,coverageStatus:data.coverageStatus,error:data.error,source:data.source,lastSuccessfulAt:data.lastSuccessfulAt,lastObservationAt:data.lastObservationAt,lastSourceEventAt:data.lastSourceEventAt,lastTradeAt:data.lastTradeAt,scanAt:data.scanAt,rowCount:data.rows?.length,tail:data.rows?.slice(-3)})).catch(error=>report.notes.push('Candle response diagnostic unavailable: '+String(error)));if(/\/api\/stream(?:\?|$)/.test(response.url())){const h=response.headers();report.streamResponseHeaders.push({at:Date.now(),url:response.url(),status:response.status(),contentEncoding:h['content-encoding'],contentType:h['content-type'],vary:h.vary});}if(/\/api\/dashboard(?:\?|$)/.test(response.url()))report.dashboardRequests.push({at:Date.now(),url:response.url(),status:response.status()});if(response.status()>=400)report.httpErrors.push({url:response.url(),status:response.status()});});
 page.on('requestfailed',request=>{
  const record={url:request.url(),error:request.failure()?.errorText};
  if(record.url.includes('/api/stream')&&/ABORTED|CANCELLED/i.test(record.error||''))report.expectedStreamAborts++;
  else report.requestErrors.push(record);
 });
 // Tee only the real public response. The original Response is returned
 // unchanged to the application; the diagnostic branch never injects data.
 await page.addInitScript(()=>{
  const audit=window.__productionRealtimeAudit={events:[],streams:[],streamEnds:[],candles:[],snapshot:null};
  const original=window.fetch.bind(window);
  const push=(list,value,limit=6000)=>{list.push(value);if(list.length>limit)list.splice(0,list.length-limit);};
  window.fetch=async(...args)=>{
   const response=await original(...args);
   const raw=args[0] instanceof Request?args[0].url:String(args[0]);
   const url=new URL(raw,location.href);
   if(/\/api\/dashboard$/.test(url.pathname))response.clone().json().then(data=>{
    if(!data?.unified)return;
    if(audit.snapshot?.scope==='full'&&data.unified.snapshotScope==='overview')return;
    audit.snapshot={at:Date.now(),realtime:data.realtime,scope:data.unified.snapshotScope,
     assets:(data.unified.assets||[]).map(a=>({chainId:a.chainId,token:a.token,symbol:a.symbol,price:a.price,volume24h:a.volume24h,exchangeMarkets:a.exchangeMarkets})),
     stocks:(data.unified.stockTokens||[]).map(a=>({chainId:a.chainId,token:a.tokenContractAddress,symbol:a.tokenSymbol,stockCode:a.stockCode,price:a.price,provider:a.provider,volume24h:a.volume24h,priceScope:a.priceScope,marketId:a.marketId})),
     relations:(data.unified.relations||[]).filter(r=>r.pool&&r.stock).slice(0,200).map(r=>({chainId:r.chainId,stock:r.stock,token:r.token,pool:r.pool}))};
   }).catch(()=>{});
   if(/\/api\/candles\//.test(url.pathname))response.clone().json().then(data=>{push(audit.candles,{at:Date.now(),url:url.href,httpStatus:response.status(),venue:data.venue,marketId:data.marketId,poolId:data.poolId,pool:data.pool,priceCurrency:data.priceCurrency,volumeCurrency:data.volumeCurrency,status:data.status,stale:data.stale,marketStatus:data.marketStatus,coverageStatus:data.coverageStatus,error:data.error,source:data.source,lastSuccessfulAt:data.lastSuccessfulAt,lastObservationAt:data.lastObservationAt,lastSourceEventAt:data.lastSourceEventAt,lastTradeAt:data.lastTradeAt,scanAt:data.scanAt,rowCount:data.rows?.length,tail:data.rows?.slice(-3)},200);}).catch(()=>{});
   if(/\/api\/stream$/.test(url.pathname)&&response.ok&&response.body){
    push(audit.streams,{at:Date.now(),url:url.href,after:url.searchParams.get('after'),protocol:url.searchParams.get('protocol'),candles:url.searchParams.get('candles')},100);
    const reader=response.clone().body.getReader(),decoder=new TextDecoder();
    (async()=>{let buffer='';try{
     for(;;){const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});buffer=buffer.replace(/\r\n/g,'\n');let separator;
      while((separator=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,separator);buffer=buffer.slice(separator+2);let event='message',id=null,text='';
       for(const line of frame.split('\n')){if(line.startsWith('event:'))event=line.slice(6).trim();else if(line.startsWith('id:'))id=line.slice(3).trim();else if(line.startsWith('data:'))text+=line.slice(5).trim();}
       if(!text)continue;let data;try{data=JSON.parse(text);}catch{push(audit.events,{event,id,at:Date.now(),invalidJson:true});continue;}
       push(audit.events,{event,id,at:Date.now(),revision:data.revision,schema:data.schema,chainId:data.chainId,token:data.token,venue:data.venue,marketId:data.marketId,poolId:data.poolId,bar:data.bar,
        sourceEventAt:data.sourceEventAt,receivedAt:data.receivedAt,persistedAt:data.persistedAt,projectionAt:data.now,candle:data.row?{t:data.row.t,o:data.row.o,h:data.row.h,l:data.row.l,c:data.row.c,v:data.row.v,vu:data.row.vu}:undefined,
        upserts:data.upserts?Object.fromEntries(Object.entries(data.upserts).map(([key,rows])=>[key,rows.length])):undefined,
        removes:data.removes?Object.fromEntries(Object.entries(data.removes).map(([key,rows])=>[key,rows.length])):undefined,
        meta:data.meta?Object.keys(data.meta):undefined,invalidations:data.invalidations});
      }
     }
    }catch(error){push(audit.streamEnds,{at:Date.now(),message:String(error)},100);}})();
   }
   return response;
  };
 });
 await page.goto(base+'#/live',{waitUntil:'domcontentloaded',timeout:60000});
 await page.locator('.workspace-nav').waitFor();
 await page.waitForFunction(()=>window.__productionRealtimeAudit?.snapshot?.assets?.length&&window.__productionRealtimeAudit.snapshot.scope!=='overview',{},{timeout:60000});
 const catalogue=await page.evaluate(()=>window.__productionRealtimeAudit.snapshot);
 report.catalogue={assets:catalogue.assets.length,stockTokens:catalogue.stocks.length,realtime:catalogue.realtime};
 if(catalogue.realtime?.schema!==1)report.notes.push('Snapshot has no supported realtime schema 1; deployment/protocol must be checked.');
 const navigate=async(path)=>{
  await page.evaluate(path=>{location.hash='#'+path;},path);
  await page.waitForFunction(()=>!document.querySelector('.view > [role="status"]')?.textContent.match(/正在加载页面|Loading page/));
 };
 for(const [name,path,selector]of [['home','/live','.view .panel'],['meme','/meme?filter=all','#xMemeRows'],['stock','/stock','#xStockRows'],['pair','/pair','#v2Search']]){
  const item={name,path};try{
   await navigate(path);await page.locator(selector).first().waitFor();
   item.visibleTextLength=(await page.locator('.view').innerText()).length;
   item.heading=await page.locator('.view h2').first().textContent().catch(()=>null);
   item.alerts=await page.locator('.view [role="alert"]').allTextContents();
   item.ok=item.visibleTextLength>80&&!item.alerts.length;
   await page.screenshot({path:resolve(output,`${name}.png`),fullPage:false});
  }catch(error){item.ok=false;item.error=String(error);}
  report.tabs.push(item);console.log(`TAB ${name}: ${item.ok?'PASS':'FAIL'} ${item.heading||item.error||''}`);
 }
 async function visibleValues(){return page.evaluate(()=>{
  const values={};
  for(const node of document.querySelectorAll('[data-field]')){
   if(!node.getClientRects().length)continue;
   const row=node.closest('[data-asset]');if(!row)continue;
   // Only the numeric component; quote-age/status text is intentionally excluded.
   const number=node.querySelector(':scope > span');
   values[`row:${row.dataset.asset}:${node.dataset.field}`]=(number||node).textContent.trim();
   for(const [index,quote]of [...node.querySelectorAll(':scope > small > span')].entries())values[`row:${row.dataset.asset}:${node.dataset.field}:market:${index}`]=quote.textContent.trim();
  }
  for(const [index,node]of [...document.querySelectorAll('.kpi-value')].entries()){
   if(node.getClientRects().length)values[`kpi:${node.closest('.kpi')?.querySelector('.kpi-label')?.textContent||index}`]=node.textContent.trim();
  }
  for(const [index,node]of [...document.querySelectorAll('[data-chart-ohlc]')].entries())values[`candle:${index}`]=node.textContent.trim();
  const marketSection=[...document.querySelectorAll('.view section')].find(section=>/^(市场实时成交|Streaming market trades)$/.test(section.querySelector('h2')?.textContent.trim()||''));
  const marketTradeRows=marketSection?[...marketSection.querySelectorAll('tbody tr')].map(row=>row.textContent.trim()):[];
  return {values,marketTradeRows,marketSelection:document.querySelector('[data-chart-market]')?.value,marketOptions:[...document.querySelectorAll('[data-chart-market] option')].map(e=>({value:e.value,text:e.textContent})),status:document.querySelector('#updatedAt')?.textContent,chartStatus:[...document.querySelectorAll('[data-chart-status]')].map(n=>n.textContent.trim()),canvases:document.querySelectorAll('.x-candles canvas').length};
 });}
 function latencyStats(values){const sorted=values.filter(Number.isFinite).sort((a,b)=>a-b);return sorted.length?{count:sorted.length,p50:sorted[Math.floor(sorted.length*.5)],p95:sorted[Math.min(sorted.length-1,Math.floor(sorted.length*.95))],max:sorted.at(-1)}:{count:0};}
 async function observe(name,path,ready,prepare){
  console.log(`OBSERVE ${name}: ${seconds}s ${path}`);
  const item={name,path,startedAt:Date.now(),changes:[],frames:[],status:'inconclusive'};
  try{
   await navigate(path);await page.locator(ready).first().waitFor();if(prepare)await prepare();
   // Let the initial render and canonical-market subscription settle first.
   await page.waitForTimeout(1200);
   item.before=await visibleValues();let previous=item.before.values;const seenMarketRows=new Set(item.before.marketTradeRows);item.marketRowsAdded=[];item.marketRowsAddedCount=0;
   await page.evaluate(()=>{window.__productionOhlcDetached=[];window.__productionOhlcObserver?.disconnect();window.__productionOhlcObserver=new MutationObserver(records=>{for(const record of records)for(const node of record.removedNodes)if(node.nodeType===1&&(node.matches('[data-chart-ohlc]')||node.querySelector('[data-chart-ohlc]')))window.__productionOhlcDetached.push({at:Date.now(),marketSelection:document.querySelector('[data-chart-market]')?.value});});window.__productionOhlcObserver.observe(document.querySelector('.view'),{childList:true,subtree:true});});
   const started=Date.now();item.startedAt=started;
   while(Date.now()-started<seconds*1000){
    await page.waitForTimeout(1000);const current=await visibleValues();
    for(const [key,value]of Object.entries(current.values))if(previous[key]!=null&&previous[key]!==value){
     item.changes.push({at:Date.now(),key,from:previous[key],to:value});
     console.log(`CHANGE ${name} ${key}: ${previous[key]} -> ${value}`);
    }
    for(const row of current.marketTradeRows)if(!seenMarketRows.has(row)){seenMarketRows.add(row);item.marketRowsAddedCount++;if(item.marketRowsAdded.length<30)item.marketRowsAdded.push({at:Date.now(),text:row});}
    previous=current.values;item.after=current;
   }
   item.ohlcDetachments=await page.evaluate(()=>{window.__productionOhlcObserver?.disconnect();return window.__productionOhlcDetached;});
   item.frames=await page.evaluate(since=>window.__productionRealtimeAudit.events.filter(e=>e.at>=since),started);
   item.frameCounts=Object.fromEntries([...new Set(item.frames.map(e=>e.event))].map(type=>[type,item.frames.filter(e=>e.event===type).length]));
   const pathParts=path.split('/'),selectedPool=item.before.marketSelection?.startsWith('pool:')?item.before.marketSelection.slice(5):null;
   const selectedFrames=item.frames.filter(e=>String(e.chainId)===pathParts[2]&&e.token?.toLowerCase()===pathParts[3]?.toLowerCase()&&(!selectedPool||(e.poolId||e.marketId)?.toLowerCase()===selectedPool.toLowerCase()));
   item.sourceFreshness=Object.fromEntries(['trade','candle'].map(type=>{const frames=selectedFrames.filter(e=>e.event===type&&e.sourceEventAt>0);return [type,{count:frames.length,withinObservation:frames.filter(e=>e.sourceEventAt>=started&&e.sourceEventAt<=e.at+1000).length,within30SecondsOfDelivery:frames.filter(e=>e.at-e.sourceEventAt>=-1000&&e.at-e.sourceEventAt<=30000).length,historicalBeforeObservation:frames.filter(e=>e.sourceEventAt<started).length,minSourceAt:frames.length?Math.min(...frames.map(e=>e.sourceEventAt)):null,maxSourceAt:frames.length?Math.max(...frames.map(e=>e.sourceEventAt)):null,sourceToBrowserMs:latencyStats(frames.map(e=>e.at-e.sourceEventAt)),currentWindowSourceToBrowserMs:latencyStats(frames.filter(e=>e.sourceEventAt>=started&&e.sourceEventAt<=e.at+1000).map(e=>e.at-e.sourceEventAt))}];}));
   item.latencyMs={receivedToPersisted:latencyStats(item.frames.filter(e=>e.receivedAt&&e.persistedAt).map(e=>e.persistedAt-e.receivedAt)),persistedToBrowser:latencyStats(item.frames.filter(e=>e.persistedAt).map(e=>e.at-e.persistedAt)),receivedToBrowser:latencyStats(item.frames.filter(e=>e.receivedAt).map(e=>e.at-e.receivedAt))};
   item.numericChanges=item.changes.filter(c=>!c.key.startsWith('candle:')).length;
   item.candleChanges=item.changes.filter(c=>c.key.startsWith('candle:')).length;
   item.status=item.changes.length?'observed':'inconclusive';
   if(!item.changes.length)item.note='No displayed numeric change observed in this window; this alone does not prove a broken stream or absence of market trades.';
   await page.screenshot({path:resolve(output,`${name}.png`),fullPage:false});
   if(await page.locator('.x-candles').count())await page.locator('.x-candles').first().locator('..').screenshot({path:resolve(output,`${name}-chart.png`)}).catch(error=>report.notes.push('Chart screenshot unavailable: '+String(error)));
  }catch(error){item.status='error';item.error=String(error);}
  report.observations.push(item);console.log(`RESULT ${name}: ${item.status}; numbers=${item.numericChanges||0}; candles=${item.candleChanges||0}; frames=${JSON.stringify(item.frameCounts||{})}`);
 }
 const stocks=catalogue.stocks.filter(s=>s.token&&s.stockCode);
 const stock=stocks.filter(s=>s.priceScope==='exchange'&&/binance/i.test(s.provider||'')).sort((a,b)=>(Number(b.volume24h)||0)-(Number(a.volume24h)||0))[0]||stocks[0];
 const candidates=catalogue.assets.filter(a=>a.token);
 const meme=candidates.filter(a=>a.exchangeMarkets?.some(m=>m.venue==='binance-alpha')).sort((a,b)=>Math.max(...b.exchangeMarkets.map(m=>Number(m.volume24h)||0))-Math.max(...a.exchangeMarkets.map(m=>Number(m.volume24h)||0)))[0]||candidates.sort((a,b)=>(Number(b.volume24h)||0)-(Number(a.volume24h)||0))[0];
 report.selected={stock,meme};
 if(stock&&windows.has('stock'))await observe('stock-values',`/stock?q=${encodeURIComponent(stock.stockCode)}`,'#xStockRows',async()=>{
  // Standard disclosure controls only; never changes a server-side preference.
  const theme=page.locator('.v3-theme-card').first();if(await theme.count()&&!await theme.evaluate(el=>el.hasAttribute('open')))await theme.locator(':scope > summary').click();
  const versions=page.locator('.v3-versions').first();if(await versions.count()&&!await versions.evaluate(el=>el.hasAttribute('open')))await versions.locator(':scope > summary').click();
 });
 if(meme&&windows.has('meme'))await observe('meme-values',`/meme?filter=all&q=${encodeURIComponent(meme.token)}`,'#xMemeRows');
 const explicitDetail=process.env.PROD_DETAIL_URL?new URL(process.env.PROD_DETAIL_URL,base).hash.slice(1):null;
 const detailPath=explicitDetail?.startsWith('/detail/')?explicitDetail:meme?`/detail/${encodeURIComponent(meme.chainId||'196')}/${encodeURIComponent(meme.token)}`:null;
 if(detailPath&&windows.has('detail'))await observe('live-candles',detailPath,'.x-candles',process.env.PROD_POOL_ID?async()=>{await page.locator('[data-chart-market]').selectOption(`pool:${process.env.PROD_POOL_ID}`);}:undefined);
 if(process.env.PROD_NATIVE_DETAIL_URL&&process.env.PROD_NATIVE_POOL_ID&&windows.has('native')){
  const hash=new URL(process.env.PROD_NATIVE_DETAIL_URL,base).hash.slice(1);
  if(hash.startsWith('/detail/'))await observe('native-pool',hash,'.x-candles',async()=>{await page.locator('[data-chart-market]').selectOption(`pool:${process.env.PROD_NATIVE_POOL_ID}`);});
 }
 else if(process.env.PROD_PAIR_URL&&windows.has('native')){const hash=new URL(process.env.PROD_PAIR_URL,base).hash.slice(1);if(hash.startsWith('/pair/'))await observe('native-pool',hash,'.x-candles');}
 report.candleHookResponses=await page.evaluate(()=>window.__productionRealtimeAudit.candles);
 report.streams=await page.evaluate(()=>window.__productionRealtimeAudit.streams);
 report.streamEnds=await page.evaluate(()=>window.__productionRealtimeAudit.streamEnds);
 report.events=await page.evaluate(()=>window.__productionRealtimeAudit.events);
 report.streamScope={protocol1:report.streams.every(s=>s.protocol==='1'),noChartFiltered:report.streams.some(s=>s.candles==='none'),visibleMarketFiltered:report.streams.some(s=>s.candles&&s.candles!=='none'&&s.candles!=='all'),resumedWithCursor:report.streams.some(s=>s.after!=null)};
 const essentialHttpErrors=report.httpErrors.filter(e=>e.status>=500&&/\/api\/(dashboard|token|candles|stream|health)/.test(e.url));
 report.failures={snapshotProtocol:catalogue.realtime?.schema===1?0:1,streamProtocol:report.streams.length&&report.streamScope.protocol1?0:1,tabs:report.tabs.filter(t=>!t.ok).length,pageErrors:report.pageErrors.length,essentialHttpErrors:essentialHttpErrors.length,observationErrors:report.observations.filter(o=>o.status==='error').length};
 report.result=Object.values(report.failures).some(Boolean)?'failed':report.observations.some(o=>o.status==='observed')?'observed':'inconclusive';
 if(report.result==='failed')process.exitCode=1;
}catch(error){report.result='failed';report.fatal=String(error);process.exitCode=1;if(page)await page.screenshot({path:resolve(output,'fatal.png')}).catch(()=>{});}
finally{
 report.finishedAt=new Date().toISOString();
 report.notes.push('Event payload traces are bounded to the most recent 6,000 frames; frameCounts are sampled lower bounds, not complete event throughput. Numeric changes exclude quote-age text. New market row counts compare rendered row text (not underlying trade IDs), sample each second, and are lower bounds; only 30 examples are retained.');
 await writeFile(resolve(output,'report.json'),JSON.stringify(report,null,2)+'\n');
 console.log(`Production audit ${report.result}: ${resolve(output,'report.json')}`);
 await browser.close();
}
