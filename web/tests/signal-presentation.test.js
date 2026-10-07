import test from 'node:test';
import assert from 'node:assert/strict';
import { relationSignal, quoteSignal, riskSignal, signalTime } from '../src/utils/signal-presentation.js';

const now = 1_800_000_000_000;
const pool = '0x' + '1'.repeat(40), stock = '0x' + '2'.repeat(40);
const paired = { status: 'verified', level: 'A', pool, stock, evidenceStatus: 'qualified', liquidityUsd: 2000, liquidityAt: now };
const clear = { status: 'clear', reason: 'scan-clear', checkedAt: now, provider: 'Source' };
const safety = Object.fromEntries(['tax', 'permissions', 'concentration', 'liquidityLock', 'creatorHolding'].map(key => [key, { ...clear }]));

test('null signals and invalid observation times never establish a verified or safe state', () => {
  assert.equal(relationSignal(null, now).state, 'unknown');
  assert.equal(quoteSignal(null, false, now).state, 'missing');
  assert.equal(riskSignal(null, now).state, 'not-tested');
  for (const value of [null, '', 'unknown', true, {}, -1, 0, Infinity, now + 2000]) assert.equal(signalTime(value, now), null);
  assert.equal(signalTime(String(now), now), now);
});

test('a qualifying verified pool becomes valuation pending when its own observation expires', () => {
  assert.equal(relationSignal(paired, now).state, 'current');
  const expired = relationSignal(paired, now + 900001);
  assert.equal(expired.state, 'valuation-pending');
  assert.equal(expired.recorded, true);
  assert.equal(expired.current, false);
  assert.match(expired.label[0], /估值待更新/);
  assert.doesNotMatch(expired.label[0], /消失|失效/);
  for (const liquidityAt of [undefined, 'unknown', now + 2000]) assert.equal(relationSignal({ ...paired, liquidityAt }, now).state, 'valuation-pending');
});

test('cached A labels alone and name-only clues never establish a verified pool', () => {
  assert.equal(relationSignal({ level: 'A' }, now).state, 'unknown');
  assert.equal(relationSignal({ ...paired, status: 'pending' }, now).state, 'unknown');
  assert.equal(relationSignal({ ...paired, stock: '0xshort' }, now).state, 'unknown');
  assert.equal(relationSignal({ ...paired, level: 'B' }, now).state, 'name-only');
  assert.equal(relationSignal({ ...paired, evidenceStatus: 'name-only' }, now).recorded, false);
});

test('issuer identity, missing valuation and the liquidity threshold remain independent facts', () => {
  assert.equal(relationSignal({ ...paired, stockIdentity: { verificationStatus: 'unverified' } }, now).state, 'issuer-unverified');
  assert.equal(relationSignal({ ...paired, evidenceStatus: 'issuer-deployment-unverified', liquidityAt: now - 1000000 }, now).state, 'issuer-unverified');
  assert.equal(relationSignal({ ...paired, liquidityUsd: null }, now).state, 'valuation-missing');
  assert.equal(relationSignal({ ...paired, liquidityUsd: 999 }, now).state, 'recorded');
  assert.equal(relationSignal({ ...paired, liquidityUsd: 1000 }, now).state, 'current');
});

test('the quote clock does not borrow liquidity time and unproven live declarations stay latest observed', () => {
  const row = { price: 1, quoteStatus: 'live', provider: 'OKX', priceCurrency: 'WETH', fieldTimes: { price: now }, totalLiquidityAt: now + 1000000 };
  const result = quoteSignal(row, false, now);
  assert.equal(result.state, 'scheduled');
  assert.equal(result.label[0], '最新观测');
  assert.equal(result.source, 'OKX');
  assert.equal(result.currency, 'WETH');
  assert.equal(quoteSignal({ ...row, fieldTimes: { price: now - 900001 } }, false, now).state, 'stale');
});

test('malformed, missing or future quote times cannot be described as live', () => {
  const row = { price: 1, provider: 'Binance', quoteStatus: 'live', priceProvenance: { timeKind: 'market' } };
  for (const priceAt of [null, 'bad-time', 0, -10, now + 2000]) {
    const result = quoteSignal({ ...row, fieldTimes: { price: priceAt } }, false, now);
    assert.equal(result.state, 'unknown-time');
    assert.equal(result.at, null);
  }
  assert.equal(quoteSignal({ ...row, fieldTimes: { price: now } }, false, now).state, 'live');
  assert.equal(quoteSignal({ ...row, price: '' }, false, now).state, 'missing');
});

test('reference quotes preserve their independent time, source and unit', () => {
  const row = { price: 1, priceCurrency: 'USDT', provider: 'Binance', fieldTimes: { price: now }, stockPrice: 2, referenceCurrency: 'USD', referenceProvider: 'EquitySource', referenceAt: now - 400000, referenceRealtime: false };
  assert.equal(quoteSignal(row, false, now).state, 'scheduled');
  const reference = quoteSignal(row, true, now);
  assert.equal(reference.state, 'stale');
  assert.equal(reference.at, row.referenceAt);
  assert.equal(reference.source, 'EquitySource');
  assert.equal(reference.currency, 'USD');
  assert.equal(quoteSignal({ ...row, referenceAt: now, referenceRealtime: true }, true, now).state, 'live');
  assert.equal(quoteSignal({ ...row, referenceAt: now }, true, now).state, 'delayed');
});

test('terminal quote states retain source and unit without claiming a market quote', () => {
  const result = quoteSignal({ quoteStatus: 'no-verified-market', provider: 'Issuer', priceCurrency: 'USD' }, false, now);
  assert.equal(result.state, 'no-verified-market');
  assert.equal(result.source, 'Issuer');
  assert.equal(result.currency, 'USD');
});

test('risk coverage distinguishes partial checks, unchecked and all current rules untriggered', () => {
  assert.equal(riskSignal({}, now).label[0], '未检测');
  assert.equal(riskSignal({ riskStatus: 'clear' }, now).state, 'not-tested');
  const partial = riskSignal({ riskAssessment: { safety: { tax: clear } } }, now);
  assert.equal(partial.state, 'partial');
  assert.equal(partial.completed, 1);
  assert.equal(partial.label[0], '部分检测');
  const complete = riskSignal({ riskAssessment: { safety } }, now);
  assert.equal(complete.state, 'clear');
  assert.equal(complete.label[0], '未触发');
  assert.equal(complete.completed, 5);
});

test('old clear checks need an update while old triggered checks remain historical flags', () => {
  const oldNow = now + 21600001;
  const result = riskSignal({ riskAssessment: { safety } }, oldNow);
  assert.equal(result.state, 'not-tested');
  assert.equal(result.label[0], '检测待更新');
  assert.equal(result.completed, 0);
  const flagged = riskSignal({ riskAssessment: { safety: { tax: { ...clear, status: 'triggered', severity: 'critical' } } } }, oldNow);
  assert.equal(flagged.state, 'flagged');
  assert.equal(flagged.historical, true);
  assert.equal(flagged.critical, true);
});

test('critical restrictions precede heuristic flags and duplicate risk categories are counted once', () => {
  const asset = { riskFlags: ['wash_suspect', 'contract_risk', 'contract_risk'], riskAssessment: { safety: { permissions: { ...clear, status: 'triggered', severity: 'critical' } }, checks: { wash_suspect: { ...clear, status: 'triggered' }, contract_risk: { ...clear, status: 'triggered' } } } };
  const result = riskSignal(asset, now);
  assert.equal(result.alerts.length, 2);
  assert.equal(result.alerts[0].key, 'safety:permissions');
  assert.equal(result.label[0], '权限风险');
});

test('legacy risk flags retain unknown evidence and never acquire fabricated wallet identities', () => {
  const result = riskSignal({ riskFlags: ['holder_anomaly', 'nonexistent'] }, now);
  assert.equal(result.alerts.length, 1);
  assert.equal(result.historical, true);
  assert.equal(result.alerts[0].evidenceMissing, true);
  assert.equal(result.alerts[0].provider, null);
  assert.equal(result.alerts[0].at, null);
  assert.equal(result.coverage, 'not-tested');
  assert.equal(result.label[0], '地址异常');
});

test('deduplication preserves the most severe evidence even when a safety check has lower severity', () => {
  const result = riskSignal({ riskFlags: ['contract_risk'], riskAssessment: { safety: { permissions: { ...clear, status: 'triggered', severity: 'warning' } }, checks: { contract_risk: { ...clear, status: 'triggered', severity: 'critical', evidence: { pausable: true } } } } }, now);
  assert.equal(result.alerts.length, 1);
  assert.equal(result.alerts[0].key, 'contract_risk');
  assert.equal(result.critical, true);
  assert.deepEqual(result.alerts[0].evidence, { pausable: true });
});

test('the evidence popover stays within the viewport and closes with its native details', async () => {
  const { syncSignalDisclosure } = await import('../src/utils/signal-presentation.js');
  const previousWindow = globalThis.window;
  const style = { removeProperty(key) { delete this[key]; } };
  let open = false;
  const panel = { style, showPopover() { open = true; }, hidePopover() { open = false; }, matches: () => open, getBoundingClientRect: () => ({ height: 160 }) };
  const details = { open: true, querySelector: () => panel, getBoundingClientRect: () => ({ right: 995, top: 450, bottom: 470 }) };
  try {
    globalThis.window = { innerWidth: 1000, innerHeight: 500 };
    syncSignalDisclosure(details);
    assert.equal(open, true);
    assert.equal(style.width, '320px');
    assert.equal(style.left, '668px');
    assert.equal(style.top, '284px');
    details.open = false;
    syncSignalDisclosure(details);
    assert.equal(open, false);
  } finally { globalThis.window = previousWindow; }
});

test('mobile popovers leave placement to the bounded bottom-sheet CSS and unsupported browsers keep details', async () => {
  const { syncSignalDisclosure } = await import('../src/utils/signal-presentation.js');
  const previousWindow = globalThis.window;
  const style = { top: 'old', width: 'old', removeProperty(key) { delete this[key]; } };
  let open = false;
  try {
    globalThis.window = { innerWidth: 390, innerHeight: 844 };
    syncSignalDisclosure({ open: true, querySelector: () => ({ style, matches: () => open, showPopover() { open = true; } }) });
    assert.equal(open, true);
    assert.equal(style.top, undefined);
    assert.equal(style.width, undefined);
    assert.doesNotThrow(() => syncSignalDisclosure({ open: true, querySelector: () => ({}) }));
    assert.doesNotThrow(() => syncSignalDisclosure(null));
  } finally { globalThis.window = previousWindow; }
});
