const TERMINAL_STATES = new Set(['market_closed', 'unsupported', 'no-verified-market', 'entitlement-required', 'identity-unverified', 'quota-exhausted', 'budget-exhausted']);

export function quoteAt(row, reference = false) {
  return reference ? row?.referenceAt ?? row?.fieldTimes?.stockPrice : row?.fieldTimes?.price ?? row?.quoteAt;
}

export function quoteFreshness(row, reference = false, now = Date.now()) {
  const value = reference ? row?.stockPrice : row?.price;
  const at = quoteAt(row, reference);
  const provider = String(reference ? row?.referenceProvider ?? '' : row?.fieldSources?.price ?? row?.provider ?? '').toLowerCase();
  const declared = String(reference ? row?.referenceStatus ?? '' : row?.quoteStatus ?? '').toLowerCase();
  const reason = reference ? row?.referenceReason : row?.quoteReason;
  if (TERMINAL_STATES.has(declared)) return { state: declared, at, stale: false, reason };
  if (value == null || !Number.isFinite(Number(value))) return { state: 'missing', at, stale: true, reason };
  if (!at || Number(at) > now + 1000) return { state: 'unknown-time', at, stale: true, reason };
  const threshold = reference ? (row?.referenceDelayMs ?? 0) + 5 * 60_000 : provider === 'binance' ? 2 * 60_000 : 15 * 60_000;
  if (now - Number(at) > threshold) return { state: 'stale', at, stale: true, reason };
  if (reference && row?.referenceScope === 'issuer-reference') return { state: 'issuer-reference', at, stale: false };
  if (reference && row?.referenceRealtime !== true || declared === 'delayed') return { state: 'delayed', at, stale: false };
  const marketTime = row?.priceProvenance?.timeKind === 'market';
  if (reference && row?.referenceRealtime === true || !reference && marketTime && (declared === 'live' || provider === 'binance')) return { state: 'live', at, stale: false };
  return { state: 'scheduled', at, stale: false };
}
