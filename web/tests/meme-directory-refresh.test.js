import test from 'node:test';
import assert from 'node:assert/strict';
import { createPinia, setActivePinia } from 'pinia';
import { mergeDirectoryAsset, planMemeDirectoryRefresh } from '../src/utils/meme-directory-refresh.js';
import { useMemeDirectoryStore } from '../src/stores/meme-directory.js';
import { memeDirectoryQuery } from '../src/utils/meme-directory-query.js';
import { applyVisibleQuote } from '../src/utils/visible-quotes.js';

const group = (symbol, price, token = symbol) => ({ symbol, memberCount: 2, representative: { chainId: '56', token, symbol, price, quoteAt: price * 1000 } });
const packet = (revision, groups) => ({ realtime: { revision }, now: revision, snapshotAt: revision, groups, chart: { points: [] }, directory: { groupTotal: groups.length }, unified: { relations: [{ id: revision, ticker: '700', level: 'A' }] } });
const json = data => ({ ok: true, json: async () => data });

test('ranking changes immediately use the incoming order with current observations and relationships', () => {
  const before = packet(1, [group('A', 1), group('B', 2)]);
  const after = packet(2, [group('B', 5), group('A', 4)]);
  const result = planMemeDirectoryRefresh(before, after);
  assert.deepEqual(result.snapshot.groups.map(row => row.symbol), ['B', 'A']);
  assert.deepEqual(result.snapshot.groups.map(row => row.representative.price), [5, 4]);
  assert.equal(result.snapshot.realtime.revision, 2);
  assert.deepEqual(result.snapshot.unified.relations, after.unified.relations);
  assert.deepEqual(before.groups.map(row => row.symbol), ['A', 'B'], 'the original publication remains unchanged');
});

test('new membership and representative changes are visible as soon as the publication arrives', () => {
  const before = packet(1, [group('SAME', 1)]);
  for (const after of [packet(2, [group('NEW', 2)]), packet(3, [group('SAME', 3, 'another-contract')])]) {
    const result = planMemeDirectoryRefresh(before, after);
    assert.deepEqual(result.snapshot.groups, after.groups);
    assert.deepEqual(result.snapshot.directory, after.directory);
    assert.deepEqual(result.snapshot.unified.relations, after.unified.relations);
    assert.equal(result.snapshot.realtime.revision, after.realtime.revision);
  }
});

test('membership changes commit new relationships and totals together while merging individual quote clocks', () => {
  const before = packet(1, [group('A', 1), group('B', 2)]);
  before.groups[0].representative.volume24h = 100;
  before.groups[0].representative.fieldTimes = { volume24h: 1000 };
  const after = packet(2, [group('A', 5), group('NEW', 3)]);
  after.groups[0].representative.volume24h = 500;
  after.groups[0].representative.fieldTimes = { volume24h: 5000 };
  after.summary = { observedVolumeTotal: 700, comparableCount: 2 };
  const result = planMemeDirectoryRefresh(before, after);
  assert.deepEqual(result.snapshot.groups.map(row => row.symbol), ['A', 'NEW']);
  assert.deepEqual(result.snapshot.groups.map(row => row.representative.price), [5, 3]);
  assert.equal(result.snapshot.groups[0].representative.fieldTimes.volume24h, 5000);
  assert.equal(result.snapshot.realtime.revision, 2);
  assert.equal(result.snapshot.unified, after.unified);
  assert.equal(result.snapshot.directory, after.directory);
  assert.deepEqual(result.snapshot.summary, after.summary);
  assert.equal(before.groups[0].representative.price, 1);
});

test('the store immediately displays each new publication and retains the latest success on failure', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), original = globalThis.fetch;
  let next = packet(1, [group('A', 1)]), fail = false;
  globalThis.fetch = async () => { if (fail) throw new Error('offline'); return json(next); };
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'auto-update-publication' }));
    next = packet(2, [group('B', 3)]); await store.refresh();
    assert.equal(store.groups[0].symbol, 'B'); assert.equal(store.snapshot.realtime.revision, 2);
    next = packet(3, [group('C', 4)]); await store.refresh();
    assert.equal(store.groups[0].symbol, 'C'); assert.equal(store.snapshot.realtime.revision, 3);
    fail = true; await store.refresh();
    assert.equal(store.groups[0].symbol, 'C'); assert.equal(store.error, 'offline');
  } finally { store.stop(); globalThis.fetch = original; }
});

test('expanded rows retain source times while outdated relationships stay excluded until refreshed', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), original = globalThis.fetch;
  let revision = 1, failGroup = false;
  globalThis.fetch = async url => {
    if (!url.includes('/memes/group?')) return json(packet(revision, [group('A', revision)]));
    if (failGroup) throw new Error('offline');
    return json({ ...packet(revision, []), group: { memberCount: 2, offset: 0 }, rows: [group('A', revision).representative, group('CHILD', revision).representative] });
  };
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'expanded-publication-refresh' })); await store.loadGroup('A');
    assert.equal(store.members.A.rows.length, 2);
    const oldQuoteAt = store.members.A.rows[1].quoteAt;
    revision = 2; await store.refresh();
    assert.equal(store.members.A.rows[1].quoteAt, oldQuoteAt);
    assert.equal(store.members.A.stale, true);
    assert.deepEqual(store.relations.map(row => row.id), [2]);
    failGroup = true; await store.loadGroup('A');
    assert.equal(store.members.A.rows.length, 2); assert.equal(store.members.A.stale, true);
    failGroup = false; await store.loadGroup('A');
    assert.equal(store.members.A.stale, false); assert.equal(store.members.A.revision, 2);
    assert.equal(store.members.A.rows[1].quoteAt, 2000);
  } finally { store.stop(); globalThis.fetch = original; }
});

test('refreshing an expanded group retains all opened segments and keeps them intact if a later segment fails', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), original = globalThis.fetch;
  let revision = 1, failLater = false;
  globalThis.fetch = async url => {
    if (!url.includes('/memes/group?')) return json(packet(revision, [group('A', revision)]));
    const offset = Number(new URL(url, 'https://example.test').searchParams.get('offset'));
    if (offset && failLater) throw new Error('later segment offline');
    return json({ ...packet(revision, []), group: { memberCount: 4, offset }, rows: [0, 1].map(index =>
      group('A', revision, 'member-' + (offset + index)).representative) });
  };
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'retained-expanded-segments' }));
    await store.loadGroup('A'); await store.loadGroup('A', { more: true });
    assert.equal(store.members.A.rows.length, 4);
    revision = 2; await store.refresh(); await store.loadGroup('A');
    assert.equal(store.members.A.rows.length, 4); assert.equal(store.members.A.nextOffset, 4);
    assert.ok(store.members.A.rows.every(row => row.quoteAt === 2000));
    revision = 3; failLater = true; await store.refresh(); await store.loadGroup('A');
    assert.equal(store.members.A.rows.length, 4); assert.equal(store.members.A.stale, true);
    assert.ok(store.members.A.rows.every(row => row.quoteAt === 2000));
  } finally { store.stop(); globalThis.fetch = original; }
});

const address = index => '0x' + String(index).padStart(40, '0');
const liveAsset = (at, price = 10, extra = {}) => ({ chainId: '56', token: address(1), symbol: 'LIVE', price,
  provider: 'OKX DEX', venue: 'dex', priceScope: 'token', quoteType: 'dex', priceCurrency: 'USD',
  quoteAt: at, fieldTimes: { price: at }, fieldSources: { price: 'OKX DEX' }, fieldScopes: { price: 'token' }, ...extra });
const livePublication = (revision, asset) => packet(revision, [{ symbol: 'LIVE', memberCount: 1, representative: asset }]);

test('publication merging retains newer push fields and does not erase omitted fields or their metadata', () => {
  const previous = liveAsset(5000, 5, { volume24h: 400, volumeCurrency: 'USD', volumeScope: 'token',
    fieldTimes: { price: 5000, volume24h: 4500 }, fieldSources: { price: 'OKX DEX', volume24h: 'Aggregate' },
    fieldAvailability: { price: { value: 5, at: 5000, status: 'current' }, volume24h: { value: 400, at: 4500, status: 'current' } } });
  const incoming = liveAsset(4000, 4, { fieldTimes: { price: 4000 }, fieldAvailability: { price: { value: 4, at: 4000, status: 'current' } } });
  const result = mergeDirectoryAsset(previous, incoming);
  assert.equal(result.price, 5); assert.equal(result.quoteAt, 5000);
  assert.equal(result.volume24h, 400); assert.equal(result.fieldTimes.volume24h, 4500);
  assert.equal(result.fieldSources.volume24h, 'Aggregate'); assert.equal(result.volumeCurrency, 'USD');
  assert.equal(result.fieldAvailability.price.value, 5);
  assert.equal(previous.fieldAvailability.price.value, 5); assert.equal(incoming.fieldAvailability.price.value, 4);
});

test('an explicit invalidation and a published primary market switch replace the old selected observation', () => {
  const previous = liveAsset(5000, 5, { marketId: 'market-a', fieldAvailability: { price: { value: 5, at: 5000, status: 'current' } } });
  const invalidated = mergeDirectoryAsset(previous, liveAsset(4000, null, { fieldAvailability: { price: { value: null, status: 'missing', reason: 'market-removed' } } }));
  assert.equal(invalidated.price, null); assert.equal(invalidated.fieldAvailability.price.value, null);
  const switched = mergeDirectoryAsset(previous, liveAsset(4000, 4, { marketId: 'market-b' }));
  assert.equal(switched.price, 4); assert.equal(switched.marketId, 'market-b');
  assert.equal(switched.quoteAt, 4000); assert.equal(switched.fieldAvailability.price, undefined);
});

test('new volume fills a missing value without freshening holders or an older price', () => {
  const previous = liveAsset(5000, 5, { volume24h: null, holders: 20, fieldTimes: { price: 5000, holders: 1000 } });
  const incoming = liveAsset(4000, 4, { volume24h: 125, volumeCurrency: 'USD', volumeScope: 'token',
    fieldTimes: { price: 4000, volume24h: 4500 }, fieldSources: { volume24h: 'Token aggregate' } });
  const result = mergeDirectoryAsset(previous, incoming);
  assert.equal(result.price, 5); assert.equal(result.volume24h, 125);
  assert.equal(result.fieldTimes.price, 5000); assert.equal(result.fieldTimes.volume24h, 4500);
  assert.equal(result.holders, 20); assert.equal(result.fieldTimes.holders, 1000);
});

test('out-of-order publications cannot replace current relationships or quotes', () => {
  const before = livePublication(10, liveAsset(5000, 5));
  const result = planMemeDirectoryRefresh(before, livePublication(9, liveAsset(6000, 6)));
  assert.equal(result.snapshot, before); assert.equal(result.ignored, true);
});

test('same-name additions immediately update the count and representative observations', () => {
  const before = livePublication(1, liveAsset(1000));
  const after = livePublication(2, liveAsset(2000)); after.groups[0].memberCount = 3;
  const result = planMemeDirectoryRefresh(before, after);
  assert.equal(result.snapshot.groups[0].memberCount, 3);
  assert.equal(result.snapshot.groups[0].representative.quoteAt, 2000);
  assert.equal(result.snapshot.realtime.revision, 2);
});

test('successive automatic refreshes show new members, current ranking and statistics without an apply action', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), original = globalThis.fetch;
  let next = packet(1, [group('A', 1), group('B', 2)]);
  globalThis.fetch = async () => json(next);
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'automatic-membership-ranking' }));
    next = packet(2, [group('B', 3), group('A', 2)]); await store.refresh();
    assert.deepEqual(store.groups.map(row => row.symbol), ['B', 'A']);
    next = packet(3, [group('C', 4), group('B', 5)]); next.summary = { observedVolumeTotal: 1000 };
    await store.refresh();
    assert.deepEqual(store.groups.map(row => row.symbol), ['C', 'B']);
    assert.deepEqual(store.relations.map(row => row.id), [3]);
    assert.equal(store.summary.observedVolumeTotal, 1000);
    next = packet(4, [group('D', 6)]); await store.refresh();
    assert.equal(store.groups[0].symbol, 'D'); assert.equal(store.directory.groupTotal, 1);
    assert.ok(store.addedKeys.includes('56:d'));
  } finally { store.stop(); globalThis.fetch = original; }
});

test('a quote tick updates representatives and expanded contracts without committing another publication', () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), at = Date.now() - 1000;
  store.snapshot = livePublication(20, liveAsset(at, 1));
  store.members.LIVE = { rows: [liveAsset(at, 1), liveAsset(at, 2, { token: address(2) })], relations: [{ id: 'retained' }], revision: 20 };
  const snapshot = store.snapshot, entry = store.members.LIVE;
  assert.equal(store.applyQuotePacket(liveAsset(at + 500, 3)), true);
  assert.equal(store.snapshot, snapshot); assert.equal(store.members.LIVE, entry);
  assert.equal(store.groups[0].representative.price, 3); assert.equal(store.members.LIVE.rows[0].price, 3);
  assert.equal(store.members.LIVE.rows[1].price, 2); assert.equal(store.snapshot.realtime.revision, 20);
  assert.equal(store.applyQuotePacket(liveAsset(at, 1)), false);
  assert.equal(store.applyQuotePacket(liveAsset(at + 500, 9, { chainId: '196' })), false);
  store.stop();
});

test('a slow HTTP publication cannot roll back a newer quote when new membership immediately commits', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), original = globalThis.fetch;
  const at = Date.now() - 2000; let next = livePublication(1, liveAsset(at, 1)), finish;
  globalThis.fetch = async () => finish ? new Promise(resolve => { finish = resolve; }) : json(next);
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'push-http-race' }));
    finish = true; const refresh = store.refresh(); await Promise.resolve(); await Promise.resolve();
    store.applyQuotePacket(liveAsset(at + 1000, 3));
    finish(json(livePublication(2, liveAsset(at + 500, 2)))); await refresh;
    assert.equal(store.groups[0].representative.price, 3); assert.equal(store.groups[0].representative.quoteAt, at + 1000);
    finish = null;
    next = packet(3, [group('NEW', 4), ...livePublication(3, liveAsset(at + 500, 2)).groups]); await store.refresh();
    assert.deepEqual(store.groups.map(row => row.symbol), ['NEW', 'LIVE']);
    assert.equal(store.groups[1].representative.price, 3, 'the newer quote survives the immediate membership update');
    store.applyQuotePacket(liveAsset(at + 1500, 5));
    assert.equal(store.groups[1].representative.price, 5); assert.equal(store.snapshot.realtime.revision, 3);
  } finally { store.stop(); globalThis.fetch = original; }
});

test('a slow expanded member response retains a quote received during its request', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), original = globalThis.fetch;
  const at = Date.now() - 2000; let complete;
  globalThis.fetch = async url => url.includes('/memes/group?') ? new Promise(resolve => { complete = resolve; }) : json(livePublication(1, liveAsset(at, 1)));
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'expanded-push-http-race' }));
    store.members.LIVE = { rows: [liveAsset(at, 1, { token: address(2) })], segments: { 0: 1 }, relations: [], stale: true, total: 1 };
    const loading = store.loadGroup('LIVE'); await Promise.resolve(); await Promise.resolve();
    store.applyQuotePacket(liveAsset(at + 1000, 8, { token: address(2) }));
    complete(json({ ...livePublication(1, liveAsset(at, 1)), group: { memberCount: 1, offset: 0 }, rows: [liveAsset(at + 500, 2, { token: address(2) })] }));
    await loading; assert.equal(store.members.LIVE.rows[0].price, 8); assert.equal(store.members.LIVE.stale, false);
  } finally { store.stop(); globalThis.fetch = original; }
});

test('expanded groups immediately merge their refreshed order and new contracts without requiring resume', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), original = globalThis.fetch;
  let revision = 1, groupCalls = 0; const at = Date.now() - 5000;
  globalThis.fetch = async url => {
    if (!url.includes('/memes/group?')) return json(livePublication(revision, liveAsset(at + revision * 500, revision)));
    groupCalls++;
    const members = revision === 1 ? [address(1), address(2)] : [address(2), address(1), address(3)];
    return json({ ...livePublication(revision, liveAsset(at, 1)), group: { memberCount: members.length, offset: 0 },
      rows: members.map(token => liveAsset(at + revision * 500, revision, { token })) });
  };
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'expanded-automatic-order' })); await store.loadGroup('LIVE');
    revision = 2; await store.refresh(); await store.loadGroup('LIVE');
    assert.deepEqual(store.members.LIVE.rows.map(row => row.token), [address(2), address(1), address(3)]);
    assert.equal(store.members.LIVE.rows[0].price, 2); assert.equal(store.members.LIVE.stale, false);
    assert.equal(store.members.LIVE.total, 3); assert.ok(store.addedKeys.includes('56:' + address(3)));
    await store.loadGroup('LIVE'); assert.equal(groupCalls, 2, 'a completed current group is not downloaded again');
    store.applyQuotePacket(liveAsset(at + 2500, 9, { token: address(2) }));
    assert.equal(store.members.LIVE.rows[0].price, 9); assert.equal(store.members.LIVE.stale, false);
  } finally { store.stop(); globalThis.fetch = original; }
});

test('automatic membership updates immediately publish the current name, classification and group index', () => {
  const before = livePublication(1, liveAsset(1000, 1, { name: 'Original', match: { level: 'B' }, groupIndex: 3 }));
  const after = livePublication(2, liveAsset(2000, 2, { name: 'Changed', match: { level: 'A' }, groupIndex: 0 }));
  after.groups.push(group('NEW', 3));
  const result = planMemeDirectoryRefresh(before, after);
  assert.equal(result.snapshot.groups[0].representative.price, 2);
  assert.equal(result.snapshot.groups[0].representative.name, 'Changed');
  assert.equal(result.snapshot.groups[0].representative.match.level, 'A');
  assert.equal(result.snapshot.groups[0].representative.groupIndex, 0);
  assert.equal(result.snapshot.groups[1].symbol, 'NEW');
});

test('an older response cannot replace a newer immediately displayed publication', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), original = globalThis.fetch;
  let next = packet(1, [group('A', 1)]); globalThis.fetch = async () => json(next);
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'automatic-publication-order' }));
    next = packet(5, [group('B', 5)]); await store.refresh();
    assert.equal(store.groups[0].symbol, 'B'); assert.equal(store.snapshot.realtime.revision, 5);
    next = packet(3, [group('C', 3)]); await store.refresh();
    assert.equal(store.groups[0].symbol, 'B'); assert.equal(store.snapshot.realtime.revision, 5);
  } finally { store.stop(); globalThis.fetch = original; }
});

test('a real pushed observation survives a higher field revision carrying older same-market price and volume', () => {
  const at = Date.now() - 3000;
  const row = liveAsset(at, 1, { volume24h: 10, volumeScope: 'token', volumeCurrency: 'USD',
    fieldTimes: { price: at, volume24h: at }, fieldSources: { price: 'OKX DEX', volume24h: 'OKX DEX' },
    fieldScopes: { price: 'token', volume24h: 'token' }, fieldRevisions: { price: 20, volume24h: 20 },
    fieldAvailability: { price: { value: 1, at, status: 'current' } } });
  const push = liveAsset(at + 2000, 5, { volume24h: 50, volumeScope: 'token', volumeCurrency: 'USD',
    fieldTimes: { price: at + 2000, volume24h: at + 2000 }, fieldSources: { price: 'OKX DEX', volume24h: 'OKX DEX' },
    fieldScopes: { price: 'token', volume24h: 'token' }, fieldRevisions: { price: 500, volume24h: 500 } });
  assert.equal(applyVisibleQuote(row, push), true);
  assert.equal(row.fieldRevisions.price, 20, 'push source clocks are independent of directory field revisions');
  const oldHttp = liveAsset(at + 1000, 2, { volume24h: 20, volumeScope: 'token', volumeCurrency: 'USD',
    fieldTimes: { price: at + 1000, volume24h: at + 1000 }, fieldRevisions: { price: 21, volume24h: 21 } });
  const merged = mergeDirectoryAsset(row, oldHttp);
  assert.equal(merged.price, 5); assert.equal(merged.volume24h, 50);
  assert.equal(merged.fieldTimes.price, at + 2000); assert.equal(merged.fieldTimes.volume24h, at + 2000);
  assert.equal(merged.quoteAt, at + 2000);
  assert.equal(merged.fieldRevisions.price, 20); assert.equal(merged.fieldRevisions.volume24h, 20);
  const invalidated = mergeDirectoryAsset(row, { ...oldHttp, price: null });
  assert.equal(invalidated.price, null, 'explicit publication invalidation still clears a pushed quote');
  const switched = mergeDirectoryAsset({ ...row, marketId: 'primary-a' }, { ...oldHttp, marketId: 'primary-b' });
  assert.equal(switched.price, 2); assert.equal(switched.marketId, 'primary-b');
});

test('the directory store reconciles a late high-revision HTTP response after a real quote push without rewinding', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), original = globalThis.fetch;
  const at = Date.now() - 3000; let finish;
  globalThis.fetch = async () => finish
    ? new Promise(resolve => { finish = resolve; })
    : json(livePublication(20, liveAsset(at, 1, { fieldRevisions: { price: 20 } })));
  try {
    await store.setQuery(memeDirectoryQuery({ q: 'high-revision-real-push-race' }));
    finish = true; const refresh = store.refresh(); await Promise.resolve(); await Promise.resolve();
    assert.equal(store.applyQuotePacket(liveAsset(at + 2000, 5)), true);
    finish(json(livePublication(21, liveAsset(at + 1000, 2, { fieldRevisions: { price: 21 } }))));
    await refresh;
    assert.equal(store.snapshot.realtime.revision, 21, 'new publication facts commit');
    assert.equal(store.groups[0].representative.price, 5, 'newer market observation survives the publication');
    assert.equal(store.groups[0].representative.fieldTimes.price, at + 2000);
    assert.equal(store.groups[0].representative.fieldRevisions.price, 20);
  } finally { store.stop(); globalThis.fetch = original; }
});

test('pool ranking and membership also merge immediately with current pool summaries', async () => {
  setActivePinia(createPinia()); const store = useMemeDirectoryStore(), original = globalThis.fetch;
  const poolGroup = (key, value) => ({ key, items: [{ key, chainId: '56', volume24h: value }] });
  let next = { ...packet(1, []), groups: [poolGroup('pool-a', 1), poolGroup('pool-b', 2)], directory: { totalPools: 2, groupTotal: 2 } };
  globalThis.fetch = async () => json(next);
  try {
    await store.setQuery(memeDirectoryQuery({ view: 'pool', q: 'pool-automatic-update' }));
    next = { ...packet(2, []), groups: [poolGroup('pool-b', 5), poolGroup('pool-c', 8)], directory: { totalPools: 3, groupTotal: 2 }, summary: { volume24h: { known: 2, total: 13 } } };
    await store.refresh();
    assert.deepEqual(store.groups.map(row => row.key), ['pool-b', 'pool-c']);
    assert.equal(store.groups[0].items[0].volume24h, 5);
    assert.equal(store.directory.totalPools, 3); assert.equal(store.summary.volume24h.total, 13);
    assert.equal(store.snapshot.realtime.revision, 2);
  } finally { store.stop(); globalThis.fetch = original; }
});
