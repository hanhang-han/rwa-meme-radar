import test from 'node:test';
import assert from 'node:assert/strict';
import { createStockDirectoryCache, STOCK_DIRECTORY_PREVIEW_MS, STOCK_DIRECTORY_REUSE_MS } from '../src/utils/stock-directory-cache.js';
import { stockDirectoryQuery, stockDirectoryPath, STOCK_DIRECTORY_PAGE_SIZE } from '../src/utils/stock-directory-query.js';
import { getStockDirectory, peekStockDirectory } from '../src/api/product.js';
import { preloadStockDirectory } from '../src/preload.js';

const snapshot = (now = 12345) => ({ now, unified: { stockThemes: [{ ticker: 'QQQ', stock: { fieldTimes: { price: 12000 } } }] } });
const memoryStorage = () => {
  const values = new Map();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), values };
};

test('route normalization shares chain aliases, all catalog, search, sort and 20-row pages', () => {
  assert.deepEqual(stockDirectoryQuery({ chain: 'bnb', q: '  QqQ ', sort: 'related', catalog: 'paired', page: '2.8' }), {
    chain: '56', options: { q: 'qqq', sort: 'related', catalog: 'paired', limit: 20, offset: 40 },
  });
  assert.deepEqual(stockDirectoryQuery({ chain: 'all', sort: 'bad', catalog: 'bad', page: '-3' }, '196'), {
    chain: 'all', options: { q: '', sort: 'related', catalog: 'all', limit: 20, offset: 0 },
  });
  assert.equal(stockDirectoryQuery({}, 'xlayer').chain, '196');
  assert.equal(stockDirectoryQuery({ page: 'Infinity' }, 'robinhood').options.offset, 0);
  assert.equal(STOCK_DIRECTORY_PAGE_SIZE, 20);
  assert.equal(stockDirectoryPath('56', { q: ' QQQ ', catalog: 'all' }), stockDirectoryPath('bnb', { q: 'qqq' }));
});

test('missing route chain reads the saved preference without overriding an explicit all scope', () => {
  const original = globalThis.localStorage;
  globalThis.localStorage = { getItem: key => key === 'radar-chain' ? 'bnb' : null };
  try {
    assert.equal(stockDirectoryQuery({}).chain, '56');
    assert.equal(stockDirectoryQuery({ chain: 'all' }).chain, 'all');
  } finally {
    if (original === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = original;
  }
});

test('parallel normal and forced consumers share one transport and receive independent snapshots', async () => {
  let calls = 0, complete;
  const cache = createStockDirectoryCache({ apiBase: '/dashboard/api/', storage: null, now: () => 100000,
    request: () => { calls++; return new Promise(resolve => { complete = resolve; }); } });
  const path = stockDirectoryPath('all');
  const first = cache.get(path), second = cache.get(path), forced = cache.get(path, { force: true });
  await Promise.resolve();
  assert.equal(calls, 1);
  const source = snapshot();
  complete(source);
  const [a, b, c] = await Promise.all([first, second, forced]);
  a.unified.stockThemes[0].ticker = 'CHANGED';
  source.unified.stockThemes[0].stock.fieldTimes.price = 99999;
  assert.equal(b.unified.stockThemes[0].ticker, 'QQQ');
  assert.equal(c.unified.stockThemes[0].ticker, 'QQQ');
  const preview = cache.peek(path);
  assert.equal(preview.now, 12345);
  assert.equal(preview.unified.stockThemes[0].stock.fieldTimes.price, 12000);
  preview.now = 1;
  assert.equal(cache.peek(path).now, 12345);
});

test('chain, query, sort, catalog, page and page size each use a separate cache key', async () => {
  const calls = [];
  const cache = createStockDirectoryCache({ apiBase: '/dashboard/api/', storage: null, now: () => 100000,
    request: async path => { calls.push(path); return { path }; } });
  const paths = [
    stockDirectoryPath('all'), stockDirectoryPath('56'), stockDirectoryPath('196'),
    stockDirectoryPath('all', { q: 'QQQ' }), stockDirectoryPath('all', { sort: 'ticker' }),
    stockDirectoryPath('all', { catalog: 'paired' }), stockDirectoryPath('all', { offset: 20 }),
    stockDirectoryPath('all', { limit: 40 }),
  ];
  for (const path of paths) await cache.get(path);
  for (const path of paths) assert.deepEqual(cache.peek(path), { path });
  assert.equal(calls.length, paths.length);
  assert.equal(new Set(calls).size, paths.length);
});

test('short reuse expires, forced refresh bypasses it, and 30-second refresh gets new data', async () => {
  let time = 100000, calls = 0;
  const cache = createStockDirectoryCache({ apiBase: '/api/', storage: null, now: () => time,
    request: async () => snapshot(++calls) });
  const path = stockDirectoryPath();
  assert.equal((await cache.get(path)).now, 1);
  time += 1000;
  assert.equal((await cache.get(path)).now, 1);
  assert.equal((await cache.get(path, { force: true })).now, 2);
  time += STOCK_DIRECTORY_REUSE_MS + 1;
  assert.equal((await cache.get(path)).now, 3);
  time += 30000;
  assert.equal((await cache.get(path)).now, 4);
  time += STOCK_DIRECTORY_PREVIEW_MS + 1;
  assert.equal(cache.peek(path), null);
  assert.equal((await cache.get(path)).now, 5);
});

test('a failed refresh rejects, retains last success unchanged and immediately allows retry', async () => {
  let time = 100000, calls = 0;
  const cache = createStockDirectoryCache({ apiBase: '/api/', storage: null, now: () => time,
    request: async () => { calls++; if (calls === 2) throw new Error('offline'); return snapshot(calls); } });
  const path = stockDirectoryPath();
  await cache.get(path);
  time += 30000;
  await assert.rejects(cache.get(path), /offline/);
  assert.equal(cache.peek(path).now, 1);
  assert.equal((await cache.get(path)).now, 3);
  assert.equal(calls, 3);
});

test('an initial failure is not cached and the next request can succeed', async () => {
  let calls = 0;
  const cache = createStockDirectoryCache({ apiBase: '/api/', storage: null,
    request: async () => { if (++calls === 1) throw new Error('offline'); return snapshot(); } });
  const path = stockDirectoryPath();
  await assert.rejects(cache.get(path), /offline/);
  assert.equal(cache.peek(path), null);
  await cache.get(path);
  assert.equal(calls, 2);
});

test('session restoration preserves publication times and never crosses dashboard mount paths', async () => {
  const storage = memoryStorage();
  let time = 100000, calls = 0;
  const request = async () => { calls++; return snapshot(); };
  const path = stockDirectoryPath('56');
  await createStockDirectoryCache({ apiBase: '/dashboard/api/', storage, now: () => time, request }).get(path);
  time += 30000;
  const restored = createStockDirectoryCache({ apiBase: '/dashboard/api/', storage, now: () => time, request });
  assert.equal(restored.peek(path).now, 12345);
  assert.equal(restored.peek(path).unified.stockThemes[0].stock.fieldTimes.price, 12000);
  assert.equal(calls, 1, 'peek restores the snapshot without transport or timestamp promotion');
  const otherMount = createStockDirectoryCache({ apiBase: '/dashboardv2/api/', storage, now: () => time, request });
  assert.equal(otherMount.peek(path), null);
  time += STOCK_DIRECTORY_PREVIEW_MS;
  assert.equal(restored.peek(path), null);
  const expiredReload = createStockDirectoryCache({ apiBase: '/dashboard/api/', storage, now: () => time, request });
  assert.equal(expiredReload.peek(path), null);
});

test('cache bounds memory and persisted entries, evicts old keys, and tolerates blocked storage', async () => {
  const storage = memoryStorage();
  const cache = createStockDirectoryCache({ apiBase: '/api/', storage, now: () => 100000, request: async () => snapshot() });
  for (let page = 0; page < 22; page++) await cache.get(stockDirectoryPath('all', { offset: page * 20 }));
  assert.equal(cache.peek(stockDirectoryPath('all')), null);
  assert.ok(cache.peek(stockDirectoryPath('all', { offset: 420 })));
  assert.equal(JSON.parse([...storage.values.values()][0]).length, 20);
  const blocked = createStockDirectoryCache({ apiBase: '/api/', now: () => 100000,
    storage: () => { throw new Error('denied'); }, request: async () => snapshot() });
  await blocked.get(stockDirectoryPath());
  assert.equal(blocked.peek(stockDirectoryPath()).now, 12345);
  const large = createStockDirectoryCache({ apiBase: '/large/api/', storage, now: () => 100000,
    request: async () => ({ ...snapshot(), description: 'x'.repeat(300000) }) });
  await large.get(stockDirectoryPath());
  assert.ok(storage.values.get('cliperx:stock-directory-cache:v1:/large/api/').length <= 262144);
  assert.equal(large.peek(stockDirectoryPath()).now, 12345, 'large data still works in memory');
});

test('public stock API uses the mounted cache path and exact normalized directory options', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async url => { calls.push(url); return { ok: true, json: async () => snapshot() }; };
  try {
    const options = { q: ' Cache-API-test ', sort: 'related', catalog: 'all', offset: 20, limit: 20 };
    const [a, b] = await Promise.all([getStockDirectory('bnb', options), getStockDirectory('56', options)]);
    assert.deepEqual(calls, ['/api/v2/stocks?chain=56&sort=related&limit=20&offset=20&q=cache-api-test&catalog=all']);
    a.now = -1;
    assert.equal(b.now, 12345);
    assert.equal(peekStockDirectory('56', options).now, 12345);
    assert.equal(peekStockDirectory('all', options), null);
    await getStockDirectory('56', { ...options, force: true });
    assert.equal(calls.length, 2);
  } finally { globalThis.fetch = original; }
});

test('the independent preloader requests stock lists only and shares the page request', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async url => { calls.push(url); return { ok: true, json: async () => snapshot() }; };
  try {
    assert.equal(preloadStockDirectory('#/stock/QQQ?chain=all'), null);
    assert.equal(preloadStockDirectory('#/meme?chain=all'), null);
    assert.equal(preloadStockDirectory('#/stocks/QQQ'), null);
    const loading = preloadStockDirectory('#/stocks?chain=all&q=Preload-Test&sort=ticker&catalog=paired&page=1');
    const pageQuery = stockDirectoryQuery({ chain: 'all', q: 'Preload-Test', sort: 'ticker', catalog: 'paired', page: '1' });
    const page = getStockDirectory(pageQuery.chain, pageQuery.options);
    const [a, b] = await Promise.all([loading, page]);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls, ['/api/v2/stocks?chain=all&sort=ticker&limit=20&offset=20&q=preload-test&catalog=paired']);
    a.now = -1;
    assert.equal(b.now, 12345);
  } finally { globalThis.fetch = original; }
});

test('preloading fails silently, and the page can retry instead of inheriting a cached error', async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    if (++calls === 1) throw new Error('offline');
    return { ok: true, json: async () => snapshot() };
  };
  try {
    assert.equal(await preloadStockDirectory('#/stock?chain=all&q=preload-failure-test'), null);
    assert.equal(peekStockDirectory('all', { q: 'preload-failure-test' }), null);
    assert.equal((await getStockDirectory('all', { q: 'preload-failure-test' })).now, 12345);
    assert.equal(calls, 2);
  } finally { globalThis.fetch = original; }
});
