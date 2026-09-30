import test from 'node:test';
import assert from 'node:assert/strict';
import {marketCatalogReady,memeFilterValues,memeQuoteMatches} from '../src/utils/meme-filter-model.js';
const now=2_000_000_000;
const current={kind:'candidate',token:'0xa',price:1,fieldTimes:{price:now},volume24h:10,totalLiquidityUsd:1000,totalLiquidityStatus:'current',totalLiquidityAt:now};

test('overview previews are not ready for list totals or stock contribution denominators',()=>{
 assert.equal(marketCatalogReady(null),false);
 assert.equal(marketCatalogReady({unified:{snapshotScope:'overview',assets:[current]}}),false);
 assert.equal(marketCatalogReady({unified:{snapshotScope:'market',assets:[]}}),true);
 assert.equal(marketCatalogReady({unified:{snapshotScope:'full',assets:[]}}),true);
 assert.equal(marketCatalogReady({unified:{assets:[]}}),true);
});
test('discovery entry defaults include assets with no quote but respect explicit filter edits',()=>{
 const defaults=memeFilterValues({new:'24h'});
 assert.equal(defaults.relation,'all');assert.equal(defaults.fresh,'0');assert.equal(defaults.showMissing,true);
 assert.equal(memeQuoteMatches({token:'0xa'},defaults,now),true);
 const edited=memeFilterValues({new:'24h',rel:'B',fresh:'1',minLiq:'10000',risk:'hide',showMissing:'0'});
 assert.equal(edited.newAssets,true);assert.equal(edited.relation,'B');assert.equal(edited.fresh,'1');assert.equal(edited.minLiquidity,'10000');assert.equal(edited.hideRisk,true);assert.equal(edited.showMissing,false);
 assert.equal(memeQuoteMatches({token:'0xa'},edited,now),false);
});
test('including incomplete data never bypasses a fresh price selection',()=>{
 const filter=memeFilterValues({showMissing:'1',fresh:'1'});
 assert.equal(memeQuoteMatches({...current,volume24h:null},filter,now),true);
 assert.equal(memeQuoteMatches({...current,volume24h:null,fieldTimes:{price:now-900001}},filter,now),false);
 assert.equal(memeQuoteMatches({...current,price:null},filter,now),false);
 assert.equal(memeQuoteMatches({...current,price:0},filter,now),false);
});
test('without an explicit fresh price filter incomplete historical data is visible only when requested',()=>{
 const asset={...current,volume24h:null,fieldTimes:{price:now-900001}};
 assert.equal(memeQuoteMatches(asset,memeFilterValues({fresh:'0'}),now),false);
 assert.equal(memeQuoteMatches(asset,memeFilterValues({fresh:'0',showMissing:'1'}),now),true);
});
test('active KPI still has its exact independent qualification rather than requiring volume',()=>{
 const filter=memeFilterValues({qualified:'1',new:'24h',fresh:'0'});
 assert.equal(filter.relation,'all');assert.equal(filter.fresh,'1');assert.equal(filter.minLiquidity,'1000');
 assert.equal(memeQuoteMatches({...current,volume24h:null},filter,now),true);
 assert.equal(memeQuoteMatches({...current,totalLiquidityAt:now-1800001},filter,now),false);
});
