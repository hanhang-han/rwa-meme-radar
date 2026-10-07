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

const COMPANY_NAMES_ZH = {AAPL:'苹果',AMD:'超威半导体',AMZN:'亚马逊',BABA:'阿里巴巴',COIN:'Coinbase',GME:'游戏驿站',GOOGL:'谷歌',HOOD:'Robinhood',INTC:'英特尔',META:'Meta',MSFT:'微软',NVDA:'英伟达',NFLX:'奈飞',QQQ:'纳斯达克100指数ETF',SPY:'标普500指数ETF',TSLA:'特斯拉',TSM:'台积电',MU:'美光科技',PLTR:'帕兰提尔','1':'长江和记实业','700':'腾讯控股','1024':'快手','1038':'长江基建','1088':'中国神华','1093':'石药集团','1810':'小米集团','9992':'泡泡玛特'};
const HK_COMPANY_NAMES_EN = {'1':'CK Hutchison','700':'Tencent','1024':'Kuaishou','1038':'CK Infrastructure','1088':'China Shenhua','1093':'CSPC Pharmaceutical','1810':'Xiaomi','9992':'Pop Mart'};
export function stockThemeName(stock, ticker, language='zh') {
  const identity=stock?.stockIdentity, code=normalizeTicker(ticker||stockTicker(stock));
  return language==='en' ? identity?.nameEn || HK_COMPANY_NAMES_EN[code] || stock?.tokenName || code
    : identity?.nameZh || COMPANY_NAMES_ZH[code] || identity?.nameEn || stock?.tokenName || code;
}

// Padding is presentation only. Routes and relation lookups keep normalized keys.
export function stockThemeCodeLabel(stock, ticker, language='zh') {
  const code=normalizeTicker(ticker||stockTicker(stock));
  const exchange=String(stock?.stockIdentity?.market??stock?.stockIdentity?.exchange??'').toUpperCase();
  const isHongKong=Object.hasOwn(HK_COMPANY_NAMES_EN,code)||['HKEX','SEHK','XHKG','HKG','HK'].includes(exchange);
  return isHongKong && /^\d{1,5}$/.test(code)
    ? `${language==='en'?'HK':'港股'} · ${code.padStart(5,'0')}` : code;
}

export function stockAtlasCards(cards = [], limit = 6) {
  // Keep empty themes in the directory, but give existing relationships room in the atlas.
  const paired=cards.filter(card=>Number(card.theme?.pairedCount)>0);
  const named=cards.filter(card=>Number(card.theme?.nameCount)>0);
  const visible=paired.length?paired:named.length?named:cards;
  return visible.slice().sort((a,b)=>Number(b.theme?.volume?.known>0)-Number(a.theme?.volume?.known>0)
    || Number(b.theme?.pairedCount??0)-Number(a.theme?.pairedCount??0)).slice(0,limit);
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

// Pool identity survives an expired valuation. Keep these records separate
// from current A/B rows so they cannot enter current volume or share totals.
export function buildRecordedPoolRows(relations = [], assets = [], { scope = 'all', activeKeys = [] } = {}) {
  const address = value => /^0x[0-9a-f]{40}$/i.test(String(value ?? ''));
  const assetIndex = new Map(assets.map(asset => [keyOf(asset), asset]));
  const excluded = new Set(activeKeys);
  const records = new Map();
  for (const relation of relations) {
    if (relation?.status !== 'verified' || !address(relation.pool) || !address(relation.stock) || !inChainScope(relation, scope)) continue;
    const chainId = String(relation.chainId ?? relation.chain ?? '');
    const key = `${chainId}:${String(relation.pool).toLowerCase()}`;
    const asset = assetIndex.get(keyOf(relation)) ?? null;
    if (excluded.has(key) || asset?.assetCategory === 'derivative') continue;
    const previous = records.get(key);
    if (previous && Number(previous.relation.checkedAt ?? 0) > Number(relation.checkedAt ?? 0)) continue;
    records.set(key, { key, chainId, token:relation.token, relation, asset,
      detailAvailable:asset != null && address(asset.token ?? asset.tokenContractAddress),
    });
  }
  return [...records.values()].sort((a,b)=>Number(b.relation.checkedAt??0)-Number(a.relation.checkedAt??0)||a.key.localeCompare(b.key));
}

export function buildStockTheme({ ticker, stockTokens = [], assets = [], relations = [], events = [], scope = 'all', now = Date.now() }) {
  const selectedTicker = normalizeTicker(ticker);
  const versions = stockTokens.filter(stock => stockTicker(stock) === selectedTicker && inChainScope(stock, scope));
  const stockKeys = new Set(versions.map(keyOf));
  const matchesTicker = relation => normalizeTicker(relation.ticker ?? relation.stockIdentity?.ticker) === selectedTicker
    || stockKeys.has(keyOf({ ...relation, token:relation.stock }));
  const paired = buildPoolRows(relations.filter(matchesTicker), assets, { scope, now })
    .filter(pool => pool.asset?.assetCategory !== 'derivative');
  const recordedPools = buildRecordedPoolRows(relations.filter(matchesTicker), assets, { scope, activeKeys:paired.map(pool=>pool.key) });
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
    const deliveryKey=event.id?`${event.chainId??event.relation?.chainId??''}:${event.id}`:null;
    if (deliveryKey && eventIds.has(deliveryKey)) continue;
    if (deliveryKey) eventIds.add(deliveryKey);
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
    const stock=String(event.stock??relation.stock??eventTicker??selectedTicker).toLowerCase();
    const key = `${chainId}:${String(pool).toLowerCase()||row.key}:${stock}:${kind}`;
    const previous = groupedEvents.get(key);
    if (!previous) groupedEvents.set(key, { key, chainId, token, kind, at, pool, stock, ticker:eventTicker||selectedTicker, dex:event.dex??relation.protocol, asset:row.asset, count:1 });
    else { previous.at = Math.max(previous.at, at); previous.count += 1; }
  }
  return { ticker:selectedTicker, versions, rows, pools:paired, recordedPools, pairedCount:pairs.length,
    nameCount:rows.length - pairs.length,
    volume:{ value:total, known:known.length, total:pairs.length, complete },
    concentrated:complete && rows.some(row => row.volumeShare > .5),
    events:[...groupedEvents.values()].sort((a, b) => b.at - a.at).slice(0, 12),
  };
}
