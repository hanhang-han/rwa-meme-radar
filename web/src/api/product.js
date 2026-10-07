import { API_BASE, getJSON } from './client.js';
import { createStockDirectoryCache } from '../utils/stock-directory-cache.js';
import { stockDirectoryPath } from '../utils/stock-directory-query.js';
import { createMemeDirectoryCache } from '../utils/meme-directory-cache.js';
import { memeDirectoryPath } from '../utils/meme-directory-query.js';

const stockDirectoryCache = createStockDirectoryCache({ apiBase: API_BASE, request: path => getJSON(path, 15000) });
const memeDirectoryCache = createMemeDirectoryCache({ apiBase: API_BASE, request: path => getJSON(path, 20000) });

export function getMemeDirectory(query, options = {}) { return memeDirectoryCache.get(memeDirectoryPath(query), options); }
export function peekMemeDirectory(query) { return memeDirectoryCache.peek(memeDirectoryPath(query)); }
export function getMemeGroup(query, group, options = {}) { return memeDirectoryCache.get(memeDirectoryPath(query, { group, offset: options.offset, focus: options.focus, revision: options.revision }), { force: options.force }); }
export function getMemeChart(query, options = {}) { return memeDirectoryCache.get(memeDirectoryPath(query, { chart: true, revision: options.revision }), options); }

const pending = new Map();
function productRequest(path) {
  if (pending.has(path)) return pending.get(path);
  const request = getJSON(path, 15000).finally(() => pending.delete(path));
  pending.set(path, request);
  return request;
}

export function getStockTheme(ticker, chain = 'all') {
  return productRequest(`v2/stocks/${encodeURIComponent(ticker)}?chain=${encodeURIComponent(chain)}`);
}

export function getStockDirectory(chain = 'all', options = {}) {
  return stockDirectoryCache.get(stockDirectoryPath(chain, options), { force: options.force === true });
}

export function peekStockDirectory(chain = 'all', options = {}) {
  return stockDirectoryCache.peek(stockDirectoryPath(chain, options));
}

export function getProductSearch(q, chain = 'all') {
  return productRequest(`v2/search?${new URLSearchParams({q:q.trim().slice(0,80),chain,limit:'12'})}`);
}

export function getStockChart(ticker, chain = 'all', tf = '1h', range = '24h', stock) {
  const query = new URLSearchParams({ chain, tf, range });
  if (stock?.tokenContractAddress) { query.set('stock', stock.tokenContractAddress); query.set('stockChain', String(stock.chainId)); }
  return productRequest(`v2/stocks/${encodeURIComponent(ticker)}/chart?${query}`);
}

export function getEquityChart(ticker, chain = 'all', range = '6m') {
  return productRequest(`v2/stocks/${encodeURIComponent(ticker)}/equity-chart?${new URLSearchParams({ chain, range })}`);
}
