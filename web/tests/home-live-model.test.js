import test from 'node:test';
import assert from 'node:assert/strict';
import { createReadingBuffer, filterHomeThemeRecords, mergeHomeThemeAssets } from '../src/utils/home-live-model.js';

const relation = (overrides = {}) => ({ chainId:'56', token:'0xa', pool:'0xp', stock:'0xstock', ticker:'700', level:'A', status:'verified', ...overrides });
const trade = (id, overrides = {}) => ({ id, chainId:'56', token:'0xa', venue:'dex', type:'buy', ...overrides });
const event = (id, overrides = {}) => ({ id, chainId:'56', token:'0xa', pool:'0xp', ticker:'700', kind:'pair-observed', ...overrides });
const key = row => `${row.chainId}:${row.id}`;
const ids = result => result.rows.map(row => row.id);

test('all-market filtering preserves records and selected empty themes never fall back to all', () => {
  const records = [trade('a'), event('b')];
  assert.equal(filterHomeThemeRecords(records, ''), records);
  assert.deepEqual(filterHomeThemeRecords(records, 'NVDA'), []);
  assert.deepEqual(filterHomeThemeRecords(null, ''), []);
});

test('trade themes normalize HK codes and use chain plus normalized contract, not a label', () => {
  const records = [trade('correct', { token:'56:0xA' }), trade('wrong-chain', { chainId:'196' }),
    trade('wrong-token', { token:'0xb', ticker:'00700' }), trade('right-identity-wrong-label', { ticker:'TSLA' })];
  assert.deepEqual(filterHomeThemeRecords(records, '00700', [relation({ token:'0xA' })]).map(row => row.id),
    ['correct', 'right-identity-wrong-label']);
});

test('name-only and unverified relationships cannot put a trade in a stock theme', () => {
  const records = [trade('name-only', { token:'0xname' }), trade('observed', { token:'0xobserved' }), trade('revoked', { token:'0xrevoked' })];
  const relations = [relation({ token:'0xname', level:'B', evidenceStatus:'name-only' }),
    relation({ token:'0xobserved', status:'unverified' }), relation({ token:'0xrevoked', status:'revoked' })];
  const assets = [{ chainId:'56', token:'0xname', match:{ level:'B', evidenceStatus:'name-only', ticker:'700' } }];
  assert.deepEqual(filterHomeThemeRecords(records, '700', relations, assets), []);
});

test('official stock contract can resolve a relation with no ticker, without crossing chains', () => {
  const assets = [{ chainId:'56', tokenContractAddress:'0xSTOCK', stockCode:'00700', tokenSymbol:'TENCENTx' }];
  const relations = [relation({ ticker:null }), relation({ chainId:'196', ticker:null })];
  assert.deepEqual(filterHomeThemeRecords([trade('correct'), trade('wrong-chain', { chainId:'196' })], '700', relations, assets).map(row => row.id), ['correct']);
  assert.deepEqual(filterHomeThemeRecords([event('symbol', { ticker:null, stockSymbol:'TENCENTx' })], '700', [], assets).map(row => row.id), ['symbol']);
});

test('pool discovery labels work before qualification but do not upgrade or mutate evidence', () => {
  const observed = event('own-observation', { ticker:'00700', token:'0xnew', pool:'0xnewpool' });
  assert.deepEqual(filterHomeThemeRecords([observed], '700'), [observed]);
  assert.equal(observed.kind, 'pair-observed');
  assert.equal(observed.level, undefined);
  assert.deepEqual(filterHomeThemeRecords([event('missing-pool', { pool:null })], '700'), []);
  assert.deepEqual(filterHomeThemeRecords([event('missing-chain', { chainId:null })], '700'), []);
});

test('the event own stock and pool are kept distinct when a token has several relations', () => {
  const records = [event('correct'), event('other-stock', { ticker:'1700' }),
    event('unknown-pool', { ticker:null, pool:'0xother' }), event('nested', { chainId:null, token:null, ticker:null, pool:null,
      relation:relation({ token:'56:0xA' }) })];
  assert.deepEqual(filterHomeThemeRecords(records, '700', [relation()]).map(row => row.id), ['correct', 'nested']);
});

test('discovery name clues require explicit matching evidence; bare names and labels do not qualify', () => {
  const records = [event('bare', { pool:null, kind:'discovered', token:'0xbare' }),
    event('projected', { pool:null, kind:'discovered', token:'0xprojected', matchType:'symbol', keyword:'700' }),
    event('own-match', { pool:null, kind:'discovered', token:'0xown', ticker:null,
      match:{ level:'B', evidenceStatus:'name-only', ticker:'00700' } }),
    event('indexed-match', { pool:null, kind:'discovered', token:'0xindexed', ticker:null }),
    event('lookalike-chain', { pool:null, kind:'discovered', token:'0xindexed', chainId:'196', ticker:null })];
  const assets = [{ chainId:'56', token:'0xindexed', match:{ level:'B', evidenceStatus:'name-only', ticker:'700' } }];
  assert.deepEqual(filterHomeThemeRecords(records, '700', [], assets).map(row => row.id), ['projected', 'own-match', 'indexed-match']);
});

test('activity buffer treats an empty preload and the first real snapshot as bootstrap', () => {
  const buffer = createReadingBuffer({ key, limit:2 });
  assert.deepEqual(ids(buffer.sync([])), []);
  const initial = buffer.sync([trade('a'), trade('b'), trade('c')], { paused:true });
  assert.deepEqual(ids(initial), ['a', 'b']);
  assert.equal(initial.pendingCount, 0);
  assert.equal(initial.addedKeys.size, 0);
});

test('explicitly paused activity retains order, sees corrections, and buffers only unique new identities', () => {
  const buffer = createReadingBuffer({ key, limit:2 });
  buffer.sync([trade('a', { volume:1 }), trade('b')]);
  const result = buffer.sync([trade('c'), trade('c'), trade('b'), trade('a', { volume:9 })], { paused:true });
  assert.deepEqual(ids(result), ['a', 'b']);
  assert.equal(result.rows[0].volume, 9);
  assert.equal(result.pendingCount, 1);
  assert.equal(result.addedKeys.size, 0);
  assert.equal(buffer.sync([trade('c', { volume:10 }), trade('b'), trade('a')], { paused:true }).pendingCount, 1);
});

test('a disappeared visible or pending row cannot be resurrected on flush', () => {
  const buffer = createReadingBuffer({ key, limit:3 });
  buffer.sync([trade('a'), trade('b')]);
  buffer.sync([trade('c'), trade('b'), trade('a')], { paused:true });
  const removed = buffer.sync([trade('b')], { paused:true });
  assert.deepEqual(ids(removed), ['b']);
  assert.equal(removed.pendingCount, 0);
  assert.deepEqual(ids(buffer.flush()), ['b']);
});

test('resume and explicit flush follow upstream order and highlight newly displayed rows only', () => {
  const buffer = createReadingBuffer({ key, limit:2 });
  buffer.sync([trade('a'), trade('b'), trade('c')]);
  buffer.sync([trade('d'), trade('e'), trade('a'), trade('b'), trade('c')], { paused:true });
  const shown = buffer.flush();
  assert.deepEqual(ids(shown), ['d', 'e']);
  assert.deepEqual(shown.addedKeys, new Set(['56:d', '56:e']));
  assert.equal(shown.pendingCount, 0);
  const resumed = buffer.sync([trade('f'), trade('d'), trade('e')]);
  assert.deepEqual(ids(resumed), ['f', 'd']);
  assert.deepEqual(resumed.addedKeys, new Set(['56:f']));
  assert.equal(buffer.sync([trade('f'), trade('d'), trade('e')]).addedKeys.size, 0);
});

test('reordering known records does not count as new; initially hidden records are known', () => {
  const buffer = createReadingBuffer({ key, limit:2 });
  buffer.sync([trade('a'), trade('b'), trade('c')]);
  const result = buffer.sync([trade('c'), trade('b'), trade('a')]);
  assert.deepEqual(ids(result), ['c', 'b']);
  assert.equal(result.addedKeys.size, 0);
});

test('theme/chain/filter context changes clear old pending rows and bootstrap the new view', () => {
  const buffer = createReadingBuffer({ key, limit:2, context:'all:700' });
  buffer.sync([trade('a')]);
  buffer.sync([trade('b'), trade('a')], { paused:true });
  const next = buffer.sync([trade('x', { chainId:'196' })], { paused:true, context:'196:NVDA' });
  assert.deepEqual(ids(next), ['x']);
  assert.equal(next.pendingCount, 0);
  assert.equal(next.addedKeys.size, 0);
  assert.deepEqual(buffer.reset('all').rows, []);
  assert.deepEqual(ids(buffer.flush()), []);
});

test('default buffer key keeps identical feed IDs on different chains separate', () => {
  const buffer = createReadingBuffer({ limit:5 });
  buffer.sync([trade('same')]);
  const result = buffer.sync([trade('same', { chainId:'196' }), trade('same')], { paused:true });
  assert.equal(result.pendingCount, 1);
  assert.equal(buffer.flush().rows.length, 2);
});

const quotedAsset = (overrides = {}) => ({ chainId:'56', token:'0xa', name:'Theme asset', price:1, priceCurrency:'USD',
  change24h:5, volume24h:100, volumeCurrency:'USD', volumeScope:'token-aggregate',
  fieldTimes:{price:100,change24h:100,volume24h:100}, fieldSources:{price:'historical-price',change24h:'historical-change',volume24h:'historical-volume'},
  fieldScopes:{price:'token',change24h:'token',volume24h:'token-aggregate'}, ...overrides });

test('theme live enrichment keeps exact membership, normalizes addresses, and isolates the chain', () => {
  const theme = [quotedAsset(), quotedAsset({chainId:'196'})];
  const merged = mergeHomeThemeAssets(theme, [quotedAsset({token:'56:0xA',price:2,fieldTimes:{price:200}}),
    quotedAsset({token:'0xunrelated',price:999,fieldTimes:{price:300}})]);
  assert.equal(merged.length, 2);
  assert.equal(merged[0].price, 2);
  assert.equal(merged[1].price, 1);
  assert.equal(theme[0].price, 1);
});

test('a newer live quote updates the selected theme without replacing newer HTTP fields with older clocks', () => {
  const theme = quotedAsset();
  const live = quotedAsset({price:2,change24h:99,volume24h:999,priceCurrency:'USDT',priceScope:'exchange',volumeCurrency:'USDT',volumeScope:'pool:0xp',
    fieldTimes:{price:200,change24h:90,volume24h:90},
    fieldSources:{price:'new-price',change24h:'old-change',volume24h:'old-volume'},
    fieldScopes:{price:'exchange',change24h:'exchange',volume24h:'pool:0xp'}});
  const [merged] = mergeHomeThemeAssets([theme], [live]);
  assert.equal(merged.price, 2);
  assert.equal(merged.priceCurrency, 'USDT');
  assert.equal(merged.fieldSources.price, 'new-price');
  assert.equal(merged.fieldTimes.price, 200);
  assert.equal(merged.change24h, 5);
  assert.equal(merged.fieldSources.change24h, 'historical-change');
  assert.equal(merged.volume24h, 100);
  assert.equal(merged.volumeCurrency, 'USD');
  assert.equal(merged.volumeScope, 'token-aggregate');
  assert.equal(merged.fieldSources.volume24h, 'historical-volume');
  assert.equal(merged.fieldScopes.volume24h, 'token-aggregate');
  assert.deepEqual(theme, quotedAsset());
});

test('fresh volume and change metadata do not relabel an older retained price', () => {
  const theme = quotedAsset();
  const live = quotedAsset({price:999,change24h:-2,volume24h:200,priceCurrency:'wNVDAx',volumeCurrency:'USDT',volumeScope:'pool:0xother',
    fieldTimes:{price:50,change24h:150,volume24h:150},
    fieldSources:{price:'old-native-price',change24h:'new-change',volume24h:'new-volume'},
    fieldScopes:{price:'pool:0xother',change24h:'pool:0xother',volume24h:'pool:0xother'}});
  const [merged] = mergeHomeThemeAssets([theme], [live]);
  assert.equal(merged.price, 1);
  assert.equal(merged.priceCurrency, 'USD');
  assert.equal(merged.fieldTimes.price, 100);
  assert.equal(merged.fieldSources.price, 'historical-price');
  assert.equal(merged.change24h, -2);
  assert.equal(merged.fieldSources.change24h, 'new-change');
  assert.equal(merged.volume24h, 200);
  assert.equal(merged.volumeCurrency, 'USDT');
  assert.equal(merged.volumeScope, 'pool:0xother');
  assert.equal(merged.fieldSources.volume24h, 'new-volume');
});

test('higher canonical projection revision preserves explicit null and field removal even with an older clock', () => {
  const theme = quotedAsset({_revision:10});
  const live = quotedAsset({_revision:11,price:null,change24h:null,volume24h:null,fieldTimes:{price:50,change24h:50,volume24h:50},fieldStatus:{price:'invalid'}});
  delete live.volumeScope;
  const [merged] = mergeHomeThemeAssets([theme], [live]);
  assert.equal(merged._revision, 11);
  assert.equal(merged.price, null);
  assert.equal(merged.change24h, null);
  assert.equal(merged.volume24h, null);
  assert.equal(merged.fieldStatus.price, 'invalid');
  assert.equal(Object.hasOwn(merged, 'volumeScope'), false);
  assert.equal(theme.price, 1);
  assert.equal(theme.volumeScope, 'token-aggregate');
});

test('older entity revision and older per-field revisions cannot roll back the selected theme', () => {
  const theme = quotedAsset({_revision:12,fieldRevisions:{price:8,volume24h:8}});
  const [olderEntity] = mergeHomeThemeAssets([theme], [quotedAsset({_revision:11,price:99,fieldTimes:{price:999}})]);
  assert.equal(olderEntity.price, 1);
  assert.equal(olderEntity._revision, 12);
  const [olderField] = mergeHomeThemeAssets([theme], [quotedAsset({price:99,volume24h:99,fieldTimes:{price:999,volume24h:999},fieldRevisions:{price:7,volume24h:7}})]);
  assert.equal(olderField.price, 1);
  assert.equal(olderField.volume24h, 100);
  assert.equal(olderField.fieldRevisions.price, 8);
});
