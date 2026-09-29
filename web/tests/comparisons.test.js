import test from 'node:test';
import assert from 'node:assert/strict';
import { createPinia, setActivePinia } from 'pinia';
import { currentMetric, lineSegments, mergePacket, normalizedHistory, chartSamples, windowHistory } from '../src/utils/comparisons.js';
import { useComparisonStore } from '../src/stores/comparisons.js';
import { handleStreamEvent } from '../src/composables/useStream.js';

test('a quiet connection still downgrades and expires an old comparison', () => {
  const m = {value:2, status:'realtime', realtimeUntil:30, validUntil:300};
  assert.equal(currentMetric(m, 10).status, 'realtime');
  assert.equal(currentMetric(m, 31).status, 'snapshot');
  assert.equal(currentMetric(m, 301).value, null);
  assert.equal(m.value, 2);
  assert.equal(currentMetric({...m,value:Infinity}, 10).value, null);
});

test('an old HTTP response cannot replace a newer comparison push', () => {
  const current = {chainId:'196',token:'0xA',calculatedAt:200,pairs:[]};
  assert.equal(mergePacket(current, {...current,calculatedAt:100}), current);
});

test('comparison SSE accepts full packet, isolates chains, and rejects malformed frames', async () => {
  setActivePinia(createPinia());
  assert.equal(await handleStreamEvent('comparison', JSON.stringify({chainId:'196',token:'0xA',calculatedAt:200,pairs:[],premium:{value:2}})), true);
  const store = useComparisonStore();
  store.receive({chainId:'56',token:'0xA',calculatedAt:201,pairs:[],premium:{value:3}});
  assert.equal(store.latest.get('196:0xa').premium.value, 2);
  assert.equal(store.latest.get('56:0xa').premium.value, 3);
  assert.equal(await handleStreamEvent('comparison', '{"token":"0xa"}'), false);
});

test('normalized prices use a shared baseline and reset on a provider change', () => {
  const rows = normalizedHistory([
    {at:1,stock:100,meme:10,stockSource:'A',adjustmentVersion:'v1'},
    {at:2,stock:103,meme:12,stockSource:'A',adjustmentVersion:'v1'},
  ]);
  assert.deepEqual(rows, [{at:1,stock:100,meme:100},{at:2,stock:103,meme:120}]);
  assert.deepEqual(normalizedHistory([{at:1,stock:1,meme:1,stockSource:'A',adjustmentVersion:'v1'}, {at:2,stock:2,meme:3,stockSource:'B',adjustmentVersion:'v1'}]), [{at:2,stock:100,meme:100}]);
});

test('charts break at missing values and long gaps rather than inventing continuity', () => {
  assert.equal(lineSegments([{at:1,value:1},{at:2,value:null},{at:3,value:2}], 'value').length, 2);
  assert.equal(lineSegments([{at:1,value:1},{at:400000,value:2}], 'value').length, 2);
});

test('chart reduction preserves real spikes and does not manufacture points', () => {
  const rows = Array.from({length:5000}, (_,i) => ({at:i*1000, value:i===2333?100:1}));
  const selected = chartSamples(rows, ['value']);
  assert.ok(selected.length < 500);
  assert.ok(selected.some(p => p.at===rows[2333].at && p.value===100));
  assert.ok(selected.every(p => rows.some(r => r.at===p.at && r.value===p.value)));
  const longDay = rows.map(r => ({...r,at:r.at*17}));
  assert.equal(lineSegments(chartSamples(longDay, ['value']), 'value').length, 1);
});


test('window selection really changes the visible history', () => {
  const now = 86400000;
  const points = [{at:1,value:1},{at:now-3500000,value:2},{at:now,value:3},{at:now+2000,value:4}];
  assert.equal(windowHistory(points, 3600000, now).length, 2);
  assert.equal(windowHistory(points, 86400000, now).length, 3);
});

test('corporate-action versions reset the chart instead of displaying a false split return', () => {
  const rows = [{at:1,stock:100,meme:10,adjustmentVersion:'pre-split'}, {at:2,stock:50,meme:10,adjustmentVersion:'post-split'}];
  assert.deepEqual(normalizedHistory(rows), [{at:2,stock:100,meme:100}]);
  assert.deepEqual(normalizedHistory([{at:1,stock:100,meme:10}]), []);
  assert.equal(lineSegments([{at:1,value:2,version:'old'}, {at:2,value:2,version:'new'}], 'value').length, 2);
});
