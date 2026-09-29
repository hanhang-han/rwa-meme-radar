import test from 'node:test';
import assert from 'node:assert/strict';
import { createPinia, setActivePinia } from 'pinia';
import { applyQuote, assetKey, pricePrecision, quoteKey } from '../src/utils/realtime.js';
import { useDashboardStore } from '../src/stores/dashboard.js';
import { relKey, useFeedStore } from '../src/stores/feed.js';
import { appendSseChunk, handleStreamEvent, parseSseFrame } from '../src/composables/useStream.js';

test('quote reducer rejects an older market observation and applies a newer complete quote', () => {
  const row = { price: 20, change24h: 1, fieldTimes: { price: 200 }, provider: 'OKX' };
  assert.equal(applyQuote(row, { price: 10, change24h: -5, marketAt: 100, provider: 'OKX' }), false);
  assert.equal(row.price, 20);
  assert.equal(row.change24h, 1);

  assert.equal(applyQuote(row, { price: 21, change24h: 2, volume24h: 300, marketAt: 201, provider: 'OKX' }), true);
  assert.deepEqual({ price: row.price, change24h: row.change24h, volume24h: row.volume24h, at: row.fieldTimes.price },
    { price: 21, change24h: 2, volume24h: 300, at: 201 });
});

test('asset and quote identities include chain and market scope', () => {
  assert.notEqual(assetKey('56', '0xAbC'), assetKey('196', '0xabc'));
  assert.notEqual(
    quoteKey({ chainId: '56', token: '0xAbC', venue: 'binance', quoteType: 'exchange' }),
    quoteKey({ chainId: '56', token: '0xabc', venue: 'dex', quoteType: 'dex' }),
  );
});

test('dashboard never lets an old push replace a newer snapshot', () => {
  setActivePinia(createPinia());
  const store = useDashboardStore();
  store.snapshot = { unified: { assets: [{ chainId: '56', token: '0xabc', price: 20, fieldTimes: { price: 200 } }], stockTokens: [] } };
  assert.equal(store.applyPrice('56', '0xABC', 10, 100), false);
  assert.equal(store.assets[0].price, 20);
  assert.equal(store.assets[0].fieldTimes.price, 200);
});

test('canonical relationship event id deduplicates HTTP and SSE shapes', () => {
  setActivePinia(createPinia());
  const store = useFeedStore();
  const id = '196:196:pool:token:stock:verified:200';
  const pushed = { id, chainId: '196', relation: { id: '196:pool:token:stock' }, kind: 'verified', at: 200 };
  const fetched = { id, chainId: '196', asset: '0xtoken', kind: 'verified', t: 200 };
  assert.equal(relKey(pushed), relKey(fetched));
  store.appendRelationship(pushed);
  store.appendRelationship(fetched);
  assert.equal(store.relationships.length, 1);
});

test('SSE parser accepts CRLF frames and preserves every sequenced event id', () => {
  assert.deepEqual(
    parseSseFrame('id: 42\r\nevent: stock-quote\r\ndata: {"price":1}\r'),
    { id: '42', event: 'stock-quote', data: '{"price":1}' },
  );
  let buffered = appendSseChunk('', 'id: 43\r');
  buffered = appendSseChunk(buffered, '\nevent: price\r\ndata: {"price":2}\r\n\r\n');
  const boundary = buffered.indexOf('\n\n');
  assert.ok(boundary > 0);
  assert.deepEqual(
    parseSseFrame(buffered.slice(0, boundary)),
    { id: '43', event: 'price', data: '{"price":2}' },
  );
});

test('micro prices retain non-zero chart precision', () => {
  const price = 0.00000000123;
  const precision = pricePrecision(price);
  assert.ok(precision > 8);
  assert.notEqual(price.toFixed(precision), '0.00000000');
});

test('invalid sequenced business events are rejected instead of being acknowledged', async () => {
  setActivePinia(createPinia());
  assert.equal(await handleStreamEvent('relationship', JSON.stringify({ id: '43', chainId: '196' })), false);
  assert.equal(await handleStreamEvent('trade', JSON.stringify({ token: '0xabc', fresh: [{ price: 1 }] })), false);
  assert.equal(await handleStreamEvent('future-event', JSON.stringify({ value: 1 })), false);
});
