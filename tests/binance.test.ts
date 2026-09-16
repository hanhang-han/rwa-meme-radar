import { test } from 'node:test';
import assert from 'node:assert/strict';
import { BSTOCKS, normalizeBinanceBstocks } from '../src/lib/bstocks';

test('Binance bStocks enter the common catalogue with exchange-scoped turnover', () => {
  const first = BSTOCKS[0];
  const assets = BSTOCKS.map(asset => ({ assetCode: asset.symbol, assetName: asset.name, underlyingEquitySymbol: asset.underlying, multiplier: 1, multiplierValid: true }));
  const tokens = normalizeBinanceBstocks(assets, new Map([[first.symbol, { price: 200, chg24h: 1.5, volume24h: 12345, trades24h: 17 }]]), 100);
  assert.equal(tokens.length, BSTOCKS.length);
  assert.equal(tokens[0].tokenContractAddress, first.addr);
  assert.equal(tokens[0].stockCode, first.underlying);
  assert.equal(tokens[0].volume24h, 12345);
  assert.equal(tokens[0].volumeScope, 'exchange');
  assert.equal(tokens[0].exchangeTrades24h, 17);
  assert.equal(tokens[1].price, null);
});
