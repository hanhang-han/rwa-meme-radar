import test from 'node:test';
import assert from 'node:assert/strict';
import { relationBadgeType, relationLevel } from '../src/utils/product-labels.js';

const pool = `0x${'1'.repeat(40)}`;
const stock = `0x${'2'.repeat(40)}`;

test('verified pool identity survives stale valuation without becoming current A', () => {
  const relation = { status:'verified', pool, stock, evidenceStatus:'liquidity-stale' };
  assert.equal(relationBadgeType(relation), 'recorded-pending');
  assert.equal(relationLevel(relation), null);
  assert.equal(relationBadgeType({ ...relation, level:'A' }), 'recorded-pending');
  assert.equal(relationBadgeType({ status:'verified', pool, stock, liquidityStatus:'stale' }), 'recorded-pending');
});

test('badge distinguishes verified pairing from absent or invalid pool evidence', () => {
  assert.equal(relationBadgeType({ status:'verified', pool, stock }), 'recorded');
  assert.equal(relationBadgeType({ status:'pending', pool, stock, evidenceStatus:'liquidity-stale' }), 'unknown');
  assert.equal(relationBadgeType({ status:'verified', pool:'0xshort', stock }), 'unknown');
  assert.equal(relationBadgeType({ status:'verified', pool, stock:null }), 'unknown');
  assert.equal(relationBadgeType(null), 'unknown');
});

test('verified onchain pool does not imply verified issuer identity or an available valuation', () => {
  const relation = { status:'verified', pool, stock };
  assert.equal(relationBadgeType({ ...relation, evidenceStatus:'issuer-deployment-unverified', liquidityStatus:'stale' }), 'recorded-stock-unverified');
  assert.equal(relationBadgeType({ ...relation, evidenceStatus:'liquidity-unknown' }), 'recorded-no-valuation');
  assert.equal(relationBadgeType({ ...relation, level:'A', evidenceStatus:'issuer-deployment-unverified' }), 'recorded-stock-unverified');
});

test('current A/B/C relationships preserve their existing badge types', () => {
  for (const level of ['A','B','C']) assert.equal(relationBadgeType({ level }), level);
  assert.equal(relationBadgeType({ status:'verified', pool, stock, level:'A', evidenceStatus:'qualified' }), 'A');
});
