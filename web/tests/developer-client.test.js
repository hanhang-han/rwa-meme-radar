import test from 'node:test';
import assert from 'node:assert/strict';
import { createDeveloperKey, getDeveloperSession, registerDeveloper, revokeDeveloperKey } from '../src/api/developer.js';

test('developer registration uses invitation and a same-origin credentialed request', async () => {
  const original = globalThis.fetch;
  let call;
  globalThis.fetch = async (url, options) => {
    call = { url, options };
    return { ok: true, json: async () => ({ user: { email: 'a@example.com' }, csrfToken: 'csrf' }) };
  };
  try {
    await registerDeveloper('a@example.com', 'INVITE', 'long-test-password');
    assert.equal(call.url, '/api/developer/register');
    assert.equal(call.options.credentials, 'include');
    assert.deepEqual(JSON.parse(call.options.body), { email: 'a@example.com', inviteCode: 'INVITE', password: 'long-test-password' });
    assert.equal(call.options.headers.Authorization, undefined);
  } finally { globalThis.fetch = original; }
});

test('session recovery treats unauthorized as a real 401 and key mutations carry CSRF', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith('/me')) return { ok: false, status: 401, json: async () => ({ detail: 'unauthorized' }) };
    return { ok: true, json: async () => ({ secret: 'cx_trial_once' }) };
  };
  try {
    await assert.rejects(() => getDeveloperSession(), error => error.status === 401);
    await createDeveloperKey('Research', 'csrf-value');
    await revokeDeveloperKey('key-1', 'csrf-value');
    assert.equal(calls[1].options.headers['X-CSRF-Token'], 'csrf-value');
    assert.equal(calls[1].options.credentials, 'include');
    assert.equal(calls[2].options.method, 'DELETE');
    assert.equal(calls[2].options.headers['X-CSRF-Token'], 'csrf-value');
  } finally { globalThis.fetch = original; }
});
