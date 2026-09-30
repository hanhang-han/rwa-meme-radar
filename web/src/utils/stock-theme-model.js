import { inChainScope } from './chain-scope.js';
import { normalizeEventAddress } from './event-records.js';

const finite = value => value != null && value !== '' && Number.isFinite(Number(value));
const keyOf = row => `${String(row?.chainId ?? row?.chain ?? '')}:${String(row?.token ?? row?.tokenContractAddress ?? '').toLowerCase()}`;
const time = value => finite(value) && Number(value) > 0 ? Number(value) : null;
export const isRecentObservation = (at, windowMs = 900_000, now = Date.now()) => time(at) != null && now >= Number(at) && now - Number(at) <= windowMs;

export function normalizeTicker(value) {
  const ticker = String(value ?? '').trim().toUpperCase();
  return /^\d+$/.test(ticker) ? ticker.replace(/^0+(?=\d)/, '') : ticker;
}

export function stockTicker(stock) {
  return normalizeTicker(stock?.stockIdentity?.code ?? stock?.stockCode ?? stock?.ticker);
}

const COMPANY_NAMES_ZH = {AAPL:'苹果',AMD:'超威半导体',AMZN:'亚马逊',BABA:'阿里巴巴',COIN:'Coinbase',GME:'游戏驿站',GOOGL:'谷歌',HOOD:'Robinhood',INTC:'英特尔',META:'Meta',MSFT:'微软',NVDA:'英伟达',NFLX:'奈飞',QQQ:'纳斯达克100指数ETF',SPY:'标普500指数ETF',TSLA:'特斯拉',TSM:'台积电',MU:'美光科技',PLTR:'帕兰提尔','700':'腾讯控股','1810':'小米集团','9992':'泡泡玛特'};
export function stockThemeName(stock, ticker, language='zh') {
  const identity=stock?.stockIdentity, code=normalizeTicker(ticker||stockTicker(stock));
  return language==='en' ? identity?.nameEn || stock?.tokenName || code
    : identity?.nameZh || COMPANY_NAMES_ZH[code] || identity?.nameEn || stock?.tokenName || code;
}

// These are the same eligibility windows used by the published active-Meme KPI.
export function isQualifiedMeme(asset, now = Date.now()) {
  return asset?.kind === 'candidate' && !!asset.token
    && finite(asset.price) && Number(asset.price) > 0
    && isRecentObservation(asset.fieldTimes?.price, 900_000, now)
    && finite(asset.totalLiquidityUsd) && Number(asset.totalLiquidityUsd) >= 1000
    && asset.totalLiquidityStatus === 'current'
    && isRecentObservation(asset.totalLiquidityAt, 1_800_000, now);
}

export function hasBasicMarketData(asset) {
  return finite(asset?.price) && Number(asset.price) > 0
    && finite(asset?.volume24h) && Number(asset.volume24h) >= 0
    && finite(asset?.totalLiquidityUsd) && Number(asset.totalLiquidityUsd) > 0;
}

export function isQualifiedPool(relation, now = Date.now()) {
  return relation?.level === 'A' && relation.status === 'verified' && !!relation.pool
    && finite(relation.liquidityUsd) && Number(relation.liquidityUsd) >= 1000
    && isRecentObservation(relation.liquidityAt, 900_000, now);
}

export function comparableVolume(asset, now = Date.now()) {
  const scope = asset?.fieldScopes?.volume24h ?? asset?.volumeScope;
  if (!finite(asset?.volume24h) || Number(asset.volume24h) < 0 || (asset.volumeCurrency ?? asset.priceCurrency) !== 'USD'
      || !['token', 'token-aggregate'].includes(scope) || !isRecentObservation(asset.fieldTimes?.volume24h, 900_000, now)) return null;
  return Number(asset.volume24h);
}

export function poolCreatedAt(relation) {
  return time(relation?.poolCreatedAt);
}

export function buildPoolRows(relations = [], assets = [], { scope = 'all', qualified = false, q = '', now = Date.now() } = {}) {
  const assetIndex = new Map(assets.map(asset => [keyOf(asset), asset]));
  const pools = new Map();
  for (const relation of relations) {
    if (relation?.level !== 'A' || relation.status !== 'verified' || !relation.pool || !inChainScope(relation, scope)) continue;
    const key = `${String(relation.chainId ?? relation.chain ?? '')}:${String(relation.pool).toLowerCase()}`;
    const previous = pools.get(key);
    if (!previous || Number(relation.liquidityAt ?? 0) > Number(previous.liquidityAt ?? 0)
        || (Number(relation.liquidityAt ?? 0) === Number(previous.liquidityAt ?? 0) && Number(relation.checkedAt ?? 0) > Number(previous.checkedAt ?? 0))) pools.set(key, relation);
  }
  const search = String(q).trim().toLowerCase();
  return [...pools].flatMap(([key, relation]) => {
    if (qualified && !isQualifiedPool(relation, now)) return [];
    const asset = assetIndex.get(keyOf(relation)) ?? null;
    if (search && ![asset?.symbol, asset?.name, relation.token, relation.pool, relation.stock, relation.stockSide, relation.ticker, relation.protocol]
      .some(value => String(value ?? '').toLowerCase().includes(search))) return [];
    const market = relation.poolMarket;
    const volumeMatches = market?.scope === `pool:${String(relation.pool).toLowerCase()}`
      && (!market.currency || market.currency === 'USD') && (!market.volumeCurrency || market.volumeCurrency === 'USD');
    const volume = volumeMatches && finite(market.volume24h) && Number(market.volume24h) >= 0 ? Number(market.volume24h) : null;
    return [{ key, chainId:String(relation.chainId ?? relation.chain ?? ''), relation, asset,
      liquidity:finite(relation.liquidityUsd) ? Number(relation.liquidityUsd) : null,
      liquidityCurrent:isRecentObservation(relation.liquidityAt, 900_000, now),
      createdAt:poolCreatedAt(relation), volume24h:volume,
      volumeAt:volumeMatches ? time(market.updatedAt) : null,
      volumeCurrent:volume != null && isRecentObservation(market.updatedAt, 900_000, now),
    }];
  });
}

export function buildStockTheme({ ticker, stockTokens = [], assets = [], relations = [], events = [], scope = 'all', now = Date.now() }) {
  const selectedTicker = normalizeTicker(ticker);
  const versions = stockTokens.filter(stock => stockTicker(stock) === selectedTicker && inChainScope(stock, scope));
  const stockKeys = new Set(versions.map(keyOf));
  const matchesTicker = relation => normalizeTicker(relation.ticker ?? relation.stockIdentity?.ticker) === selectedTicker
    || stockKeys.has(keyOf({ ...relation, token:relation.stock }));
  const paired = buildPoolRows(relations.filter(matchesTicker), assets, { scope, now })
    .filter(pool => pool.asset?.assetCategory !== 'derivative');
  const rowsByKey = new Map();
  for (const pool of paired) {
    const key = keyOf(pool.relation);
    if (!rowsByKey.has(key)) rowsByKey.set(key, { key, chainId:pool.chainId, token:pool.relation.token, asset:pool.asset, level:'A', pools:[] });
    rowsByKey.get(key).pools.push(pool);
  }
  for (const asset of assets) {
    const match = asset?.match;
    if (asset.kind !== 'candidate' || asset.assetCategory === 'derivative' || !inChainScope(asset, scope) || match?.level !== 'B'
        || match.evidenceStatus !== 'name-only' || normalizeTicker(match.ticker) !== selectedTicker) continue;
    const key = keyOf(asset);
    if (!rowsByKey.has(key)) rowsByKey.set(key, { key, chainId:String(asset.chainId), token:asset.token, asset, level:'B', pools:[] });
  }
  const rows = [...rowsByKey.values()].map(row => ({ ...row,
    volume:row.level === 'A' ? comparableVolume(row.asset, now) : null,
    createdAt:row.pools.map(pool => pool.createdAt).filter(Boolean).sort((a, b) => a - b)[0] ?? null,
  }));
  const pairs = rows.filter(row => row.level === 'A');
  const known = pairs.filter(row => row.volume != null);
  const total = known.length ? known.reduce((sum, row) => sum + row.volume, 0) : null;
  const complete = pairs.length > 0 && known.length === pairs.length;
  for (const row of rows) row.volumeShare = row.volume != null && total > 0 ? row.volume / total : null;
  rows.sort((a, b) => (a.level === 'A' ? 0 : 1) - (b.level === 'A' ? 0 : 1)
    || Number(b.volume != null) - Number(a.volume != null) || (b.volume ?? 0) - (a.volume ?? 0) || a.key.localeCompare(b.key));

  // Group repeated observations without turning an indexing time into a creation time.
  const groupedEvents = new Map();
  const eventIds = new Set();
  for (const event of events) {
    if (event.id && eventIds.has(event.id)) continue;
    if (event.id) eventIds.add(event.id);
    const relation = event.relation ?? {};
    const chainId = String(event.chainId ?? relation.chainId ?? '');
    const token = normalizeEventAddress(event.asset ?? relation.token ?? event.token, chainId);
    const row = rowsByKey.get(`${chainId}:${token}`);
    if (!row || !inChainScope({ chainId }, scope)) continue;
    const eventTicker = normalizeTicker(event.ticker ?? relation.ticker);
    if (eventTicker && eventTicker !== selectedTicker) continue;
    const kind = event.kind ?? event.type;
    if (!['verified', 'discovered', 'pair-observed', 'relation-verified', 'relation-observed', 'pool-created'].includes(kind)) continue;
    if (row.level !== 'A' && ['verified', 'relation-verified', 'pool-created'].includes(kind)) continue;
    const at = time(event.t ?? event.at ?? event.verifiedAt ?? event.discoveredAt);
    if (!at || at > now) continue;
    const pool = event.pool ?? relation.pool ?? '';
    const key = `${row.key}:${kind}:${String(pool).toLowerCase()}`;
    const previous = groupedEvents.get(key);
    if (!previous) groupedEvents.set(key, { key, chainId, token, kind, at, pool, asset:row.asset, count:1 });
    else { previous.at = Math.max(previous.at, at); previous.count += 1; }
  }
  return { ticker:selectedTicker, versions, rows, pools:paired, pairedCount:pairs.length,
    nameCount:rows.length - pairs.length,
    volume:{ value:total, known:known.length, total:pairs.length, complete },
    concentrated:complete && rows.some(row => row.volumeShare > .5),
    events:[...groupedEvents.values()].sort((a, b) => b.at - a.at).slice(0, 12),
  };
}
