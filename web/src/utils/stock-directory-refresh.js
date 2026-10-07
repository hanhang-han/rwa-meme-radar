const themeKey = row => String(row?.ticker ?? '').toUpperCase();
const stockKey = stock => stock?.chainId != null && (stock.tokenContractAddress || stock.token)
  ? `${stock.chainId}:${String(stock.tokenContractAddress ?? stock.token).toLowerCase()}` : null;
const validAt = at => typeof at === 'number' && Number.isFinite(at) && at > 0 ? at : null;

// The HTTP directory and the watched quote channel can arrive in either order.
// A directory publication still owns relationships, totals and representative
// selection; an older quote for the same market must not rewind a live price.
export function mergeStockDirectoryRow(previous, incoming) {
  if (!previous || !incoming || stockKey(previous.stock) !== stockKey(incoming.stock) || !stockKey(incoming.stock)) return incoming;
  const old = previous.stock, next = incoming.stock;
  const compatible = old.priceCurrency === next.priceCurrency
    && String(old.priceScope ?? '') === String(next.priceScope ?? '')
    && String(old.venue ?? '') === String(next.venue ?? '')
    && String(old.marketId ?? old.market ?? old.poolId ?? old.pool ?? '').toLowerCase() === String(next.marketId ?? next.market ?? next.poolId ?? next.pool ?? '').toLowerCase();
  if (!compatible) return incoming;
  const stock = { ...next, fieldTimes: { ...next.fieldTimes }, fieldSources: { ...next.fieldSources } };
  for (const field of ['price', 'change24h']) {
    const oldAt = validAt(old.fieldTimes?.[field]) ?? (field === 'price' ? validAt(old.quoteAt) : null);
    const nextAt = validAt(next.fieldTimes?.[field]) ?? (field === 'price' ? validAt(next.quoteAt) : null);
    // Explicit unknown observations are respected. No publication time is used
    // as a substitute for a provider's quote time.
    if (oldAt && nextAt && oldAt > nextAt) {
      stock[field] = old[field]; stock.fieldTimes[field] = oldAt;
      if (old.fieldSources?.[field]) stock.fieldSources[field] = old.fieldSources[field];
      for (const map of ['fieldScopes', 'fieldAvailability', 'fieldTimeKinds']) {
        if (Object.hasOwn(old[map] ?? {}, field)) stock[map] = { ...stock[map], [field]: old[map][field] };
      }
      if (field === 'price') for (const key of ['quoteAt', 'provider', 'quoteStatus', 'priceProvenance']) {
        if (Object.hasOwn(old, key)) stock[key] = old[key];
      }
    }
  }
  let equity = incoming.equity;
  const oldEquity = previous.equity;
  if (equity && oldEquity && equity.referenceCurrency === oldEquity.referenceCurrency
    && old.stockIdentity?.id && old.stockIdentity.id === next.stockIdentity?.id
    && validAt(oldEquity.referenceAt) > validAt(equity.referenceAt)) equity = oldEquity;
  return { ...incoming, stock, equity };
}

// Apply membership, order and statistics immediately. Merge matching market
// observations so an older HTTP quote cannot overwrite a newer streamed quote.
export function mergeStockDirectorySnapshot(previous, incoming) {
  if (!previous) return incoming;
  const old = new Map((previous.unified?.stockThemes ?? []).map(row => [themeKey(row), row]));
  const oldTop = new Map((previous.directory?.topThemes ?? []).map(row => [themeKey(row), row]));
  return { ...incoming, directory: { ...incoming.directory,
    topThemes: (incoming.directory?.topThemes ?? []).map(row => mergeStockDirectoryRow(oldTop.get(themeKey(row)), row)) }, unified: { ...incoming.unified,
    stockThemes: (incoming.unified?.stockThemes ?? []).map(row => mergeStockDirectoryRow(old.get(themeKey(row)), row)) } };
}

export function stockQuoteAgeLabel(at, now, language = 'zh') {
  const time = validAt(at);
  if (!time || !Number.isFinite(now) || time > now + 1000) return language === 'en' ? 'Time unconfirmed' : '时间待确认';
  const seconds = Math.max(0, Math.floor((now - time) / 1000));
  if (seconds < 60) return language === 'en' ? `${seconds}s ago` : `${seconds} 秒前`;
  if (seconds < 3600) return language === 'en' ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return language === 'en' ? `${Math.floor(seconds / 3600)}h ago` : `${Math.floor(seconds / 3600)} 小时前`;
  return language === 'en' ? `${Math.floor(seconds / 86400)}d ago` : `${Math.floor(seconds / 86400)} 天前`;
}

export function applyStockDirectoryQuote(card, packet, applyTokenQuote, now = Date.now()) {
  if (!card?.stock) return false;
  const changed = applyTokenQuote(card.stock, packet);
  const quote = packet?.stock ?? packet?.row ?? packet;
  const underlying = card.stock.stockIdentity;
  if (!quote || !stockKey(quote) || stockKey(quote) !== stockKey(card.stock)
    || !underlying?.id || underlying.id !== quote.stockIdentity?.id
    || !underlying.currency || underlying.currency !== quote.referenceCurrency
    || quote.referenceScope !== 'equity-exchange' || quote.referenceIdentityVerified !== true
    || !quote.referenceProvider || typeof quote.stockPrice !== 'number' || !Number.isFinite(quote.stockPrice) || quote.stockPrice <= 0
    || !validAt(quote.referenceAt) || quote.referenceAt > now + 1000
    || validAt(card.equity?.referenceAt) > quote.referenceAt) return changed;
  const equity = { ...card.equity };
  for (const field of ['stockPrice', 'referenceAt', 'referenceObservedAt', 'referenceProvider', 'referenceCurrency',
    'referenceChange24h', 'referenceStatus', 'referenceRealtime', 'referenceDelayMs', 'referenceDelayStatus',
    'referenceSourceUrl', 'marketSession', 'marketSessionAt', 'marketSessionSource']) {
    if (Object.hasOwn(quote, field)) equity[field] = quote[field];
  }
  card.equity = equity;
  return true;
}
