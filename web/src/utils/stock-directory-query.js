import { chainScope, savedChainScope } from './chain-scope.js';

export const STOCK_DIRECTORY_PAGE_SIZE = 20;
const SORTS = ['volume24h', 'ticker', 'change24h', 'related'];

// Both the HTML preloader and the routed page must request the same directory.
export function stockDirectoryQuery(query = {}, fallbackChain) {
  const page = Number(query.page);
  const requestedPage = Number.isFinite(page) ? Math.max(0, Math.floor(page)) : 0;
  return {
    chain: chainScope({ chain: query.chain ?? fallbackChain ?? savedChainScope() }),
    options: {
      q: String(query.q ?? '').trim().toLowerCase(),
      sort: SORTS.includes(query.sort) ? query.sort : 'related',
      catalog: query.catalog === 'paired' ? 'paired' : 'all',
      limit: STOCK_DIRECTORY_PAGE_SIZE,
      offset: requestedPage * STOCK_DIRECTORY_PAGE_SIZE,
    },
  };
}

export function stockDirectoryPath(chain = 'all', options = {}) {
  const normalized = stockDirectoryQuery({ ...options, chain });
  const limit = Number(options.limit ?? STOCK_DIRECTORY_PAGE_SIZE);
  const offset = Number(options.offset ?? 0);
  const query = new URLSearchParams({
    chain: normalized.chain,
    sort: normalized.options.sort,
    limit: String(Number.isFinite(limit) ? Math.max(1, Math.floor(limit)) : STOCK_DIRECTORY_PAGE_SIZE),
    offset: String(Number.isFinite(offset) ? Math.max(0, Math.floor(offset)) : 0),
  });
  if (normalized.options.q) query.set('q', normalized.options.q);
  query.set('catalog', normalized.options.catalog);
  return `v2/stocks?${query}`;
}
