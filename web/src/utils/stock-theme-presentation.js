import { comparableVolume, isRecentObservation, isQualifiedPool } from './stock-theme-model.js';

const amount = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const keyOf = row => `${String(row?.chainId ?? '')}:${String(row?.token ?? '').toLowerCase()}`;
const observation = value => typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
const poolOrder = row => [observation(row?.liquidityAt) ?? 0, observation(row?.checkedAt) ?? 0, observation(row?.poolMarket?.updatedAt) ?? 0];
const moreRecent = (row, previous) => { const a=poolOrder(row), b=poolOrder(previous);for(let i=0;i<a.length;i++){if(a[i]!==b[i])return a[i]>b[i];}return false; };

// The shared relation model also serves recorded links. This view counts only
// currently eligible pools, while preserving other pool evidence below.
export function currentStockTheme(theme = {}, now = Date.now(), relations = []) {
  const latest = new Map();
  for (const relation of relations) {
    if (!relation?.pool) continue;
    const key=`${String(relation.chainId ?? '')}:${String(relation.pool).toLowerCase()}`;
    if (!latest.has(key) || moreRecent(relation, latest.get(key))) latest.set(key, relation);
  }
  const pools = (theme.pools ?? []).flatMap(pool => {
    const relation=latest.get(pool.key) ?? pool.relation;
    return isQualifiedPool(relation, now) ? [{...pool,relation}] : [];
  });
  const active = new Set(pools.map(pool => pool.key));
  const activePools = new Map(pools.map(pool => [pool.key,pool]));
  const rows = (theme.rows ?? []).flatMap(row => {
    if (row.level !== 'A') return [row];
    const current = (row.pools ?? []).filter(pool => active.has(pool.key)).map(pool=>activePools.get(pool.key));
    return current.length ? [{ ...row, pools: current }] : [];
  });
  const recorded = new Map((theme.recordedPools ?? []).map(row => [row.key, row]));
  for (const pool of theme.pools ?? []) {
    if (active.has(pool.key)) continue;
    recorded.set(pool.key, { key: pool.key, chainId: pool.chainId, token: pool.relation.token,
      relation: latest.get(pool.key) ?? pool.relation, asset: pool.asset,
      detailAvailable: !!pool.asset && /^0x[0-9a-f]{40}$/i.test(String(pool.relation.token ?? '')) });
  }
  return { ...theme, rows, pools, recordedPools: [...recorded.values()],
    pairedCount: rows.filter(row => row.level === 'A').length,
    nameCount: rows.filter(row => row.level === 'B').length };
}

// Pool contributions and asset turnover intentionally keep separate denominators.
// A missing pool contribution never falls back to the asset's all-market volume.
export function stockThemePresentation(theme = {}, metrics = {}, now = Date.now()) {
  const source = new Map();
  const metricsCurrent = isRecentObservation(metrics?.at, 900_000, now);
  if (metricsCurrent && Array.isArray(metrics?.contributions)) {
    for (const item of metrics.contributions) {
      const value = amount(item?.volume24hUsd);
      if (value != null) source.set(keyOf(item), value);
    }
  }
  const paired = (theme.rows ?? []).filter(row => row.level === 'A');
  const known = paired.filter(row => source.has(keyOf(row)));
  const total = known.length ? known.reduce((sum, row) => sum + source.get(keyOf(row)), 0) : null;
  const rows = (theme.rows ?? []).map(row => {
    let poolVolume = row.level === 'A' ? source.get(keyOf(row)) ?? null : null;
    const assetVolume = comparableVolume(row.asset, now);
    const currentPools = (row.pools ?? []).filter(pool => {
      const market = pool.relation?.poolMarket;
      const currencies = [market?.volumeCurrency,market?.currency].filter(Boolean);
      const usd = currencies.length ? currencies.every(currency=>currency==='USD') : market?.provider==='DexScreener';
      return market?.scope === `pool:${String(pool.relation?.pool ?? '').toLowerCase()}`
        && !!market?.provider && usd && amount(market?.volume24h) != null
        && isRecentObservation(market?.updatedAt, 900_000, now);
    });
    const poolTimes = currentPools.map(pool => observation(pool.relation.poolMarket.updatedAt)).filter(Boolean);
    const currentPoolTotal = currentPools.reduce((sum, pool) => sum + pool.relation.poolMarket.volume24h, 0);
    // A retained response cannot keep a now-expired contribution current.
    if (poolVolume != null && (!poolTimes.length || Math.abs(poolVolume-currentPoolTotal) > Math.max(1e-8, poolVolume*1e-9))) poolVolume = null;
    return { ...row, poolVolume, assetVolume, poolKnownCount: poolVolume == null ? 0 : poolTimes.length, poolShare: poolVolume != null && total > 0 ? poolVolume / total : null,
      poolObservedAt: poolTimes.length ? Math.min(...poolTimes) : null,
      poolObservedLatest: poolTimes.length ? Math.max(...poolTimes) : null };
  });
  const validKnown = rows.filter(row => row.level === 'A' && row.poolVolume != null);
  const validTotal = validKnown.length ? validKnown.reduce((sum, row) => sum + row.poolVolume, 0) : null;
  for (const row of rows) row.poolShare = row.poolVolume != null && validTotal > 0 ? row.poolVolume / validTotal : null;
  const contributions = rows.filter(row => row.level === 'A' && row.poolVolume != null)
    .sort((a, b) => b.poolVolume - a.poolVolume || a.key.localeCompare(b.key));
  const times = contributions.flatMap(row => [row.poolObservedAt, row.poolObservedLatest]).filter(Boolean);
  return { rows, contributions, poolTotal: validTotal, knownMemes: validKnown.length, pairedMemes: paired.length,
    poolObservedAt: times.length ? Math.min(...times) : null,
    poolObservedLatest: times.length ? Math.max(...times) : null };
}

export function stockThemeSnapshotTime(payload) {
  // The response/metric calculation time is not a provider observation time.
  return observation(payload?.snapshotAt);
}

// Missing valuations and missing timestamps are not expired observations.
export function recordedPoolReason(relation = {}, now = Date.now()) {
  if (relation.evidenceStatus === 'issuer-deployment-unverified') return 'issuer-unverified';
  if (relation.evidenceStatus === 'liquidity-unknown' || amount(relation.liquidityUsd) == null) return 'liquidity-unknown';
  const at = observation(relation.liquidityAt);
  if (!at || at > now + 1000) return 'liquidity-time-unknown';
  if (!isRecentObservation(at, 900_000, now)) return 'liquidity-expired';
  if (relation.liquidityUsd < 1000) return 'liquidity-below-threshold';
  return 'criteria-unmet';
}

export function canShowStockVolumeDistribution(presentation = {}) {
  return (presentation.contributions ?? []).length >= 2 && amount(presentation.poolTotal) > 0;
}
