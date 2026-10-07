import test from 'node:test';
import assert from 'node:assert/strict';
import { createPinia, setActivePinia } from 'pinia';
import { useFeedStore } from '../src/stores/feed.js';
const reply = body => new Response(JSON.stringify(body), { status: 200 });
const live = { id: 'relation:updated:200', t: 200, chainId: '56', relation: { id: 'relation', checkedAt: 200, status: 'verified' } };
function setup(payload) {
  const original = global.fetch;
  global.fetch = async () => reply(payload);
  setActivePinia(createPinia());
  return { feed: useFeedStore(), cleanup() { global.fetch = original; } };
}

test('cached equal-time HTTP metadata cannot strip an existing SSE relation', async () => {
  const env = setup({ at: 100, trades: [], relationships: [{ id: live.id, t: 200 }] });
  try {
    env.feed.loaded = true; env.feed.lastAt = 300; env.feed.appendRelationship(live);
    await env.feed.load();
    assert.deepEqual(env.feed.relationships[0], live);
    assert.equal(env.feed.lastAt, 300);
  } finally { env.cleanup(); }
});

test('HTTP requires a strictly newer known source version to replace a relation', async () => {
  const env = setup({ at: 400, trades: [], relationships: [{ id: live.id, t: 201, status: 'updated' }] });
  try {
    env.feed.loaded = true; env.feed.appendRelationship(live);
    await env.feed.load();
    assert.equal(env.feed.relationships[0].t, 201);
    assert.equal(env.feed.relationships[0].status, 'updated');
    env.feed.appendRelationship({ id: live.id, t: 201, relation: { id: 'relation', checkedAt: 201 } });
    assert.equal(env.feed.relationships[0].relation.checkedAt, 201);
  } finally { env.cleanup(); }
});

test('an unknown-version snapshot cannot roll back a paused SSE relation', async () => {
  const env = setup({ at: 400, trades: [], relationships: [{ id: live.id, status: 'old' }] });
  try {
    env.feed.loaded = true; env.feed.setPaused(true); env.feed.appendRelationship(live);
    await env.feed.load(); env.feed.setPaused(false);
    assert.deepEqual(env.feed.relationships[0], live);
  } finally { env.cleanup(); }
});

test('SSE received before first snapshot retains rich relations and newer trades', async () => {
  const token = '0x' + '1'.repeat(40), old = { id: 'old', t: 100, token, chainId: '56' };
  const incoming = { id: 'new', t: 200, token, chainId: '56' };
  const env = setup({ at: 100, trades: [old], relationships: [{ id: live.id, t: 200 }] });
  try {
    env.feed.appendRelationship(live); env.feed.appendTrades([incoming]);
    env.feed.removeTrades({ chainId: '56', token, ids: ['old'] });
    await env.feed.load();
    assert.deepEqual(env.feed.relationships[0], live);
    assert.deepEqual(env.feed.trades.map(t => t.id), ['new']);
  } finally { env.cleanup(); }
});

test('stream recovery fresh load bypasses the ordinary HTTP request and rejects failure', async () => {
  const original = global.fetch, calls = [];
  let release;
  global.fetch = async url => {
    calls.push(String(url));
    if (String(url).includes('fresh=1')) return new Response('{}', { status: 503 });
    return new Promise(resolve => { release = () => resolve(reply({ at: 100, trades: [], relationships: [] })); });
  };
  setActivePinia(createPinia()); const feed = useFeedStore();
  const ordinary = feed.load('56');
  try {
    await assert.rejects(feed.load('56', { fresh: true }), /HTTP 503/);
    assert.ok(feed._loading, 'ordinary request is still pending');
    assert.deepEqual(calls, ['/api/feed?chain=56', '/api/feed?chain=56&fresh=1']);
  } finally { release(); await ordinary; global.fetch = original; }
});
