import test from 'node:test';
import assert from 'node:assert/strict';
import { consumeSseStream, parseSseFrame } from '../src/composables/useStream.js';

const encoder = new TextEncoder();
const wire = (id, value = { value: id }) => `id: ${id}\nevent: projection.delta\ndata: ${JSON.stringify(value)}\n\n`;
const readerFor = chunks => {
  let index = 0;
  return { read: async () => index < chunks.length
    ? { done: false, value: chunks[index++] }
    : { done: true } };
};
const noSizeOrTimeLimit = { maxChars: Infinity, maxMs: Infinity };

test('a single-chunk replay yields to timers in bounded batches without dropping or reordering frames', async () => {
  const received = [];
  let countAtInput;
  const input = new Promise(resolve => setTimeout(() => { countAtInput = received.length; resolve(); }, 0));
  await consumeSseStream(readerFor([encoder.encode(Array.from({ length: 61 }, (_, i) => wire(i + 1)).join(''))]),
    frame => { received.push(Number(parseSseFrame(frame).id)); }, noSizeOrTimeLimit);
  await input;
  assert.equal(countAtInput, 20, 'input gets a real task between bounded batches, not after all replay frames');
  assert.deepEqual(received, Array.from({ length: 61 }, (_, i) => i + 1));
});

test('the budget spans immediately available network chunks instead of restarting on each read', async () => {
  const received = [];
  let countAtInput;
  const input = new Promise(resolve => setTimeout(() => { countAtInput = received.length; resolve(); }, 0));
  const chunks = Array.from({ length: 41 }, (_, i) => encoder.encode(wire(i + 1)));
  await consumeSseStream(readerFor(chunks), frame => { received.push(parseSseFrame(frame).id); }, noSizeOrTimeLimit);
  await input;
  assert.equal(countAtInput, 20);
  assert.equal(received.length, 41);
  assert.equal(received.at(-1), '41');
});

test('large frames give input a turn before parsing and retain every payload and cursor', async () => {
  const payload = '市'.repeat(800_000);
  const received = [];
  let inputRan = false;
  const input = new Promise(resolve => setTimeout(() => { inputRan = true; resolve(); }, 0));
  const chunk = encoder.encode(wire(1, { payload }) + wire(2));
  await consumeSseStream(readerFor([chunk]), frame => {
    assert.equal(inputRan, true, 'a large frame yields before its synchronous JSON parse');
    const parsed = parseSseFrame(frame);
    received.push({ id: parsed.id, value: JSON.parse(parsed.data) });
  });
  await input;
  assert.deepEqual(received, [{ id: '1', value: { payload } }, { id: '2', value: { value: 2 } }]);
});

test('a time budget yields even before the frame-count limit is reached', async () => {
  let clock = 0;
  let received = 0;
  let countAtInput;
  const input = new Promise(resolve => setTimeout(() => { countAtInput = received; resolve(); }, 0));
  await consumeSseStream(readerFor([encoder.encode(wire(1) + wire(2) + wire(3) + wire(4))]), () => {
    received += 1;
    clock += 3;
  }, { maxFrames: 100, maxChars: Infinity, maxMs: 8, now: () => clock });
  await input;
  assert.equal(countAtInput, 3);
  assert.equal(received, 4);
});

test('split CRLF separators and UTF-8 characters retain complete frames but never commit an incomplete tail', async () => {
  const body = (wire(8, { name: '腾讯控股' }) + wire(9, { name: '英伟达' }) + 'id: 10\ndata: {"partial":').replace(/\n/g, '\r\n');
  const bytes = encoder.encode(body);
  const received = [];
  await consumeSseStream(readerFor([...bytes].map(byte => Uint8Array.of(byte))), frame => {
    const parsed = parseSseFrame(frame);
    received.push({ id: parsed.id, value: JSON.parse(parsed.data) });
  });
  assert.deepEqual(received, [{ id: '8', value: { name: '腾讯控股' } }, { id: '9', value: { name: '英伟达' } }]);
});

test('abort during a yield stops the old subscription before the next buffered frame is applied', async () => {
  const controller = new AbortController();
  const received = [];
  let committedCursor = '0';
  await consumeSseStream(readerFor([encoder.encode(wire(1) + wire(2) + wire(3))]), frame => {
    committedCursor = parseSseFrame(frame).id;
    received.push(committedCursor);
  }, {
    ...noSizeOrTimeLimit,
    maxFrames: 1,
    signal: controller.signal,
    yieldToMain: async () => { controller.abort(); },
  });
  assert.deepEqual(received, ['1']);
  assert.equal(committedCursor, '1', 'reconnect replays from the last applied frame');
  await consumeSseStream(readerFor([encoder.encode(wire(2) + wire(3))]), frame => {
    committedCursor = parseSseFrame(frame).id;
    received.push(committedCursor);
  });
  assert.deepEqual(received, ['1', '2', '3']);
});

test('handler failure does not acknowledge the failed frame or apply later frames', async () => {
  let committedCursor = '0';
  await assert.rejects(consumeSseStream(readerFor([encoder.encode(wire(1) + wire(2) + wire(3))]), frame => {
    const { id } = parseSseFrame(frame);
    if (id === '2') throw new Error('projection unavailable');
    committedCursor = id;
  }), /projection unavailable/);
  assert.equal(committedCursor, '1');
});
