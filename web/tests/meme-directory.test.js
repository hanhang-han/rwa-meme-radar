import test from 'node:test';
import assert from 'node:assert/strict';
import { createPinia, setActivePinia } from 'pinia';
import { useMemeDirectoryStore } from '../src/stores/meme-directory.js';
import { memeDirectoryPath, memeDirectoryQuery, memeFilterKey } from '../src/utils/meme-directory-query.js';
import { createMemeDirectoryCache } from '../src/utils/meme-directory-cache.js';
import { createMemeDirectoryStream } from '../src/utils/meme-directory-stream.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const wait = async predicate => { for (let i = 0; i < 30 && !predicate(); i++) await tick(); assert.ok(predicate()); };
const asset = index => ({ chainId: '196', token: `0x${String(index).padStart(40, '0')}`, symbol: 'SHARED', name: 'Shared', price: index + 1, volume24h: 1000 - index, volumeCurrency: 'USD', fieldTimes: { price: 12000, volume24h: 12000 } });
const publication = (revision = 17) => ({ now: 12345, snapshotAt: 12345, evaluatedAt: Date.now(), realtime: { cursor: revision, revision, schema: 2 } });
const directoryPacket = (revision = 17) => ({ ...publication(revision), groups: [{ symbol: 'SHARED', memberCount: 600, representative: asset(0) }], directory: { catalogTotal: 2196, filteredTotal: 2196, groupTotal: 1668, hiddenMissingCount: 555, limit: 20, offset: 0 }, summary: { comparableCount: 1800, observedVolumeTotal: 5000000, volumeMax: 1000, liquidityMax: 7000, plotVolumeMax: 1000, plotLiquidityMax: 7000 }, chart: { points: [], total: 0 }, unified: { snapshotScope: 'memes', relations: [] } });
const json = data => ({ ok: true, json: async () => data });

test('query preserves every filter and keeps column changes independent of 20-coin or 15-pair pages', () => {
  const query = memeDirectoryQuery({ chain: 'bnb', q: ' QQQ ', rel: 'B', fresh: '1', minLiq: '10000', risk: 'hide', category: 'meme', new: '24h', rank: 'volume24h', ticker: '0700', showMissing: '0', filter: 'history', sort: 'holders', page: '2' });
  assert.equal(query.chain, '56'); assert.equal(query.q, 'qqq'); assert.equal(query.ticker, '700');
  assert.equal(query.offset, 40); assert.equal(query.limit, 20); assert.equal(query.rel, 'B');
  assert.equal(query.fresh, '1'); assert.equal(query.minLiq, '10000'); assert.equal(query.risk, 'hide');
  assert.equal(query.category, 'meme'); assert.equal(query.new, '24h'); assert.equal(query.rank, 'volume24h');
  assert.equal(query.showMissing, '0'); assert.equal(query.filter, 'history'); assert.equal(query.sort, 'holders');
  assert.equal(memeDirectoryQuery({}, true).limit, 20);
  assert.equal(memeDirectoryQuery({ page: '2' }, true).offset, 40);
  assert.equal(memeDirectoryQuery({ view: 'pool', page: '2', sort: 'createdAt' }, true).offset, 30);
  assert.equal(memeDirectoryQuery({ view: 'pool' }, true).limit, 15);
  const qualified = memeDirectoryQuery({ qualified: '1', rel: 'B', fresh: '0', minLiquidity: '1' });
  assert.equal(qualified.rel, 'all'); assert.equal(qualified.fresh, '1'); assert.equal(qualified.minLiq, '1000');
  assert.equal(memeDirectoryQuery({ filter: 'related' }).fresh, '1');
  assert.equal(memeDirectoryQuery({ filter: 'name' }).rel, 'B');
  assert.equal(memeDirectoryQuery({}).showMissing, '1');
  assert.equal(memeDirectoryQuery({ page: '-3' }).offset, 0);
  assert.equal(memeDirectoryQuery({ page: 'Infinity' }).offset, 0);
  assert.equal(memeFilterKey(query), memeFilterKey({ ...query, limit: 50, offset: 200 }));
  assert.notEqual(memeFilterKey(query), memeFilterKey({ ...query, chain: 'all' }));
  assert.ok(memeDirectoryPath(query, { group: 'SHARED', focus: '196:0xa', revision: 17 }).includes('limit=50'));
  assert.ok(memeDirectoryPath(query, { chart: true }).startsWith('v2/memes/chart?'));
  assert.ok(!memeDirectoryPath(query, { chart: true }).includes('offset='));
});

test('bounded directory cache shares transport, preserves times, isolates queries, and retains last success on failure', async () => {
  let time = 100000, calls = 0, fail = false;
  const cache = createMemeDirectoryCache({ apiBase: '/dashboard/api/', now: () => time, request: async path => { calls++; if (fail) throw new Error('offline'); return { ...publication(), path }; } });
  const a = memeDirectoryPath(memeDirectoryQuery({ chain: 'all' }));
  const [one, two] = await Promise.all([cache.get(a), cache.get(a)]);
  assert.equal(calls, 1); one.now = -1; assert.equal(two.now, 12345); assert.equal(cache.peek(a).now, 12345);
  const b = memeDirectoryPath(memeDirectoryQuery({ chain: '56', q: 'qqq', page: '1' }));
  assert.equal(cache.peek(b), null); await cache.get(b); assert.equal(calls, 2);
  await cache.get(a); assert.equal(calls, 2);
  await cache.get(a, { force: true }); assert.equal(calls, 3);
  time += 30000; fail = true; await assert.rejects(cache.get(a), /offline/); assert.equal(cache.peek(a).now, 12345);
  fail = false; await cache.get(a); assert.equal(calls, 5);
  time += 120001; assert.equal(cache.peek(a), null);
  for (let index = 0; index < 13; index++) await cache.get(`v2/memes?q=${index}`);
  assert.equal(cache.peek('v2/memes?q=0'), null); assert.ok(cache.peek('v2/memes?q=12'));
});

test('real Pinia store ignores late pages and keeps server-owned full-filter summaries', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(); const original = globalThis.fetch, resolvers = new Map(), calls = [];
  globalThis.fetch = url => { calls.push(url); return new Promise(resolve => resolvers.set(url, resolve)); };
  const firstQuery = memeDirectoryQuery({ q: 'late-page-test' }), secondQuery = memeDirectoryQuery({ q: 'late-page-test', page: '1' });
  try {
    const first = store.setQuery(firstQuery); await tick();
    const second = store.setQuery(secondQuery); await tick();
    const secondPacket = directoryPacket(18); secondPacket.directory.offset = 20; secondPacket.groups[0].representative = asset(20);
    resolvers.get('/api/' + memeDirectoryPath(secondQuery))(json(secondPacket)); await second;
    resolvers.get('/api/' + memeDirectoryPath(firstQuery))(json(directoryPacket())); await first;
    assert.equal(store.groups[0].representative.token, asset(20).token);
    assert.equal(store.directory.filteredTotal, 2196); assert.equal(store.directory.groupTotal, 1668);
    assert.equal(store.summary.observedVolumeTotal, 5000000); assert.equal(store.snapshot.now, 12345);
    assert.equal(calls.length, 2); assert.ok(!calls.some(url => url.includes('view=market')));
  } finally { store.stop(); globalThis.fetch = original; }
});

test('first-load failure ends loading, retry succeeds, and refresh failure preserves the original rows and timestamps', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(); const original = globalThis.fetch;
  let fail = true;
  globalThis.fetch = async () => { if (fail) throw new Error('offline'); return json(directoryPacket()); };
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'retry-failure-test' }));
    assert.equal(store.loading, false); assert.equal(store.ready, false); assert.equal(store.error, 'offline');
    fail = false; await store.refresh(); assert.equal(store.ready, true); assert.equal(store.error, null);
    fail = true; await store.refresh(); assert.equal(store.loading, false); assert.equal(store.ready, true);
    assert.equal(store.snapshot.now, 12345); assert.equal(store.groups[0].representative.fieldTimes.price, 12000);
    assert.equal(store.error, 'offline');
  } finally { store.stop(); globalThis.fetch = original; }
});

test('real Pinia group expansion keeps proxy identity and a distant focus never skips earlier segments', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(); const original = globalThis.fetch, requests = [];
  globalThis.fetch = async url => {
    requests.push(url); const query = new URL(url, 'http://localhost').searchParams;
    if (!url.includes('/memes/group?')) return json(directoryPacket());
    const offset = query.has('focus') ? 500 : Number(query.get('offset'));
    return json({ ...publication(), group: { symbol: 'SHARED', memberCount: 600, limit: 50, offset }, rows: Array.from({ length: 50 }, (_, i) => asset(offset + i)), unified: { relations: [{ chainId: '196', id: `rel-${offset}`, token: asset(offset).token, ticker: '700', level: 'B' }] } });
  };
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'focus-expansion-test' }));
    await store.loadGroup('SHARED', { focus: `196:${asset(510).token}` });
    assert.equal(store.members.SHARED.rows.length, 50); assert.equal(store.members.SHARED.nextOffset, 0);
    assert.ok(store.members.SHARED.rows.some(row => row.token === asset(510).token));
    await store.loadGroup('SHARED', { more: true });
    assert.equal(store.members.SHARED.nextOffset, 50); assert.equal(store.members.SHARED.rows[0].token, asset(0).token);
    await store.loadGroup('SHARED', { more: true });
    assert.equal(store.members.SHARED.nextOffset, 100); assert.equal(store.members.SHARED.rows.length, 150);
    assert.ok(store.members.SHARED.hasMore); assert.equal(store.relations.length, 3);
    assert.ok(requests.filter(url => url.includes('/group?')).every(url => url.includes('limit=50') && url.includes('revision=17')));
    await store.setQuery(memeDirectoryQuery({ q: 'focus-expansion-test', chain: '56' }));
    assert.deepEqual(store.members, {});
  } finally { store.stop(); globalThis.fetch = original; }
});

test('a group response from another publication never populates the current page', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(); const original = globalThis.fetch;
  globalThis.fetch = async url => url.includes('/memes/group?')
    ? json({ ...publication(18), group: { memberCount: 1, offset: 0 }, rows: [asset(99)] }) : json(directoryPacket());
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'group-publication-test' })); await store.loadGroup('SHARED');
    assert.equal(store.members.SHARED.rows.length, 0); assert.equal(store.snapshot.realtime.revision, 17);
  } finally { store.stop(); globalThis.fetch = original; }
});

test('large charts wait until explicitly opened and preserve full-range totals', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(); const original = globalThis.fetch; let completeChart;
  globalThis.fetch = async url => {
    if (url.includes('/memes/chart?')) return new Promise(resolve => { completeChart = resolve; });
    return json({ ...directoryPacket(), chart: { points: [], total: 1800, requiresFetch: true } });
  };
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'chart-independence-test' }));
    assert.equal(store.ready, true); assert.equal(store.groups.length, 1); assert.equal(store.summary.comparableCount, 1800);
    await tick(); assert.equal(completeChart, undefined, 'collapsed analysis never requests the large chart');
    store.setChartVisible(true);
    await wait(() => completeChart); assert.equal(store.chartLoading, true);
    completeChart(json({ ...publication(), chart: { total: 1, points: [{ key: `196:${asset(510).token}`, chainId: '196', token: asset(510).token, volume: 10, liquidity: 20, groupSymbol: 'SHARED', groupIndex: 43, volumeAt: 12000, liquidityAt: 12000 }] } }));
    await wait(() => !store.chartLoading);
    assert.equal(store.snapshot.chart.points[0].groupIndex, 43); assert.equal(store.snapshot.now, 12345);
    assert.equal(store.snapshot.chart.points[0].volumeAt, 12000); assert.equal(store.summary.comparableCount, 1800);
  } finally { store.stop(); globalThis.fetch = original; }
});

test('chart results for an old filter cannot overwrite the new page', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(); const original = globalThis.fetch; let completeChart;
  globalThis.fetch = async url => {
    if (url.includes('/memes/chart?')) return new Promise(resolve => { completeChart = resolve; });
    if (url.includes('q=chart-old-filter')) return json({ ...directoryPacket(), chart: { points: [], total: 1800, requiresFetch: true } });
    return json(directoryPacket(18));
  };
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'chart-old-filter' })); store.setChartVisible(true); await wait(() => completeChart);
    await store.setQuery(memeDirectoryQuery({ q: 'chart-new-filter' }));
    completeChart(json({ ...publication(), chart: { total: 1, points: [{ key: 'OLD' }] } })); await tick(); await tick();
    assert.equal(store.snapshot.realtime.revision, 18); assert.deepEqual(store.snapshot.chart.points, []);
  } finally { store.stop(); globalThis.fetch = original; }
});

test('pool pages retain server summaries and use the pool endpoint with a 15-pair limit', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(); const original = globalThis.fetch, calls = [];
  globalThis.fetch = async url => { calls.push(url); return json({ ...publication(), groups: [{ key: 'pair', items: [{ key: 'pool', chainId: '196' }] }], directory: { totalPools: 81, groupTotal: 31, limit: 15, offset: 15 }, summary: { liquidity: { known: 60, total: 1000000, max: 200000 }, volume24h: { known: 10, total: 400000, max: 80000 } } }); };
  try {
    await store.setQuery(memeDirectoryQuery({ view: 'pool', page: '1', q: 'pool-scope-test' }));
    assert.ok(calls[0].startsWith('/api/v2/memes/pools?')); assert.ok(calls[0].includes('limit=15') && calls[0].includes('offset=15'));
    assert.equal(store.directory.totalPools, 81); assert.equal(store.summary.liquidity.total, 1000000);
    assert.equal(store.snapshot.now, 12345); assert.equal(store.groups.length, 1);
  } finally { store.stop(); globalThis.fetch = original; }
});

test('the lightweight stream handles split CRLF/UTF-8, scopes invalidations, ignores duplicates and cleans up', async () => {
  const calls = [], statuses = [], invalidations = []; let send;
  const transport = async (url, options) => {
    calls.push(url);
    return { ok: true, body: new ReadableStream({ start(controller) { send = text => controller.enqueue(new TextEncoder().encode(text)); options.signal.addEventListener('abort', () => controller.error(new Error('aborted')), { once: true }); } }) };
  };
  const stream = createMemeDirectoryStream({ apiBase: '/dashboard/api/', getCursor: () => 17, fetchImpl: transport, onStatus: patch => statuses.push(patch), onInvalidate: packet => invalidations.push(packet) });
  try {
    stream.start(); await wait(() => send);
    send('event: hello\r'); send('\ndata: {"cursor":17}\r'); send('\n\r'); send('\n');
    send('event: directory.invalidate\nid: 18\ndata: {"scope":"memes","revision":18}\n\n');
    send('event: directory.invalidate\nid: 18\ndata: {"scope":"memes","revision":18}\n\n');
    send('event: directory.invalidate\nid: 19\ndata: {"scope":"market","revision":19}\n\n');
    send('event: reset\ndata: {"cursor":20}\n\n');
    await wait(() => invalidations.length === 2);
    assert.ok(calls[0].startsWith('/dashboard/api/v2/stream?'));
    const params = new URL(calls[0], 'http://localhost').searchParams;
    assert.equal(params.get('scope'), 'memes'); assert.equal(params.get('trades'), 'none'); assert.equal(params.get('candles'), 'none'); assert.equal(params.get('after'), '17');
    assert.ok(statuses.some(status => status.connected === true));
  } finally { stream.stop(); }
  assert.equal(statuses.at(-1).connected, false); assert.equal(statuses.at(-1).state, 'paused');
});

test('visibility stops the dedicated stream and foreground refreshes only the independent directory', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(); const originalFetch = globalThis.fetch, originalDocument = globalThis.document;
  const listeners = new Map(), requests = [], streamSignals = [];
  globalThis.document = { hidden: false, addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener: name => listeners.delete(name) };
  globalThis.fetch = async (url, options = {}) => {
    requests.push(url);
    if (url.includes('/v2/stream?')) {
      streamSignals.push(options.signal);
      return { ok: true, body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('event: hello\ndata: {"cursor":17}\n\n')); options.signal.addEventListener('abort', () => controller.error(new Error('aborted')), { once: true }); } }) };
    }
    return json(directoryPacket());
  };
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'visibility-lifecycle-test' })); store.start(); await wait(() => store.stream.connected);
    globalThis.document.hidden = true; listeners.get('visibilitychange')(); assert.ok(streamSignals[0].aborted);
    globalThis.document.hidden = false; listeners.get('visibilitychange')();
    await wait(() => requests.filter(url => url.includes('/v2/memes?')).length === 2);
    assert.ok(!requests.some(url => url.includes('view=market') || url.includes('scope=market')));
    store.stop(); assert.equal(listeners.size, 0); assert.ok(streamSignals.every(signal => signal.aborted));
  } finally { store.stop(); globalThis.fetch = originalFetch; if (originalDocument === undefined) delete globalThis.document; else globalThis.document = originalDocument; }
});

test('hello latest cursor never skips invalidations replayed after the older HTTP snapshot', async () => {
  const invalidations = []; let send;
  const stream = createMemeDirectoryStream({ apiBase: '/api/', getCursor: () => 100, onInvalidate: packet => invalidations.push(packet),
    fetchImpl: async (_, options) => ({ ok: true, body: new ReadableStream({ start(controller) { send = text => controller.enqueue(new TextEncoder().encode(text)); options.signal.addEventListener('abort', () => controller.error(new Error('aborted')), { once: true }); } }) }) });
  try {
    stream.start(); await wait(() => send);
    send('event: hello\ndata: {"cursor":105}\n\n');
    send('event: directory.invalidate\nid: 101\ndata: {"scope":"memes","revision":101,"cursor":101}\n\n');
    send('event: directory.invalidate\nid: 105\ndata: {"scope":"memes","revision":105,"cursor":105}\n\n');
    await wait(() => invalidations.length === 2);
    assert.deepEqual(invalidations.map(packet => packet.revision), [101, 105]);
  } finally { stream.stop(); }
});

test('invalidation bursts are bounded and a slow response is committed before one dirty follow-up', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: 100000 });
  const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(); const original = globalThis.fetch;
  let calls = 0, completeSlow;
  globalThis.fetch = async (url, options = {}) => {
    if (url.includes('/v2/stream?')) return { ok: true, body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('event: hello\ndata: {"cursor":17}\n\n')); options.signal.addEventListener('abort', () => controller.error(new Error('aborted')), { once: true }); } }) };
    calls++;
    if (calls === 2) return new Promise(resolve => { completeSlow = resolve; });
    return json(directoryPacket(16 + calls));
  };
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'bounded-invalidation-test' })); store.start(); await flush();
    for (let i = 0; i < 100; i++) store.queueRefresh();
    t.mock.timers.tick(4999); await flush(); assert.equal(calls, 1);
    t.mock.timers.tick(1); await flush(); assert.equal(calls, 2);
    for (let i = 0; i < 100; i++) store.queueRefresh();
    t.mock.timers.tick(10000); await flush(); assert.equal(calls, 2, 'pending refresh shares its transport rather than repeatedly discarding itself');
    completeSlow(json(directoryPacket(18))); await flush();
    assert.equal(store.snapshot.realtime.revision, 18); assert.equal(store.loading, false);
    t.mock.timers.tick(299); await flush(); assert.equal(calls, 2);
    t.mock.timers.tick(1); await flush(); assert.equal(calls, 3, 'one dirty refresh follows the completed request');
    assert.equal(store.snapshot.realtime.revision, 19); assert.equal(store.snapshot.now, 12345);
  } finally { store.stop(); await flush(); globalThis.fetch = original; t.mock.timers.reset(); }
});

test('a focus click during a pending first segment loads the requested member afterwards', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(); const original = globalThis.fetch; let completeFirst;
  const packets = offset => ({ ...publication(), group: { symbol: 'SHARED', memberCount: 600, limit: 50, offset }, rows: Array.from({ length: 50 }, (_, i) => asset(offset + i)), unified: { relations: [] } });
  globalThis.fetch = async url => {
    if (!url.includes('/memes/group?')) return json(directoryPacket());
    if (new URL(url, 'http://localhost').searchParams.has('focus')) return json(packets(500));
    return new Promise(resolve => { completeFirst = resolve; });
  };
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'concurrent-focus-test' }));
    const first = store.loadGroup('SHARED'); await wait(() => completeFirst);
    const focus = store.loadGroup('SHARED', { focus: `196:${asset(510).token}` });
    completeFirst(json(packets(0))); await Promise.all([first, focus]);
    assert.ok(store.members.SHARED.rows.some(row => row.token === asset(510).token));
    assert.equal(store.members.SHARED.nextOffset, 50); assert.equal(store.members.SHARED.rows.length, 100);
  } finally { store.stop(); globalThis.fetch = original; }
});
