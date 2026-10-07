import { isRecentObservation, isQualifiedPool } from './stock-theme-model.js';
import { recordedPoolReason } from './stock-theme-presentation.js';

const finite = value => value != null && value !== '' && Number.isFinite(Number(value));

// Each cell keeps its own observation. A fresh quote does not renew its volume,
// liquidity or holder count, and an unknown volume unit never inherits USD.
export function directoryObservation(asset = {}, field, now = Date.now()) {
  const isWindow = ['change5m', 'change1h', 'change6h'].includes(field);
  const metric = isWindow ? asset.productMetrics?.changes?.[{ change5m: 'm5', change1h: 'h1', change6h: 'h6' }[field]] : null;
  const availability = asset.fieldAvailability?.[field];
  const expired = availability?.status === 'stale' || availability?.reason === 'observation-expired';
  const hasPublishedValue = availability && Object.hasOwn(availability, 'value');
  const raw = metric ? metric.value : hasPublishedValue && finite(availability.value) ? availability.value
    : hasPublishedValue && !expired ? null : asset[field];
  const unconfirmedEstimate = field === 'totalLiquidityUsd' && ['indexed-coverage-incomplete', 'valuation-unavailable', 'coverage-incomplete', 'missing-valuation'].includes(availability?.reason);
  // A zero subtotal for indexed pools cannot prove that the whole asset has no
  // liquidity. Even a current observation needs complete aggregate coverage.
  const unconfirmedZero = field === 'totalLiquidityUsd' && finite(raw) && Number(raw) === 0
    && asset.totalLiquidityCoverage?.complete !== true;
  const unavailable = availability?.status === 'missing' && !expired;
  const value = !unconfirmedEstimate && !unconfirmedZero && !unavailable && finite(raw) && (field.startsWith('change') || Number(raw) >= 0) ? Number(raw) : null;
  const at = metric ? metric.at : availability?.at ?? (field === 'price' ? asset.fieldTimes?.price ?? asset.quoteAt
    : field === 'totalLiquidityUsd' ? asset.totalLiquidityAt ?? asset.fieldTimes?.totalLiquidityUsd
      : asset.fieldTimes?.[field]);
  const source = metric?.source ?? availability?.source ?? availability?.provider ?? asset.fieldSources?.[field]
    ?? (field === 'price' ? asset.provider : field === 'totalLiquidityUsd' ? asset.totalLiquidityCoverage?.provider : null);
  const currency = field === 'price' ? asset.priceCurrency ?? '' : field === 'volume24h' ? asset.volumeCurrency ?? ''
    : field === 'totalLiquidityUsd' ? 'USD' : '';
  const scope = field === 'volume24h' ? asset.fieldScopes?.volume24h ?? asset.volumeScope : null;
  let state = value == null ? 'missing' : !finite(at) || Number(at) <= 0 || Number(at) > now ? 'unknown-time'
    : !isRecentObservation(at, field === 'totalLiquidityUsd' ? 1800000 : 900000, now) ? 'historical' : 'current';
  if (value != null && field === 'totalLiquidityUsd') {
    if (asset.totalLiquidityStatus === 'stale') state = 'historical';
    else if (asset.totalLiquidityStatus !== 'current') state = 'unverified';
  }
  if (value != null && availability?.status) {
    if (expired) state = 'historical';
    else if (availability.status === 'current' && isRecentObservation(at, field === 'totalLiquidityUsd' ? 1800000 : 900000, now)) state = 'current';
    else if (availability.status !== 'current') state = 'unverified';
  }
  if (value != null && metric?.status && metric.status !== 'current') state = metric.status === 'stale' ? 'historical' : 'unverified';
  const scopeKnown = field !== 'volume24h' || ['token', 'token-aggregate'].includes(scope);
  return { value, at: finite(at) && Number(at) > 0 ? Number(at) : null, source: source ?? null, currency, scope, scopeKnown, state };
}

export function comparableDirectoryPoints(points = [], now = Date.now()) {
  return points.filter(point => finite(point.volume) && Number(point.volume) >= 0
    && finite(point.liquidity) && Number(point.liquidity) >= 0
    && isRecentObservation(point.volumeAt, 900000, now)
    && isRecentObservation(point.liquidityAt, 1800000, now)
    && (!point.volumeCurrency || point.volumeCurrency === 'USD')
    && (!point.liquidityCurrency || point.liquidityCurrency === 'USD'));
}

export function directoryAssetKey(asset) {
  return `${asset?.chainId}:${String(asset?.token ?? '').toLowerCase()}`;
}

export function directoryRelationState(relation = {}, now = Date.now()) {
  if (relation.level === 'B') return 'name-match';
  if (relation.level === 'C') return 'community';
  if (relation.evidenceStatus === 'issuer-deployment-unverified'
    || relation.stockIdentity?.eligibleForPair === false || relation.sideIdentity?.eligibleForPair === false) return 'issuer-unverified';
  if (relation.evidenceStatus === 'liquidity-unknown') return 'liquidity-unknown';
  if (relation.liquidityStatus === 'stale' || relation.evidenceStatus === 'liquidity-stale') return 'liquidity-expired';
  return isQualifiedPool(relation, now) ? 'current-pair' : recordedPoolReason(relation, now);
}
