import { test } from 'node:test';
import assert from 'node:assert/strict';
import { assessRelation, relationTypes } from '../src/lib/relations';
import { numeric, signature, refreshOkx, okxState } from '../src/lib/okx';
import { createHmac } from 'node:crypto';

test('name matches never become verified financial or safety claims', () => {
  const result = assessRelation({ ticker: 'TSLA', matchType: 'company' });
  assert.equal(result.status, 'candidate');
  assert.equal(result.safety, 'unchecked');
  assert.deepEqual(result.types, ['name']);
  assert.equal(result.evidence[0].verified, false);
  assert.equal(assessRelation(null).status, 'unknown');
  assert.equal(relationTypes.length, 6);
});
test('missing or invalid prices remain null while zero is preserved', () => {
  for (const value of ['', undefined, null, 'NaN', Infinity, {}, false]) assert.equal(numeric(value), null);
  assert.equal(numeric('0'), 0);
  assert.equal(numeric('123.45'), 123.45);
});
test('signed path includes exact encoded query', () => {
  const path = '/api/v6/dex/market/rwa/tokens?chainIndex=196&cursor=Mg%3D%3D';
  assert.equal(signature('timestamp', path, 'secret'), createHmac('sha256', 'secret').update('timestampGET' + path).digest('base64'));
});
test('OKX pagination filters other chains and retains last good data on failure', async () => {
  const original = globalThis.fetch;
  const names = ['OKX_API_KEY','OKX_SECRET_KEY','OKX_PASSPHRASE'];
  const previous = names.map(k => process.env[k]);
  names.forEach(k => process.env[k] = 'test');
  let calls = 0;
  try {
    globalThis.fetch = (async (input) => {
      calls++;
      const url = new URL(String(input));
      assert.equal(url.searchParams.get('chainIndex'), '196');
      assert.equal(url.searchParams.get('category'), '47');
      assert.equal(url.searchParams.get('limit'), '100');
      return new Response(JSON.stringify({code:'0', data:{ cursor: calls === 1 ? 'next' : '', list: [
        {chainIndex: calls === 1 ? '56' : '196', tokenContractAddress: '0x'+'a'.repeat(40), price:'', stockPrice:'2'}
      ]}}));
    }) as typeof fetch;
    await refreshOkx();
    assert.equal(calls, 2);
    assert.equal(okxState.tokens.length, 1);
    assert.equal(okxState.tokens[0].price, null);
    assert.equal(okxState.status, 'ready');
    globalThis.fetch = (async () => new Response('{}', {status:401})) as typeof fetch;
    await refreshOkx();
    assert.equal(okxState.status, 'stale');
    assert.equal(okxState.tokens.length, 1);
  } finally {
    globalThis.fetch = original;
    names.forEach((k,i) => previous[i] === undefined ? delete process.env[k] : process.env[k] = previous[i]);
  }
});

test('direct pool verification requires both asset addresses; RPC errors are not absence', async () => {
  const { verifyDirectPools } = await import('../src/lib/verification');
  const meme = '0x'+'a'.repeat(40), stock = '0x'+'b'.repeat(40), pool = '0x'+'c'.repeat(40);
  const word = (a: string) => '0x'+'0'.repeat(24)+a.slice(2);
  let calls = 0;
  const good = await verifyDirectPools(meme, [{addr:stock,symbol:'TSLAx'}], async () => word([pool,meme,stock][calls++]));
  assert.equal(good.status, 'verified');
  assert.equal(good.pools[0].pool, pool);
  const bad = await verifyDirectPools(meme, [{addr:stock,symbol:'TSLAx'}], async () => word(pool));
  assert.equal(bad.status, 'error');
  const absent = await verifyDirectPools(meme, [{addr:stock,symbol:'TSLAx'}], async () => '0x'+'0'.repeat(64));
  assert.equal(absent.status, 'not_found');
});
