import { chainScope, savedChainScope } from './chain-scope.js';
import { memeFilterValues } from './meme-filter-model.js';
import { normalizeTicker } from './stock-theme-model.js';

const COIN_SORTS = ['volume24h', 'price', 'change24h', 'totalLiquidityUsd', 'holders', 'firstSeen'];
const POOL_SORTS = ['liquidity', 'volume24h', 'createdAt'];
export const MEME_GROUP_SEGMENT_SIZE = 50;

export function memeDirectoryQuery(query = {}) {
  const filters = memeFilterValues(query);
  const view = query.view === 'pool' ? 'pool' : 'coin';
  const sorts = view === 'pool' ? POOL_SORTS : COIN_SORTS;
  const limit = view === 'pool' ? 15 : 20;
  const page = Number(query.page);
  return {
    view,
    chain: chainScope({ chain: query.chain ?? savedChainScope() }),
    q: String(query.q ?? '').trim().toLowerCase(),
    sort: sorts.includes(query.sort) ? query.sort : sorts[0],
    rel: filters.relation,
    fresh: filters.fresh,
    minLiq: String(filters.minLiquidity),
    risk: filters.hideRisk ? 'hide' : '',
    qualified: filters.qualified ? '1' : '0',
    category: ['meme', 'derivative'].includes(query.category) ? query.category : '',
    new: filters.newAssets ? '24h' : '',
    rank: filters.ranking ? 'volume24h' : '',
    ticker: normalizeTicker(query.ticker),
    showMissing: filters.showMissing ? '1' : '0',
    filter: query.filter === 'history' ? 'history' : '',
    limit,
    offset: (Number.isFinite(page) ? Math.max(0, Math.floor(page)) : 0) * limit,
  };
}

export function memeDirectoryPath(query, { group, offset = 0, focus, chart = false, revision } = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) if (key !== 'view' && value !== '' && value != null) params.set(key, String(value));
  let path = query.view === 'pool' ? 'v2/memes/pools' : 'v2/memes';
  if (chart) { path = 'v2/memes/chart'; params.delete('limit'); params.delete('offset'); }
  if (group != null) {
    path = 'v2/memes/group';
    params.set('group', group);
    params.set('limit', String(MEME_GROUP_SEGMENT_SIZE));
    params.set('offset', String(Math.max(0, Math.floor(Number(offset) || 0))));
    if (focus) params.set('focus', focus);
  }
  if (revision != null) params.set('revision', String(revision));
  return `${path}?${params}`;
}

export function memeFilterKey(query) {
  const { limit, offset, view, ...filters } = query;
  return new URLSearchParams(filters).toString();
}
