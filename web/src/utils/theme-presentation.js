const DEFAULT_FRESH_MS = 15 * 60 * 1000;

const norm = (value) => String(value ?? '').trim().toLowerCase();
const finite = (value) => value != null && Number.isFinite(Number(value));

export function observationState(value, observedAt, now = Date.now(), freshMs = DEFAULT_FRESH_MS) {
  if (!finite(value)) return 'missing';
  if (!observedAt || !Number.isFinite(Number(observedAt))) return 'historical';
  return now - Number(observedAt) <= freshMs ? 'fresh' : 'historical';
}

export function fieldObservedAt(asset, field) {
  return asset?.fieldTimes?.[field] ?? asset?.quoteAt ?? asset?.updatedAt ?? null;
}

export function buildThemeRows(relations, assets, now = Date.now()) {
  const assetMap = new Map(
    (assets ?? []).map((asset) => [
      `${String(asset.chainId ?? asset.chain ?? '196')}:${norm(asset.token)}`,
      asset,
    ]),
  );
  const pools = new Map();

  for (const relation of relations ?? []) {
    if (relation?.level !== 'A' || !relation?.token) continue;
    const chainId = String(relation.chainId ?? relation.chain ?? '196');
    const poolKey = `${chainId}:${norm(relation.pool || relation.id)}`;
    if (pools.has(poolKey)) continue;
    const asset = assetMap.get(`${chainId}:${norm(relation.token)}`) ?? null;
    pools.set(poolKey, {
      relation,
      asset,
      chainId,
      liquidityState: observationState(relation.liquidityUsd, relation.liquidityAt, now),
    });
  }

  const grouped = new Map();
  for (const pool of pools.values()) {
    const assetKey = `${pool.chainId}:${norm(pool.relation.token)}`;
    if (!grouped.has(assetKey)) grouped.set(assetKey, []);
    grouped.get(assetKey).push(pool);
  }

  return [...grouped.entries()].map(([key, assetPools]) => {
    assetPools.sort((a, b) => {
      const freshDiff = Number(b.liquidityState === 'fresh') - Number(a.liquidityState === 'fresh');
      if (freshDiff) return freshDiff;
      return (Number(b.relation.liquidityUsd) || -1) - (Number(a.relation.liquidityUsd) || -1);
    });
    const representative = assetPools[0];
    return {
      key,
      relation: representative.relation,
      asset: representative.asset,
      chainId: representative.chainId,
      liquidityState: representative.liquidityState,
      poolCount: assetPools.length,
      pools: assetPools.map((item) => item.relation),
    };
  });
}

function rowMetric(row, sort) {
  if (sort === 'volume24h') return finite(row.asset?.volume24h) ? Number(row.asset.volume24h) : -Infinity;
  if (sort === 'change24h') return finite(row.asset?.change24h) ? Number(row.asset.change24h) : -Infinity;
  if (sort === 'firstSeen') return Number(row.relation?.firstSeen) || -Infinity;
  return finite(row.relation?.liquidityUsd) ? Number(row.relation.liquidityUsd) : -Infinity;
}

function sortState(row, sort, now) {
  if (sort === 'poolLiquidity') return row.liquidityState;
  if (sort === 'volume24h') return observationState(row.asset?.volume24h, fieldObservedAt(row.asset, 'volume24h'), now);
  if (sort === 'change24h') return observationState(row.asset?.change24h, fieldObservedAt(row.asset, 'change24h'), now);
  return row.relation?.firstSeen ? 'fresh' : 'missing';
}

export function sortThemeRows(rows, sort = 'poolLiquidity', includeHistorical = true, now = Date.now()) {
  const stateRank = { fresh: 0, historical: 1, missing: 2 };
  return (rows ?? [])
    .filter((row) => includeHistorical || row.liquidityState === 'fresh')
    .slice()
    .sort((a, b) => {
      const stateDiff = stateRank[sortState(a, sort, now)] - stateRank[sortState(b, sort, now)];
      if (stateDiff) return stateDiff;
      const metricDiff = rowMetric(b, sort) - rowMetric(a, sort);
      if (Number.isFinite(metricDiff) && metricDiff) return metricDiff;
      return String(a.asset?.symbol ?? a.relation?.token ?? '').localeCompare(String(b.asset?.symbol ?? b.relation?.token ?? ''));
    });
}

export function fieldCoverage(rows, field, now = Date.now(), freshMs = DEFAULT_FRESH_MS) {
  let known = 0;
  let fresh = 0;
  for (const row of rows ?? []) {
    const value = row?.[field];
    if (!finite(value)) continue;
    known += 1;
    if (observationState(value, fieldObservedAt(row, field), now, freshMs) === 'fresh') fresh += 1;
  }
  return { field, total: (rows ?? []).length, known, fresh };
}

export { DEFAULT_FRESH_MS };
