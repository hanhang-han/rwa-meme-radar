import { normalizeTicker, stockThemeName } from './stock-theme-model.js';

const ADDRESS = /^0x[0-9a-f]{40}$/i;
const TICKER = /^[A-Z0-9.^_-]{1,24}$/i;
const ROOTS = { live: '/live', stock: '/stock', meme: '/meme', watch: '/watch', me: '/me' };
const SOURCES = new Set(Object.keys(ROOTS));
const rootSource = path => ({ '/live':'live', '/stock':'stock', '/stocks':'stock', '/meme':'meme', '/memes':'meme', '/watch':'watch', '/me':'me' })[path];
const sourceAlias = source => source === 'events' ? 'watch' : source === 'account' ? 'me' : source;

export function safeInternalBack(value) {
  if (typeof value !== 'string' || !value || value.length >= 2048 || /[\\#\u0000-\u001f\u007f]/.test(value)) return null;
  const path = value.split('?')[0];
  if (/%(?:2f|5c)/i.test(path)) return null;
  let decoded;
  try { decoded = decodeURIComponent(path); } catch { return null; }
  if (/^\/(?:live|meme|memes|stock|stocks|watch|events|me|developer|status|pair|pools)$/.test(decoded)) return value;
  if (decoded.startsWith('/stock/')) {
    const ticker = decoded.slice(7);
    if (TICKER.test(ticker) && /[A-Z0-9]/i.test(ticker)) return value;
  }
  if (/^\/(?:asset|detail|token|pair|pools)\/\d{1,16}\/0x[0-9a-f]{40}$/i.test(decoded)) return value;
  return null;
}

export function navigationRoute(value) {
  const safe = safeInternalBack(value);
  if (!safe) return null;
  const split = safe.indexOf('?');
  return { fullPath: safe, path: decodeURIComponent(split < 0 ? safe : safe.slice(0, split)),
    query: Object.fromEntries(new URLSearchParams(split < 0 ? '' : safe.slice(split + 1))) };
}

export function navigationSource(route = {}, fallback) {
  const ownRoot = rootSource(route.path ?? String(route.fullPath ?? '').split('?')[0]);
  if (ownRoot) return ownRoot;
  let saved = navigationRoute(route.query?.back);
  const visited = new Set();
  for (let depth = 0; saved && depth < 6 && !visited.has(saved.fullPath); depth++) {
    visited.add(saved.fullPath);
    const root = rootSource(saved.path);
    if (root) return root;
    const parent = navigationRoute(saved.query.back);
    if (!parent) {
      const source = sourceAlias(saved.query.from);
      if (SOURCES.has(source)) return source;
    }
    saved = parent;
  }
  const source = sourceAlias(route.query?.from);
  if (SOURCES.has(source)) return source;
  if (fallback && SOURCES.has(fallback)) return fallback;
  const path = route.path ?? '';
  return path.startsWith('/stock/') ? 'stock' : path === '/events' ? 'watch'
    : ['/developer','/status'].includes(path) ? 'me' : 'meme';
}

export function navigationRoot(source, scope = 'all') {
  return { path: ROOTS[source] ?? '/meme', query: { chain: scope } };
}

// Source is the original main tab; back is the exact parent page. Only a
// destination's own parameters accompany it, never the source's detail filters.
export function pageNavigationLink(path, { route = {}, scope = 'all', query = {} } = {}) {
  const ownRoot = rootSource(path);
  const parameters = { chain: scope, ...query };
  if (ownRoot) {
    delete parameters.from; delete parameters.back;
    return { path, query: parameters };
  }
  if (path === route.path) {
    const current = { ...route.query, ...parameters, from: navigationSource(route) };
    if (!safeInternalBack(current.back)) delete current.back;
    return { path, query: current };
  }
  const back = safeInternalBack(route.fullPath);
  return { path, query: { ...parameters, from: navigationSource(route), ...(back && back.split('?')[0] !== path ? { back } : {}) } };
}

export function assetNavigationLink(asset = {}, { route = {}, scope = 'all', tab = 'overview', pool, market } = {}) {
  const token = asset.token ?? asset.tokenContractAddress ?? asset.address;
  const path = `/asset/${encodeURIComponent(String(asset.chainId ?? ''))}/${encodeURIComponent(String(token ?? ''))}`;
  return pageNavigationLink(path, { route, scope, query: {
    tab: ['overview','trades','holders','relation'].includes(tab) ? tab : 'overview',
    ...(ADDRESS.test(String(pool ?? '')) ? { pool: String(pool) }
      : typeof market === 'string' && market.length <= 160 && /^[a-zA-Z0-9:._|/-]+$/.test(market) ? { market } : {}),
  } });
}

export function themeNavigationLink(ticker, { route = {}, scope = 'all' } = {}) {
  const raw = String(ticker ?? '').trim();
  if (!TICKER.test(raw) || !/[A-Z0-9]/i.test(raw)) return navigationRoot('stock', scope);
  return pageNavigationLink(`/stock/${encodeURIComponent(normalizeTicker(raw))}`, { route, scope });
}

export function navigationParent(route = {}, scope = 'all') {
  const saved = safeInternalBack(route.query?.back);
  if (saved && saved.split('?')[0] !== route.path) return saved;
  const source = navigationSource(route);
  if (route.path?.startsWith('/asset/') && source === 'stock' && TICKER.test(String(route.query?.ticker ?? '')) && /[A-Z0-9]/i.test(route.query.ticker)) {
    return { path: `/stock/${encodeURIComponent(normalizeTicker(route.query.ticker))}`, query: { chain: scope } };
  }
  return navigationRoot(source, scope);
}

export function navigationLabel(path, lang = 'zh') {
  const roots = { '/live':['首页','Home'], '/stock':['股票主题','Stock themes'], '/stocks':['股票主题','Stock themes'],
    '/meme':['Meme','Memes'], '/memes':['Meme','Memes'], '/watch':['关注','Watchlist'], '/me':['我的','My account'],
    '/events':['发现记录','Discovery log'], '/developer':['开发者 API','Developer API'], '/status':['运行状态','Operations'],
    '/pair':['配对交易池','Pool pairs'], '/pools':['配对交易池','Pool pairs'] };
  if (roots[path]) return roots[path][lang === 'en' ? 1 : 0];
  if (path.startsWith('/stock/')) return stockThemeName(null, decodeURIComponent(path.slice(7)), lang);
  if (/^\/(?:asset|detail|token)\//.test(path)) return lang === 'en' ? 'Asset details' : '资产详情';
  if (/^\/(?:pair|pools)\//.test(path)) return lang === 'en' ? 'Pool pair' : '配对交易池';
  return lang === 'en' ? 'Details' : '详情';
}

export function navigationAncestors(route = {}, scope = 'all') {
  const ownRoot = rootSource(route.path);
  if (ownRoot) return [];
  const parent = navigationParent(route, scope);
  let saved = typeof parent === 'string' ? navigationRoute(parent) : { ...parent, fullPath: parent.path + '?' + new URLSearchParams(parent.query) };
  const rows = [], visited = new Set([route.fullPath]);
  for (let depth = 0; saved && depth < 4 && !visited.has(saved.fullPath); depth++) {
    visited.add(saved.fullPath); rows.unshift({ to: saved.fullPath, path: saved.path });
    if (rootSource(saved.path)) break;
    saved = navigationRoute(saved.query?.back);
  }
  const source = navigationSource(route), root = ROOTS[source];
  if (!rows.some(row => rootSource(row.path) === source)) rows.unshift({ to: navigationRoot(source, scope), path: root });
  return rows.filter((row,index) => rows.findIndex(other=>other.path === row.path) === index);
}

export function isNavigationReturn(to, from) {
  const target = safeInternalBack(to?.fullPath);
  return !!target && navigationAncestors(from, from.query?.chain ?? 'all').some(row =>
    typeof row.to === 'string' ? row.to === target : row.to.path === to.path && row.to.query.chain === to.query?.chain);
}

export function returnNavigation(router, parent, historyBack) {
  const target = router.resolve(parent).fullPath;
  if (safeInternalBack(historyBack) === target) router.back();
  else return router.replace(parent);
}
