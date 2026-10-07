import { inChainScope } from './chain-scope.js';
import { preferredOfficialStock } from './product-labels.js';
import { buildPoolRows, buildStockTheme, isQualifiedMeme, isRecentObservation } from './stock-theme-model.js';

const number = value => value != null && Number.isFinite(Number(value)) ? Number(value) : null;
const identity = row => `${row?.chainId}:${String(row?.token ?? row?.tokenContractAddress ?? '').toLowerCase()}`;
const qualifiedRelation = relation => relation.level === 'A';

export function metricsForScope(unified, scope, now = Date.now()) {
  const metrics = unified?.metrics ?? {};
  if (scope === 'all') return {
    active: metrics.activeMemeCount ?? metrics.actionableAssets,
    pools: metrics.pairCount ?? metrics.verifiedPools,
    liquidity: metrics.pairLiquidityTotal ?? metrics.pairedLiquidityUsd,
    newPairs: Object.hasOwn(metrics,'newPair24h') ? metrics.newPair24h : null,
    newAssets: Object.hasOwn(metrics,'newAssets24h') ? metrics.newAssets24h : null,
    deltas: metrics.deltas ?? {},
  };
  const scoped = unified?.metricsByChain?.[scope];
  if (scoped) return metricsForScope({ metrics: scoped }, 'all', now);
  const assets = (unified?.assets ?? []).filter(a => a.kind === 'candidate' && inChainScope(a, scope));
  if (unified?.snapshotScope === 'overview') return { active:null, pools:null, liquidity:null, newPairs:null, newAssets:null, deltas:{} };
  const uniqueAssets = [...new Map(assets.map(asset => [identity(asset),asset])).values()];
  const pools = buildPoolRows(unified?.relations, uniqueAssets, { scope, qualified:true, now });
  return { active:uniqueAssets.filter(asset => isQualifiedMeme(asset,now)).length,
    pools:pools.length, liquidity:pools.reduce((sum,pool)=>sum+pool.liquidity,0), newPairs:null,
    newAssets:uniqueAssets.filter(asset=>isRecentObservation(asset.firstSeen,86_400_000,now)).length, deltas:{} };
}

// Compact snapshots carry totals calculated before rows are limited. Never
// reconstruct market-wide counts from the twelve bootstrap assets.
export function homeStockCards(unified, scope = 'all', now = Date.now()) {
  const published = scope === 'all' ? unified?.hotStocks : unified?.hotStocksByChain?.[scope];
  if (Array.isArray(published)) return published.map(card => ({ ...card, volume:card.volume24h, assetCount:card.assetCount }));
  if (!unified || unified.snapshotScope === 'overview') return [];
  return topStockCards(unified.stockTokens, unified.relations, unified.assets, scope).map(card => {
    const theme = buildStockTheme({ ticker:card.ticker, stockTokens:card.versions, assets:unified.assets, relations:unified.relations, scope, now });
    return { ...card, assetCount:theme.pairedCount, volume:theme.volume.value, volumeKnown:theme.volume.known, volumeTotal:theme.volume.total };
  });
}

export function onchainRecordedTrades(trades, knownAssets, scope = 'all', minAmount = 100, direction = 'all') {
  const known = knownAssets == null ? null : new Set(knownAssets.map(identity));
  return (trades ?? []).filter(trade => (known == null || known.has(identity(trade)))
    && (String(trade.venue ?? '').toLowerCase() === 'dex' || (!trade.venue && !!trade.hash))
    && inChainScope(trade,scope) && (direction === 'all' || trade.type === direction)
    && (minAmount <= 0 || (tradeDisplayAmount(trade)?.currency === 'USD' && tradeDisplayAmount(trade).value >= minAmount))).slice(0,50);
}

export function groupActivityItems(items = []) {
  const grouped = new Map();
  const seen = new Set();
  for (const item of items) {
    const deliveryKey=item.id?`${item.chainId??item.relation?.chainId??''}:${item.id}`:null;
    if (deliveryKey && seen.has(deliveryKey)) continue;
    if (deliveryKey) seen.add(deliveryKey);
    const relation=item.relation??{};
    const pool=String(item.pool??relation.pool??'').toLowerCase();
    const stock=String(item.stock??relation.stock??item.ticker??relation.ticker??'').toLowerCase();
    const key = `${String(item.chainId??relation.chainId??'')}:${pool||identity(item)}:${stock}:${item.type ?? item.kind ?? ''}`;
    const at = Number(item.at ?? item.t ?? item.verifiedAt ?? item.discoveredAt ?? 0);
    const previous = grouped.get(key);
    if (!previous) grouped.set(key, { ...item, id:key, recordCount:Number(item.recordCount) || 1 });
    else {
      const count = previous.recordCount + (Number(item.recordCount) || 1);
      const previousAt = Number(previous.at ?? previous.t ?? previous.verifiedAt ?? previous.discoveredAt ?? 0);
      grouped.set(key, { ...(at > previousAt ? item : previous), id:key, recordCount:count });
    }
  }
  return [...grouped.values()].sort((a,b)=>Number(b.at ?? b.t ?? b.verifiedAt ?? b.discoveredAt ?? 0)-Number(a.at ?? a.t ?? a.verifiedAt ?? a.discoveredAt ?? 0));
}

export function tradeDisplayAmount(trade) {
  const currency = String(trade.volumeCurrency ?? trade.priceCurrency ?? '').toUpperCase();
  const quote = number(trade.quoteQuantity);
  if (quote != null && (currency === 'USD' || currency === 'USDT')) return {value:quote,currency};
  const volume = number(trade.volume);
  if (volume != null && (!currency || currency === 'USD')) return {value:volume,currency:'USD'};
  return null;
}

export function filteredTrades(trades, scope, minAmount = 100, direction = 'all') {
  return (trades ?? []).filter(t => inChainScope(t,scope) && (direction === 'all' || t.type === direction) && (tradeDisplayAmount(t)?.value ?? -1) >= minAmount).slice(0,50);
}

export function discoveryTime(row) {
  const relation = row?.relation ?? row ?? {};
  const createdAt = number(relation.poolCreatedAt ?? row?.poolCreatedAt);
  const block = number(relation.creationBlock ?? row?.creationBlock);
  const tx = relation.creationTx ?? row?.creationTx;
  const confirmed = (relation.confirmationStatus ?? row?.confirmationStatus) === 'confirmed'
    || (relation.creationStatus ?? row?.creationStatus) === 'confirmed';
  if (createdAt > 0 && block > 0 && tx && confirmed) return { at:createdAt, kind:'created' };
  return {
    at: number(row?.discoveredAt ?? relation.discoveredAt ?? relation.firstSeen ?? row?.t ?? row?.at),
    kind:'indexed',
  };
}

export function topStockCards(stockTokens, relations, assets, scope) {
  const assetMap = new Map((assets ?? []).map(a => [identity(a),a]));
  const cards = new Map();
  for (const stock of stockTokens ?? []) {
    if (!inChainScope(stock,scope)) continue;
    const ticker = stock.stockCode ?? stock.stockIdentity?.code ?? stock.tokenSymbol;
    if (!ticker) continue;
    const key = String(ticker).toUpperCase();
    if (!cards.has(key)) cards.set(key,{ticker:key,stock:null,versions:[],memes:new Map(),volume:null});
    const card = cards.get(key);
    card.versions.push(stock);
  }
  for (const relation of relations ?? []) {
    if (!inChainScope(relation,scope) || !qualifiedRelation(relation)) continue;
    const ticker = String(relation.ticker ?? '').toUpperCase();
    const card = cards.get(ticker);
    if (!card) continue;
    const asset = assetMap.get(identity(relation));
    if (asset) card.memes.set(identity(asset),asset);
  }
  for (const card of cards.values()) {
    card.stock = preferredOfficialStock(card.versions);
    const memes = [...card.memes.values()];
    card.volume = memes.length && memes.every(a => a.volumeCurrency === 'USD' && number(a.volume24h) != null)
      ? memes.reduce((total,a) => total + number(a.volume24h),0)
      : null;
  }
  return [...cards.values()].filter(card=>card.stock && card.memes.size).sort((a,b) =>
    Number(b.volume != null)-Number(a.volume != null)
    || (b.volume ?? 0)-(a.volume ?? 0)
    || b.memes.size-a.memes.size
    || a.ticker.localeCompare(b.ticker),
  ).slice(0,8);
}
