import test from 'node:test';
import assert from 'node:assert/strict';
import { createPinia, setActivePinia } from 'pinia';
import { isReactive } from 'vue';
import { MAX_REPLAY_BYTES, MAX_REPLAY_FRAMES, projectionByteLength, useDashboardStore } from '../src/stores/dashboard.js';
import { handleStreamEvent } from '../src/composables/useStream.js';

const asset = price => ({ chainId:'56', token:'a', kind:'candidate', price });
const snapshot = revision => ({ realtime:{schema:1,revision,cursor:revision}, unified:{snapshotScope:'market',assets:[asset(revision)],stockTokens:[],relations:[]} });
const delta = (revision, padding = '') => ({ schema:1,revision,cursor:revision,upserts:{assets:[asset(revision)]},removes:{},meta:{padding} });
function setup() { setActivePinia(createPinia());const store=useDashboardStore();store.acceptSnapshot(snapshot(1));return store; }

test('replay history respects actual UTF-8 bytes, not only frame count or string length', () => {
  const store = setup();
  const padding = '市'.repeat(1_000_000);
  for (let revision = 2; revision <= 4; revision++) store.applyProjection(delta(revision, padding));
  assert.equal(store.recentDeltas.length, 2);
  assert.deepEqual(store.recentDeltas.map(row => row.revision), [3,4]);
  assert.equal(store.bufferFloorRevision, 2);
  assert.equal(store.recentDeltaBytes, store.recentDeltas.reduce((total,row) => total + projectionByteLength(row),0));
  assert.ok(store.recentDeltaBytes <= MAX_REPLAY_BYTES);
  assert.equal(store.assets[0].price, 4, 'eviction never skips applying the current projection');
});

test('one oversized stream frame applies once without remaining in replay history', async () => {
  const store = setup();
  store.applyProjection(delta(2));
  const oversized = delta(3,'x'.repeat(MAX_REPLAY_BYTES));
  assert.equal(await handleStreamEvent('projection.delta', JSON.stringify(oversized)), true);
  assert.equal(store.assets[0].price, 3);
  assert.equal(store.cursor, 3);
  assert.equal(store.recentDeltas.length, 0);
  assert.equal(store.recentDeltaSizes.length, 0);
  assert.equal(store.recentDeltaBytes, 0);
  assert.equal(store.bufferFloorRevision, 3);
  assert.throws(() => store.acceptSnapshot(snapshot(2)), /snapshot-behind-replay-window/);
  assert.equal(store.assets[0].price, 3);
  store.applyProjection(delta(4));
  store.acceptSnapshot(snapshot(3));
  assert.equal(store.assets[0].price, 4, 'a snapshot at the retained floor can replay all newer changes');
});

test('the frame-count ceiling still bounds many tiny projections and advances the safe floor', () => {
  const store = setup();
  for (let revision = 2; revision <= MAX_REPLAY_FRAMES + 6; revision++) store.applyProjection(delta(revision));
  assert.equal(store.recentDeltas.length, MAX_REPLAY_FRAMES);
  assert.equal(store.recentDeltaSizes.length, MAX_REPLAY_FRAMES);
  assert.equal(store.bufferFloorRevision, 6);
  assert.equal(store.recentDeltas[0].revision, 7);
  assert.ok(store.recentDeltaBytes < MAX_REPLAY_BYTES);
  store.acceptSnapshot(snapshot(6));
  assert.equal(store.revision, MAX_REPLAY_FRAMES + 6);
});

test('replay containers and packets stay nonreactive while displayed entities remain reactive', () => {
  const store = setup();
  const packet = delta(2);
  store.applyProjection(packet);
  assert.equal(isReactive(store.recentDeltas), false);
  assert.equal(isReactive(store.recentDeltaSizes), false);
  assert.equal(isReactive(store.recentDeltas[0]), false);
  assert.equal(isReactive(store.recentDeltas[0].upserts.assets[0]), false);
  assert.equal(store.recentDeltas[0], packet);
  assert.equal(isReactive(store.assets[0]), true);
});

test('a late HTTP snapshot behind the replay floor is re-fetched instead of rolling back the page', async () => {
  const store = setup();
  store.applyProjection(delta(10),true,MAX_REPLAY_BYTES+1);
  const originalFetch = global.fetch;
  let calls = 0;
  global.fetch = async () => ({ok:true,json:async()=>snapshot(++calls===1?9:10)});
  try {
    const result = await store.poll({view:'market'});
    assert.equal(calls, 2);
    assert.equal(result.realtime.revision, 10);
    assert.equal(store.assets[0].price, 10);
    assert.equal(store.error, null);
  } finally { global.fetch=originalFetch; }
});

test('re-fetching a persistently old snapshot is bounded and preserves the latest displayed data', async () => {
  const store = setup();
  store.applyProjection(delta(10),true,MAX_REPLAY_BYTES+1);
  const originalFetch = global.fetch;
  let calls = 0;
  global.fetch = async () => { calls++;return {ok:true,json:async()=>snapshot(9)}; };
  try {
    assert.equal(await store.poll({view:'market'}), null);
    assert.equal(calls, 2);
    assert.equal(store.revision, 10);
    assert.equal(store.assets[0].price, 10);
    assert.equal(store.error, 'snapshot-behind-replay-window');
  } finally { global.fetch=originalFetch; }
});
