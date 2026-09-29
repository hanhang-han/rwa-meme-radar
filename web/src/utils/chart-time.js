export const chartTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
function asDate(time) {
  if (typeof time === 'number') return new Date(time * 1000);
  if (typeof time === 'string') return new Date(time);
  return new Date(Date.UTC(time.year,time.month-1,time.day));
}
export function chartTime(time, kind, zone = chartTimeZone) {
  const options = kind === 0 ? {year:'numeric'} : kind === 1 ? {month:'short',year:'2-digit'} : kind === 2 ? {month:'2-digit',day:'2-digit'} : {hour:'2-digit',minute:'2-digit',hourCycle:'h23',...(kind === 4 ? {second:'2-digit'} : {})};
  return new Intl.DateTimeFormat(undefined,{...options,timeZone:zone}).format(asDate(time));
}
export function chartDateTime(time, zone = chartTimeZone) {
  return new Intl.DateTimeFormat(undefined,{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(asDate(time));
}
export function samplePoints(samples, asset) {
  const scope = asset.priceScope ?? 'dex';
  const currency = asset.priceCurrency ?? (scope === 'dex' ? 'USD' : null);
  const observationScope = p => p.venue === 'dex' ? 'dex' : (p.scope === 'exchange-token' || p.venue === 'binance') ? 'exchange' : p.scope;
  const compatible = p => !p.provenance || (observationScope(p.provenance) === scope && p.provenance.currency === currency
    && (!asset.provider || String(p.provenance.provider).toLowerCase() === String(asset.provider).toLowerCase()));
  const rows = samples.filter(compatible);
  const at = asset.fieldTimes?.price ?? asset.quoteAt;
  // Legacy rows can still be inspected, but their unknown units/source must
  // never be stitched to a current quote from a different market.
  const verified = rows.length > 0 && rows.every(p => p.provenance && compatible(p));
  if (verified && asset.price != null && at) rows.push({t:at,price:asset.price});
  const points = new Map();
  for (const p of rows) if (Number.isFinite(p.t) && Number.isFinite(p.price)) points.set(Math.floor(p.t/1000),p.price);
  return [...points].sort((a,b)=>a[0]-b[0]).map(([time,value])=>({time,value}));
}

const BAR_MS = {
  '1m': 60_000, '5m': 300_000, '15m': 900_000,
  '1H': 3_600_000, '4H': 14_400_000, '1D': 86_400_000, '1W': 604_800_000,
};

export function candleTailState(info, bar, nowMs = Date.now()) {
  const rows = info?.rows;
  if (!Array.isArray(rows) || !rows.length) return 'missing';
  if (info?.stale || info?.status === 'stale') return 'stale';
  const tail = Number(rows.at(-1)?.t);
  if (!Number.isFinite(tail)) return 'missing';
  const grace = Math.max(10 * 60_000, (BAR_MS[bar] ?? 300_000) * 2);
  return nowMs - tail > grace ? 'quiet' : 'current';
}
