import test from 'node:test';
import assert from 'node:assert/strict';
import { stockDirectorySignals } from '../src/utils/stock-signal-presentation.js';

test('directory signals preserve real zero volume and distinguish missing amounts and coverage', () => {
  const card = { stock: { price: 10, priceCurrency: 'USD', change24h: 0 },
    theme: { pairedCount: 0, nameCount: 2, volume: { value: 0, known: 1, total: 1 }, poolCoverage: { recorded: 1 } } };
  assert.deepEqual(stockDirectorySignals(card), { price: 10, change24h: 0, pairedCount: 0, nameCount: 2,
    poolVolume: 0, poolTotal: 1, poolKnown: 1, poolRecorded: 1, ratio7d: null });
  assert.deepEqual(stockDirectorySignals({}), { price: null, change24h: null, pairedCount: null, nameCount: null,
    poolVolume: null, poolTotal: null, poolKnown: null, poolRecorded: null, ratio7d: null });
});

test('invalid numbers and impossible coverage never become usable directory signals', () => {
  for (const value of [null, '', '0', NaN, Infinity, -1]) {
    const result = stockDirectorySignals({ stock: { price: value, priceCurrency: 'USD' },
      theme: { pairedCount: value, nameCount: value, volume: { value, known: value, total: value } } });
    assert.equal(result.price, null); assert.equal(result.poolVolume, null);
    assert.equal(result.pairedCount, null); assert.equal(result.poolKnown, null); assert.equal(result.poolTotal, null);
  }
  assert.equal(stockDirectorySignals({ stock: { price: 10, priceCurrency: '' } }).price, null);
  assert.equal(stockDirectorySignals({ theme: { volume: { known: 2, total: 1 } } }).poolKnown, null);
  assert.equal(stockDirectorySignals({ stock: { change24h: -3 }, volumeRatio7d: { value: 0 } }).change24h, -3);
  assert.equal(stockDirectorySignals({ volumeRatio7d: { value: 0 } }).ratio7d, 0);
});
