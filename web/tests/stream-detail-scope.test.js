import test from 'node:test';
import assert from 'node:assert/strict';
import { createPinia, setActivePinia } from 'pinia';
import { currentTradeScope, startStream, stopStream } from '../src/composables/useStream.js';
import { useDetailStore } from '../src/stores/detail.js';
import { useFeedStore } from '../src/stores/feed.js';

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate) {
  for (let i = 0; i < 150; i++) {
    if (predicate()) return;
    await pause(20);
  }
  assert.fail('timed out waiting for scoped stream reconnection');
}

test('detail changes scope only trade events and leaving detail restores the global feed', async () => {
  setActivePinia(createPinia());
  const oldFetch = global.fetch;
  const requests = [];
  let returningToGlobal = false;
  global.fetch = async (input, options = {}) => {
    const url = String(input);
    if (url.includes('/dashboard?')) return new Response(JSON.stringify({
      realtime: { schema: 1, cursor: 100 }, unified: { assets: [], stockTokens: [] },
    }), { status: 200 });
    if (url.endsWith('/feed')) return new Response(JSON.stringify({
      trades: returningToGlobal ? [{ chainId: '56', token: '0xelsewhere', venue: 'dex',
        marketId: '0xpool', id: 'global-return', t: 200 }] : [], relationships: [], at: 200,
    }), { status: 200 });
    if (url.includes('/stream?')) {
      requests.push(url);
      const body = new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('event: hello\ndata: {"schema":1,"cursor":100}\n\n'));
          options.signal.addEventListener('abort', () => {
            try { controller.error(new DOMException('aborted', 'AbortError')); } catch { /* already closed */ }
          }, { once: true });
        },
      });
      return new Response(body, { status: 200 });
    }
    throw new Error(`unexpected request: ${url}`);
  };

  try {
    const detail = useDetailStore();
    assert.equal(currentTradeScope(), null);
    startStream();
    await until(() => requests.length === 1);
    detail.watch('196', '0xABC');
    assert.equal(currentTradeScope(), '196:0xabc');
    await until(() => requests.length === 2);
    detail.watch('56', '0xABC');
    await until(() => requests.length === 3);
    returningToGlobal = true;
    detail.unwatch();
    await until(() => requests.length === 4);
    await until(() => useFeedStore().trades.some(row => row.id === 'global-return'));

    const scopes = requests.map(raw => new URL(raw, 'http://localhost').searchParams.get('trades'));
    assert.deepEqual(scopes, [null, '196:0xabc', '56:0xabc', null]);
    assert.ok(requests.every(raw => new URL(raw, 'http://localhost').searchParams.get('after') === '100'));
  } finally {
    stopStream();
    global.fetch = oldFetch;
  }
});
