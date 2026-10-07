import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createPinia, setActivePinia } from 'pinia';
import { applyVisibleExchangeQuote, visibleExchangeObservation } from '../src/utils/visible-exchange-quote.js';
import { mergeDirectoryAsset } from '../src/utils/meme-directory-refresh.js';
import { useMemeDirectoryStore } from '../src/stores/meme-directory.js';
import { memeDirectoryQuery } from '../src/utils/meme-directory-query.js';
import { directoryObservation } from '../src/utils/meme-directory-presentation.js';

const address = index => '0x' + String(index).padStart(40, '0');
const now = 1800000000000;
const asset = (extra = {}) => ({ chainId: '56', token: address(1), symbol: 'CT', provider: 'OKX',
  priceScope: 'dex', quoteType: 'dex', venue: 'dex', priceCurrency: 'USD', price: .53, change24h: -11.8,
  volume24h: 160000000, volumeScope: 'token', volumeCurrency: 'USD', quoteAt: now - 600000,
  fieldTimes: { price: now - 600000, volume24h: now - 600000 }, ...extra });
const quote = (extra = {}) => ({ chainId: '56', token: address(1), symbol: 'CT', provider: 'Binance Alpha',
  source: 'Binance Alpha', venue: 'binance-alpha', marketId: 'ALPHA_1228USDT', priceScope: 'exchange',
  quoteType: 'exchange-token', priceCurrency: 'USDT', volumeCurrency: 'USDT', price: .52865068,
  change24h: -13.046, volume24h: 140000000, marketAt: now - 1000, statisticsAt: now - 1200,
  receivedAt: now - 900, timeKind: 'market', ...extra });
const publication = (revision, row) => ({ now: now - 1000, snapshotAt: now - 1000, realtime: { revision },
  groups: [{ symbol: row.symbol, memberCount: 1, representative: row }], directory: { groupTotal: 1 }, chart: { points: [] } });
const json = data => ({ ok: true, json: async () => data });

test('exact exchange quotes are independent from the DEX price, unit, sort value and token volume', () => {
  const row = asset(), original = structuredClone(row);
  assert.equal(applyVisibleExchangeQuote(row, quote(), now), true);
  const observation = visibleExchangeObservation(row, now);
  assert.equal(observation.price, .52865068); assert.equal(observation.priceCurrency, 'USDT');
  assert.equal(observation.marketId, 'ALPHA_1228USDT'); assert.equal(observation.provider, 'Binance Alpha');
  assert.equal(observation.at, now - 1000); assert.equal(observation.changeAt, now - 1200);
  assert.equal(observation.current, true); assert.equal(observation.changeCurrent, true);
  const { _exchangeQuote, ...unchanged } = row; assert.deepEqual(unchanged, original);
  assert.equal(_exchangeQuote.volume24h, undefined); assert.equal(_exchangeQuote.volumeCurrency, undefined);
  assert.equal(directoryObservation(row, 'price', now).value, .53);
  assert.equal(directoryObservation(row, 'volume24h', now).value, 160000000);
});

test('first acceptance requires exact contract, explicit exchange series, usable unit and real source time', () => {
  for (const patch of [{ chainId: '196' }, { token: address(2) }, { token: '' }, { priceScope: 'dex' },
    { venue: '' }, { provider: '', source: '' }, { marketId: '' }, { priceCurrency: '' }, { priceCurrency: 'UNKNOWN' },
    { price: null }, { price: 0 }, { price: NaN }, { price: Infinity }, { marketAt: null },
    { marketAt: now + 2000 }, { marketAt: now - 900001 }, { fieldSources: { price: 'Different provider' } },
    { fieldScopes: { price: 'pool' } }, { fieldScopes: { price: 'token' } },
    { fieldObservations: { price: { currency: 'USD' } } }]) {
    const row = asset(); assert.equal(applyVisibleExchangeQuote(row, quote(patch), now), false, JSON.stringify(patch));
    assert.equal(row._exchangeQuote, undefined);
  }
});

test('the selected exchange venue, market, unit and provider remain fixed rather than racing to select every packet', () => {
  const row = asset(); applyVisibleExchangeQuote(row, quote(), now);
  for (const patch of [{ venue: 'other-exchange' }, { marketId: 'ALPHA_123USDT' }, { priceCurrency: 'USD' }, { provider: 'Other' }])
    assert.equal(applyVisibleExchangeQuote(row, quote({ ...patch, marketAt: now - 100, price: .6 }), now), false);
  assert.equal(row._exchangeQuote.price, .52865068);
  assert.equal(applyVisibleExchangeQuote(row, quote({ marketAt: now - 100, price: .6, change24h: -12, statisticsAt: now - 200 }), now), true);
  assert.equal(row._exchangeQuote.price, .6); assert.equal(row._exchangeQuote.change24h, -12);
});

test('price and rolling change statistics keep their independent clocks; malformed units or source scopes cannot freshen them', () => {
  const row = asset(); applyVisibleExchangeQuote(row, quote(), now);
  assert.equal(applyVisibleExchangeQuote(row, quote({ marketAt: now - 100, price: .6, statisticsAt: now - 5000, change24h: 99 }), now), true);
  assert.equal(row._exchangeQuote.price, .6); assert.equal(row._exchangeQuote.change24h, -13.046);
  assert.equal(row._exchangeQuote.fieldTimes.change24h, now - 1200);
  assert.equal(applyVisibleExchangeQuote(row, quote({ marketAt: now - 3000, price: .1, statisticsAt: now - 50, change24h: 0 }), now), true);
  assert.equal(row._exchangeQuote.price, .6); assert.equal(row._exchangeQuote.quoteAt, now - 100);
  assert.equal(row._exchangeQuote.change24h, 0); assert.equal(row._exchangeQuote.fieldTimes.change24h, now - 50);
  for (const patch of [{ statisticsAt: null }, { statisticsAt: now + 2000 }, { fieldScopes: { change24h: 'token' } }, { change24h: '' }, { fieldSources: { change24h: 'Other' } }]) {
    applyVisibleExchangeQuote(row, quote({ ...patch, marketAt: now - 100, price: .6, change24h: Object.hasOwn(patch, 'change24h') ? patch.change24h : 88 }), now);
    assert.equal(row._exchangeQuote.change24h, 0);
  }
});

test('null invalidates the fixed series without unlocking it, and silent/old trades do not claim current data', () => {
  const row = asset(); applyVisibleExchangeQuote(row, quote(), now);
  assert.equal(applyVisibleExchangeQuote(row, quote({ price: null, marketAt: now - 100, change24h: null, statisticsAt: now - 100 }), now), true);
  assert.equal(row._exchangeQuote.price, null); assert.equal(row._exchangeQuote.change24h, null);
  assert.equal(applyVisibleExchangeQuote(row, quote({ price: 8, marketAt: now - 200, statisticsAt: now - 200 }), now), false);
  assert.equal(applyVisibleExchangeQuote(row, quote({ marketAt: now - 50, price: 1, marketId: 'OTHERUSDT' }), now), false);
  assert.equal(visibleExchangeObservation(row, now + 900001).current, false);
});

test('the primary exchange series is never duplicated as an apparent second independent market', () => {
  const row = { ...asset(), ...quote() };
  assert.equal(applyVisibleExchangeQuote(row, quote(), now), false);
  const dex = asset(); applyVisibleExchangeQuote(dex, quote(), now);
  const switched = mergeDirectoryAsset(dex, { ...asset(), ...quote() });
  assert.equal(visibleExchangeObservation(switched, now), null);
  assert.equal(visibleExchangeObservation({ ...dex, token: address(2) }, now), null);
});

test('directory reconciliation preserves the exchange observation only for the exact same asset', () => {
  const row = asset(); applyVisibleExchangeQuote(row, quote(), now);
  const next = mergeDirectoryAsset(row, asset({ price: .54, quoteAt: now - 20000, fieldTimes: { price: now - 20000 } }));
  assert.equal(next.price, .54); assert.equal(next._exchangeQuote.price, .52865068);
  assert.equal(mergeDirectoryAsset(row, asset({ token: address(2) }))._exchangeQuote, undefined);
});

test('real production Binance Alpha messages prove identity and yield actual changes for current Meme rows', () => {
  const evidence = JSON.parse(readFileSync(new URL('./fixtures/visible-exchange-production.json', import.meta.url), 'utf8'));
  const rows = evidence.snapshots.memes.packet.groups.map(group => structuredClone(group.representative));
  const original = rows.map(row => ({ price: row.price, unit: row.priceCurrency, volume: row.volume24h }));
  let accepted = 0, movements = 0;
  for (const event of evidence.events) {
    if (event.event !== 'price') continue;
    for (const row of rows) {
      const before = row._exchangeQuote?.price;
      if (applyVisibleExchangeQuote(row, event.data, evidence.finishedAt)) {
        accepted++; if (before != null && before !== row._exchangeQuote.price) movements++;
      }
    }
  }
  assert.ok(accepted > 0); assert.ok(movements > 0);
  assert.deepEqual(rows.map(row => ({ price: row.price, unit: row.priceCurrency, volume: row.volume24h })), original);
});

test('Pinia exchange quotes survive slow HTTP and apply to immediately displayed new membership', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), original = globalThis.fetch;
  const liveNow = Date.now(), initial = asset({ quoteAt: liveNow - 3000, fieldTimes: { price: liveNow - 3000 } });
  let next = publication(1, initial), finish;
  globalThis.fetch = async () => finish ? new Promise(resolve => { finish = resolve; }) : json(next);
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'independent-exchange-push-race' }));
    finish = true; const refreshing = store.refresh(); await Promise.resolve(); await Promise.resolve();
    assert.equal(store.applyQuotePacket(quote({ marketAt: liveNow - 1000, statisticsAt: liveNow - 1200 })), true);
    finish(json(publication(2, asset({ price: .54, quoteAt: liveNow - 2000, fieldTimes: { price: liveNow - 2000 } }))));
    await refreshing;
    assert.equal(store.groups[0].representative.price, .54);
    assert.equal(store.groups[0].representative.priceCurrency, 'USD');
    assert.equal(store.groups[0].representative._exchangeQuote.price, .52865068);
    finish = null;
    next = publication(3, asset()); next.groups.push({ symbol: 'OTHER', memberCount: 1, representative: asset({ symbol: 'OTHER', token: address(2) }) });
    await store.refresh();
    assert.equal(store.groups[1].symbol, 'OTHER', 'new membership is visible before another quote or apply action');
    store.applyQuotePacket(quote({ marketAt: liveNow - 100, price: .6, statisticsAt: liveNow - 100 }));
    assert.equal(store.groups[0].representative._exchangeQuote.price, .6);
    assert.equal(store.groups[1].representative._exchangeQuote, undefined);
    assert.equal(store.groups[0].representative.volume24h, 160000000);
  } finally { store.stop(); globalThis.fetch = original; }
});


test('explicit exchange field scope and matching observation currency remain eligible', () => {
  const row = asset();
  assert.equal(applyVisibleExchangeQuote(row, quote({ fieldScopes: { price: 'exchange' },
    fieldObservations: { price: { currency: 'USDT' } } }), now), true);
  assert.equal(row._exchangeQuote.priceCurrency, 'USDT');
  assert.equal(applyVisibleExchangeQuote(row, quote({ marketAt: now - 10, price: .7,
    fieldScopes: { price: 'pool' } }), now), false);
  assert.equal(applyVisibleExchangeQuote(row, quote({ marketAt: now - 10, price: .7,
    fieldObservations: { price: { currency: 'USD' } } }), now), false);
  assert.equal(row._exchangeQuote.price, .52865068);
});


test('a conflicting explicit pool or DEX quote type cannot masquerade as an exchange series', () => {
  for (const quoteType of ['pool', 'dex']) {
    const row = asset(); assert.equal(applyVisibleExchangeQuote(row, quote({ quoteType }), now), false);
    assert.equal(row._exchangeQuote, undefined);
  }
  for (const quoteType of ['exchange-token', 'exchange', undefined]) {
    const row = asset(); assert.equal(applyVisibleExchangeQuote(row, quote({ quoteType }), now), true);
    assert.equal(row._exchangeQuote.priceCurrency, 'USDT');
  }
});
