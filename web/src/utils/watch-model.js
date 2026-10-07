import { mergeDirectoryAsset } from './meme-directory-refresh.js';
import { applyVisibleQuote, visibleQuoteIdentity, visibleQuoteAt, visibleQuoteMatches } from './visible-quotes.js';
import { applyVisibleExchangeQuote, visibleExchangeObservation } from './visible-exchange-quote.js';
import { normalizeTicker } from './stock-theme-model.js';
import { normalizeEventAddress, eventKey, mergeEventItems } from './event-records.js';

export const WATCH_PAGE_SIZE = 20;
export function watchKeys(values = []) {
  return [...new Set(values.map(value => {
    const key = String(value ?? '');
    if (/^stock:[A-Z0-9.^-]{1,24}$/i.test(key)) return `stock:${key.slice(6).toUpperCase()}`;
    if (/^[1-9]\d{0,9}:0x[a-f0-9]{40}$/i.test(key)) return key.toLowerCase();
    return '';
  }).filter(Boolean))].slice(0, 200);
}
export function placeholderWatch(key) {
  if (key.startsWith('stock:')) return { key, kind: 'theme', ticker: normalizeTicker(key.slice(6)), asset: null, available: false };
  const [chainId, token] = key.split(':');
  return { key, kind: 'asset', chainId, token, available: false, asset: { chainId, token } };
}
export function mergeWatchRow(previous, incoming) {
  const next = incoming.asset ? { ...incoming.asset, token: incoming.asset.token ?? incoming.asset.tokenContractAddress } : null;
  if (!previous || previous.key !== incoming.key) return { ...incoming, asset: next };
  // Null means this asset is no longer available in the publication. Only
  // quotes for the same contract/market retain their newer observations.
  const sameSeries = next && previous.asset && visibleQuoteMatches(previous.asset, next);
  if (next && previous.asset && !sameSeries && previous.asset.priceCurrency && next.priceCurrency) {
    // A directory-selected provider or market switch starts a new primary
    // series. An old observation from another source is never spliced in.
    if (visibleQuoteIdentity(previous.asset) === visibleQuoteIdentity(next) && previous.asset._exchangeQuote)
      next._exchangeQuote = previous.asset._exchangeQuote;
    return { ...incoming, asset: next };
  }
  if (sameSeries) {
    for (const field of ['price', 'change24h']) {
      const before = Number(previous.asset.fieldTimes?.[field] ?? (field === 'price' ? visibleQuoteAt(previous.asset) : 0)) || 0;
      const after = Number(next.fieldTimes?.[field] ?? (field === 'price' ? visibleQuoteAt(next) : 0)) || 0;
      if (next[field] === null && before > after) {
        delete next[field];
        if (next.fieldAvailability) { next.fieldAvailability = { ...next.fieldAvailability }; delete next.fieldAvailability[field]; }
      }
    }
  }
  const asset = next ? mergeDirectoryAsset(previous.asset, next) : null;
  return { ...incoming, asset };
}
export function planWatchSummary(previous = [], incoming = []) {
  const old = new Map(previous.map(row => [row.key, row]));
  return { rows: incoming.map(row => mergeWatchRow(old.get(row.key), row)), pending: null };
}
export function applyWatchQuote(rows, packet, now = Date.now()) {
  let changed = false;
  for (const row of rows ?? []) {
    if (!row.asset || visibleQuoteIdentity(row.asset) !== visibleQuoteIdentity(packet)) continue;
    const main = applyVisibleQuote(row.asset, packet, now);
    const exchange = applyVisibleExchangeQuote(row.asset, packet, now);
    changed = main || exchange || changed;
  }
  return changed;
}
export function latestWatchQuote(rows, now = Date.now()) {
  return Math.max(0, ...(rows ?? []).map(row => Math.max(visibleQuoteAt(row.asset),
    visibleExchangeObservation(row.asset, now)?.at ?? 0)));
}
export function watchEventMatches(event, keys, chain = 'all') {
  const chainId = String(event?.chainId ?? event?.relation?.chainId ?? '');
  if (!chainId || chain !== 'all' && chainId !== String(chain)) return false;
  const ticker = normalizeTicker(event?.ticker ?? event?.relation?.ticker);
  const addresses = [event?.asset, event?.token, event?.stock, event?.relation?.token,
    event?.relation?.stock, event?.relation?.stockSide].map(value => normalizeEventAddress(value, chainId)).filter(Boolean);
  return keys.some(key => key.startsWith('stock:') ? normalizeTicker(key.slice(6)) === ticker
    : key.startsWith(`${chainId}:`) && (addresses.includes(key.slice(chainId.length + 1))
      // The API can prove a wrapper through the pool's persisted native-stock
      // identity even when historical body fields contain only the native side.
      || Array.isArray(event.matchedKeys) && event.matchedKeys.includes(key)));
}
export function filterWatchEvents(items, keys, chain = 'all') {
  return mergeEventItems([], (items ?? []).filter(event => watchEventMatches(event, keys, chain)));
}
export function planWatchEvents(current, pending, incoming) {
  return { items: mergeEventItems(current, mergeEventItems(pending ?? [], incoming)), pending: [] };
}
