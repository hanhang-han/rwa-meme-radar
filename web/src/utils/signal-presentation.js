import { RISK_LABELS, riskFlags } from './product-labels.js';
import { quoteAt, quoteFreshness } from './quote-status.js';
import { riskDisplayCheck, riskProvenance } from './risk-presentation.js';
import { isQualifiedPool } from './stock-theme-model.js';

const address = value => /^0x[0-9a-f]{40}$/i.test(String(value ?? ''));
const numeric = value => value != null && value !== '' && ['number', 'string'].includes(typeof value) && Number.isFinite(Number(value));
export const signalText = (pair, language = 'zh') => pair[language === 'en' ? 1 : 0];

// A response timestamp is never a substitute for the field's own observation.
export function signalTime(value, now = Date.now()) {
  return numeric(value) && Number(value) > 0 && Number(value) <= now + 1000 ? Number(value) : null;
}

const relationCopy = {
  current: { label: ['同池已核实', 'Verified pool'], icon: '⇄', tone: 'verified', explanation: ['链上配对池已核实，当前池流动性达到收录标准。', 'The onchain pairing is verified and its current liquidity meets the inclusion threshold.'] },
  'name-only': { label: ['仅名称线索', 'Name only'], icon: '≈', tone: 'neutral', explanation: ['仅名称与股票主题相关，尚无已核实的配对池依据。', 'Only the name links to this stock theme; no verified pair-pool evidence is available.'] },
  community: { label: ['社区意见', 'Community'], icon: '◇', tone: 'neutral', explanation: ['社区投票达到记录门槛，仅代表社区意见。', 'Community votes meet the recording threshold; this represents community opinion.'] },
  'valuation-pending': { label: ['同池 · 估值待更新', 'Paired · update due'], icon: '⇄', tone: 'pending', explanation: ['配对池身份已核实，估值已过期或观测时间未知；当前暂不计入达标同池统计。', 'The pool identity is verified. Its valuation is outdated or its observation time is unknown, so it is excluded from current qualifying-pool totals.'] },
  'valuation-missing': { label: ['同池 · 暂无估值', 'Paired · no valuation'], icon: '⇄', tone: 'pending', explanation: ['配对池身份已核实，暂无可用池流动性估值；当前暂不计入达标同池统计。', 'The pool identity is verified, but no usable liquidity valuation is available. It is excluded from current qualifying-pool totals.'] },
  'issuer-unverified': { label: ['同池 · 身份待核实', 'Paired · identity pending'], icon: '⇄', tone: 'pending', explanation: ['链上配对池已核实，股票侧代币的发行方身份尚未核实；当前暂不计入达标同池统计。', 'The onchain pairing is verified, but the stock-side token issuer identity is unverified. It is excluded from current qualifying-pool totals.'] },
  recorded: { label: ['同池 · 暂未达标', 'Paired · criteria unmet'], icon: '⇄', tone: 'pending', explanation: ['配对池身份已核实，暂未满足当前收录条件；配对依据仍保留。', 'The pool identity is verified but does not meet the current inclusion criteria. The pairing evidence is retained.'] },
  unknown: { label: ['关系待核实', 'Unverified'], icon: '◇', tone: 'neutral', explanation: ['暂无足够依据确认配对关系。', 'There is not enough evidence to confirm the pairing.'] },
};

export function relationSignal(relation, now = Date.now()) {
  const nameOnly = relation?.level === 'B' || relation?.evidenceStatus === 'name-only';
  const recorded = !nameOnly && relation?.status === 'verified' && address(relation?.pool) && address(relation?.stock);
  const at = signalTime(relation?.liquidityAt, now);
  let state = 'unknown';
  if (recorded) {
    const identityUnverified = [relation.sideIdentity, relation.stockIdentity].some(identity => identity && (identity.eligibleForPair === false || ['unverified', 'legacy'].includes(identity.verificationStatus)));
    if (relation.evidenceStatus === 'issuer-deployment-unverified' || identityUnverified) state = 'issuer-unverified';
    else if (relation.evidenceStatus === 'liquidity-unknown' || !numeric(relation.liquidityUsd) || Number(relation.liquidityUsd) < 0) state = 'valuation-missing';
    else if (!at || now - at > 900_000 || relation.liquidityStatus === 'stale' || relation.evidenceStatus === 'liquidity-stale') state = 'valuation-pending';
    else if (isQualifiedPool(relation, now) && (!relation.evidenceStatus || relation.evidenceStatus === 'qualified')) state = 'current';
    else state = 'recorded';
  } else if (nameOnly) state = 'name-only';
  else if (relation?.level === 'C') state = 'community';
  return { state, recorded, current: state === 'current', ...relationCopy[state] };
}

const quoteCopy = {
  missing: ['暂无报价', 'No quote'], 'no-verified-market': ['暂无市场报价', 'No market quote'],
  stale: ['历史报价', 'Historical quote'], delayed: ['延迟参考', 'Delayed reference'],
  live: ['实时行情', 'Live quote'], scheduled: ['最新观测', 'Latest observed'],
  market_closed: ['市场休市', 'Market closed'], unsupported: ['暂不支持', 'Unsupported'],
  'unknown-time': ['时间待核实', 'Time unverified'], 'issuer-reference': ['发行方参考', 'Issuer reference'],
  'entitlement-required': ['暂无独立股票行情', 'No independent stock quote'],
  'identity-unverified': ['对应股票未确认', 'Underlying stock unconfirmed'],
  'quota-exhausted': ['行情暂不可用', 'Quote unavailable'], 'budget-exhausted': ['行情暂不可用', 'Quote unavailable'],
};
const quoteReasons = {
  live: ['来源明确标注实时属性，且报价观测时间仍在有效窗口内。', 'The source explicitly identifies realtime data and the quote observation is within its freshness window.'],
  scheduled: ['这是来源的最新可用观测，尚不能证明为实时行情。', 'This is the latest available source observation; realtime delivery has not been established.'],
  stale: ['报价观测已超出该来源的有效时间窗口，仅保留作历史参考。', 'The quote observation is beyond this source’s freshness window and is retained as a historical reference.'],
  delayed: ['此参考报价有延迟，不能用于判断即时价格。', 'This reference quote is delayed and does not establish the immediate price.'],
  'unknown-time': ['无法确认报价观测时间，不能判断其时效。', 'The quote observation time cannot be verified, so its freshness is unknown.'],
  'issuer-reference': ['此值是发行方参考价，未证明为独立市场成交价。', 'This is an issuer reference value; an independent market execution price has not been established.'],
  market_closed: ['来源标记市场休市；下方时间为已有报价的观测时间。', 'The source marks the market as closed. The time below belongs to the available quote observation.'],
};

export function quoteSignal(row, reference = false, now = Date.now()) {
  const freshness = quoteFreshness(row, reference, now);
  const rawAt = quoteAt(row, reference);
  const at = signalTime(rawAt, now);
  const value = reference ? row?.stockPrice : row?.price;
  let state = freshness.state;
  // Keep the existing provider-specific windows, while rejecting malformed times.
  if (['live', 'scheduled', 'delayed', 'issuer-reference', 'stale', 'unknown-time'].includes(state)) {
    if (!numeric(value)) state = 'missing';
    else if (at == null) state = 'unknown-time';
  }
  return { ...freshness, state, at, rawAt,
    label: quoteCopy[state] ?? ['行情状态未知', 'Quote status unknown'],
    explanation: quoteReasons[state] ?? ['当前没有可用报价；来源和已知观测信息如下。', 'No usable quote is available; its source and known observation information are below.'],
    source: reference ? row?.referenceProvider ?? null : row?.fieldSources?.price ?? row?.provider ?? null,
    currency: reference ? row?.referenceCurrency ?? null : row?.priceCurrency ?? null,
    icon: state === 'live' ? '●' : state === 'stale' || state === 'delayed' || state === 'unknown-time' ? '◷' : '·',
    tone: state === 'live' ? 'verified' : ['stale', 'delayed', 'unknown-time'].includes(state) ? 'pending' : 'neutral',
  };
}

export const SIGNAL_SAFETY_LABELS = {
  tax: ['买卖税 / 交易限制', 'Taxes / trading limits'], permissions: ['合约权限', 'Contract permissions'],
  concentration: ['持仓集中度', 'Holder concentration'], liquidityLock: ['流动性锁定', 'Liquidity lock'],
  creatorHolding: ['创建者持仓', 'Creator holdings'],
};
const safetyShort = { tax: ['交易限制', 'Trading limits'], permissions: ['权限风险', 'Permissions'], concentration: ['持仓集中', 'Concentrated'], liquidityLock: ['流动性未锁', 'Liquidity unlocked'], creatorHolding: ['创建者持仓', 'Creator holdings'] };
const flagShort = { wash_suspect: ['成交异常', 'Unusual trading'], thin_spike: ['低深度暴涨', 'Thin & spiking'], contract_risk: ['合约风险', 'Contract risk'], concentrated: ['持仓集中', 'Concentrated'], holder_anomaly: ['地址异常', 'Holder anomaly'], liquidity_unlock: ['未锁定', 'Unlocked'] };
const safetyFlagKeys = { contract_risk: 'permissions', concentrated: 'concentration', liquidity_unlock: 'liquidityLock' };

export function signalRiskCheck(check, now = Date.now()) {
  const source = riskProvenance(check);
  const result = riskDisplayCheck(check, now);
  const at = signalTime(source.at, now);
  const current = at != null && at <= now && now - at <= 21_600_000;
  const reason = !check ? 'missing-evidence' : result.status === 'unknown' && at == null ? 'missing-evidence' : result.reason;
  return { ...result, ...source, at, reason, current, historical: result.status === 'triggered' && !current };
}

export function riskSignal(asset, now = Date.now()) {
  const safety = asset?.riskAssessment?.safety ?? {};
  const checks = asset?.riskAssessment?.checks ?? {};
  const rows = Object.entries(SIGNAL_SAFETY_LABELS).map(([key, label]) => ({ key, label, ...signalRiskCheck(safety[key], now) }));
  const keys = new Set(riskFlags(asset));
  for (const [key, check] of Object.entries(checks)) if (RISK_LABELS[key] && check?.status === 'triggered') keys.add(key);
  const flags = [...keys].map(key => {
    const check = signalRiskCheck(checks[key], now);
    // Legacy flags stay visible even if their underlying check is unavailable.
    return { key, label: RISK_LABELS[key], shortLabel: flagShort[key], ...check, status: 'triggered', historical: !check.current || checks[key]?.status !== 'triggered', evidenceMissing: !checks[key] };
  });
  const safetyFlags = rows.filter(row => row.status === 'triggered').map(row => ({ ...row, key: 'safety:' + row.key, shortLabel: safetyShort[row.key] }));
  const priority = (a, b) => Number(b.severity === 'critical') - Number(a.severity === 'critical') || Number(a.historical) - Number(b.historical);
  const categories = new Map();
  for (const item of [...safetyFlags, ...flags].sort(priority)) {
    const category = item.key.startsWith('safety:') ? item.key.slice(7) : safetyFlagKeys[item.key] ?? item.key;
    if (!categories.has(category)) categories.set(category, item);
  }
  const alerts = [...categories.values()];
  const completed = rows.filter(row => row.current && ['clear', 'triggered'].includes(row.status)).length;
  const hasRecords = Object.keys(safety).length > 0;
  const coverage = completed === rows.length ? 'complete' : completed > 0 ? 'partial' : 'not-tested';
  const state = alerts.length ? 'flagged' : coverage === 'complete' ? 'clear' : coverage;
  const label = alerts.length ? alerts[0].shortLabel : state === 'clear' ? ['未触发', 'No flags']
    : state === 'partial' ? ['部分检测', 'Partial checks'] : hasRecords ? ['检测待更新', 'Check update due'] : ['未检测', 'Not checked'];
  return { state, label, rows, flags, alerts, completed, total: rows.length, coverage, hasRecords,
    critical: alerts.some(item => item.severity === 'critical'),
    historical: alerts.length > 0 && alerts.every(item => item.historical),
    icon: alerts.length ? '▲' : state === 'clear' ? '✓' : '○',
    tone: alerts.some(item => item.severity === 'critical') ? 'critical' : alerts.length ? 'warning' : 'neutral',
  };
}

// A native popover uses the browser's top layer and escapes table overflow.
// Unsupported browsers retain the regular details disclosure as a fallback.
export function syncSignalDisclosure(details) {
  const panel = details?.querySelector?.(':scope > .signal-panel');
  if (!panel || typeof panel.showPopover !== 'function') return;
  if (!details.open) {
    if (panel.matches(':popover-open')) panel.hidePopover();
    return;
  }
  for (const key of ['top', 'left', 'right', 'bottom', 'width']) panel.style.removeProperty(key);
  if (!panel.matches(':popover-open')) panel.showPopover();
  if (window.innerWidth <= 600) return;
  const anchor = details.getBoundingClientRect();
  const width = Math.min(320, window.innerWidth - 24);
  panel.style.width = width + 'px';
  panel.style.right = 'auto';
  panel.style.bottom = 'auto';
  panel.style.left = Math.max(12, Math.min(anchor.right - width, window.innerWidth - width - 12)) + 'px';
  const height = panel.getBoundingClientRect().height;
  const below = anchor.bottom + 6;
  panel.style.top = Math.max(12, Math.min(below + height <= window.innerHeight - 12 ? below : anchor.top - height - 6, window.innerHeight - height - 12)) + 'px';
}
