import { observedField, recordedPool } from './detail-presentation.js';
import { RISK_LABELS, riskFlags } from './product-labels.js';
import { riskDisplayCheck, riskProvenance } from './risk-presentation.js';
import { isQualifiedPool } from './stock-theme-model.js';
import { recordedPoolReason } from './stock-theme-presentation.js';

const numeric = value => typeof value === 'number' && Number.isFinite(value);
const observedAt = (value, now) => numeric(value) && value > 0 && value <= now;

// Historical observations remain useful when their own time and unit are known.
// Unknown/incompatible fields must not recover an unqualified raw aggregate.
export function detailMetricObservation(asset, name, now = Date.now()) {
  const current = observedField(asset, name, now);
  const declared = asset?.fieldAvailability?.[name];
  const at = current.at;
  const status = name === 'totalLiquidityUsd' ? asset?.totalLiquidityStatus ?? current.status : current.status;
  const historical = declared?.historicalValue ?? declared?.value ?? asset?.[name];
  const coverage = declared?.coverage ?? asset?.totalLiquidityCoverage;
  const zeroVerified = name !== 'totalLiquidityUsd' || historical !== 0 || coverage?.complete === true;
  const blocked = /coverage-incomplete|coverage-incompatible|scope-conflict|currency-conflict|unit-unverified|missing-valuation/.test(String(declared?.reason ?? ''));
  const expired = observedAt(at, now) && now - at > (name === 'holders' ? 86_400_000 : 1_800_000);
  const knownHistory = !blocked && zeroVerified && numeric(historical) && historical >= 0 && observedAt(at, now)
    && (numeric(declared?.historicalValue) || !!current.source)
    && (status === 'stale' || status === 'historical' || expired && (!declared || declared.status === 'current'));
  const value = !blocked && current.value != null ? current.value : knownHistory ? historical : null;
  return { ...current, value, status: value != null && current.value != null ? 'current' : knownHistory ? 'historical' : 'unknown',
    currency: name === 'totalLiquidityUsd' ? 'USD' : name === 'volume24h'
      ? (knownHistory ? declared?.historicalCurrency : null) || declared?.currency || asset?.volumeCurrency || null : null,
    scope: declared?.scope ?? asset?.fieldScopes?.[name] ?? (name === 'volume24h' ? asset?.volumeScope : name === 'totalLiquidityUsd' ? asset?.totalLiquidityCoverage?.scope : null) };
}

export function hasDetailExtraMetrics(metrics = {}, now = Date.now()) {
  const current = metric => metric?.status === 'current' && numeric(metric.value) && observedAt(metric.at, now) && now - metric.at <= 1_800_000;
  if ([metrics.fdvUsd, metrics.buyShare24h, ...Object.values(metrics.changes ?? {})].some(current)) return true;
  if (metrics.poolAgeHours?.status === 'current' && observedAt(metrics.poolAgeHours.at, now)) return true;
  return Object.values(metrics.exitImpacts ?? {}).concat(metrics.exitImpact1k ?? []).some(metric =>
    metric?.status === 'current' && numeric(metric.valuePercent) && observedAt(metric.at, now)
    && (!numeric(metric.validUntil) || metric.validUntil >= now) && now - metric.at <= 1_800_000);
}

const safetyLabels = {
  tax: ['买卖税 / 交易限制', 'Taxes / trading limits'], permissions: ['合约权限', 'Contract permissions'],
  concentration: ['持仓集中度', 'Holder concentration'], liquidityLock: ['流动性锁定', 'Liquidity lock'],
  creatorHolding: ['创建者持仓', 'Creator holdings'],
};

export function detailTriggeredRisks(asset = {}, now = Date.now()) {
  const checks = asset.riskAssessment?.checks ?? {};
  const rows = riskFlags(asset).map(key => {
    const check = checks[key];
    return { key, label: RISK_LABELS[key], ...riskDisplayCheck(check, now), ...riskProvenance(check) };
  });
  for (const [key, label] of Object.entries(safetyLabels)) {
    const check = riskDisplayCheck(asset.riskAssessment?.safety?.[key], now);
    if (check.status === 'triggered') rows.push({ key: 'safety:' + key, label, ...check, ...riskProvenance(check) });
  }
  return rows;
}

const relationLabels = {
  current: ['当前同池', 'Current same pool'], 'name-only': ['名称匹配 · 未确认同池', 'Name match · pool unconfirmed'],
  unverified: ['配对关系待核实', 'Pairing unverified'], 'issuer-unverified': ['股票侧身份待核实', 'Stock-side identity unverified'],
  'liquidity-unknown': ['缺少池流动性估值', 'Pool liquidity valuation unavailable'],
  'liquidity-time-unknown': ['池估值时间待核实', 'Pool valuation time unverified'],
  'liquidity-expired': ['池流动性观测已过期', 'Pool liquidity observation expired'],
  'liquidity-below-threshold': ['池流动性低于当前收录门槛', 'Pool liquidity below the current threshold'],
  'criteria-unmet': ['池身份已核实 · 暂未计入当前同池', 'Pool identity verified · excluded from current pairs'],
};

export function detailRelationState(relation = {}, now = Date.now()) {
  const qualified = recordedPool(relation) && isQualifiedPool(relation, now)
    && relation.liquidityStatus !== 'stale'
    && !['issuer-deployment-unverified','liquidity-unknown','liquidity-stale'].includes(relation.evidenceStatus);
  const key = qualified ? 'current'
    : recordedPool(relation) ? recordedPoolReason(relation, now)
    : relation.level === 'B' || relation.evidenceStatus === 'name-only' ? 'name-only' : 'unverified';
  return { key, label: relationLabels[key], recorded: recordedPool(relation), current: key === 'current' };
}
