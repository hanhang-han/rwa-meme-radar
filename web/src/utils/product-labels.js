export const RISK_LABELS = {
  wash_suspect: ['成交异常', 'Unusual trading'],
  thin_spike: ['低深度暴涨', 'Thin & spiking'],
  contract_risk: ['合约风险', 'Contract risk'],
  concentrated: ['持仓集中', 'Concentrated'],
  holder_anomaly: ['持币地址异常', 'Holder anomaly'],
};

export function relationLevel(relation) {
  if (['A','B','C'].includes(relation?.level)) return relation.level;
  return null;
}

export function relationStockIdentityStatus(relation) {
  const identities = [relation?.sideIdentity, relation?.stockIdentity];
  if (identities.some(identity => identity?.verificationStatus === 'legacy')) return 'legacy';
  const bothOfficial = identities.every(identity =>
    identity?.verificationStatus === 'official' && identity?.eligibleForPair === true,
  );
  return relation?.level === 'A' && relation?.evidenceStatus === 'qualified' && bothOfficial
    ? 'official'
    : 'unverified';
}

export function riskFlags(asset) {
  return Array.isArray(asset?.riskFlags) ? asset.riskFlags.filter(flag => Object.hasOwn(RISK_LABELS, flag)) : [];
}

// The pinned issuer catalogue publishes "official", "legacy" and
// "unverified". Only current official deployments may supply the stock
// headline quote; legacy wrappers remain visible in the version list.
export function isOfficialStock(row) {
  return row?.issuerIdentity?.verificationStatus === 'official' && row.issuerIdentity?.eligibleForPair !== false;
}

export function preferredOfficialStock(rows) {
  const official = (rows ?? []).filter(isOfficialStock);
  const hasPrice = row => row.price != null && Number.isFinite(Number(row.price));
  const observedAt = row => Number(row.quoteAt ?? row.fieldTimes?.price ?? 0) || 0;
  return official.sort((a,b) => Number(hasPrice(b)) - Number(hasPrice(a)) || observedAt(b) - observedAt(a) || String(a.chainId).localeCompare(String(b.chainId)))[0] ?? null;
}

export function volumeLiquidityRatio(asset) {
  const volume = asset?.volume24h;
  const liquidity = asset?.totalLiquidityUsd;
  if (volume == null || liquidity == null || Number(liquidity) <= 0 || !Number.isFinite(Number(volume)) || !Number.isFinite(Number(liquidity))) return null;
  if (asset.volumeCurrency !== 'USD' || asset.totalLiquidityStatus !== 'current') return null;
  const coverage=asset.totalLiquidityCoverage;
  if (coverage?.scope !== 'token-aggregate' || coverage?.coverage !== 'provider-indexed-pools' || !coverage?.provider) return null;
  const volumeScope=asset.fieldScopes?.volume24h ?? asset.volumeScope;
  if (volumeScope !== 'token-aggregate' || asset.fieldSources?.volume24h !== coverage.provider) return null;
  const volumeAt=Number(asset.fieldTimes?.volume24h),liquidityAt=Number(asset.totalLiquidityAt),now=Date.now();
  if (!volumeAt || !liquidityAt || Math.abs(volumeAt-liquidityAt)>300000 || now-volumeAt>1800000 || now-liquidityAt>1800000 || volumeAt>now+1000 || liquidityAt>now+1000) return null;
  return Number(volume) / Number(liquidity);
}
