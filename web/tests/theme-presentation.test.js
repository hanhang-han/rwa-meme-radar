import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildThemeAssetMap,
  buildThemeRows,
  countThemeRows,
  fieldCoverage,
  observationState,
  sortThemeRows,
} from '../src/utils/theme-presentation.js';

const NOW = 1_000_000;

test('theme rows group pools by chain and asset, then select a fresh representative pool', () => {
  const relations = [
    { id: 'old-large', status: 'verified', level: 'A', chainId: '196', token: '0xA', pool: '0x1', liquidityUsd: 5000, liquidityAt: NOW - 2_000_000 },
    { id: 'fresh-small', status: 'verified', level: 'A', chainId: '196', token: '0xa', pool: '0x2', liquidityUsd: 100, liquidityAt: NOW - 100 },
    { id: 'other-chain', status: 'verified', level: 'A', chainId: '56', token: '0xa', pool: '0x3', liquidityUsd: 200, liquidityAt: NOW - 100 },
    { id: 'unverified-issuer', status: 'verified', level: null, chainId: '56', token: '0xb', pool: '0x4', liquidityUsd: 999999, liquidityAt: NOW - 100 },
  ];
  const assets = [
    { chainId: '196', token: '0xa', symbol: 'ONE' },
    { chainId: '56', token: '0xa', symbol: 'TWO' },
  ];
  const rows = buildThemeRows(relations, assets, NOW);
  assert.equal(rows.length, 2);
  const xlayer = rows.find((row) => row.chainId === '196');
  assert.equal(xlayer.poolCount, 2);
  assert.equal(xlayer.relation.id, 'fresh-small');
  assert.equal(xlayer.asset.symbol, 'ONE');
  assert.equal(rows.some(row => row.relation.id === 'unverified-issuer'),false);
  assert.deepEqual(buildThemeRows(relations, buildThemeAssetMap(assets), NOW), rows);
  assert.equal(countThemeRows(relations), rows.length);
});

test('theme count follows first pool observation when duplicate pools claim different assets', () => {
  const relations = [
    { id: 'first', level: 'A', chainId: '196', token: '0xa', pool: '0xpool' },
    { id: 'duplicate', level: 'A', chainId: '196', token: '0xb', pool: '0xpool' },
    { id: 'other', level: 'A', chainId: '56', token: '0xb', pool: '0xpool' },
  ];
  assert.equal(countThemeRows(relations), 2);
  assert.deepEqual(buildThemeRows(relations, []).map(row => row.key), ['196:0xa', '56:0xb']);
});

test('historical toggle keeps only rows with a fresh pool estimate', () => {
  const rows = [
    { key: 'fresh', liquidityState: 'fresh', relation: { liquidityUsd: 1 }, asset: {} },
    { key: 'old', liquidityState: 'historical', relation: { liquidityUsd: 100 }, asset: {} },
  ];
  assert.deepEqual(sortThemeRows(rows, 'poolLiquidity', false, NOW).map((row) => row.key), ['fresh']);
  assert.deepEqual(sortThemeRows(rows, 'poolLiquidity', true, NOW).map((row) => row.key), ['fresh', 'old']);
});

test('zero is an observed value and coverage does not treat it as missing', () => {
  assert.equal(observationState(0, NOW - 1, NOW), 'fresh');
  const coverage = fieldCoverage([
    { volume5m: 0, fieldTimes: { volume5m: NOW - 1 } },
    { volume5m: null, fieldTimes: { volume5m: NOW - 1 } },
    { volume5m: 4, fieldTimes: { volume5m: NOW - 2_000_000 } },
  ], 'volume5m', NOW);
  assert.deepEqual(coverage, { field: 'volume5m', total: 3, known: 2, fresh: 1 });
});
