import { visibleQuoteIdentity } from './visible-quotes.js';

const own = (row, field) => Object.hasOwn(row ?? {}, field);
const text = value => String(value ?? '').trim();
const label = value => text(value).toLowerCase().replace(/[\s_-]+/g, '');
const currency = value => text(value).toUpperCase();
const time = value => Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : 0;
const currencies = new Set(['USD', 'USDT', 'USDC', 'BTC', 'ETH', 'BNB', 'EUR', 'GBP', 'JPY', 'HKD', 'CNY', 'KRW', 'AUD', 'CAD', 'CHF', 'SGD']);
const safeLabel = value => value.length > 0 && value.length <= 100 && !/[\u0000-\u001f\u007f]/.test(value);
const fieldAt = (packet, field) => time(packet.fieldTimes?.[field]
  ?? (field === 'price' ? packet.marketAt ?? packet.quoteAt : packet.statisticsAt));
function series(packet) {
  const identity = visibleQuoteIdentity(packet), venue = text(packet?.venue), provider = text(packet?.provider ?? packet?.source);
  const marketId = text(packet?.marketId), unit = currency(packet?.priceCurrency);
  const fieldScope = packet?.fieldScopes?.price;
  const fieldCurrency = packet?.fieldObservations?.price?.currency;
  if (!identity || label(packet?.priceScope) !== 'exchange'
    || packet?.quoteType != null && !['exchange', 'exchangetoken'].includes(label(packet.quoteType))
    || own(packet.fieldScopes, 'price') && label(fieldScope) !== 'exchange'
    || fieldCurrency != null && currency(fieldCurrency) !== unit || !safeLabel(venue) || !safeLabel(provider)
    || !/^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/.test(marketId) || !currencies.has(unit)) return null;
  return { identity, venue, provider, marketId, priceCurrency: unit,
    key: `${identity}|${label(venue)}|${marketId.toUpperCase()}|${unit}|${label(provider)}` };
}
function duplicatesPrimary(asset, selected) {
  return label(asset.priceScope) === 'exchange' && label(asset.venue) === label(selected.venue)
    && text(asset.marketId).toUpperCase() === selected.marketId.toUpperCase()
    && currency(asset.priceCurrency) === selected.priceCurrency;
}

// The first proved exchange series is locked for this exact contract. It is a
// separate observation, never a currency conversion or a replacement DEX price.
export function applyVisibleExchangeQuote(asset, packet, now = Date.now()) {
  const incoming = series(packet);
  if (!incoming || visibleQuoteIdentity(asset) !== incoming.identity || duplicatesPrimary(asset, incoming)) return false;
  const previous = asset._exchangeQuote;
  if (previous && (previous.identity !== incoming.identity || previous.key !== incoming.key)) return false;
  const priceAt = fieldAt(packet, 'price'), beforeAt = time(previous?.fieldTimes?.price);
  if (!previous && (!own(packet, 'price') || packet.price == null || !Number.isFinite(Number(packet.price))
    || Number(packet.price) <= 0 || !priceAt || priceAt > now + 1000 || now - priceAt > 900000)) return false;
  const next = { ...(previous ?? incoming), fieldTimes: { ...previous?.fieldTimes },
    fieldSources: { ...previous?.fieldSources }, priceScope: 'exchange', quoteType: packet.quoteType ?? 'exchange-token' };
  let accepted = false;
  const sourceMatches = field => !packet.fieldSources?.[field] || label(packet.fieldSources[field]) === label(incoming.provider);
  if (own(packet, 'price') && priceAt && priceAt <= now + 1000 && priceAt >= beforeAt && sourceMatches('price')
    && (packet.price === null || Number.isFinite(Number(packet.price)) && Number(packet.price) > 0)) {
    next.price = packet.price === null ? null : Number(packet.price);
    next.quoteAt = priceAt; next.fieldTimes.price = priceAt; next.fieldSources.price = incoming.provider;
    next.timeKind = packet.fieldTimeKinds?.price ?? packet.timeKind ?? (packet.marketAt ? 'market' : 'received');
    next.marketAt = next.timeKind === 'market' ? time(packet.marketAt ?? priceAt) : null;
    next.receivedAt = time(packet.receivedAt) || null; accepted = true;
  }
  if (!previous && !accepted) return false;
  const statisticsAt = fieldAt(packet, 'change24h');
  const changeScope = label(packet.fieldScopes?.change24h ?? packet.priceScope);
  if (own(packet, 'change24h') && statisticsAt && statisticsAt <= now + 1000
    && statisticsAt >= time(previous?.fieldTimes?.change24h) && changeScope === 'exchange' && sourceMatches('change24h')
    && (packet.change24h === null || packet.change24h !== '' && !(typeof packet.change24h === 'string' && !packet.change24h.trim()) && Number.isFinite(Number(packet.change24h)))) {
    next.change24h = packet.change24h === null ? null : Number(packet.change24h);
    next.fieldTimes.change24h = statisticsAt; next.fieldSources.change24h = incoming.provider; accepted = true;
  }
  if (accepted) asset._exchangeQuote = next;
  return accepted;
}

export function visibleExchangeObservation(asset, now = Date.now()) {
  const selected = asset?._exchangeQuote;
  if (!selected || selected.identity !== visibleQuoteIdentity(asset) || duplicatesPrimary(asset, selected)) return null;
  const at = time(selected.fieldTimes?.price), changeAt = time(selected.fieldTimes?.change24h);
  return { ...selected, at, changeAt, current: !!at && at <= now + 1000 && now - at <= 900000,
    changeCurrent: !!changeAt && changeAt <= now + 1000 && now - changeAt <= 900000 };
}
