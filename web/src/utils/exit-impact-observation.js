const numeric = value => typeof value === 'number' && Number.isFinite(value);

// Keep value and evidence on the same packet. A requested Quoter response must
// never inherit the V2 reserve estimate's source or 30-minute lifetime.
export function exitImpactObservation(estimate, quote, now = Date.now()) {
  const packet = quote ?? estimate ?? null;
  const quotedMethod = packet?.method;
  const isQuoter = quotedMethod === 'v3-v2-quoter' || quotedMethod === 'v4-quoter';
  const observed = numeric(packet?.at) && packet.at > 0 && packet.at <= now;
  const withinEstimateWindow = observed && now - packet.at <= 1_800_000;
  const validUntil = packet?.validUntil;
  const withinQuoteWindow = observed && numeric(validUntil) && validUntil > packet.at && now < validUntil;
  const isEstimate = quotedMethod === 'v2-balanced-reserves-estimate';
  const current = packet?.status === 'current' && numeric(packet.valuePercent)
    && (quote != null ? isQuoter && withinQuoteWindow : isEstimate && withinEstimateWindow);
  const expired = packet?.status === 'current' && observed
    && (isQuoter ? numeric(validUntil) && now >= validUntil : isEstimate && !withinEstimateWindow);
  return { packet, quotedMethod, isQuoter, current, expired,
    valuePercent: current ? packet.valuePercent : null };
}
