import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeRobinhoodSnapshot } from '../src/lib/robinhood';

const address = '0x' + 'a'.repeat(40);

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
