// Exercise the deployed HTML + all application scripts against its real APIs
// in a DOM runtime. This does not substitute for visual browser verification.
import { JSDOM, VirtualConsole } from 'jsdom';
import assert from 'node:assert/strict';
import { runInContext } from 'node:vm';
const pageUrl = process.argv[2] || 'http://129.226.135.20';
const origin = new URL(pageUrl).origin;
const basePath = new URL(pageUrl).pathname.replace(/\/$/, '');
const errors=[];
const vc=new VirtualConsole();vc.on('jsdomError',error=>errors.push(error.message));
const html=await (await fetch(pageUrl)).text();
const page = new URL(pageUrl); page.hash = '#live';
const dom=new JSDOM(html,{url:page.href,runScripts:'outside-only',pretendToBeVisual:true,virtualConsole:vc});
const w=dom.window;
// Chart layout is the only browser subsystem stubbed; application routing,
// data loading, filtering, and DOM rendering run from deployed JS unchanged.
w.echarts={init:()=>({setOption(){},resize(){},dispose(){},clear(){},on(){}})};
w.fetch=(url,options)=>fetch(new URL(url,origin),options);
for(const name of ['i18n.js','app.js','xlayer-workspace.js','radar-v2.js','workspace.js']) {
  const response=await fetch(new URL(`${basePath}/${name}`,origin));assert.equal(response.status,200);
  runInContext(await response.text(),dom.getInternalVMContext(),{filename:name});
}
async function until(predicate){for(let i=0;i<120;i++){if(predicate())return;await new Promise(r=>setTimeout(r,250));}throw new Error('DOM condition timed out');}
try {
  await until(()=>w.document.querySelector('#xlayerLive'));
  assert.match(w.document.querySelector('#sourceStatus').textContent,/已接入数据源|Data sources/);
  assert.match(w.document.querySelector('#sourceStatus').textContent,/OKX|Robinhood|Binance/);
  if (page.searchParams.get('lang') !== 'en') {
    w.document.getElementById('langEn').click();
    assert.equal(w.document.getElementById('langEn').disabled,true);
    await until(()=>/Data sources/.test(w.document.querySelector('#sourceStatus').textContent));
    assert.equal(w.document.getElementById('langEn').disabled,false);
  }
  const guide=w.document.querySelector('#xlayerLive [data-help]');assert.ok(guide);guide.click();
  assert.equal(w.document.getElementById('v2HelpDialog').hidden,false);
  assert.match(w.document.getElementById('v2HelpContent').textContent,/Overview/);
  w.document.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));assert.equal(w.document.getElementById('v2HelpDialog').hidden,true);
  assert.ok(w.document.querySelector('[data-open-id="quality"]'));
  const first=w.document.querySelector('#xlayerLive .x-signal') || w.document.querySelector('#xStockRows tbody a[href^="#detail/"]');
  const href=first.getAttribute('href');
  w.location.hash=href;
  await until(()=>w.document.querySelector('#memeDetail .page-heading'));
  assert.match(w.document.querySelector('#memeDetail').textContent,/关系|最近成交|Relationship|Recent activity/);
  if (w.document.documentElement.lang === 'en') assert.doesNotMatch(w.document.querySelector('#memeDetail').textContent,/[\p{Script=Han}]/u);
  w.location.hash='#meme';
  await until(()=>!w.document.querySelector('#view-meme').hidden);
  assert.ok(w.document.querySelector('#xMemeRows'));
  w.location.hash='#stock';
  await until(()=>!w.document.querySelector('#view-stock').hidden);
  assert.ok(w.document.querySelector('#xStockRows .x-stock'));
  const pair=w.document.querySelector('#xStockRows a[href^="#pair/"]');
  assert.ok(pair);w.location.hash=pair.getAttribute('href');
  await until(()=>!w.document.querySelector('#view-pair').hidden && /Cross-market|跨市场|Stock-token|股票代币/.test(w.document.querySelector('#view-pair').textContent));
  w.location.hash='#stock?q='+encodeURIComponent('快手');
  await until(()=>!w.document.querySelector('#view-stock').hidden && /Kuaishou/.test(w.document.querySelector('#xStockRows').textContent));
  assert.match(w.document.querySelector('#xStockRows').textContent,/HKEX · 01024/);
  const actual=errors.filter(e=>!e.includes('HTMLCanvasElement')&&!e.includes('Could not load'));
  assert.deepEqual(actual,[]);
  console.log(JSON.stringify({source:w.document.querySelector('#sourceStatus').textContent,detail:href,views:5,errors:actual.length}));
}finally{w.close();}
