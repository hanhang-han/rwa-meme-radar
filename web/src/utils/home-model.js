import { inChainScope } from './chain-scope.js';
import { preferredOfficialStock } from './product-labels.js';

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
    deltas: metrics.deltas ?? {},
  };
  const scoped = unified?.metricsByChain?.[scope];
  if (scoped) return metricsForScope({ metrics: scoped }, 'all', now);
  const assets = (unified?.assets ?? []).filter(a => a.kind === 'candidate' && inChainScope(a, scope));
  const relations = (unified?.relations ?? []).filter(r => inChainScope(r,scope) && qualifiedRelation(r));
  const pools = new Map();
  for (const r of relations) {
    const key = `${r.chainId}:${String(r.pool ?? '').toLowerCase()}`;
    if (r.pool && !pools.has(key)) pools.set(key,r);
  }
  const valued = [...pools.values()].map(r => number(r.liquidityUsd));
  const active = assets.filter(a => {
    const liquidity = number(a.totalLiquidityUsd);
    const at = a.fieldTimes?.totalLiquidityUsd ?? a.totalLiquidityAt;
    return liquidity != null && liquidity >= 1000 && at && now - Number(at) <= 900000;
  }).length;
  return { active, pools: pools.size, liquidity: valued.length === pools.size ? valued.reduce((a,b)=>a+b,0) : null, newPairs: null, deltas: {} };
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
