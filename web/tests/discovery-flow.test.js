import test from 'node:test';
import assert from 'node:assert/strict';
import { createPinia, setActivePinia } from 'pinia';
import { handleStreamEvent } from '../src/composables/useStream.js';
import { useDashboardStore } from '../src/stores/dashboard.js';
import { useFeedStore } from '../src/stores/feed.js';
import { eventFromRelationship } from '../src/utils/event-records.js';
import { applyQuote } from '../src/utils/realtime.js';

const event={id:'196:candidate:0xnew:123',chainId:'196',t:123,kind:'discovered',asset:{token:'0xnew',symbol:'NEW',chainId:'196',kind:'candidate',updatedAt:123}};

test('new candidates appear from SSE without waiting for the next full snapshot', async () => {
  setActivePinia(createPinia());
  const dashboard=useDashboardStore(), feed=useFeedStore();
  dashboard.snapshot={unified:{assets:[],signals:[]}};
  assert.equal(await handleStreamEvent('discovery',JSON.stringify(event)),true);
  assert.equal(dashboard.assets[0].token,'0xnew');
  assert.equal(dashboard.snapshot.unified.signals[0].kind,'discovered');
  assert.equal(feed.relationships[0].asset,'0xnew');
  assert.equal(eventFromRelationship(feed.relationships[0]).kind,'discovered');
  await handleStreamEvent('discovery',JSON.stringify(event));
  assert.equal(dashboard.assets.length,1);
  assert.equal(feed.relationships.length,1);
});

test('discovery arriving before an old HTTP snapshot survives and feed reading stays paused', async () => {
  setActivePinia(createPinia());
  const dashboard=useDashboardStore(), feed=useFeedStore();
  feed.setPaused(true);
  await handleStreamEvent('discovery',JSON.stringify(event));
  assert.equal(feed.relationships.length,0);
  assert.equal(feed.pendingRelationships.length,1);
  dashboard.snapshot={unified:{assets:[],signals:[]}};
  dashboard._restoreSse();
  assert.equal(dashboard.assets.length,1);
  feed.clearNew();
  assert.equal(feed.relationships.length,1);
});

test('quote currency and exchange volume scope survive incremental updates', () => {
  const row={price:10,quoteAt:1,buys24h:2,sells24h:3,txs24h:5};
  assert.equal(applyQuote(row,{price:11,marketAt:2,provider:'Binance',priceScope:'exchange',priceCurrency:'USDT',volumeCurrency:'USDT',volumeScope:'exchange',priceProvenance:{timeKind:'market'}}),true);
  assert.equal(row.priceCurrency,'USDT');
  assert.equal(row.volumeCurrency,'USDT');
  assert.equal(row.buys24h,null);
  assert.equal(row.txs24h,null);
});
