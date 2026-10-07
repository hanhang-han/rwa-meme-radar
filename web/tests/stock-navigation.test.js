import test from 'node:test';
import assert from 'node:assert/strict';
import { createMemoryHistory, createRouter } from 'vue-router';
import { detailStockContext, relatedStockThemeLink, relationshipAssetLink,
  resolveDetailBack, resolveStockThemeBack, safeInternalBack, stockDirectoryLink, themeAssetLink } from '../src/utils/stock-navigation.js';
import { stockThemeName } from '../src/utils/stock-theme-model.js';

const token = '0x' + 'a'.repeat(40), stock = '0x' + 'b'.repeat(40), pool = '0x' + 'c'.repeat(40);
const relation = { chainId: '196', ticker: '700', token, stock, pool };
const makeRouter = () => createRouter({ history: createMemoryHistory(), routes: [
  '/live', '/stock', '/stock/:ticker', '/meme', '/watch', '/events', '/asset/:chain/:address',
].map(path => ({ path, component: { render: () => null } })) });

test('directory filters, pagination and network survive theme → Meme → stock token → Meme and return', async () => {
  const router = makeRouter();
  await router.push({ path: '/stock', query: { chain: 'all', q: '腾讯', catalog: 'paired', sort: 'related', page: '4', mode: 'compare' } });
  const directoryPath = router.currentRoute.value.fullPath;
  await router.push({ path: '/stock/700', query: { chain: 'all', back: directoryPath } });
  const themePath = router.currentRoute.value.fullPath;
  const asset = themeAssetLink({ chainId: '196', token, pools: [{ relation }] }, { ticker: '700', chain: 'all', themePath, relation: true });
  assert.equal(asset.query.tab, 'relation'); assert.equal(asset.query.pool, pool);
  await router.push(asset);
  assert.equal(stockDirectoryLink(router.currentRoute.value.query), directoryPath);
  for (const side of ['stock', 'meme']) {
    const next = relationshipAssetLink(relation, side, router.currentRoute.value, 'all', pool);
    await router.push(next);
    assert.equal(router.currentRoute.value.params.address, side === 'stock' ? stock : token);
    assert.deepEqual(router.currentRoute.value.query, { chain: 'all', tab: 'overview', from: 'stock', ticker: '700', back: themePath, pool });
    assert.equal(stockDirectoryLink(router.currentRoute.value.query), directoryPath);
  }
  await router.push(resolveDetailBack(router.currentRoute.value.query));
  assert.equal(router.currentRoute.value.fullPath, themePath);
  await router.push(resolveStockThemeBack(router.currentRoute.value.query));
  assert.equal(router.currentRoute.value.fullPath, directoryPath);
  assert.equal(router.currentRoute.value.query.page, '4');
});

test('market entry opens overview and evidence entry opens relation, keeping a valid chosen pool', () => {
  const row = { chainId: '56', token, pools: [{ relation }] };
  const options = { ticker: '700', chain: '56', themePath: '/stock/700?chain=56&back=%2Fstock%3Fpage%3D2' };
  const market = themeAssetLink(row, { ...options, pool });
  const evidence = themeAssetLink(row, { ...options, relation: true });
  assert.equal(market.query.tab, 'overview'); assert.equal(evidence.query.tab, 'relation');
  assert.equal(market.query.pool, pool); assert.equal(evidence.query.pool, pool);
  assert.equal(themeAssetLink(row, { ...options, relation: true, pool: '//evil.test' }).query.pool, undefined);
});

test('known internal sources and full contract asset destinations are safe; external and malformed returns are rejected', () => {
  for (const path of ['/live?chain=56', '/meme?view=pool', '/watch', '/events?page=3', '/stock?q=Tencent&page=5', '/stock/700?back=%2Fstock', `/asset/196/${token}?tab=relation`]) {
    assert.equal(safeInternalBack(path), path);
  }
  for (const path of ['https://evil.test/stock', '//evil.test', 'javascript:alert(1)', '/stock/../../evil', '/stock/700#https://evil.test', '/stock\\evil', '/stock/700\n', '/%2F%2Fevil.test', '/stock%2F700', '/stock/700%2F..', '/unknown', `/asset/196/not-an-address`, '/stock?q=' + 'x'.repeat(2048), ['/stock']]) {
    assert.equal(safeInternalBack(path), null, String(path));
    assert.deepEqual(resolveStockThemeBack({ back: path }, '56'), { path: '/stock', query: { chain: '56' } });
  }
});

test('home, Meme, watch and discovery can return from themes without acquiring detail-only filters', async () => {
  const router = makeRouter();
  for (const source of ['/live?chain=196', '/meme?chain=all&q=monkey&page=2', '/watch?chain=56', '/events?kind=discovered']) {
    await router.push({ path: '/stock/700', query: { chain: 'all', back: source } });
    await router.push(resolveStockThemeBack(router.currentRoute.value.query, 'all'));
    assert.equal(router.currentRoute.value.fullPath, source);
  }
  assert.deepEqual(resolveDetailBack({ from: 'events', tab: 'relation', pool, chain: '56' }, '56'), { path: '/events', query: { chain: '56' } });
});

test('pure Meme exploration keeps Meme return context and never claims stock provenance', async () => {
  const router = makeRouter(), original = '/meme?chain=56&page=3&sort=volume24h';
  await router.push({ path: `/asset/196/${token}`, query: { chain: '56', from: 'meme', back: original } });
  assert.equal(detailStockContext(router.currentRoute.value.query, '56'), null);
  const next = relationshipAssetLink(relation, 'stock', router.currentRoute.value, '56', pool);
  assert.equal(next.query.from, 'meme'); assert.equal(next.query.ticker, undefined);
  await router.push(next);
  assert.equal(resolveDetailBack(router.currentRoute.value.query, '56'), original);
  assert.equal(detailStockContext(router.currentRoute.value.query, '56'), null);
});

test('related theme links use the original theme path or return to the current asset with its context', async () => {
  const router = makeRouter(), theme = '/stock/700?chain=56&back=%2Fstock%3Fpage%3D3';
  await router.push({ path: `/asset/56/${token}`, query: { chain: '56', from: 'stock', ticker: '700', back: theme } });
  assert.equal(relatedStockThemeLink('700', router.currentRoute.value, '56'), theme);
  const otherTheme = relatedStockThemeLink('1024', router.currentRoute.value, '56');
  const assetPath = router.currentRoute.value.fullPath;
  assert.equal(otherTheme.query.back, assetPath);
  await router.push(otherTheme);
  assert.equal(resolveStockThemeBack(router.currentRoute.value.query, '56'), assetPath);
});

test('direct links degrade to a valid source and company labels remain bilingual rather than bare numeric codes', () => {
  assert.deepEqual(resolveDetailBack({}, 'all'), { path: '/meme', query: { chain: 'all' } });
  assert.equal(detailStockContext({ ticker: '700' }, 'all'), null);
  assert.deepEqual(resolveDetailBack({ from: 'stock', ticker: '00700', back: '//evil.test' }, '196'), { path: '/stock/700', query: { chain: '196' } });
  assert.deepEqual(resolveDetailBack({ from: 'stock', ticker: '../../evil' }, 'all'), { path: '/stock', query: { chain: 'all' } });
  const context = detailStockContext({ from: 'stock', ticker: '00700' }, '196');
  assert.equal(context.ticker, '700');
  assert.deepEqual(context.themePath, { path: '/stock/700', query: { chain: '196' } });
  assert.equal(stockThemeName(null, context.ticker, 'zh'), '腾讯控股');
  assert.equal(stockThemeName(null, context.ticker, 'en'), 'Tencent');
  assert.equal(themeAssetLink({ chainId: '56', token }, { ticker: '01024', themePath: '/stock/01024?back=%2Fstock%3Fq%3D01024' }).query.back, '/stock/01024?back=%2Fstock%3Fq%3D01024');
});

test('caret and dotted catalog tickers retain context through real router navigation; invalid and overlong tickers fall back safely', async () => {
  const router = makeRouter(), directory = '/stock?chain=196&page=3&q=Index';
  for (const ticker of ['^GSPC', 'BRK.B', '.SPX', 'A'.repeat(24)]) {
    await router.push({ path: `/stock/${encodeURIComponent(ticker)}`, query: { chain: '196', back: directory } });
    const themePath = router.currentRoute.value.fullPath;
    assert.equal(router.currentRoute.value.params.ticker, ticker);
    assert.equal(safeInternalBack(themePath), themePath);
    await router.push(themeAssetLink({ chainId: '196', token }, { ticker, chain: '196', themePath }));
    assert.equal(detailStockContext(router.currentRoute.value.query, '196').ticker, ticker);
    await router.push(relationshipAssetLink(relation, 'stock', router.currentRoute.value, '196', pool));
    assert.equal(router.currentRoute.value.query.ticker, ticker);
    assert.equal(stockDirectoryLink(router.currentRoute.value.query, '196'), directory);
    await router.push(resolveDetailBack(router.currentRoute.value.query, '196'));
    assert.equal(router.currentRoute.value.params.ticker, ticker);
    assert.equal(router.currentRoute.value.fullPath, themePath);
  }
  for (const ticker of ['.', '..', '...', '^._-', 'A'.repeat(25)]) {
    const maliciousTheme = `/stock/${encodeURIComponent(ticker)}?chain=196`;
    assert.equal(safeInternalBack(maliciousTheme), null);
    assert.equal(detailStockContext({ from: 'stock', ticker, back: maliciousTheme }, '196'), null);
    await router.push(resolveDetailBack({ from: 'stock', ticker, back: maliciousTheme }, '196'));
    assert.equal(router.currentRoute.value.path, '/stock');
    assert.equal(router.currentRoute.value.query.chain, '196');
    assert.deepEqual(relatedStockThemeLink(ticker, router.currentRoute.value, '196'), { path: '/stock', query: { chain: '196' } });
  }
});
