import test from 'node:test';
import assert from 'node:assert/strict';
import { groupActivityItems, homeStockCards, metricsForScope, onchainRecordedTrades } from '../src/utils/home-model.js';

test('overview stock cards use published full-universe counts and never infer them from clipped assets', () => {
  const overview={snapshotScope:'overview',assets:[{token:'0xa',chainId:'56'}],hotStocks:[{ticker:'700',assetCount:23,volume24h:1200,volumeKnown:20,volumeTotal:23}],hotStocksByChain:{'56':[{ticker:'700',assetCount:8,volume24h:400,volumeKnown:7,volumeTotal:8}]}};
  assert.equal(homeStockCards(overview)[0].assetCount,23);
  assert.equal(homeStockCards(overview)[0].volume,1200);
  assert.equal(homeStockCards(overview,'56')[0].assetCount,8);
  assert.deepEqual(homeStockCards(overview,'196'),[]);
  assert.deepEqual(homeStockCards({snapshotScope:'overview',stockTokens:[{stockCode:'700'}],assets:overview.assets}),[]);
});

test('scoped KPI fallback uses exact activity windows and cannot manufacture compact-snapshot totals', () => {
  const now=2_000_000_000,asset={chainId:'56',token:'0xa',kind:'candidate',price:1,fieldTimes:{price:now},totalLiquidityUsd:1000,totalLiquidityStatus:'current',totalLiquidityAt:now-1_500_000,firstSeen:now-1000};
  const full={assets:[asset,{...asset}],relations:[]};
  assert.equal(metricsForScope(full,'56',now).active,1);
  assert.equal(metricsForScope(full,'56',now).newAssets,1);
  assert.equal(metricsForScope({...full,snapshotScope:'overview'},'56',now).active,null);
  assert.equal(metricsForScope({metrics:{newAssets24h:29}},'all',now).newAssets,29);
});

test('home onchain feed rejects exchange markets, retains unknown USD only without an amount floor', () => {
  const trades=[{chainId:'56',token:'0xa',venue:'dex',hash:'0x1',volume:100,type:'buy'},
    {chainId:'56',token:'0xb',venue:'dex',hash:'0x2',volume:null,type:'sell'},
    {chainId:'56',token:'0xa',venue:'binance',volume:10000,type:'buy'},
    {chainId:'196',token:'0xa',venue:'dex',volume:400,type:'buy'}];
  assert.equal(onchainRecordedTrades(trades,null,'56',0).length,2);
  assert.equal(onchainRecordedTrades(trades,null,'56',10).length,1);
  assert.equal(onchainRecordedTrades(trades,[{chainId:'56',token:'0xa'}],'all',0).length,1);
});

test('activity groups repeated records by asset and type without merging chains or implying creation', () => {
  const result=groupActivityItems([{chainId:'56',token:'0xA',type:'relation-verified',verifiedAt:10,relation:{pool:'0x1'}},
    {chainId:'56',token:'0xa',type:'relation-verified',verifiedAt:20,relation:{pool:'0x2'}},
    {chainId:'196',token:'0xa',type:'relation-verified',verifiedAt:30},
    {chainId:'56',token:'0xa',kind:'discovered',at:5}]);
  assert.equal(result.length,3);
  const grouped=result.find(row=>row.chainId==='56'&&row.type==='relation-verified');
  assert.equal(grouped.recordCount,2);
  assert.equal(grouped.relation.pool,'0x2');
  assert.equal(grouped.occurredAt,undefined);
});


test('repeated delivery of a feed ID does not inflate the activity record count', () => {
  const item={id:'evt-1',chainId:'56',token:'0xa',type:'relation-verified',verifiedAt:10};
  assert.equal(groupActivityItems([item,item])[0].recordCount,1);
});
