import test from 'node:test';
import assert from 'node:assert/strict';
import { buildStockTheme } from '../src/utils/stock-theme-model.js';
import { canShowStockVolumeDistribution, currentStockTheme, recordedPoolReason, stockThemePresentation, stockThemeSnapshotTime } from '../src/utils/stock-theme-presentation.js';

const now=1_800_000_000_000, address=c=>'0x'+c.repeat(40);
const asset=(c,volume=1000)=>({chainId:'56',token:address(c),symbol:c,kind:'candidate',volume24h:volume,
  volumeCurrency:'USD',fieldScopes:{volume24h:'token-aggregate'},fieldTimes:{volume24h:now}});
const relation=(token,pool,volume,extra={})=>({chainId:'56',token:address(token),stock:address('f'),ticker:'700',level:'A',status:'verified',pool:address(pool),liquidityUsd:2000,liquidityAt:now,
  poolMarket:{scope:'pool:'+address(pool),provider:'DexScreener',volumeCurrency:'USD',volume24h:volume,updatedAt:now-1000},...extra});
function model(assets,relations){return currentStockTheme(buildStockTheme({ticker:'700',assets,relations,now}),now);}
const metrics=contributions=>({at:now,contributions:contributions.map(([c,value])=>({chainId:'56',token:address(c),volume24hUsd:value,share:.999}))});

test('stock theme shows direct pool and all-market volume separately without inheriting backend share',()=>{
  const theme=model([asset('a',9000),asset('b',5000)],[relation('a','c',300),relation('b','d',100)]);
  const result=stockThemePresentation(theme,metrics([['a',300],['b',100]]),now);
  assert.equal(result.poolTotal,400);assert.equal(result.rows[0].assetVolume,9000);
  assert.equal(result.rows[0].poolVolume,300);assert.equal(result.rows[0].poolShare,.75);
  assert.equal(result.rows[1].poolShare,.25);assert.equal(result.poolObservedAt,now-1000);
});

test('missing paired-pool volume never falls back to nonzero asset volume; valid zero stays zero',()=>{
  const theme=model([asset('a'),asset('b')],[relation('a','c',0),relation('b','d',null)]);
  const result=stockThemePresentation(theme,metrics([['a',0]]),now);
  assert.equal(result.poolTotal,0);assert.equal(result.knownMemes,1);assert.equal(result.pairedMemes,2);
  assert.equal(result.rows[0].poolVolume,0);assert.equal(result.rows[0].poolShare,null);
  assert.equal(result.rows[1].poolVolume,null);assert.equal(result.rows[1].assetVolume,1000);
  assert.equal(result.rows[0].poolKnownCount,1);assert.equal(result.rows[1].poolKnownCount,0);
});

test('multiple pools sum once per Meme while cross-chain identities remain separate',()=>{
  const first=relation('a','c',70), second=relation('a','d',30), other={...relation('a','e',50),chainId:'196'};
  const theme=model([asset('a'),{...asset('a'),chainId:'196'}],[first,second,{...first},other]);
  const result=stockThemePresentation(theme,{at:now,contributions:[{chainId:'56',token:address('a'),volume24hUsd:100},{chainId:'196',token:address('a'),volume24hUsd:50}]},now);
  assert.equal(result.poolTotal,150);assert.equal(result.rows.length,2);
  assert.equal(result.rows.find(row=>row.chainId==='56').poolKnownCount,2);
  assert.equal(result.rows.find(row=>row.chainId==='196').poolVolume,50);
});

test('name matches are separate and cannot enter the direct paired-pool denominator',()=>{
  const named={...asset('b',50000),match:{level:'B',ticker:'700',evidenceStatus:'name-only'}};
  const theme=model([asset('a'),named],[relation('a','c',100)]);
  const result=stockThemePresentation(theme,metrics([['a',100],['b',9999]]),now);
  assert.equal(result.poolTotal,100);assert.equal(theme.nameCount,1);
  assert.equal(result.rows.find(row=>row.level==='B').assetVolume,50000);
  assert.equal(result.rows.find(row=>row.level==='B').poolVolume,null);
  assert.equal(result.contributions.length,1);
});

test('expired and thin A pools become recorded evidence instead of active table rows',()=>{
  const theme=model([asset('a'),asset('b'),asset('c')],[relation('a','d',10),relation('b','e',20,{liquidityAt:now-900001}),relation('c','f',30,{liquidityUsd:999})]);
  assert.equal(theme.pairedCount,1);assert.equal(theme.pools.length,1);
  assert.equal(theme.recordedPools.length,2);assert.equal(theme.recordedPools[0].detailAvailable,true);
  assert.equal(stockThemePresentation(theme,metrics([['a',10],['b',20],['c',30]]),now).poolTotal,10);
});

test('cache aging, wrong currency and mismatched pool scope withhold stale contributions',()=>{
  const expired=relation('a','c',100,{poolMarket:{scope:'pool:'+address('c'),provider:'DexScreener',volume24h:100,updatedAt:now-900001}});
  const eur=relation('b','d',200,{poolMarket:{scope:'pool:'+address('d'),provider:'DexScreener',volumeCurrency:'EUR',volume24h:200,updatedAt:now}});
  const mismatch=relation('c','e',300,{poolMarket:{scope:'token',provider:'DexScreener',volumeCurrency:'USD',volume24h:300,updatedAt:now}});
  const theme=model([asset('a'),asset('b'),asset('c')],[expired,eur,mismatch]);
  const result=stockThemePresentation(theme,metrics([['a',100],['b',200],['c',300]]),now);
  assert.equal(result.poolTotal,null);assert.equal(result.contributions.length,0);
  assert.ok(result.rows.every(row=>row.poolVolume===null));
  const current=model([asset('a')],[relation('a','c',100)]);
  assert.equal(stockThemePresentation(current,{...metrics([['a',100]]),at:now-900001},now).poolTotal,null);
});

test('snapshot time never substitutes request or metrics calculation time',()=>{
  assert.equal(stockThemeSnapshotTime({now,themeMetrics:{at:now}}),null);
  assert.equal(stockThemeSnapshotTime({snapshotAt:now-10000,now}),now-10000);
});

test('an explicit currency conflict or missing provider never counts as usable USD pool volume',()=>{
  const conflict=relation('a','c',10);conflict.poolMarket.currency='EUR';
  const noProvider=relation('b','d',20);delete noProvider.poolMarket.provider;
  const theme=model([asset('a'),asset('b')],[conflict,noProvider]);
  const result=stockThemePresentation(theme,metrics([['a',10],['b',20]]),now);
  assert.equal(result.poolTotal,null);assert.equal(result.contributions.length,0);
  assert.ok(result.rows.every(row=>row.poolKnownCount===0&&row.poolObservedAt===null));
});

test('latest raw observations cannot revive an older qualified pool or retain its old volume',()=>{
  const old=relation('a','c',10,{checkedAt:now-1000});
  const invalid={...old,checkedAt:now,status:'unverified',level:'B'};
  const legacy=buildStockTheme({ticker:'700',assets:[asset('a')],relations:[old,invalid],now});
  const hidden=currentStockTheme(legacy,now,[old,invalid]);
  assert.equal(hidden.pairedCount,0);assert.equal(hidden.pools.length,0);
  const updated={...old,poolMarket:{...old.poolMarket,volume24h:30,updatedAt:now}};
  const revised=currentStockTheme(legacy,now,[old,updated]);
  assert.equal(stockThemePresentation(revised,metrics([['a',30]]),now).poolTotal,30);
});

test('recorded pool reasons distinguish missing valuation, unknown time and actual expiry',()=>{
  assert.equal(recordedPoolReason({evidenceStatus:'liquidity-unknown'},now),'liquidity-unknown');
  assert.equal(recordedPoolReason({liquidityUsd:null,liquidityAt:now-900001},now),'liquidity-unknown');
  assert.equal(recordedPoolReason({liquidityUsd:2000},now),'liquidity-time-unknown');
  assert.equal(recordedPoolReason({liquidityUsd:2000,liquidityAt:now+60000},now),'liquidity-time-unknown');
  assert.equal(recordedPoolReason({liquidityUsd:2000,liquidityAt:now-900001},now),'liquidity-expired');
  assert.equal(recordedPoolReason({liquidityUsd:999,liquidityAt:now},now),'liquidity-below-threshold');
  assert.equal(recordedPoolReason({evidenceStatus:'issuer-deployment-unverified',liquidityUsd:null},now),'issuer-unverified');
});

test('distribution needs at least two known contributors and a positive total',()=>{
  assert.equal(canShowStockVolumeDistribution({contributions:[{}],poolTotal:300}),false);
  assert.equal(canShowStockVolumeDistribution({contributions:[{},{}],poolTotal:0}),false);
  assert.equal(canShowStockVolumeDistribution({contributions:[{},{}],poolTotal:null}),false);
  assert.equal(canShowStockVolumeDistribution({contributions:[{},{}],poolTotal:300}),true);
});
