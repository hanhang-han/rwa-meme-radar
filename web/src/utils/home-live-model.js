import { normalizeEventAddress } from './event-records.js';
import { normalizeTicker } from './stock-theme-model.js';
import { applyQuote, mergeEntity } from './realtime.js';

const chainOf = row => String(row?.chainId ?? row?.chain ?? row?.chainIndex ?? row?.relation?.chainId ?? '').trim();
const tokenOf = row => {
  const asset = typeof row?.asset === 'object' ? row.asset?.token ?? row.asset?.tokenContractAddress : row?.asset;
  return normalizeEventAddress(row?.token ?? row?.tokenContractAddress ?? row?.relation?.token ?? asset, chainOf(row));
};
const poolOf = row => normalizeEventAddress(row?.pool ?? row?.relation?.pool, chainOf(row));
const identity = (chain, address) => chain && address ? `${chain}:${normalizeEventAddress(address, chain)}` : '';
const tickersOf = row => [...new Set([
  row?.stockIdentity?.code, row?.stockIdentity?.ticker, row?.stockCode, row?.ticker,
  ...(Array.isArray(row?.tickers) ? row.tickers : []),
].map(normalizeTicker).filter(Boolean))];

const isTrade = row => ['buy', 'sell', 'trade'].includes(String(row?.type ?? row?.kind ?? '').toLowerCase())
  || !!row?.venue || !!row?.tradeId || !!row?.hash;
const isVerifiedPool = row => !!chainOf(row) && !!tokenOf(row) && !!poolOf(row)
  && row?.level !== 'B' && row?.evidenceStatus !== 'name-only'
  && (row?.status === 'verified' || (row?.level === 'A' && !row?.status));

const quoteFields = ['price', 'change24h', 'volume24h', 'txs24h', 'buys24h', 'sells24h', 'exchangeTrades24h', 'marketCap', 'liquidity', 'holders', 'volume5m', 'volume1h'];
const quoteMaps = ['fieldTimes', 'fieldSources', 'fieldScopes', 'fieldTimeKinds', 'fieldObservations', 'fieldStatus', 'fieldRevisions'];
const priceMetadata = ['provider', 'venue', 'marketId', 'quoteType', 'priceScope', 'priceCurrency', 'priceProvenance', 'quoteStatus', 'quoteReason', 'marketAt', 'receivedAt', 'timeKind', 'quoteAt'];
const has = (row, name) => Object.hasOwn(row ?? {}, name);
const copyAsset = row => Object.fromEntries(Object.entries(row).map(([name, value]) =>
  [name, quoteMaps.includes(name) ? { ...value } : value]));
const copyField = (target, source, name) => {
  if (has(source, name)) target[name] = source[name];
  else delete target[name];
};

// A theme response is a directory snapshot, while the dashboard owns the live
// observations. Keep its membership and merge only the exact same asset.
export function mergeHomeThemeAssets(themeAssets = [], liveAssets = []) {
  const liveIndex = new Map((liveAssets ?? []).map(asset => [identity(chainOf(asset), tokenOf(asset)), asset]));
  return (themeAssets ?? []).map(asset => {
    const key = identity(chainOf(asset), tokenOf(asset)), live = key && liveIndex.get(key);
    const base = copyAsset(asset);
    if (!live) return base;
    const revision = Number(live._revision) || 0;
    // Canonical projection rows are complete. Their ordered invalidations also
    // win over a historical HTTP quote whose source clock happens to be newer.
    if (revision) return mergeEntity(base, live, revision, true);

    const merged = mergeEntity(base, live);
    const quotes = copyAsset(asset);
    applyQuote(quotes, live);
    const volume = copyAsset(asset), volumeObservation = { ...live };
    for (const field of quoteFields) if (field !== 'volume24h') delete volumeObservation[field];
    applyQuote(volume, volumeObservation);
    // mergeEntity also merges non-market facts. Preserve applyQuote's field
    // decisions and attach each accepted source, time, scope and currency.
    for (const field of quoteFields) {
      copyField(merged, quotes, field);
      for (const map of quoteMaps) {
        const values = { ...merged[map] };
        copyField(values, quotes[map], field);
        if (Object.keys(values).length || has(merged, map)) merged[map] = values;
      }
    }
    for (const name of priceMetadata) copyField(merged, quotes, name);
    // An exchange price change alone must not relabel a retained USD volume.
    for (const name of ['volumeCurrency', 'volumeScope']) copyField(merged, volume, name);
    return merged;
  });
}

/** Filter identities rather than names; an empty selected theme stays empty. */
export function filterHomeThemeRecords(records = [], ticker, relations = [], assets = []) {
  const selected = normalizeTicker(ticker);
  if (!selected) return records ?? [];

  const stockCodes = new Map();
  const stockSymbols = new Map();
  const nameMatches = new Set();
  for (const asset of assets ?? []) {
    const chain = chainOf(asset), token = tokenOf(asset), key = identity(chain, token);
    const code = normalizeTicker(asset?.stockIdentity?.code ?? asset?.stockCode);
    if (key && code && (asset?.tokenContractAddress || ['stock', 'stock-token'].includes(asset?.kind))) {
      stockCodes.set(key, code);
      const symbol = normalizeTicker(asset?.tokenSymbol ?? asset?.symbol);
      if (symbol) stockSymbols.set(`${chain}:${symbol}`, code);
    }
    if (key && asset?.match?.level === 'B' && asset.match.evidenceStatus === 'name-only'
        && normalizeTicker(asset.match.ticker) === selected) nameMatches.add(key);
  }

  const relationTickers = row => {
    const canonical = normalizeTicker(row?.stockIdentity?.code ?? row?.stockIdentity?.ticker ?? row?.stockCode ?? row?.ticker);
    const explicit = canonical ? [canonical] : [];
    const stockCode = stockCodes.get(identity(chainOf(row), row?.stock));
    return explicit.length ? explicit : stockCode ? [stockCode] : [];
  };
  const matched = (relations ?? []).filter(relation => relationTickers(relation).includes(selected));
  const selectedTokens = new Set(matched.map(row => identity(chainOf(row), tokenOf(row))).filter(Boolean));
  const selectedPools = new Set(matched.map(row => identity(chainOf(row), poolOf(row))).filter(Boolean));
  const tradeTokens = new Set(matched.filter(isVerifiedPool).map(row => identity(chainOf(row), tokenOf(row))));

  return (records ?? []).filter(row => {
    const chain = chainOf(row), token = tokenOf(row), tokenKey = identity(chain, token), pool = poolOf(row);
    if (!chain || !tokenKey) return false;
    if (isTrade(row)) return tradeTokens.has(tokenKey);

    const relation = row?.relation ?? {};
    const labels = [...new Set([...tickersOf(row), ...relationTickers({ ...relation, chainId:chain })])];
    const stockSymbol = normalizeTicker(row?.stockSymbol ?? relation.stockSymbol);
    const symbolCode = stockSymbols.get(`${chain}:${stockSymbol}`) ?? stockSymbol;
    if (!labels.length && symbolCode) labels.push(symbolCode);
    // A token may have several stock pools. Keep the event's own stock identity.
    if (labels.length && !labels.includes(selected)) return false;

    if (pool && selectedPools.has(identity(chain, pool))
        && matched.some(item => chainOf(item) === chain && poolOf(item) === pool && tokenOf(item) === token)) return true;
    if (!pool && selectedTokens.has(tokenKey)) return true;

    const kind = String(row?.type ?? row?.kind ?? '');
    const isRelationEvent = ['pair-observed', 'relation-observed', 'relation-verified', 'verified', 'pool-created'].includes(kind);
    if (pool && isRelationEvent && labels.includes(selected)) return true;

    const match = row?.match ?? relation.match;
    const ownNameMatch = match?.level === 'B' && match.evidenceStatus === 'name-only'
      && normalizeTicker(match.ticker) === selected;
    const projectedNameMatch = labels.includes(selected) && !!row?.matchType && !!row?.keyword;
    return kind === 'discovered' && (nameMatches.has(tokenKey) || ownNameMatch || projectedNameMatch);
  });
}

const defaultRecordKey = row => {
  const chain = chainOf(row), token = tokenOf(row), id = row?.id ?? row?.tradeId;
  if (id != null && String(id)) return `${chain}:${token}:${String(row?.venue ?? '')}:${String(row?.marketId ?? '')}:${id}`;
  return `${chain}:${token}:${poolOf(row)}:${String(row?.stock ?? row?.relation?.stock ?? '')}:${row?.type ?? row?.kind ?? ''}:${row?.t ?? row?.at ?? row?.verifiedAt ?? row?.discoveredAt ?? ''}`;
};

/** Buffer only an explicit user pause. Pass the complete current list, including removals. */
export function createReadingBuffer({ key = defaultRecordKey, limit = 5, context = '' } = {}) {
  const size = Number.isFinite(Number(limit)) ? Math.max(0, Math.floor(Number(limit))) : 5;
  const historyLimit = Math.max(1000, size * 10);
  let currentContext = context;
  let initialized = false;
  let latest = new Map();
  let visible = [];
  let known = new Set();
  let pending = new Set();

  const result = (addedKeys = new Set()) => ({
    rows:visible.map(recordKey => latest.get(recordKey)).filter(Boolean),
    pendingCount:pending.size,
    addedKeys:new Set(addedKeys),
  });
  const reset = (nextContext = '') => {
    currentContext = nextContext;
    initialized = false;
    latest = new Map();
    visible = [];
    known = new Set();
    pending = new Set();
    return result();
  };
  const flush = () => {
    visible = [...latest.keys()].slice(0, size);
    const added = new Set(visible.filter(recordKey => pending.has(recordKey)));
    pending.clear();
    return result(added);
  };
  const sync = (records = [], { paused = false, context:nextContext = currentContext } = {}) => {
    if (nextContext !== currentContext) reset(nextContext);
    latest = new Map();
    for (const row of records ?? []) {
      const rawKey = key(row);
      if (rawKey == null || rawKey === '') continue;
      const recordKey = String(rawKey);
      // Input already has the desired newest-first order; first delivery wins.
      if (!latest.has(recordKey)) latest.set(recordKey, row);
    }
    pending = new Set([...pending].filter(recordKey => latest.has(recordKey)));
    if (!initialized && latest.size) {
      initialized = true;
      known = new Set(latest.keys());
      visible = [...latest.keys()].slice(0, size);
      return result();
    }
    for (const recordKey of latest.keys()) {
      if (!known.has(recordKey)) pending.add(recordKey);
      known.add(recordKey);
    }
    // Bound past identities when a page stays open through a busy session.
    if (known.size > historyLimit) for (const recordKey of known) {
      if (!latest.has(recordKey)) known.delete(recordKey);
      if (known.size <= historyLimit) break;
    }
    if (paused) {
      visible = visible.filter(recordKey => latest.has(recordKey));
      return result();
    }
    return flush();
  };
  return { sync, flush, reset };
}
