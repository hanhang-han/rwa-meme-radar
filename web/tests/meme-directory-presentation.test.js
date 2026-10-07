import test from 'node:test';
import assert from 'node:assert/strict';
import { directoryObservation, comparableDirectoryPoints, directoryAssetKey, directoryRelationState } from '../src/utils/meme-directory-presentation.js';

const now = 1_790_994_300_000;
const asset = {
  chainId: '56', token: '0xABC', price: 2, priceCurrency: 'USD', quoteAt: now - 1000,
  volume24h: 0, volumeCurrency: 'USD', volumeScope: 'token', totalLiquidityUsd: 0,
  totalLiquidityStatus: 'current', totalLiquidityAt: now - 2000,
  totalLiquidityCoverage: { provider: 'DexScreener', complete: true },
  fieldTimes: { volume24h: now - 3000 }, fieldSources: { volume24h: 'OKX' }, provider: 'OKX',
};

test('real zeroes retain their units and their own timestamps', () => {
  assert.deepEqual(directoryObservation(asset, 'volume24h', now), {
    value: 0, at: now - 3000, source: 'OKX', currency: 'USD', scope: 'token', scopeKnown: true, state: 'current',
  });
  assert.equal(directoryObservation(asset, 'totalLiquidityUsd', now).value, 0);
  assert.equal(directoryObservation(asset, 'totalLiquidityUsd', now).source, 'DexScreener');
  assert.equal(directoryAssetKey(asset), '56:0xabc');
});

test('fresh quotes do not renew historical volume or liquidity', () => {
  const row = { ...asset, fieldTimes: { volume24h: now - 900001 }, totalLiquidityAt: now - 1800001 };
  assert.equal(directoryObservation(row, 'price', now).state, 'current');
  assert.equal(directoryObservation(row, 'volume24h', now).state, 'historical');
  assert.equal(directoryObservation(row, 'totalLiquidityUsd', now).state, 'historical');
  assert.equal(directoryObservation({ ...asset, totalLiquidityStatus: 'stale' }, 'totalLiquidityUsd', now).state, 'historical');
});

test('unknown volume units never inherit quote USD and unknown scopes remain explicit', () => {
  const row = { ...asset, volumeCurrency: undefined, volumeScope: undefined };
  assert.equal(directoryObservation(row, 'volume24h', now).currency, '');
  assert.equal(directoryObservation(row, 'volume24h', now).scopeKnown, false);
  assert.equal(directoryObservation({ ...asset, volumeCurrency: 'USDT' }, 'volume24h', now).currency, 'USDT');
});

test('coverage failures do not present inherited liquidity zeroes as measured USD zeroes', () => {
  const row = { ...asset, fieldAvailability: { totalLiquidityUsd: { status: 'missing', reason: 'indexed-coverage-incomplete', value: null } } };
  assert.equal(directoryObservation(row, 'totalLiquidityUsd', now).value, null);
  assert.equal(directoryObservation(row, 'totalLiquidityUsd', now).state, 'missing');
  assert.equal(directoryObservation({ ...row, totalLiquidityUsd: 12 }, 'totalLiquidityUsd', now).value, null);
});

test('whole-asset liquidity zero requires complete coverage, including explicit current zero records', () => {
  for (const complete of [false, undefined]) {
    const row = { ...asset, totalLiquidityCoverage: { provider: 'DexScreener', complete } };
    assert.equal(directoryObservation(row, 'totalLiquidityUsd', now).value, null);
    row.fieldAvailability = { totalLiquidityUsd: { status: 'current', value: 0, at: now - 1000 } };
    assert.equal(directoryObservation(row, 'totalLiquidityUsd', now).value, null);
  }
  assert.equal(directoryObservation(asset, 'totalLiquidityUsd', now).value, 0);
});

test('published field availability takes precedence over inherited values, status and time', () => {
  const row = { ...asset, totalLiquidityUsd: 999, totalLiquidityAt: now - 1800001,
    fieldAvailability: { totalLiquidityUsd: { status: 'current', value: 4, at: now - 1000, source: 'Measured pools' } } };
  const value = directoryObservation(row, 'totalLiquidityUsd', now);
  assert.equal(value.value, 4); assert.equal(value.at, now - 1000); assert.equal(value.source, 'Measured pools'); assert.equal(value.state, 'current');
  row.fieldAvailability.totalLiquidityUsd = { status: 'missing', value: null, at: now - 1000 };
  assert.equal(directoryObservation(row, 'totalLiquidityUsd', now).value, null);
  row.fieldAvailability.totalLiquidityUsd = { status: 'stale', reason: 'observation-expired', value: null, at: now - 1800001 };
  assert.equal(directoryObservation(row, 'totalLiquidityUsd', now).value, 999, 'recorded historical estimates remain available with their old time');
  assert.equal(directoryObservation(row, 'totalLiquidityUsd', now).state, 'historical');
});

test('missing, invalid and future observations are not silently current', () => {
  assert.equal(directoryObservation({ ...asset, price: '' }, 'price', now).value, null);
  assert.equal(directoryObservation({ ...asset, volume24h: -1 }, 'volume24h', now).value, null);
  assert.equal(directoryObservation({ ...asset, quoteAt: now + 1000 }, 'price', now).state, 'unknown-time');
  assert.equal(directoryObservation({ ...asset, quoteAt: null }, 'price', now).state, 'unknown-time');
  assert.equal(directoryObservation({ ...asset, totalLiquidityStatus: 'missing' }, 'totalLiquidityUsd', now).state, 'unverified');
});

test('published change windows use their own values, source and status', () => {
  const row = { ...asset, change1h: 7, fieldTimes: { change1h: now }, productMetrics: { changes: { h1: { value: -2, at: now - 1000, source: 'DexScreener', status: 'stale' } } } };
  const result = directoryObservation(row, 'change1h', now);
  assert.equal(result.value, -2); assert.equal(result.source, 'DexScreener'); assert.equal(result.state, 'historical');
  row.productMetrics.changes.h1.value = null;
  assert.equal(directoryObservation(row, 'change1h', now).value, null, 'missing published data is not filled with a differently scoped raw value');
});

test('optional scatter accepts measured zeroes but excludes expired, missing and non-USD observations', () => {
  const valid = { key: 'valid', volume: 0, liquidity: 0, volumeAt: now - 1000, liquidityAt: now - 1000 };
  const points = [valid, { ...valid, key: 'old', volumeAt: now - 900001 }, { ...valid, key: 'unit', volumeCurrency: 'OKB' }, { ...valid, key: 'missing', liquidity: null }, { ...valid, key: 'bad', volume: -1 }];
  assert.deepEqual(comparableDirectoryPoints(points, now).map(point => point.key), ['valid']);
});

test('stock relationships require current usable valuation and verified identity for the current-pair label', () => {
  const relation = { level: 'A', status: 'verified', pool: '0xabc', liquidityUsd: 2000, liquidityAt: now - 1000 };
  assert.equal(directoryRelationState(relation, now), 'current-pair');
  assert.equal(directoryRelationState({ ...relation, liquidityUsd: null }, now), 'liquidity-unknown');
  assert.equal(directoryRelationState({ ...relation, liquidityAt: now - 900001 }, now), 'liquidity-expired');
  assert.equal(directoryRelationState({ ...relation, evidenceStatus: 'issuer-deployment-unverified' }, now), 'issuer-unverified');
  assert.equal(directoryRelationState({ ...relation, stockIdentity: { eligibleForPair: false } }, now), 'issuer-unverified');
  assert.equal(directoryRelationState({ ...relation, liquidityStatus: 'stale' }, now), 'liquidity-expired');
  assert.equal(directoryRelationState({ ...relation, evidenceStatus: 'liquidity-stale' }, now), 'liquidity-expired');
  assert.equal(directoryRelationState({ ...relation, level: 'B' }, now), 'name-match', 'names never upgrade to confirmed pairs');
});
