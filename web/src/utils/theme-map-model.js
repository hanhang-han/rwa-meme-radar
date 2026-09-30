import { inChainScope } from './chain-scope.js';
import { isOfficialStock } from './product-labels.js';

const finite = value => value != null && value !== '' && Number.isFinite(Number(value));
const identity = bubble => `${String(bubble?.chainId ?? '')}:${String(bubble?.token ?? '').toLowerCase()}`;
const observedMs = value => value == null ? null : Number.isFinite(Number(value)) ? Number(value) : Date.parse(value);
const identityOf = (chainId, token) => `${String(chainId ?? '')}:${String(token ?? '').toLowerCase()}`;
export const THEME_WATCH_KEY = 'cliperx-theme-watch-v1';
export function watchStorageKey() {
  try { const user = localStorage.getItem('cliperx-watch-account'); return user ? `${THEME_WATCH_KEY}:${user}` : THEME_WATCH_KEY; }
  catch { return THEME_WATCH_KEY; }
}

export function readThemeWatches() {
  try {
    const saved = JSON.parse(localStorage.getItem(watchStorageKey()) || '[]');
    return new Set(Array.isArray(saved) ? saved.filter(value => typeof value === 'string').slice(0, 200) : []);
  } catch { return new Set(); }
}

export function writeThemeWatches(keys, silent = false) {
  const before = [...readThemeWatches()];
  const saved = [...keys].filter(value => typeof value === 'string').slice(0, 200);
  try { localStorage.setItem(watchStorageKey(), JSON.stringify(saved)); } catch {}
  if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('theme-watch-change', {detail:{before,after:saved,silent}}));
}

export function metricStatus(metric, now = Date.now()) {
  if (metric?.status !== 'current') return metric?.status ?? 'missing';
  const at = observedMs(metric.observedAt);
  if (!Number.isFinite(at) || at > now + 60_000) return 'unknown-time';
  return now - at <= 900_000 ? 'current' : 'stale';
}

export function currentMetric(metric, now = Date.now()) {
  return metricStatus(metric, now) === 'current' && finite(metric.value) ? Number(metric.value) : null;
}

export function currentUsdVolume(bubble, now = Date.now()) {
  return bubble?.volume24h?.currency === 'USD' && currentMetric(bubble.volume24h, now) != null
    ? Math.max(0, currentMetric(bubble.volume24h, now))
    : null;
}

export function bubbleTone(bubble, now = Date.now()) {
  const change = currentMetric(bubble?.change24h, now);
  return change == null ? 'unknown' : change > 0 ? 'up' : change < 0 ? 'down' : 'flat';
}

export function changeBreadth(rows, now = Date.now()) {
  const result = { up:0, down:0, flat:0, unknown:0 };
  for (const row of rows) result[bubbleTone(row, now)] += 1;
  return result;
}

export function bubbleDiameter(bubble, maxVolume, now = Date.now()) {
  const value = currentUsdVolume(bubble, now);
  if (value == null || !maxVolume) return 58;
  // Circle area follows volume within visible min/max sizes. The legend
  // explicitly describes that clamp and the table keeps the exact value.
  return Math.round(Math.min(112, Math.max(58, Math.sqrt(value / maxVolume) * 112)));
}

export function buildThemeMap(themeMap, scope = 'all', limits = {}, now = Date.now()) {
  const maxThemes = limits.maxThemes ?? 6;
  const maxPerTheme = limits.maxPerTheme ?? 6;
  const bubbles = new Map();
  for (const bubble of themeMap?.bubbles ?? []) {
    if (!bubble?.token || !bubble?.chainId || !bubble?.primaryTicker || !inChainScope(bubble, scope)) continue;
    const key = identity(bubble);
    if (!bubbles.has(key)) bubbles.set(key, { ...bubble, key });
  }
  const themes = (themeMap?.themes ?? []).map(theme => {
    const ticker = String(theme?.ticker ?? '').toUpperCase();
    const rows = [...bubbles.values()].filter(bubble => String(bubble.primaryTicker).toUpperCase() === ticker);
    rows.sort((a, b) => Number(currentUsdVolume(b, now) != null) - Number(currentUsdVolume(a, now) != null)
      || (currentUsdVolume(b, now) ?? 0) - (currentUsdVolume(a, now) ?? 0)
      || a.key.localeCompare(b.key));
    return {
      ...theme,
      ticker,
      rows: rows.slice(0, maxPerTheme),
      totalRows: rows.length,
      mapBreadth: changeBreadth(rows, now),
      knownVolume: rows.reduce((sum, bubble) => sum + (currentUsdVolume(bubble, now) ?? 0), 0),
      volumeKnownCount: rows.filter(bubble => currentUsdVolume(bubble, now) != null).length,
    };
  }).filter(theme => theme.ticker && theme.rows.length);
  themes.sort((a, b) => Number(b.volumeKnownCount > 0) - Number(a.volumeKnownCount > 0)
    || b.knownVolume - a.knownVolume || b.totalRows - a.totalRows || a.ticker.localeCompare(b.ticker));
  const visibleThemes = themes.slice(0, maxThemes);
  const visibleRows = visibleThemes.flatMap(theme => theme.rows);
  const maxVolume = Math.max(0, ...visibleRows.map(bubble => currentUsdVolume(bubble, now) ?? 0));
  return {
    themes: visibleThemes,
    rows: visibleRows,
    maxVolume,
    totalThemes: themes.length,
    totalAssets: bubbles.size,
  };
}

export function metricStateLabel(metric, language = 'zh', now = Date.now()) {
  const labels = {
    current: ['已更新', 'Current'],
    stale: ['已过期', 'Stale'],
    'unknown-time': ['采样时间未知', 'Observation time unknown'],
    missing: ['暂无数据', 'Unavailable'],
    'unsupported-currency': ['计价单位不同，暂不比较', 'Different currencies; not comparable'],
    'unsupported-scope': ['统计范围不同，暂不比较', 'Different coverage; not comparable'],
  };
  return (labels[metricStatus(metric, now)] ?? labels.missing)[language === 'en' ? 1 : 0];
}

export function themeEventTime(item) {
  if (item?.type === 'activity-spike') return observedMs(item?.window?.to);
  return observedMs(item?.verifiedAt ?? item?.discoveredAt);
}

export function visibleThemeEvents(importantChanges, scope = 'all', now = Date.now(), limit = 6) {
  return (importantChanges?.items ?? [])
    .filter(item => item && inChainScope(item, scope)
      && ['relation-verified', 'relation-observed'].includes(item.type)
      && themeEventTime(item) && themeEventTime(item) <= now + 60_000
      && now - themeEventTime(item) <= 86_400_000)
    .sort((a, b) => themeEventTime(b) - themeEventTime(a) || String(a.id).localeCompare(String(b.id)))
    .slice(0, limit);
}

function fallbackMetric(asset, field, now, options = {}) {
  const raw = asset?.[field];
  const at = observedMs(asset?.fieldTimes?.[field]);
  const source = asset?.fieldSources?.[field] ?? asset?.provider ?? null;
  const scope = asset?.fieldScopes?.[field] ?? (field === 'volume24h' ? asset?.volumeScope : asset?.priceScope) ?? null;
  const currency = field === 'volume24h' ? asset?.volumeCurrency : asset?.priceCurrency;
  const base = { value:null, source, scope, observedAt:Number.isFinite(at) ? at : null, currency:currency ?? null };
  if (!finite(raw)) return { ...base, status:'missing' };
  if (options.usdOnly && currency !== 'USD') return { ...base, status:'unsupported-currency' };
  if (options.aggregateOnly && scope !== 'token-aggregate') return { ...base, status:'unsupported-scope' };
  if (!Number.isFinite(at) || at <= 0 || at > now + 60_000) return { ...base, status:'unknown-time' };
  if (now - at > 900_000) return { ...base, status:'stale' };
  return { ...base, value:Number(raw), status:'current' };
}

function officialRelationTicker(relation, officialStocks) {
  if (relation?.level !== 'A' || !relation?.chainId || !relation?.stock || !relation?.token || !relation?.pool) return null;
  if (relation.status && relation.status !== 'verified') return null;
  if (relation.evidenceStatus && relation.evidenceStatus !== 'qualified') return null;
  if (relation.stockIdentity && relation.stockIdentity.verificationStatus !== 'official') return null;
  if (relation.sideIdentity && relation.sideIdentity.verificationStatus !== 'official') return null;
  const stock = officialStocks.get(identityOf(relation.chainId, relation.stock));
  if (!stock) return null;
  const ticker = relation.stockIdentity?.ticker ?? stock.stockIdentity?.code ?? stock.stockCode;
  if (!ticker || String(ticker).toUpperCase() !== String(stock.stockIdentity?.code ?? stock.stockCode ?? '').toUpperCase()) return null;
  return { ticker:String(ticker).toUpperCase(), stock };
}

// Compatibility view while the v2 backend is rolling out. It uses only the
// existing full snapshot, keeps field-specific times, and publishes no new
// activity statistics. The server projection takes priority when present.
export function fallbackThemeMap(unified, now = Date.now()) {
  if (!unified || unified.snapshotScope === 'overview') return null;
  const officialStocks = new Map((unified.stockTokens ?? []).filter(isOfficialStock)
    .map(stock => [identityOf(stock.chainId, stock.tokenContractAddress), stock]));
  const assets = new Map((unified.assets ?? []).filter(asset => asset.kind === 'candidate' && asset.token)
    .map(asset => [identity(asset), asset]));
  const grouped = new Map();
  for (const relation of unified.relations ?? []) {
    const verified = officialRelationTicker(relation, officialStocks);
    if (!verified) continue;
    const key = identityOf(relation.chainId, relation.token);
    const asset = assets.get(key);
    if (!asset) continue;
    if (!grouped.has(key)) grouped.set(key, {asset, relations:[]});
    grouped.get(key).relations.push({relation, ...verified});
  }
  const allBubbles = [...grouped.entries()].map(([key, group]) => {
    const tickers = [...new Set(group.relations.map(item => item.ticker))].sort();
    const primaryTicker = tickers[0];
    const representative = group.relations.filter(item => item.ticker === primaryTicker)
      .sort((a,b) => Number(b.relation.checkedAt ?? 0)-Number(a.relation.checkedAt ?? 0))[0].relation;
    const asset = group.asset;
    return {
      key, chainId:String(asset.chainId), token:asset.token, symbol:asset.symbol, name:asset.name,
      primaryTicker, otherTickers:tickers.slice(1),
      relation:{
        id:representative.id,pool:representative.pool,stock:representative.stock,
        checkedAt:representative.checkedAt,poolCreatedAt:representative.poolCreatedAt,
        discoveredAt:representative.discoveredAt ?? representative.firstSeen,
        liquidityUsd:representative.liquidityUsd,liquidityAt:representative.liquidityAt,
      },
      volume24h:fallbackMetric(asset,'volume24h',now,{usdOnly:true,aggregateOnly:true}),
      change24h:fallbackMetric(asset,'change24h',now),
      price:fallbackMetric(asset,'price',now),
    };
  });
  allBubbles.sort((a,b) => Number(currentUsdVolume(b,now) != null)-Number(currentUsdVolume(a,now) != null)
    || (currentUsdVolume(b,now) ?? 0)-(currentUsdVolume(a,now) ?? 0) || a.key.localeCompare(b.key));
  const bubbles = allBubbles.slice(0,50);
  const themes = [...new Set(allBubbles.flatMap(b => [b.primaryTicker,...b.otherTickers]))].sort().map(ticker => {
    const keys = bubbles.filter(b => b.primaryTicker === ticker || b.otherTickers.includes(ticker)).map(b => b.key);
    const stock = [...officialStocks.values()].find(row => String(row.stockIdentity?.code ?? row.stockCode).toUpperCase() === ticker);
    const rows = allBubbles.filter(b => b.primaryTicker === ticker || b.otherTickers.includes(ticker));
    const rising = rows.filter(b => bubbleTone(b,now) === 'up').length;
    const falling = rows.filter(b => bubbleTone(b,now) === 'down').length;
    const flat = rows.filter(b => bubbleTone(b,now) === 'flat').length;
    return {ticker,nameEn:stock?.stockIdentity?.nameEn ?? null,nameZh:stock?.stockIdentity?.nameZh ?? null,
      assetKeys:keys,breadth:{rising,falling,flat,unknown:rows.length-rising-falling-flat,valid:rising+falling+flat,total:rows.length,window:'24h'}};
  });
  return {version:'compat-full-snapshot',asOf:null,window:'24h',scope:'full-snapshot',
    totalAssets:allBubbles.length,displayedAssets:bubbles.length,themes,bubbles};
}

// B is an exact curated name/symbol match on the candidate itself. It is a
// research lead, never evidence of a stock-token pool or issuer relationship.
export function buildNameClues(unified, scope = 'all', limit = 30) {
  if (!unified || unified.snapshotScope === 'overview') return [];
  const verifiedKeys = new Set((unified.relations ?? [])
    .filter(relation => relation.status === 'verified' && relation.level === 'A')
    .map(relation => identityOf(relation.chainId,relation.token)));
  const clues = (unified.assets ?? []).filter(asset => {
    const match = asset.match;
    return asset.kind === 'candidate' && asset.chainId && asset.token && inChainScope(asset,scope)
      && !verifiedKeys.has(identity(asset)) && asset.relationLevel === 'B'
      && match?.level === 'B' && match.evidenceStatus === 'name-only' && match.ticker;
  }).map(asset => ({
    key:identity(asset),chainId:String(asset.chainId),token:asset.token,
    name:asset.name,symbol:asset.symbol,ticker:String(asset.match.ticker).toUpperCase(),
    matchType:asset.match.matchType,keyword:asset.match.keyword,
    ruleVersion:asset.match.ruleVersion,firstSeen:observedMs(asset.firstSeen),
  }));
  clues.sort((a,b) => (b.firstSeen ?? 0)-(a.firstSeen ?? 0) || a.key.localeCompare(b.key));
  return clues.slice(0,limit);
}

export function visibleRecentObservations(relationships, unified, scope = 'all', now = Date.now(), limit = 12) {
  const assets = new Map((unified?.assets ?? []).filter(asset => asset.token)
    .map(asset => [identity(asset),asset]));
  const currentPairs = new Set((unified?.relations ?? [])
    .filter(relation => relation.status === 'verified' && relation.level === 'A' && relation.pool)
    .map(relation => `${identityOf(relation.chainId,relation.token)}:${String(relation.pool).toLowerCase()}`));
  const seen = new Set();
  const observations = [];
  for (const row of relationships ?? []) {
    if (!['discovered','pair-observed'].includes(row?.kind)) continue;
    const chainId = String(row.chainId ?? '');
    const rawToken = String(row.asset ?? row.relation?.token ?? '');
    const token = rawToken.startsWith(`${chainId}:`) ? rawToken.slice(chainId.length+1).toLowerCase() : rawToken.toLowerCase();
    const at = observedMs(row.t ?? row.at);
    if (!chainId || !token || !Number.isFinite(at) || at <= 0 || at > now+60_000 || !inChainScope({chainId},scope)) continue;
    const id = String(row.id ?? `${chainId}:${token}:${row.kind}:${at}`);
    if (seen.has(id)) continue;
    seen.add(id);
    const asset = assets.get(identityOf(chainId,token));
    const match = row.match?.level === 'B' && row.match?.evidenceStatus === 'name-only' ? row.match
      : asset?.match?.level === 'B' && asset?.match?.evidenceStatus === 'name-only' ? asset.match : null;
    const pool = row.kind === 'pair-observed' ? row.pool ?? row.relation?.pool ?? null : null;
    observations.push({
      id,kind:row.kind,chainId,token,at,pool,
      symbol:asset?.symbol ?? row.symbol ?? null,name:asset?.name ?? null,
      ticker:row.kind === 'pair-observed' ? row.ticker ?? null : match?.ticker ?? null,
      matchType:match?.matchType ?? null,keyword:match?.keyword ?? null,
      discoveredAt:observedMs(row.discoveredAt),poolCreatedAt:observedMs(row.poolCreatedAt),
      currentlyVerified:!!pool && currentPairs.has(`${identityOf(chainId,token)}:${String(pool).toLowerCase()}`),
    });
  }
  observations.sort((a,b) => b.at-a.at || a.id.localeCompare(b.id));
  return observations.slice(0,limit);
}

export function fallbackRelationEvents(relationships, unified, now = Date.now()) {
  const officialStocks = new Map((unified?.stockTokens ?? []).filter(isOfficialStock)
    .map(stock => [identityOf(stock.chainId, stock.tokenContractAddress), stock]));
  const candidates = new Map((unified?.assets ?? []).filter(asset => asset.kind === 'candidate' && asset.token)
    .map(asset => [identity(asset), asset]));
  const keyFor = relation => `${identityOf(relation.chainId, relation.token)}:${String(relation.pool ?? '').toLowerCase()}`;
  const assessed = new Map();
  for (const relation of unified?.relations ?? []) {
    const verified = officialRelationTicker(relation, officialStocks);
    if (verified && candidates.has(identityOf(relation.chainId, relation.token)))
      assessed.set(keyFor(relation), {relation, ticker:verified.ticker});
  }
  const withinDay = at => Number.isFinite(at) && at > 0 && at <= now + 60_000 && now - at <= 86_400_000;
  const output = [];
  const eventRelations = new Set();
  for (const row of relationships ?? []) {
    if (row?.kind !== 'verified') continue;
    const chainId = String(row.chainId ?? row.relation?.chainId ?? '');
    const rawToken = String(row.asset ?? row.relation?.token ?? '');
    const token = rawToken.startsWith(`${chainId}:`) ? rawToken.slice(chainId.length+1).toLowerCase() : rawToken.toLowerCase();
    const pool = row.pool ?? row.relation?.pool;
    const matched = assessed.get(`${identityOf(chainId, token)}:${String(pool ?? '').toLowerCase()}`);
    const verifiedAt = observedMs(row.t ?? row.at);
    if (!matched || !withinDay(verifiedAt)) continue;
    const relation = matched.relation;
    const eventKey = keyFor(relation);
    if (eventRelations.has(eventKey)) continue;
    eventRelations.add(eventKey);
    output.push({
      id:`compat-verified:${row.id ?? eventKey}:${verifiedAt}`, type:'relation-verified',
      chainId, token, symbol:candidates.get(identityOf(chainId,token))?.symbol ?? row.symbol ?? null,
      ticker:matched.ticker, discoveredAt:observedMs(relation.discoveredAt ?? relation.firstSeen),
      verifiedAt, occurredAt:observedMs(relation.poolCreatedAt),
      relation:{id:relation.id,pool:relation.pool,stock:relation.stock},
      coverage:{currentRelationLevel:'A',poolCreationKnown:!!relation.poolCreatedAt},
    });
  }
  for (const {relation, ticker} of assessed.values()) {
    const discoveredAt = observedMs(relation.discoveredAt ?? relation.firstSeen);
    const eventKey = keyFor(relation);
    if (!withinDay(discoveredAt) || eventRelations.has(eventKey)) continue;
    output.push({
      id:`compat-observed:${eventKey}`, type:'relation-observed',
      chainId:String(relation.chainId),token:relation.token,
      symbol:candidates.get(identityOf(relation.chainId,relation.token))?.symbol ?? relation.tokenSymbol ?? null,
      ticker,discoveredAt,verifiedAt:null,occurredAt:observedMs(relation.poolCreatedAt),
      relation:{id:relation.id,pool:relation.pool,stock:relation.stock},
      coverage:{currentRelationLevel:'A',poolCreationKnown:!!relation.poolCreatedAt},
    });
  }
  output.sort((a,b) => themeEventTime(b)-themeEventTime(a) || a.id.localeCompare(b.id));
  return {version:'compat-full-snapshot',asOf:null,scope:'recent-observed',items:output.slice(0,12)};
}
