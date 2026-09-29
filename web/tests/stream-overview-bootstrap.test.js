import test from 'node:test';
import assert from 'node:assert/strict';
import { createPinia, setActivePinia } from 'pinia';
import { startStream, stopStream } from '../src/composables/useStream.js';

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

test('the stream starts from the overview cursor without waiting for the market catalogue', async () => {
  setActivePinia(createPinia());
  const oldFetch = global.fetch;
  const requests = [];
  global.fetch = async (input, options = {}) => {
    const url = String(input);
    requests.push(url);
    if (url.includes('/dashboard?view=overview')) return new Response(JSON.stringify({
      realtime: { schema: 1, cursor: 72, revision: 72 },
      unified: { snapshotScope: 'overview', assets: [], stockTokens: [] },
    }), { status: 200 });
    if (url.includes('/dashboard?view=market')) return new Promise(() => {});
    if (url.includes('/stream?')) return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('event: hello\ndata: {"schema":1,"cursor":72}\n\n'));
        options.signal.addEventListener('abort', () => controller.error(new DOMException('aborted', 'AbortError')), { once: true });
      },
    }), { status: 200 });
    if (url.endsWith('/feed')) return new Response(JSON.stringify({ trades: [], relationships: [] }), { status: 200 });
    throw new Error(`unexpected request: ${url}`);
  };

  try {
    startStream();
    for (let i = 0; i < 50 && !requests.some(url => url.includes('/stream?')); i++) await pause(20);
    const streamUrl = requests.find(url => url.includes('/stream?'));
    assert.ok(streamUrl, 'stream connected before any market catalogue response');
    assert.equal(new URL(streamUrl, 'http://localhost').searchParams.get('after'), '72');
    assert.deepEqual(requests.filter(url => url.includes('/dashboard?')), ['/api/dashboard?view=overview']);
    assert.equal(requests.filter(url => url.endsWith('/feed')).length, 0,
      'the home view owns the initial feed request');
  } finally {
    stopStream();
    global.fetch = oldFetch;
  }
});
