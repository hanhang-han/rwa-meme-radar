import { normalizeTicker } from './stock-theme-model.js';
import { navigationRoute, navigationSource, safeInternalBack, themeNavigationLink } from './navigation-context.js';
export { safeInternalBack } from './navigation-context.js';

const TICKER = /^[A-Z0-9.^_-]{1,24}$/;
const ADDRESS = /^0x[0-9a-f]{40}$/i;
const valueOf = value => typeof value === 'string' ? value : '';

function tickerOf(value) {
  const raw = valueOf(value).trim().toUpperCase();
  return TICKER.test(raw) && /[A-Z0-9]/.test(raw) ? normalizeTicker(raw) : '';
}

// A return destination is a known local page, never an arbitrary URL. Query
// strings are retained intact so directory filters and pagination survive.
function savedRoute(value) {
  return navigationRoute(value);
}

function sourceFallback(query, scope, fallback = '/stock') {
  const from = valueOf(query?.from);
  const path = from === 'watch' ? '/watch' : from === 'events' ? '/events'
    : from === 'live' ? '/live' : from === 'meme' ? '/meme' : from === 'me' ? '/me' : fallback;
  return { path, query: { chain: scope } };
}

export function resolveStockThemeBack(query = {}, scope = 'all') {
  return safeInternalBack(query.back) || sourceFallback(query, scope);
}

export function resolveDetailBack(query = {}, scope = 'all') {
  const saved = safeInternalBack(query.back);
  if (saved) return saved;
  const ticker = tickerOf(query.ticker);
  const fallback = query.from === 'stock' ? (ticker ? `/stock/${encodeURIComponent(ticker)}` : '/stock') : '/meme';
  return sourceFallback(query, scope, fallback);
}

export function stockDirectoryLink(query = {}, scope = 'all') {
  let saved = savedRoute(query.back);
  const visited = new Set();
  for (let depth = 0; saved && depth < 6 && !visited.has(saved.fullPath); depth += 1) {
    visited.add(saved.fullPath);
    if (saved.path === '/stock' || saved.path === '/stocks') return saved.fullPath;
    saved = savedRoute(saved.query.back);
  }
  return { path: '/stock', query: { chain: scope } };
}

export function detailStockContext(query = {}, scope = 'all') {
  const saved = savedRoute(query.back);
  const savedTicker = saved?.path.startsWith('/stock/') ? tickerOf(saved.path.slice(7)) : '';
  const ticker = savedTicker || (query.from === 'stock' ? tickerOf(query.ticker) : '');
  if (!ticker) return null;
  const themePath = savedTicker ? saved.fullPath : {
    path: `/stock/${encodeURIComponent(ticker)}`,
    query: { chain: scope, ...(saved?.path === '/stock' || saved?.path === '/stocks' ? { back: saved.fullPath } : {}) },
  };
  return { ticker, themePath, directory: stockDirectoryLink(query, scope) };
}

function toFullPath(location) {
  if (typeof location === 'string') return location;
  const query = new URLSearchParams(Object.entries(location.query ?? {}).filter(([, value]) => value != null));
  return `${location.path}${query.size ? `?${query}` : ''}`;
}

export function themeAssetLink(row, { ticker, chain = 'all', themePath, relation = false, pool } = {}) {
  const normalized = tickerOf(ticker);
  const saved = savedRoute(themePath);
  const themeBack = saved?.path.startsWith('/stock/') && tickerOf(saved.path.slice(7)) === normalized ? saved.fullPath
    : normalized ? toFullPath({ path: `/stock/${encodeURIComponent(normalized)}`, query: { chain } }) : null;
  const selectedPool = pool ?? (relation ? row.pools?.[0]?.relation?.pool : null);
  return {
    path: `/asset/${row.chainId}/${row.token}`,
    query: { chain, tab: relation ? 'relation' : 'overview',
      ...(normalized ? { from: navigationSource(saved ?? { path: '/stock' }), ticker: normalized, back: themeBack } : {}),
      ...(ADDRESS.test(valueOf(selectedPool)) ? { pool: selectedPool } : {}) },
  };
}

export function relationshipAssetLink(relation, side, route, scope = 'all', pool) {
  const context = detailStockContext(route.query, scope);
  const saved = safeInternalBack(route.query?.back);
  const query = { chain: scope, tab: 'overview',
    ...(context ? { from: navigationSource(route), ticker: context.ticker, back: toFullPath(context.themePath) }
      : { from: navigationSource(route), ...(saved ? { back: saved } : {}) }),
    ...(ADDRESS.test(valueOf(pool)) ? { pool } : {}) };
  return { path: `/asset/${relation.chainId}/${side === 'stock' ? relation.stock : relation.token}`, query };
}

export function relatedStockThemeLink(ticker, route, scope = 'all') {
  const normalized = tickerOf(ticker);
  if (!normalized) return { path: '/stock', query: { chain: scope } };
  const context = detailStockContext(route.query, scope);
  if (context?.ticker === normalized) return context.themePath;
  return themeNavigationLink(normalized, { route, scope });
}
