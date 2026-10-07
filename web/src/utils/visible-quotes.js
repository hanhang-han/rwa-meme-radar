import { applyQuote, normalizeAddress } from './realtime.js';

const own = (row, key) => Object.prototype.hasOwnProperty.call(row ?? {}, key);
const currency = value => String(value ?? '').trim().toUpperCase();
const name = value => String(value ?? '').trim().toLowerCase().replace(/[^a-z0-9]/g, '');
const positiveTime = value => Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : 0;
export const VISIBLE_QUOTE_LIMIT = 80;

export function visibleQuoteIdentity(row) {
  const chain = String(row?.chainId ?? '');
  const token = normalizeAddress(row?.token ?? row?.tokenContractAddress);
  return /^[1-9]\d*$/.test(chain) && /^0x[0-9a-f]{40}$/.test(token) ? `${chain}:${token}` : '';
}

export function visibleQuoteAt(row) {
  return positiveTime(row?.fieldTimes?.price ?? row?.fieldAvailability?.price?.at ?? row?.quoteAt ?? row?.marketAt ?? row?.at);
}

export function visibleQuoteTokens(rows) {
  return [...new Set((rows ?? []).map(visibleQuoteIdentity).filter(Boolean))].slice(0, VISIBLE_QUOTE_LIMIT).sort();
}

function priceScope(row) {
  const fields = [row?.fieldScopes?.price, row?.priceScope, row?.quoteType].map(name);
  if (fields.includes('pool')) return 'pool';
  if (fields.includes('exchange')) return 'exchange';
  if (fields.includes('token') || fields.includes('dex')) return 'token';
  return '';
}

function market(row) {
  return normalizeAddress(row?.marketId ?? row?.poolId ?? row?.pool ?? row?.priceProvenance?.marketId ?? row?.priceProvenance?.pool);
}

function provider(row, field = 'price') {
  return name(row?.fieldSources?.[field] ?? row?.provider ?? row?.source);
}

export function visibleQuoteMatches(row, packet) {
  const identity = visibleQuoteIdentity(row);
  if (!identity || identity !== visibleQuoteIdentity(packet)) return false;
  // A native pool price and a token-wide USD quote are different series.
  // Transport activity must never select a new market or convert a unit.
  if (!currency(row.priceCurrency) || currency(row.priceCurrency) !== currency(packet.priceCurrency)) return false;
  const scope = priceScope(row);
  if (!scope || scope !== priceScope(packet)) return false;
  if (row.venue && (!packet.venue || name(row.venue) !== name(packet.venue))) return false;
  const before = market(row), after = market(packet);
  if (scope === 'pool' && (!before || !after || before !== after)) return false;
  if (before && before !== after) return false;
  // Without an explicit market id, a quote may update only the selected
  // provider. The next directory publication owns any provider switch.
  if (!before && (!provider(row) || provider(row) !== provider(packet))) return false;
  return true;
}

const FIELDS = ['price', 'change24h', 'volume24h', 'marketCap', 'liquidity', 'holders',
  'txs24h', 'buys24h', 'sells24h', 'exchangeTrades24h', 'volume5m', 'volume1h'];
const STAT_FIELDS = new Set(FIELDS.filter(field => field !== 'price'));
const AMOUNTS = new Set(['volume24h', 'volume5m', 'volume1h', 'marketCap', 'liquidity']);
const fieldScope = value => name(value) === 'dex' ? 'token' : name(value);

export function applyVisibleQuote(row, packet, now = Date.now()) {
  if (!visibleQuoteMatches(row, packet)) return false;
  const quote = { ...packet, fieldTimes: {}, fieldRevisions: {}, fieldVersions: {}, revision: undefined };
  // These are selected-series labels, owned by the directory. Alias changes
  // such as OKX token vs dex must not look like a market switch in the next
  // reconciliation or allow an older HTTP quote to rewind this observation.
  for (const key of ['priceScope','quoteType','venue','marketId']) if (own(row,key)) quote[key]=row[key];
  const baseAt = visibleQuoteAt(packet);
  const accepted = [];
  for (const field of FIELDS) {
    delete quote[field];
    if (!own(packet, field)) continue;
    // Exchange rolling statistics have their own clock; a fresh trade price
    // alone must not freshen old daily volume or change percentages.
    const at = positiveTime(packet.fieldTimes?.[field] ?? (field === 'price' ? baseAt : packet.statisticsAt));
    const previousAt = Math.max(positiveTime(row.fieldTimes?.[field]),positiveTime(row.fieldAvailability?.[field]?.at),field === 'price' ? visibleQuoteAt(row) : 0);
    if (!at || at > now + 1000 || at < previousAt) continue;
    const value = packet[field];
    if (value != null && (!Number.isFinite(Number(value)) || field === 'price' && Number(value) <= 0 || AMOUNTS.has(field) && Number(value) < 0)) continue;
    if (STAT_FIELDS.has(field)) {
      const currentScope = row.fieldScopes?.[field] ?? (field === 'volume24h' ? row.volumeScope : row.priceScope);
      const nextScope = packet.fieldScopes?.[field] ?? (field === 'volume24h' ? packet.volumeScope : packet.priceScope);
      if (!currentScope || !nextScope || fieldScope(currentScope) !== fieldScope(nextScope)) continue;
      if (provider(row, field) && provider(row, field) !== provider(packet, field)) continue;
      if (AMOUNTS.has(field)) {
        const unit = field.startsWith('volume') ? row.volumeCurrency : row.priceCurrency;
        const incomingUnit = field.startsWith('volume') ? packet.volumeCurrency : packet.priceCurrency;
        if (!currency(unit) || currency(unit) !== currency(incomingUnit)) continue;
      }
    }
    quote[field] = value == null ? null : Number(value);
    quote.fieldTimes[field] = at;
    accepted.push(field);
  }
  if (!accepted.length || !applyQuote(row, quote)) return false;
  // Directory availability contains a published value as well as its status.
  // That older duplicate must not mask a newer accepted live observation.
  if (row.fieldAvailability) {
    row.fieldAvailability = {...row.fieldAvailability};
    for (const field of accepted) delete row.fieldAvailability[field];
  }
  return true;
}
