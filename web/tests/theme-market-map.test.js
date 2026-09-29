import test from 'node:test';
import assert from 'node:assert/strict';

import {
  bubbleDiameter, bubbleTone, buildNameClues, buildThemeMap, currentUsdVolume,
  fallbackRelationEvents, fallbackThemeMap, metricStatus,
  themeEventTime, visibleRecentObservations, visibleThemeEvents,
} from '../src/utils/theme-map-model.js';

const NOW = 1_800_000_000_000;
const official = {verificationStatus:'official',eligibleForPair:true};
const stock = {chainId:'196',tokenContractAddress:'0xstock',stockCode:'NVDA',
  issuerIdentity:official,stockIdentity:{code:'NVDA',nameZh:'英伟达',nameEn:'Nvidia'}};
function relation(token, extra = {}) {
  return {id:`r-${token}`,chainId:'196',token,stock:'0xstock',pool:`pool-${token}`,
    status:'verified',level:'A',evidenceStatus:'qualified',
    stockIdentity:{...official,ticker:'NVDA'},sideIdentity:official,
    checkedAt:NOW-500,discoveredAt:NOW-2000,poolCreatedAt:NOW-100000,
    ...extra};
}
function asset(token, extra = {}) {
  return {chainId:'196',token,kind:'candidate',symbol:'MEME',name:'Same name',
    price:0.01,priceCurrency:'USD',volume24h:100,volumeCurrency:'USD',change24h:5,
    fieldTimes:{price:NOW-1000,volume24h:NOW-1000,change24h:NOW-1000},
    fieldScopes:{price:'token',volume24h:'token-aggregate',change24h:'token'},
    fieldSources:{price:'OKX',volume24h:'OKX',change24h:'OKX'},...extra};
}

test('map keeps chain and contract identities separate and expires only the affected metric', () => {
  const current = {key:'196:0xa',chainId:'196',token:'0xa',primaryTicker:'NVDA',
    volume24h:{value:100,currency:'USD',status:'current',observedAt:NOW-1000},
    change24h:{value:3,status:'current',observedAt:NOW-1000}};
  const otherChain = {...current,key:'56:0xa',chainId:'56',volume24h:{value:500,currency:'USD',status:'current',observedAt:NOW-1000}};
  const dto = {themes:[{ticker:'NVDA',breadth:{rising:2,total:2}}],bubbles:[current,otherChain]};
  assert.equal(buildThemeMap(dto,'all',{},NOW).rows.length,2);
  assert.equal(buildThemeMap(dto,'196',{},NOW).rows.length,1);
  assert.equal(bubbleDiameter(otherChain,500,NOW),112);
  assert.equal(bubbleTone(current,NOW),'up');
  assert.equal(currentUsdVolume(current,NOW+901_000),null);
  assert.equal(bubbleTone(current,NOW+901_000),'unknown');
  assert.equal(metricStatus(current.change24h,NOW+901_000),'stale');
});

test('legacy full snapshot forms bubbles only from current official A relations with comparable fields', () => {
  const unsafe = relation('0xunverified',{id:'bad',level:'B',pool:'pool-bad'});
  const unified = {snapshotScope:'full',stockTokens:[stock],
    relations:[relation('0xa'),relation('0xb'),unsafe],
    assets:[asset('0xa'),asset('0xb',{volume24h:9,volumeCurrency:null}),asset('0xunverified')]};
  const map = fallbackThemeMap(unified,NOW);
  assert.equal(map.totalAssets,2);
  assert.equal(map.bubbles.length,2);
  assert.deepEqual(map.bubbles.map(row => row.key).sort(),['196:0xa','196:0xb']);
  assert.equal(map.bubbles.find(row => row.key==='196:0xa').volume24h.value,100);
  assert.equal(map.bubbles.find(row => row.key==='196:0xb').volume24h.status,'unsupported-currency');
  assert.equal(fallbackThemeMap({...unified,snapshotScope:'overview'},NOW),null);
});

test('legacy events match a persisted verification to the current A pool and separate three timestamps', () => {
  const active = relation('0xa');
  const indexedOnly = relation('0xb',{id:'second',discoveredAt:NOW-4000});
  const unified = {stockTokens:[stock],relations:[active,indexedOnly],assets:[asset('0xa'),asset('0xb')]};
  const feed = [
    {id:'verified-event',kind:'verified',chainId:'196',asset:'196:0xa',pool:active.pool,t:NOW-1000},
    {id:'wrong-pool',kind:'verified',chainId:'196',asset:'0xa',pool:'another-pool',t:NOW-500},
    {id:'invalidated',kind:'invalidated',chainId:'196',asset:'0xb',pool:indexedOnly.pool,t:NOW-300},
  ];
  const changes = fallbackRelationEvents(feed,unified,NOW);
  assert.equal(changes.items.length,2);
  const verified = changes.items.find(item => item.type==='relation-verified');
  assert.equal(verified.verifiedAt,NOW-1000);
  assert.equal(verified.discoveredAt,NOW-2000);
  assert.equal(verified.occurredAt,NOW-100000);
  assert.equal(themeEventTime(verified),NOW-1000);
  assert.equal(changes.items.find(item => item.type==='relation-observed').verifiedAt,null);
  assert.equal(visibleThemeEvents(changes,'196',NOW).length,2);
  assert.equal(visibleThemeEvents(changes,'56',NOW).length,0);
  assert.equal(visibleThemeEvents({items:[{type:'activity-spike',chainId:'196',window:{to:NOW}}]},'all',NOW).length,0);
});

test('B name clues stay out of verified bubbles and include the exact matching evidence', () => {
  const clue = asset('0xclue',{relationLevel:'B',match:{level:'B',ticker:'NVDA',matchType:'name',
    keyword:'NVDA',ruleVersion:'stock-name-keywords-v1',evidenceStatus:'name-only'}});
  const unified = {snapshotScope:'full',stockTokens:[stock],relations:[relation('0xa')],assets:[asset('0xa'),clue]};
  assert.deepEqual(buildNameClues(unified).map(row => [row.key,row.ticker,row.keyword]),[['196:0xclue','NVDA','NVDA']]);
  assert.equal(fallbackThemeMap(unified,NOW).bubbles.length,1);
  assert.equal(buildNameClues({...unified,assets:[{...clue,match:{...clue.match,evidenceStatus:'unverified'}}]}).length,0);
});

test('recent observations keep discoveries and pool sightings separate from grade-A changes', () => {
  const clue = asset('0xclue',{relationLevel:'B',match:{level:'B',ticker:'NVDA',matchType:'symbol',
    keyword:'NVDA',evidenceStatus:'name-only'}});
  const unified = {assets:[clue],relations:[]};
  const feed = [
    {id:'discovery',kind:'discovered',chainId:'196',asset:'196:0xclue',match:clue.match,t:NOW-500},
    {id:'pool',kind:'pair-observed',chainId:'196',asset:'0xclue',pool:'0xpool',ticker:'NVDA',t:NOW-300},
    {id:'verified',kind:'verified',chainId:'196',asset:'0xclue',pool:'0xpool',t:NOW-100},
  ];
  const rows = visibleRecentObservations(feed,unified,'all',NOW);
  assert.deepEqual(rows.map(row => row.kind),['pair-observed','discovered']);
  assert.equal(rows[0].currentlyVerified,false);
  assert.equal(rows[1].keyword,'NVDA');
  assert.equal(visibleRecentObservations(feed,unified,'56',NOW).length,0);
});
