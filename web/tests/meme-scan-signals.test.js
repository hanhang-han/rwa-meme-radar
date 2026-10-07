import test from 'node:test';
import assert from 'node:assert/strict';
import { memeGroupMembers, memeStockRelations } from '../src/utils/meme-scan-signals.js';

const now = 1_790_994_300_000;
const token = '0x' + 'a'.repeat(40);
const asset = { chainId: '56', token, symbol: 'SAME' };
const pair = { ...asset, ticker: '700', level: 'A', status: 'verified', pool: '0x' + 'b'.repeat(40), stock: '0x' + 'c'.repeat(40), liquidityUsd: 2000, liquidityAt: now - 1000 };

test('same-name members keep exact contract identity, reading order and the representative', () => {
  const otherContract = { ...asset, token: '0x' + 'd'.repeat(40) };
  const otherChain = { ...asset, chainId: '196' };
  const duplicate = { ...asset, token: token.toUpperCase(), price: 99 };
  const members = [duplicate, otherContract, { ...otherContract }, otherChain];
  const result = memeGroupMembers(asset, members);
  assert.deepEqual(result, [asset, otherContract, otherChain]);
  assert.equal(result[0], asset, 'a duplicate member cannot replace the displayed representative');
  assert.equal(members.length, 4, 'presentation does not alter the store member segments');
  assert.deepEqual(memeGroupMembers(null, members), [duplicate, otherContract, otherChain]);
});

test('stock hints match the exact chain and contract and never upgrade a name clue', () => {
  const hint = { ...asset, level: 'B', ticker: '0700' };
  const unrelated = [{ ...pair, chainId: '196' }, { ...pair, token: '0x' + 'd'.repeat(40) }];
  const result = memeStockRelations({ ...asset, match: { level: 'B', ticker: '700' } }, [hint, ...unrelated], now);
  assert.equal(result.length, 1);
  assert.equal(result[0].ticker, '700');
  assert.equal(result[0].level, 'B');
  assert.equal(result[0].pool, undefined, 'matching a name does not invent pool evidence');
});

test('a stock theme appears once with its strongest currently usable evidence', () => {
  const expired = { ...pair, id: 'expired', ticker: '0700', liquidityAt: now - 900001 };
  const current = { ...pair, id: 'current' };
  const community = { ...asset, level: 'C', ticker: 'TSLA' };
  const name = { ...asset, level: 'B', ticker: '700' };
  const records = [name, expired, community, current];
  const result = memeStockRelations(asset, records, now);
  assert.deepEqual(result.map(relation => relation.ticker), ['700', 'TSLA']);
  assert.equal(result[0].id, 'current');
  assert.equal(records[0], name, 'the original records remain unchanged');
  const later = memeStockRelations(asset, records, now + 900000);
  assert.equal(later[0].pool, pair.pool, 'expired valuation retains the recorded pool identity');
});
