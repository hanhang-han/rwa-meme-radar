import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeRobinhoodSnapshot, robinhoodCatalogueDue } from '../src/lib/robinhood';

const address = '0x' + 'a'.repeat(40);

test('Fast Robinhood price refreshes retain the actual issuer-ratio observation time', () => {
  const at=1_790_261_400_000;
  assert.equal(robinhoodCatalogueDue(null,at),true);
  assert.equal(robinhoodCatalogueDue(at,at+15_000),false);
  assert.equal(robinhoodCatalogueDue(at,at+299_999),false);
  assert.equal(robinhoodCatalogueDue(at,at+300_000),true);
  const catalogue={assets:[{id:'a',tokenSymbol:'TEST',currentMultiplier:1,deployments:[{chainId:4663,contractAddress:address}]}]};
  const before=normalizeRobinhoodSnapshot(catalogue,{quotes:[{tokenSymbol:'TEST',bid:100,ask:102}]},at)[0];
  const after=normalizeRobinhoodSnapshot(catalogue,{quotes:[{tokenSymbol:'TEST',bid:101,ask:103}]},at)[0];
  assert.equal(after.stockPrice,102);
  assert.equal(after.ratioAt,before.ratioAt);
  assert.equal(after.ratioValidUntil,before.ratioValidUntil);
});

test('Robinhood stock tokens use the multiplier and keep equity volume separate', () => {
  const tokens = normalizeRobinhoodSnapshot({ assets: [{
    id: 'asset-1', tokenSymbol: 'TEST', tokenName: 'Test • Stock Token',
    currentMultiplier: '0.25', status: 'ASSET_STATUS_ACTIVE',
    deployments: [{ chainId: 4663, contractAddress: address }, { chainId: 1, contractAddress: '0x' + 'b'.repeat(40) }],
  }] }, { quotes: [{ tokenSymbol: 'TEST', bid: '100', ask: '102', dailyTradingVolume: '987654', isTradingHalt: false, generatedAt: '2026-09-10T00:00:00.000Z' }] });
  assert.equal(tokens.length, 1);
  assert.equal(tokens[0].price, 25.25);
  assert.equal(tokens[0].stockPrice, 101);
  assert.equal(tokens[0].volume24h, null);
  assert.equal(tokens[0].dailyTradingVolume, 987654);
  assert.equal(tokens[0].chainIndex, '4663');
});

test('Robinhood missing quote fields remain unknown instead of becoming zero', () => {
  const tokens = normalizeRobinhoodSnapshot({ assets: [{
    tokenSymbol: 'TEST', tokenName: 'Test', currentMultiplier: '1', deployments: [{ chainId: '4663', contractAddress: address }],
  }] }, { quotes: [{ tokenSymbol: 'TEST', bid: '', ask: null }] });
  assert.equal(tokens[0].stockPrice, null);
  assert.equal(tokens[0].price, null);
  assert.equal(tokens[0].dailyTradingVolume, null);
});

test('Robinhood ratio evidence uses the real catalogue deployment and bounded receipt time', () => {
  const received=1_790_261_400_000;
  const catalogue={assets:[{id:'equity-a',tokenSymbol:'TEST',currentMultiplier:'0.25',deployments:[{chainId:4663,contractAddress:address}]}]};
  const prices={quotes:[{tokenSymbol:'TEST',bid:'100',ask:'102',generatedAt:'2026-09-10T00:00:00Z'}]};
  const token=normalizeRobinhoodSnapshot(catalogue,prices,received)[0];
  assert.equal(token.ratioVerified,true);
  assert.equal(token.ratioAt,received);
  assert.notEqual(token.ratioAt,token.quoteAt);
  assert.equal(token.ratioValidUntil,received+600000);
  assert.equal(token.ratioSource,'https://api.robinhood.com/rhj/assets');
  assert.deepEqual(token.ratioEvidence,{sourceAssetId:'equity-a',chainId:'4663',tokenContractAddress:address,stockCode:'TEST',currentMultiplier:.25,observedAt:received});
  assert.equal(normalizeRobinhoodSnapshot(catalogue,prices,received+300000)[0].ratioVersion,token.ratioVersion);
  const changed=normalizeRobinhoodSnapshot({assets:[{...catalogue.assets[0],currentMultiplier:'0.5'}]},prices,received+300000)[0];
  assert.notEqual(changed.ratioVersion,token.ratioVersion);
  // Ratio evidence exists independently of an equity price response.
  const noPrice=normalizeRobinhoodSnapshot(catalogue,{quotes:[{tokenSymbol:'TEST'}]},received)[0];
  assert.equal(noPrice.ratioVerified,true);
  assert.equal(noPrice.stockPrice,null);
});

test('Robinhood invalid identity, multiplier and conflicting deployments cannot be certified', () => {
  const make=(extras:any)=>normalizeRobinhoodSnapshot({assets:[{id:'equity-a',tokenSymbol:'TEST',currentMultiplier:1,deployments:[{chainId:4663,contractAddress:address}],...extras}]},{quotes:[{tokenSymbol:'TEST'}]},1_790_261_400_000)[0];
  for(const multiplier of [0,-1,'Infinity',null,''])assert.equal(make({currentMultiplier:multiplier}).ratioVerified,false);
  assert.equal(make({id:''}).ratioVerified,false);
  assert.equal(make({id:undefined}).ratioVersion,null);
  const conflict=normalizeRobinhoodSnapshot({assets:[
    {id:'a',tokenSymbol:'TEST',currentMultiplier:1,deployments:[{chainId:4663,contractAddress:address}]},
    {id:'b',tokenSymbol:'TEST',currentMultiplier:2,deployments:[{chainId:4663,contractAddress:address}]},
  ]},{quotes:[{tokenSymbol:'TEST'}]},1_790_261_400_000)[0];
  assert.equal(conflict.ratioVerified,false);
  assert.equal(conflict.ratioReason,'conflicting-issuer-deployments');
  assert.equal(conflict.ratioEvidence,null);
});
