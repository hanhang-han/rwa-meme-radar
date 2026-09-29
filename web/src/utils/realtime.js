export const normalizeAddress = value => String(value ?? '').trim().toLowerCase();
export const assetKey = (chainId, token) => `${String(chainId ?? '196')}:${normalizeAddress(token)}`;
export function quoteKey({chainId, token, venue = 'dex', quoteType = 'dex', marketId = '', priceCurrency = ''}) {
  return `${assetKey(chainId, token)}:${String(venue).toLowerCase()}:${String(quoteType).toLowerCase()}:${marketId}:${priceCurrency}`;
}
export const sameAsset = (a, b) => assetKey(a?.chainId ?? a?.chain, a?.token ?? a?.tokenContractAddress) === assetKey(b?.chainId ?? b?.chain, b?.token ?? b?.tokenContractAddress);
const has = (o, k) => Object.prototype.hasOwnProperty.call(o ?? {}, k);
export function quoteTime(row) {
  return Number(row?.fieldTimes?.price ?? row?.marketAt ?? row?.at ?? row?.quoteAt ?? row?.receivedAt) || 0;
}
const QUOTE_FIELDS = ['price','change24h','volume24h','txs24h','buys24h','sells24h','exchangeTrades24h','marketCap','liquidity','holders','volume5m','volume1h'];
const MAPS = ['fieldTimes','fieldSources','fieldScopes','fieldTimeKinds','fieldObservations','fieldStatus','fieldRevisions'];
const PRICE_META = ['provider','venue','marketId','quoteType','priceScope','priceCurrency','priceProvenance','quoteStatus','quoteReason','marketAt','receivedAt','timeKind'];

// One source time/version per field: a new price cannot smuggle an older
// volume into the row. Explicit null invalidates; an omitted key is unchanged.
export function applyQuote(row, quote) {
  if (!row || !quote) return false;
  let applied = false;
  const accepted = new Set();
  const baseAt = quoteTime(quote);
  for (const field of QUOTE_FIELDS) {
    if (!has(quote, field)) continue;
    const at = Number(quote.fieldTimes?.[field] ?? baseAt) || 0;
    const currentAt = Number(row.fieldTimes?.[field] ?? quoteTime(row)) || 0;
    const revision = Number(quote.fieldRevisions?.[field] ?? quote.fieldVersions?.[field] ?? quote.revision) || 0;
    const currentRevision = Number(row.fieldRevisions?.[field]) || 0;
    if (revision && currentRevision ? revision < currentRevision : !at || at < currentAt) continue;
    row[field] = quote[field];
    row.fieldTimes = {...row.fieldTimes, [field]: at || null};
    if (revision) row.fieldRevisions = {...row.fieldRevisions, [field]: revision};
    const metadata = {
      fieldSources: quote.fieldSources?.[field] ?? quote.provider ?? quote.venue,
      fieldScopes: quote.fieldScopes?.[field] ?? (field === 'volume24h' ? quote.volumeScope : quote.priceScope),
      fieldTimeKinds: quote.fieldTimeKinds?.[field] ?? quote.timeKind ?? (quote.marketAt ? 'market' : 'received'),
      fieldObservations: quote.fieldObservations?.[field],
      fieldStatus: quote.fieldStatus?.[field],
    };
    for (const [map, value] of Object.entries(metadata)) {
      row[map] = {...row[map]};
      if (value !== undefined) row[map][field] = value;
      else delete row[map][field];
    }
    accepted.add(field); applied = true;
  }
  if (accepted.has('price')) {
    for (const key of PRICE_META) if (has(quote, key)) row[key] = quote[key];
    row.quoteAt = row.fieldTimes.price;
    // A received-time source stays received-time even though it has an age.
    if (!has(quote, 'priceProvenance')) row.priceProvenance = {timeKind: quote.timeKind ?? (quote.marketAt ? 'market' : 'received'), venue: quote.venue, marketAt: quote.timeKind === 'received' ? null : quote.marketAt ?? null, receivedAt: quote.receivedAt ?? null};
  }
  if (accepted.has('volume24h') || accepted.has('price') && quote.priceScope === 'exchange') {
    for (const key of ['volumeCurrency','volumeScope']) if (has(quote, key)) row[key] = quote[key];
  }
  if (applied && quote.priceScope === 'exchange') {
    row.buys24h = null; row.sells24h = null;
    row.txs24h = quote.exchangeTrades24h ?? row.exchangeTrades24h ?? null;
  }
  return applied;
}

// Complete server projection rows have an ordered revision and may remove
// fields. Legacy/detail responses without revisions retain newer observations.
export function mergeEntity(row, incoming, revision = 0, replace = false) {
  if (!row) return {...incoming, ...(revision ? {_revision: revision} : {})};
  if (revision && Number(row._revision ?? 0) > revision) return row;
  const old = {...row};
  const savedMaps = Object.fromEntries(MAPS.map(k => [k, {...row[k]}]));
  if (replace) for (const key of Object.keys(row)) if (!has(incoming, key) && key !== '_revision') delete row[key];
  Object.assign(row, incoming);
  if (revision) row._revision = revision;
  if(revision)return row;
  // Only unversioned legacy/detail responses retain newer observations.
  // Revisioned projections own market selection even when source times differ.
  for (const field of QUOTE_FIELDS) {
    if (!has(old, field)) continue;
    const before = Number(savedMaps.fieldTimes[field]) || 0;
    const after = Number(incoming.fieldTimes?.[field] ?? (field === 'price' ? incoming.quoteAt : 0)) || 0;
    const explicitlyInvalid = has(incoming, field) && incoming[field] == null && revision > Number(old._revision ?? 0);
    if (before > after && !explicitlyInvalid && (!replace || has(incoming, field))) {
      row[field] = old[field];
      for (const map of MAPS) if (has(savedMaps[map], field)) row[map] = {...row[map], [field]: savedMaps[map][field]};
      if (field === 'price') for (const key of [...PRICE_META, 'quoteAt']) if (has(old, key)) row[key] = old[key];
    }
  }
  return row;
}

export function pricePrecision(value) {
  const n = Math.abs(Number(value));
  if (!Number.isFinite(n) || n === 0) return 2;
  if (n >= 100) return 2;
  if (n >= 1) return 4;
  if (n >= 0.01) return 6;
  return Math.min(16, Math.max(8, Math.ceil(-Math.log10(n)) + 4));
}
