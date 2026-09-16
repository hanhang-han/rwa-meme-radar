import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {JSDOM,VirtualConsole} from 'jsdom';
import {runInContext} from 'node:vm';
const token='0x'+'a'.repeat(40),stock='0x'+'b'.repeat(40),pool='0x'+'c'.repeat(40);
const pause=()=>new Promise(r=>setTimeout(r,35));
async function page(hash='#live'){
 const errors:string[]=[],vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e.message));
 const dom=new JSDOM(readFileSync('public/index.html','utf8'),{url:'http://localhost/dashboard/'+hash,runScripts:'outside-only',pretendToBeVisual:true,virtualConsole:vc}),w=dom.window as any,now=Date.now();
 const a={token,chain:'196',chainId:'196',symbol:'RTX',name:'RTX',kind:'candidate',firstSeen:now,updatedAt:now,price:2,volume24h:200,liquidity:1000,holders:null,fieldTimes:{price:now,volume24h:now,liquidity:now}};
 const b={...a,token:'0x'+'e'.repeat(40),symbol:'Other',name:'Other',volume24h:500};
 const r={id:'r',token,stock,pool,chainId:'196',ticker:'NVDA',status:'verified',token0:token,token1:stock,stockSide:stock,liquidityUsd:2000,liquidityAt:now,checkedAt:now,block:10};
 const data={now,assets:[],poolsFound:0,radar:{tokens:[],trades:[]},leadCandidates:[],relationTypes:[],sectors:[],okx:{tokens:[]},unified:{assets:[a,b],relations:[r],signals:[],groups:[{symbol:'RTX',members:[a]},{symbol:'Other',members:[b]}],stockTokens:[{chainId:'196',tokenContractAddress:stock,stockCode:'NVDA',tokenSymbol:'NVDAx',provider:'OKX',price:100,stockPrice:100,volumeScope:'dex',updatedAt:now}],sources:[{provider:'OKX',status:'ready'}],metrics:{actionableAssets:1,verifiedPools:1,verifiedAssets:1,liquidityCoverage:{valued:1,total:1}},sectors:[]}};
 let calls=0;w.fetch=async(url:string)=>{if(!url.includes('/api/ai/'))calls++;return {ok:true,json:async()=>url.includes('/api/token/')?{asset:{...a,...(url.endsWith(stock)?{token:stock,symbol:'NVDAx',kind:'stock'}:{})},relations:[r],trades:[],samples:[{t:now-300000,price:1},{t:now,price:2}],events:[],pools:[],activity:{count:0}}:url.includes('/api/events')?{items:[],next:null}:url.includes('/api/ai/')?{text:null,reason:'disabled'}:data};};
 w.echarts={init:()=>({setOption(){},resize(){},dispose(){},clear(){}})};
 for(const file of ['i18n.js','app.js','xlayer-workspace.js','radar-v2.js','workspace.js'])runInContext(readFileSync('public/'+file,'utf8'),dom.getInternalVMContext());
 await pause();return {dom,w,data,errors,calls:()=>calls};
}
test('V2 navigation, query sorting and evidence detail render with the production script order',async()=>{
 const {dom,w,errors}=await page('#meme?filter=all&sort=volume24h');try{
 assert.equal(w.document.querySelectorAll('.workspace-nav>a').length,4);assert.match(w.document.querySelector('.workspace-nav').textContent,/交易池分析/);
 assert.match(w.document.querySelector('#xMemeRows tbody tr').textContent,/Other/);
 w.document.getElementById('xMemeSearch').value='RTX';w.document.getElementById('xMemeSearch').dispatchEvent(new w.Event('input',{bubbles:true}));
 assert.match(w.location.hash,/q=RTX/);assert.equal(w.document.querySelectorAll('#xMemeRows tbody tr').length,1);
 w.location.hash='#detail/196/'+token;await pause();assert.match(w.document.getElementById('memeDetail').textContent,/地址已核验/);assert.match(w.document.getElementById('memeDetail').textContent,/采集排队中/);
 w.location.hash='#pair/196/'+stock+'?pool='+pool;await pause();assert.equal(w.document.getElementById('view-pair').hidden,false);assert.match(w.document.getElementById('view-pair').textContent,/流动性池分布/);assert.deepEqual(errors,[]);
 }finally{w.close();}
});
test('V2 language changes keep URL and filters, translate pair view, and use cached details',async()=>{
 const {w,errors,calls}=await page('#pair/196/'+stock+'?pool='+pool);try{
 await pause();const before=calls(),hash=w.location.hash;w.document.getElementById('langEn').click();await new Promise(r=>setTimeout(r,100));
 assert.equal(w.location.hash,hash);assert.equal(calls(),before);assert.match(w.document.querySelector('.workspace-nav').textContent,/Home/);
 assert.doesNotMatch(w.document.getElementById('view-pair').textContent,/\p{Script=Han}/u);assert.deepEqual(errors,[]);
 }finally{w.close();}
});

test('section help works on every view, preserves route, closes with Escape and switches language without requests',async()=>{
 const {w,errors,calls}=await page();try{
 for(const hash of ['#live','#meme','#stock','#pair','#events','#detail/196/'+token,'#pair/196/'+stock]){
  w.location.hash=hash;await pause();await pause();
  const visible=[...w.document.querySelectorAll('.view')].find((el:any)=>!el.hidden) as any;
  const button=visible.querySelector('[data-help]');assert.ok(button,hash);assert.ok(visible.querySelectorAll('.v2-tip').length>=1,hash+' should have module guides');assert.equal(visible.querySelectorAll('th [data-help],td [data-help]').length,0,hash+' keeps guides out of table cells');
  const before=calls();button.click();
  const dialog=w.document.querySelector('#v2HelpDialog');assert.equal(dialog.hidden,false);assert.ok(dialog.querySelector('[role="dialog"]'));
  assert.ok(dialog.querySelector('#v2HelpContent').textContent.length>25);assert.equal(w.location.hash,hash);assert.equal(calls(),before);
  w.document.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));assert.equal(dialog.hidden,true);assert.equal(w.document.activeElement,button);
 }
 w.document.querySelector('#view-pair [data-help]').click();const before=calls();
 w.document.getElementById('langEn').click();await new Promise(r=>setTimeout(r,100));
 assert.doesNotMatch(w.document.querySelector('#v2HelpDialog').textContent,/\p{Script=Han}/u);assert.equal(calls(),before);assert.deepEqual(errors,[]);
 }finally{w.close();}
});
test('Stock Radar searches identified Chinese names, displays HKEX code and flags unresolved numeric symbols',async()=>{
 const {w,data}=await page('#stock?q=快手');try{
 (data.unified.stockTokens as any[]).push({chainId:'196',assetId:'196:k',stockCode:'1024',tokenSymbol:'KUAIx',tokenContractAddress:token,stockIdentity:{id:'XHKG:01024',code:'01024',market:'HKEX',nameZh:'快手',nameEn:'Kuaishou Technology',status:'identified'}},{chainId:'196',assetId:'196:u',stockCode:'625',tokenSymbol:'SHEINx',stockIdentity:{id:'unresolved:196:u',status:'unresolved'}});
 w.RadarV2.render(data);assert.match(w.document.querySelector('#xStockRows').textContent,/快手/);assert.match(w.document.querySelector('#xStockRows').textContent,/HKEX · 01024/);
 w.location.hash='#stock?q=625';await pause();assert.match(w.document.querySelector('#xStockRows').textContent,/名称待核实/);assert.doesNotMatch(w.document.querySelector('#xStockRows').textContent,/HKEX/);
 }finally{w.close();}
});

test('Meme rows share one header and same-name contracts expand without losing route, language or polling state',async()=>{
 const {w,data,calls}=await page('#meme?filter=all');try{
 const original=data.unified.assets[0],extra={...original,token:'0x'+'f'.repeat(40),chain:'56',chainId:'56',volume24h:100};
 data.unified.assets.push(extra);data.unified.groups[0].members.push(extra);
 w.RadarV2.render(data);
 assert.equal(w.document.querySelectorAll('#xMemeRows thead').length,1);
 assert.equal(w.document.querySelectorAll('#xMemeRows tbody tr').length,2);
 const hash=w.location.hash,before=calls();w.document.querySelector('[data-v2-group]').click();
 assert.equal(w.document.querySelectorAll('#xMemeRows tbody tr').length,3);
 assert.equal(w.document.querySelector('[data-v2-group]').getAttribute('aria-expanded'),'true');
 assert.match(w.document.querySelector('.v2-member-row a').getAttribute('href'),/detail\/56/);
 w.document.getElementById('langEn').click();await new Promise(r=>setTimeout(r,100));w.RadarV2.render(data);
 assert.equal(w.document.querySelectorAll('#xMemeRows tbody tr').length,3);assert.equal(w.location.hash,hash);assert.equal(calls(),before);
 const guide=w.document.querySelector('#view-meme [data-help]');guide.click();const content=w.document.getElementById('v2HelpContent');w.RadarV2.render(data);
 assert.equal(w.document.getElementById('v2HelpContent'),content,'polling must not reset the guide');
 w.document.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 assert.equal(w.document.activeElement,w.document.querySelector('#view-meme [data-help]'));
 }finally{w.close();}
});
