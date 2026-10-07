const amount = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const count = value => Number.isInteger(value) && value >= 0 ? value : null;
const change = value => typeof value === 'number' && Number.isFinite(value) ? value : null;

// Keep display validation separate from the directory's pairing and sorting rules.
// Missing coverage stays unknown, while an observed zero remains a real zero.
export function stockDirectorySignals(card = {}) {
  const stock = card.stock ?? {}, theme = card.theme ?? {}, volume = theme.volume ?? {};
  const total = count(volume.total), known = count(volume.known);
  const ratio = card.volumeRatio7d?.value ?? card.volumeRatio7d ?? stock.volumeRatio7d?.value;
  return {
    price: amount(stock.price) > 0 && typeof stock.priceCurrency === 'string' && stock.priceCurrency ? stock.price : null,
    change24h: change(stock.change24h),
    pairedCount: count(theme.pairedCount), nameCount: count(theme.nameCount),
    poolVolume: amount(volume.value), poolTotal: total,
    poolKnown: known != null && total != null && known <= total ? known : null,
    poolRecorded: count(theme.poolCoverage?.recorded), ratio7d: amount(ratio),
  };
}
