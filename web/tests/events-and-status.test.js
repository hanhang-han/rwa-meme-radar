import test from 'node:test';
import assert from 'node:assert/strict';

import { getEvents } from '../src/api/client.js';
import {
  applyEventPage,
  eventFromRelationship,
  eventKey,
  mergeEventItems,
  normalizeEventAddress,
} from '../src/utils/event-records.js';
import { quoteFreshness } from '../src/utils/quote-status.js';
import { relationMatchesAsset, relationMatchesStock } from '../src/utils/relations.js';

test('event pages advance opaque cursors and a refresh never removes history', () => {
  let state = applyEventPage([], {}, '196', {
    items: [{ id: 'one', asset: '196:0xAbC', t: 200 }],
    next: 'opaque-page-2',
  });
  assert.equal(state.cursors['196'], 'opaque-page-2');
  assert.equal(state.items[0].asset, '0xabc');

  state = applyEventPage(state.items, state.cursors, '196', {
    items: [{ id: 'older', asset: '0xdef', t: 100 }],
    next: null,
  });
  assert.equal(state.cursors['196'], null);
  assert.deepEqual(state.items.map((item) => item.id), ['one', 'older']);

  const refreshed = mergeEventItems(state.items, [{ id: 'one', chainId: '196', asset: '0xabc', t: 200 }]);
  assert.deepEqual(refreshed.map((item) => item.id), ['one', 'older']);
});

test('event identity and address normalization keep chains separate', () => {
  assert.equal(normalizeEventAddress('196:196:0xAbC', '196'), '0xabc');
  assert.notEqual(
    eventKey({ id: 'same', chainId: '56' }),
    eventKey({ id: 'same', chainId: '196' }),
  );
});

test('a live relationship frame converts into a discovery record immediately', () => {
  const event = eventFromRelationship({
    id: '196:event-1', chainId: '196', at: 200, kind: 'verified', label: '已核验',
    relation: { id: 'relation-1', token: '0xABC', ticker: 'NVDA' },
  });
  assert.deepEqual(
    { id: event.id, chainId: event.chainId, asset: event.asset, ticker: event.ticker, t: event.t },
    { id: '196:event-1', chainId: '196', asset: '0xabc', ticker: 'NVDA', t: 200 },
  );
});

test('events client sends the server cursor as one opaque value', async () => {
  const originalFetch = globalThis.fetch;
  let requested;
  globalThis.fetch = async (url) => {
    requested = url;
    return { ok: true, json: async () => ({ items: [], next: null }) };
  };
  try {
    await getEvents('56', 'eyJ0IjoxLCJpZCI6IngifQ');
    const url = new URL(requested, 'https://example.test');
    assert.equal(url.searchParams.get('chain'), '56');
    assert.equal(url.searchParams.get('before'), 'eyJ0IjoxLCJpZCI6IngifQ');
    assert.equal(url.searchParams.has('id'), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Meme and stock relations match by chain and normalized address', () => {
  const relation = { chainId: '56', token: '0xAbC', stock: '0xDEF' };
  assert.equal(relationMatchesAsset(relation, { chainId: '56', token: '0xabc' }), true);
  assert.equal(relationMatchesAsset(relation, { chainId: '196', token: '0xabc' }), false);
  assert.equal(relationMatchesStock(relation, { chainIndex: '56', tokenContractAddress: '0xdef' }), true);
  assert.equal(relationMatchesStock(relation, { chainIndex: '196', tokenContractAddress: '0xdef' }), false);
});

test('a live quote automatically becomes stale as wall time advances', () => {
  const row = { price: 10, provider: 'Binance', priceProvenance: { timeKind: 'market' }, fieldTimes: { price: 1_000 } };
  assert.equal(quoteFreshness(row, false, 120_999).state, 'live');
  assert.equal(quoteFreshness(row, false, 121_001).state, 'stale');
  assert.equal(quoteFreshness({ ...row, quoteStatus: 'market_closed' }, false, 999_999).state, 'market_closed');
  assert.equal(quoteFreshness({ ...row, price: null, quoteStatus: 'no-verified-market' }, false, 999_999).state, 'no-verified-market');
});
