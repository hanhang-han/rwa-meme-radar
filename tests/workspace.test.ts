import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import { runInContext } from 'node:vm';

const token='0x'+'a'.repeat(40), stock='0x'+'b'.repeat(40);
function createPage(hash='#live') {
  const dom=new JSDOM(readFileSync('public/index.html','utf8'),{url:'http://localhost/'+hash,runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window;
  const asset={token,chain:'196',symbol:'RTX',name:'RTX',kind:'candidate',firstSeen:Date.now(),updatedAt:Date.now(),price:0.01,volume24h:1200,txs24h:30,holders:12,liquidity:1000,match:null};
  const relation={id:'r',token,stock,ticker:'NVDA',status:'verified',pool:'0x'+'c'.repeat(40),stockSide:stock,token0:token,token1:stock,checkedAt:Date.now(),block:123,liquidityUsd:1000};
  const data={assets:[],leadCandidates:[],relationTypes:[],sectors:[],okx:{status:'ready',updatedAt:Date.now(),tokens:[{chainIndex:'196',tokenContractAddress:stock,tokenSymbol:'NVDAx',stockCode:'NVDA',volume24h:2000}]},xlayer:{coverage:{catalog:1,scanned:1,pools:1},assets:[asset],relations:[relation],signals:[],groups:[{symbol:'RTX',members:[asset]}],metrics:{verifiedAssets:1,verifiedPools:1},sectors:[]}};
  data.unified={...data.xlayer,sources:[{provider:'OKX',status:'ready'},{provider:'Robinhood',status:'ready'},{provider:'Binance',status:'ready'}],stockTokens:[{...data.okx.tokens[0],provider:'OKX'},{chainIndex:'4663',chainId:'4663',tokenContractAddress:'0x'+'d'.repeat(40),tokenSymbol:'NVDA',stockCode:'NVDA',tokenName:'NVDA',issuer:'Robinhood Assets (Jersey) Limited',price:100,stockPrice:100,volume24h:null,provider:'Robinhood'}]};
  w.state=data;
  w.escapeHtml=(s:any)=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  w.fmtNum=(v:any)=>String(v);w.fmtBnb=(v:any)=>String(v);
  w.fetch=async()=>({ok:true,json:async()=>({asset,relations:[relation],samples:[],trades:[],events:[],activity:{count:0,buys:0,sells:0},stocks:[],analysis:{conclusion:'已核验股票配对',correlation:{reason:'样本未齐'},capture:{reason:'分母未齐'},safety:'检测未完整'}})});
  w.eval(readFileSync('public/xlayer-workspace.js','utf8'));
  w.eval(readFileSync('public/workspace.js','utf8'));
  return {dom,w,data};
}
test('four-view routing defaults to the connected source and keeps BNB counts out of its homepage',()=>{
  const {dom,w}=createPage();
  try {
    assert.equal(w.document.querySelector('#network').value,'196');
    assert.equal(w.document.querySelector('#xlayerLive').hidden,false);
    assert.equal(w.document.querySelector('#signalMetrics').hidden,true);
    assert.match(w.document.querySelector('#xlayerLive').textContent,/重点关联资产/);
    assert.equal(w.document.querySelector('#view-stock').hidden,true);
    assert.doesNotMatch(w.document.querySelector('.workspace-nav').textContent,/主场/);
    assert.doesNotMatch(w.document.querySelector('.workspace-nav').textContent,/链|来源|OKX/);
    assert.match(w.document.querySelector('#sourceStatus').textContent,/已接入数据源：\s*OKX（已更新） · Robinhood（已更新） · Binance（已更新）/);
  }finally{dom.window.close();}
});
test('stock expansion survives polling and search preserves input focus',()=>{
  const {dom,w,data}=createPage('#stock');
  try {
    w.document.querySelector('.x-stock').open=true;
    const input=w.document.querySelector('#xStockSearch');input.focus();input.value='NVDA';input.dispatchEvent(new w.Event('input',{bubbles:true}));
    w.XLayerUI.render(data);
    assert.equal(w.document.querySelector('.x-stock').open,true);
    assert.equal(w.document.activeElement,input);
    assert.equal(input.value,'NVDA');
    assert.equal(w.document.querySelectorAll('.x-stock tbody tr').length,2);
    assert.match(w.document.querySelector('.x-stock tbody').textContent,/来源 OKX/);
  }finally{dom.window.close();}
});
test('detail deep link renders proof and unknown counts without fabricating zero trades',async()=>{
  const {dom,w}=createPage('#detail/196/'+token);
  try {
    await new Promise(resolve=>setTimeout(resolve,10));
    const text=w.document.querySelector('#memeDetail').textContent;
    assert.match(text,/地址已核验/);assert.match(text,/最近成交/);
    assert.match(text,/买卖分项见已采集成交/);
    assert.match(text,/采集排队中/);
    assert.equal(w.document.querySelector('#view-detail').hidden,false);
  }finally{dom.window.close();}
});
test('same-name contracts remain individually navigable when expanded',()=>{
  const {dom,w,data}=createPage('#meme');
  try {
    const twin={...data.xlayer.assets[0],token:'0x'+'e'.repeat(40)};
    data.xlayer.groups[0].members.push(twin);
    w.document.querySelector('[data-x-filter="all"]').click();
    const group=w.document.querySelector('.x-group');assert.ok(group);group.open=true;
    assert.equal(group.querySelectorAll('tbody tr').length,2);
    assert.notEqual(group.querySelectorAll('tbody a')[0].href,group.querySelectorAll('tbody a')[1].href);
    w.XLayerUI.render(data);assert.equal(w.document.querySelector('.x-group').open,true);
  }finally{dom.window.close();}
});
test('language switch rerenders the unified screens and uses Chinese company names',()=>{
  const {dom,w}=createPage('#stock');
  try {
    assert.match(w.document.querySelector('.x-stock summary').textContent,/NVDA/);
    assert.match(w.document.querySelector('.x-stock summary').textContent,/英伟达/);
    w.eval('LANG="en"; document.dispatchEvent(new CustomEvent("languagechange", {detail:{lang:"en"}}));');
    assert.match(w.document.querySelector('#sourceStatus').textContent,/Data sources/);
    assert.match(w.document.querySelector('#okxStocks').textContent,/Stocks → on-chain assets/);
    assert.match(w.document.querySelector('.workspace-nav').textContent,/Live Radar/);
  }finally{dom.window.close();}
});
test('English asset detail translates its interface and known generated states',async()=>{
  const {dom,w}=createPage('#detail/196/'+token);
  try {
    await new Promise(resolve=>setTimeout(resolve,10));
    w.eval('LANG="en"; document.dispatchEvent(new CustomEvent("languagechange", {detail:{lang:"en"}}));');
    await new Promise(resolve=>setTimeout(resolve,10));
    const detail=w.document.querySelector('#memeDetail').textContent;
    assert.match(detail,/Relationship evidence|Verified stock pair/);
    assert.match(detail,/Recent activity/);
    assert.doesNotMatch(detail,/[\p{Script=Han}]/u);
  }finally{dom.window.close();}
});
test('English mode has no Chinese interface copy in each visible unified tab',()=>{
  for (const [page, selector] of [['#live','#xlayerLive'],['#meme','#xlayerMeme'],['#stock','#okxStocks']]) {
    const {dom,w}=createPage(page);
    try {
      w.eval('LANG="en"; document.dispatchEvent(new CustomEvent("languagechange", {detail:{lang:"en"}}));');
      const visible=w.document.querySelector(selector).textContent;
      assert.doesNotMatch(visible,/[\p{Script=Han}]/u, page);
    } finally { dom.window.close(); }
  }
});
test('language button gives immediate feedback and updates the active unified view',async()=>{
  const dom=new JSDOM(readFileSync('public/index.html','utf8'),{url:'http://localhost/#live',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window as any;
  const asset={token,chain:'196',symbol:'RTX',name:'RTX',kind:'candidate',firstSeen:Date.now(),updatedAt:Date.now(),price:0.01,volume24h:1200,txs24h:30,holders:12,liquidity:1000,match:null};
  const relation={id:'r',token,stock,ticker:'NVDA',status:'verified',pool:'0x'+'c'.repeat(40),stockSide:stock,token0:token,token1:stock,checkedAt:Date.now(),block:123,liquidityUsd:1000};
  const data:any={now:Date.now(),assets:[],poolsFound:0,fourMeme:null,attention:null,radar:{tokens:[],trades:[]},leadCandidates:[],relationTypes:[],sectors:[],okx:{status:'ready',updatedAt:Date.now(),tokens:[{chainIndex:'196',tokenContractAddress:stock,tokenSymbol:'NVDAx',stockCode:'NVDA',volume24h:2000}]},xlayer:{coverage:{catalog:1,scanned:1,pools:1},assets:[asset],relations:[relation],signals:[],groups:[{symbol:'RTX',members:[asset]}],metrics:{verifiedAssets:1,verifiedPools:1},sectors:[]}};
  data.unified={...data.xlayer,sources:[{provider:'OKX',status:'ready'}],stockTokens:[{...data.okx.tokens[0],provider:'OKX'}]};
  w.escapeHtml=(s:any)=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  w.echarts={init:()=>({setOption(){},resize(){},dispose(){}})};
  w.fetch=async()=>({ok:true,json:async()=>data});
  try {
    for(const file of ['public/i18n.js','public/app.js','public/xlayer-workspace.js','public/workspace.js']) runInContext(readFileSync(file,'utf8'),dom.getInternalVMContext(),{filename:file});
    await new Promise(resolve=>setTimeout(resolve,30));
    w.document.getElementById('langEn').click();
    assert.equal(w.document.getElementById('langEn').disabled,true);
    assert.match(w.document.getElementById('langFeedback').textContent,/Switching to English/);
    await new Promise(resolve=>setTimeout(resolve,70));
    assert.match(w.document.querySelector('#sourceStatus').textContent,/Data sources/);
    assert.match(w.document.querySelector('.workspace-nav').textContent,/Live Radar/);
    assert.equal(w.document.getElementById('langEn').disabled,false);
  }finally{dom.window.close();}
});
