import test from 'node:test';
import assert from 'node:assert/strict';
import { applyStockDirectoryQuote, mergeStockDirectoryRow, mergeStockDirectorySnapshot, stockQuoteAgeLabel } from '../src/utils/stock-directory-refresh.js';

const now = 1800000000000;
const row = (ticker, price = 10, at = now) => ({ ticker,
  stock: { chainId: '196', tokenContractAddress: `0x${ticker}`, price, priceCurrency: 'USD', priceScope: 'dex',
    provider: 'OKX', quoteAt: at, fieldTimes: { price: at, change24h: at }, change24h: 1,
    fieldSources: { price: 'OKX', change24h: 'OKX' }, stockIdentity: { id: `HK:${ticker}` } },
  theme: { pairedCount: 1, volume: { value: 300, known: 1, total: 1 } } });
const packet = rows => ({ now, directory: { total: rows.length, offset: 0, topThemes: rows },
  unified: { stockThemes: rows, sectors: [] } });

test('publications immediately apply arrivals, removals, totals and ranking without mutating the old snapshot', () => {
  const previous = packet([row('700'), row('1024')]), incoming = packet([row('1024', 11), row('9992')]);
  const before = structuredClone(previous), result = mergeStockDirectorySnapshot(previous, incoming);
  assert.deepEqual(result.unified.stockThemes.map(item => item.ticker), ['1024', '9992']);
  assert.deepEqual(result.directory.topThemes.map(item => item.ticker), ['1024', '9992']);
  assert.equal(result.unified.stockThemes[0].stock.price, 11);
  assert.equal(result.directory.total, incoming.directory.total);
  assert.deepEqual(previous, before);
});

test('reordering advances pool coverage and amounts immediately while retaining a newer streamed stock quote', () => {
  const previous = packet([row('700', 12, now + 1000), row('1024')]), incoming = packet([row('1024', 11), row('700', 10)]);
  incoming.now = now + 2000;
  incoming.unified.stockThemes[1].theme.volume = { value: 900, known: 2, total: 2 };
  const result = mergeStockDirectorySnapshot(previous, incoming);
  assert.deepEqual(result.unified.stockThemes.map(item => item.ticker), ['1024', '700']);
  assert.equal(result.unified.stockThemes[1].stock.price, 12);
  assert.equal(result.unified.stockThemes[1].stock.fieldTimes.price, now + 1000);
  assert.equal(result.unified.stockThemes[1].theme.volume.value, 900);
  assert.equal(result.unified.stockThemes[1].theme.volume.known, 2);
  assert.deepEqual(result.directory.topThemes.map(item => item.ticker), ['1024', '700']);
  assert.equal(result.directory.topThemes[1].stock.price, 12);
  assert.equal(result.now, incoming.now);
});

test('membership and total changes publish together with overlapping row updates', () => {
  const previous = packet([row('700'), row('1024')]), incoming = packet([row('1024', 14), row('9992')]);
  previous.directory.total = 99; incoming.directory.total = 100;
  const result = mergeStockDirectorySnapshot(previous, incoming);
  assert.deepEqual(result.unified.stockThemes.map(item => item.ticker), ['1024', '9992']);
  assert.equal(result.unified.stockThemes[0].stock.price, 14);
  assert.equal(result.directory.total, 100);
});

test('a slow directory response cannot rewind newer watched quotes or mislabel their provider', () => {
  const previous = row('700', 12, now + 1000), incoming = row('700', 10, now);
  previous.stock.provider = 'Chain RPC'; previous.stock.fieldSources.price = 'Chain RPC';
  previous.stock.fieldTimes.change24h = now - 1000;
  incoming.stock.change24h = 4;
  const result = mergeStockDirectoryRow(previous, incoming);
  assert.equal(result.stock.price, 12); assert.equal(result.stock.fieldTimes.price, now + 1000);
  assert.equal(result.stock.provider, 'Chain RPC'); assert.equal(result.stock.fieldSources.price, 'Chain RPC');
  assert.equal(result.stock.change24h, 4); assert.equal(result.stock.fieldTimes.change24h, now);
  assert.equal(result.stock.fieldSources.change24h, 'OKX');
});

test('different token, chain, currency and market are not combined with the old quote', () => {
  for (const patch of [{ tokenContractAddress: '0xother' }, { chainId: '56' }, { priceCurrency: 'wtCENTx' },
    { priceScope: 'exchange' }, { marketId: 'other' }, { venue: 'issuer' }]) {
    const previous = row('700', 12, now + 1000), incoming = row('700', 10, now);
    Object.assign(incoming.stock, patch);
    assert.equal(mergeStockDirectoryRow(previous, incoming).stock.price, 10);
  }
});

test('explicit missing quotes are respected and snapshot times never become quote times', () => {
  const previous = row('700', 12, now + 1000), incoming = row('700');
  Object.assign(incoming.stock, { price: null, quoteAt: null, fieldTimes: { price: null } });
  const result = mergeStockDirectoryRow(previous, incoming);
  assert.equal(result.stock.price, null); assert.equal(result.stock.fieldTimes.price, null);
  const update = mergeStockDirectorySnapshot(packet([previous]), { ...packet([incoming]), now: now + 10000 });
  assert.equal(update.unified.stockThemes[0].stock.quoteAt, null);
});

test('an independent equity reference retains its newer observation only for the same underlying', () => {
  const previous = row('700'), incoming = row('700');
  previous.equity = { stockPrice: 30, referenceCurrency: 'HKD', referenceAt: now + 1000, referenceProvider: 'Tencent' };
  incoming.equity = { stockPrice: 29, referenceCurrency: 'HKD', referenceAt: now, referenceProvider: 'Tencent' };
  assert.equal(mergeStockDirectoryRow(previous, incoming).equity.stockPrice, 30);
  incoming.stock.stockIdentity.id = 'US:700';
  assert.equal(mergeStockDirectoryRow(previous, incoming).equity.stockPrice, 29);
  incoming.equity = null;
  assert.equal(mergeStockDirectoryRow(previous, incoming).equity, null);
});

test('unchanged membership still advances amounts and unknown quote timestamps remain unconfirmed', () => {
  const previous = packet([row('700')]), incoming = packet([row('700', 11)]);
  const result = mergeStockDirectorySnapshot(previous, incoming);
  assert.deepEqual(result.unified.stockThemes.map(item => item.ticker), ['700']);
  assert.equal(result.unified.stockThemes[0].stock.price, 11);
  assert.equal(stockQuoteAgeLabel(null, now), '时间待确认');
  assert.equal(stockQuoteAgeLabel(now + 5000, now, 'en'), 'Time unconfirmed');
  assert.equal(stockQuoteAgeLabel(now - 4000, now), '4 秒前');
  assert.equal(stockQuoteAgeLabel(now - 120000, now, 'en'), '2m ago');
  assert.equal(stockQuoteAgeLabel(now - 90000000, now), '1 天前');
});

test('token quotes cannot fabricate an equity reference or paired-pool volume', () => {
  const card = row('700'); card.stock.stockIdentity.currency = 'HKD';
  const quote = { ...card.stock, price: 20, quoteAt: now + 1000, fieldTimes: { price: now + 1000 } };
  const tokenOnly = (target, packet) => { target.price = packet.price; return true; };
  assert.equal(applyStockDirectoryQuote(card, quote, tokenOnly, now), true);
  assert.equal(card.stock.price, 20); assert.equal(card.equity, undefined);
  assert.equal(card.theme.volume.value, 300);
});

test('only an explicit verified reference for the exact token and underlying may update equity', () => {
  const makeCard = () => { const card = row('700'); card.stock.stockIdentity.currency = 'HKD';
    card.equity = { stockPrice: 29, referenceCurrency: 'HKD', referenceAt: now - 1000 }; return card; };
  const card = makeCard(), quote = { ...card.stock, stockPrice: 30, referenceCurrency: 'HKD', referenceProvider: 'Tencent',
    referenceAt: now, referenceScope: 'equity-exchange', referenceIdentityVerified: true };
  const noTokenChange = () => false;
  assert.equal(applyStockDirectoryQuote(card, quote, noTokenChange, now), true);
  assert.equal(card.equity.stockPrice, 30); assert.equal(card.stock.price, 10);
  for (const patch of [{ referenceIdentityVerified: false }, { referenceScope: 'issuer-reference' }, { referenceCurrency: 'USD' },
    { chainId: '56' }, { tokenContractAddress: '0xother' }, { stockIdentity: { id: 'US:700' } },
    { referenceAt: now - 2000 }, { referenceAt: now + 5000 }, { stockPrice: null }, { referenceProvider: null }]) {
    const target = makeCard();
    assert.equal(applyStockDirectoryQuote(target, { ...quote, ...patch }, noTokenChange, now), false);
    assert.equal(target.equity.stockPrice, 29);
  }
});
