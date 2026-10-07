const amount = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const timestamp = value => typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;

export function directoryCardMarket(card = {}, now = Date.now()) {
  const stock = card.stock ?? {};
  const at = timestamp(stock.fieldTimes?.price) ?? timestamp(stock.quoteAt);
  const usable = amount(stock.price) > 0 && typeof stock.priceCurrency === 'string' && !!stock.priceCurrency;
  const state = !usable ? 'missing' : !at || !stock.provider && !stock.fieldSources?.price ? 'unconfirmed'
    : at > now + 1000 ? 'unconfirmed' : now - at > 900000 ? 'historical' : 'current';
  const reference = card.equity;
  const equity = amount(reference?.stockPrice) > 0 && timestamp(reference?.referenceAt)
    && reference.referenceProvider && reference.referenceCurrency ? reference : null;
  const volume = card.theme?.volume ?? {};
  const poolValue = amount(volume.value);
  const poolTotal = amount(volume.total) ?? 0;
  const poolKnown = amount(volume.known) ?? 0;
  const coverage = card.theme?.poolCoverage ?? {};
  const poolRecorded = amount(coverage.recorded) ?? 0;
  const pending = ['unknown','stale','identityPending','belowMinimum','other']
    .map(reason => ({reason, count: amount(coverage[reason]) ?? 0})).filter(row => row.count > 0);
  return { usable, at, state, equity, poolValue,
    poolState: poolTotal <= 0 ? poolRecorded > 0 ? 'recorded-pending' : 'no-eligible-pools' : poolValue == null ? 'unavailable'
      : poolKnown < poolTotal ? 'partial' : 'available',
    poolTotal, poolKnown, poolRecorded, pending };
}
