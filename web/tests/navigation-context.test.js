import test from 'node:test';
import assert from 'node:assert/strict';
import { createMemoryHistory, createRouter } from 'vue-router';
import { assetNavigationLink, themeNavigationLink, pageNavigationLink, navigationSource,
  navigationParent, navigationAncestors, safeInternalBack, isNavigationReturn, returnNavigation } from '../src/utils/navigation-context.js';
import { relationshipAssetLink } from '../src/utils/stock-navigation.js';
import { clearPageState, readPageState, writePageState } from '../src/utils/page-navigation-state.js';
const token='0x'+'a'.repeat(40), stock='0x'+'b'.repeat(40), pool='0x'+'c'.repeat(40);
const makeRouter=()=>createRouter({history:createMemoryHistory(),routes:['/live','/stock','/stock/:ticker','/meme','/watch','/me','/developer','/events','/asset/:chain/:address','/pair/:chain/:address'].map(path=>({path,component:{render:()=>null}}))});

test('all five main tabs keep their origin through theme, asset and sibling, returning to exact parent',async()=>{
  for(const source of ['live','stock','meme','watch','me']){
    const router=makeRouter();
    await router.push({path:`/${source}`,query:{chain:'all',q:'腾讯',page:'3',view:'pool'}});
    const original=router.currentRoute.value;
    await router.push(themeNavigationLink('700',{route:original,scope:'all'}));
    const theme=router.currentRoute.value;
    await router.push(assetNavigationLink({chainId:'56',token},{route:theme,scope:'all',pool}));
    assert.equal(navigationSource(router.currentRoute.value),source);
    assert.equal(router.currentRoute.value.query.chain,'all');
    assert.equal(router.currentRoute.value.query.pool,pool);
    assert.equal(navigationParent(router.currentRoute.value),theme.fullPath);
    await router.push(relationshipAssetLink({chainId:'56',token,stock},'stock',router.currentRoute.value,'all',pool));
    assert.equal(navigationSource(router.currentRoute.value),source);
    assert.equal(navigationParent(router.currentRoute.value),theme.fullPath);
    assert.deepEqual(navigationAncestors(router.currentRoute.value).map(row=>row.path),[`/${source}`,...(source==='stock'?[]:[]),'/stock/700']);
    assert.equal(isNavigationReturn(theme,router.currentRoute.value),true);
    await router.push(navigationParent(router.currentRoute.value));
    await router.push(navigationParent(router.currentRoute.value));
    assert.equal(router.currentRoute.value.fullPath,original.fullPath);
  }
});

test('direct roots clear detail context; child pages keep it; deep links and old events alias have valid parents',async()=>{
  const router=makeRouter();await router.push('/watch?chain=56');
  await router.push(pageNavigationLink('/events',{route:router.currentRoute.value,scope:'56'}));
  assert.equal(navigationSource(router.currentRoute.value),'watch');
  await router.push(assetNavigationLink({chainId:'196',token},{route:router.currentRoute.value,scope:'56',tab:'trades',market:'binance:alpha_42'}));
  assert.equal(navigationSource(router.currentRoute.value),'watch');
  assert.equal(router.currentRoute.value.query.market,'binance:alpha_42');
  const root=pageNavigationLink('/meme',{route:router.currentRoute.value,scope:'56',query:{from:'watch',back:'/watch',view:'pool'}});
  assert.deepEqual(root,{path:'/meme',query:{chain:'56',view:'pool'}});
  assert.equal(navigationSource({path:`/asset/196/${token}`,query:{from:'events'}}),'watch');
  assert.deepEqual(navigationParent({path:'/developer',query:{}}),{path:'/me',query:{chain:'all'}});
  assert.deepEqual(navigationParent({path:`/asset/56/${token}`,query:{back:'https://evil.test'}}),{path:'/meme',query:{chain:'all'}});
  assert.equal(assetNavigationLink({chainId:'56',token},{pool,market:'binance:42'}).query.market,undefined);
});

test('return links permit account, API and real pool routes but reject malformed or external addresses',()=>{
  for(const path of ['/me?chain=all','/developer?back=%2Fme',`/pair/196/${stock}?pool=${pool}`,'/status'])assert.equal(safeInternalBack(path),path);
  for(const path of ['//evil.test','/stock/..','/me#x','/asset/56/abc','/pair/196/abc','/developer%2f..','/me\\x'])assert.equal(safeInternalBack(path),null);
});

test('a repeated same-page entry keeps its original parent and local selection',()=>{
  const route={path:'/developer',fullPath:'/developer?chain=all&from=live&back=%2Fstock%2F700',query:{chain:'all',from:'live',back:'/stock/700?chain=all&from=live&back=%2Flive',panel:'docs'}};
  const target=pageNavigationLink('/developer',{route,scope:'all'});
  assert.equal(target.query.back,route.query.back);assert.equal(target.query.panel,'docs');assert.equal(target.query.from,'live');
});

test('explicit return uses browser back to its parent and never pushes another child-parent history cycle',async()=>{
  const router=makeRouter();await router.push('/meme?chain=all&page=3');const parent=router.currentRoute.value.fullPath;
  await router.push(assetNavigationLink({chainId:'56',token},{route:router.currentRoute.value,scope:'all'}));
  const returned=new Promise(resolve=>{const stop=router.afterEach(to=>{if(to.fullPath===parent){stop();resolve();}});});
  returnNavigation(router,parent,parent);await returned;assert.equal(router.currentRoute.value.fullPath,parent);
  await router.push('/stock/700?chain=all&from=meme');
  await returnNavigation(router,parent,'/stock?chain=all');assert.equal(router.currentRoute.value.fullPath,parent);
  const previous=new Promise(resolve=>{const stop=router.afterEach(to=>{stop();resolve(to.fullPath);});});router.back();
  assert.equal(await previous,parent);
});

test('page UI state is copied, size bounded and refuses nested credentials',()=>{
  const state={page:3,expanded:['CT'],address:token};
  assert.equal(writePageState('test:navigation',state),true);state.page=7;
  const restored=readPageState('test:navigation');assert.equal(restored.page,3);restored.page=9;
  assert.equal(readPageState('test:navigation').page,3);
  assert.equal(writePageState('test:secret',{profile:{apiKey:'private'}}),false);
  assert.equal(writePageState('test:large',{text:'x'.repeat(270000)}),false);
  for(let n=0;n<40;n++)writePageState(`test:bounded:${n}`,{page:n});
  assert.equal(readPageState('test:bounded:0'),null);assert.equal(readPageState('test:bounded:39').page,39);
  clearPageState('test:bounded:39');assert.equal(readPageState('test:bounded:39'),null);
});
