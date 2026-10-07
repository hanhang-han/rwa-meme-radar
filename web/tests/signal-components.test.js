import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import * as signal from '../src/utils/signal-presentation.js';
import * as risk from '../src/utils/risk-presentation.js';

const now = 1_800_000_000_000;
const pool = '0x' + '1'.repeat(40), stock = '0x' + '2'.repeat(40);

async function render(name, props, language = 'zh') {
  const descriptor = parse(readFileSync(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8')).descriptor;
  const source = compileScript(descriptor, { id: name, inlineTemplate: true }).content
    .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (_, bindings, path) => `const {${bindings.replace(/\s+as\s+/g, ':')}}=imports[${JSON.stringify(path)}];`)
    .replace('export default', 'return');
  const imports = { vue, '../i18n': { tr: (zh, en) => language === 'en' ? en : zh, useI18n: () => ({ lang: { lang: language } }) }, '../utils/format': { date: value => 'time:' + value, age: () => 'age', usd: value => '$' + value }, '../composables/useMinuteClock': { useMinuteClock: () => vue.ref(now) }, '../utils/signal-presentation': signal, '../utils/risk-presentation': risk };
  const component = new Function('imports', source)(imports);
  return renderToString(vue.createSSRApp(component, props));
}

const summary = html => html.match(/<summary\b[^>]*>([\s\S]*?)<\/summary>/)?.[1].replace(/<[^>]*>/g, '');

test('relation disclosure carries fact, independent clocks, sources and unmodified addresses', async () => {
  const html = await render('RelationBadge', { relation: { level: 'A', status: 'verified', pool, stock, ticker: 'TSLA', provider: 'chain-source', checkedAt: now, liquiditySource: 'valuation-source', liquidityUsd: 2000, liquidityAt: now - 900001 } });
  assert.match(summary(html), /同池 · 估值待更新/);
  assert.match(html, /chain-source/);
  assert.match(html, /valuation-source/);
  assert.match(html, new RegExp(pool));
  assert.match(html, new RegExp('time:' + (now - 900001)));
  assert.match(html, /不代表上市公司授权/);
  assert.match(html, /popover="manual"/);
  assert.match(html, /收起关系依据/);
});

test('existing null object props render safely and English labels remain readable', async () => {
  assert.match(summary(await render('RelationBadge', { relation: null })), /关系待核实/);
  assert.match(summary(await render('RelationBadge', { relation: { level: 'B' } }, 'en')), /Name only/);
  assert.match(summary(await render('QuoteStatus', { row: null }, 'en')), /No quote/);
  assert.match(summary(await render('RiskBadge', { asset: null }, 'en')), /Not checked/);
});

test('quote source remains available and can opt into the short summary while preserving unit', async () => {
  const props = { row: { price: 1, provider: 'OKX', priceCurrency: 'WETH', fieldTimes: { price: now } } };
  const defaultHtml = await render('QuoteStatus', props);
  assert.match(summary(defaultHtml), /最新观测/);
  assert.match(summary(defaultHtml), /WETH/);
  assert.doesNotMatch(summary(defaultHtml), /OKX/);
  assert.match(defaultHtml, /报价来源/);
  assert.match(defaultHtml, /OKX/);
  const visible = await render('QuoteStatus', { ...props, showSource: true });
  assert.match(summary(visible), /OKX/);
});

test('risks use meaningful short text with full evidence instead of an exclamation alone', async () => {
  const asset = { riskFlags: ['wash_suspect'], riskAssessment: { safety: { permissions: { status: 'triggered', checkedAt: now, provider: 'scanner', severity: 'critical', reason: 'permission-detected', evidence: { mintable: true, ownerAddress: pool } } } } };
  const html = await render('RiskBadge', { asset, compact: true });
  assert.match(summary(html), /权限风险/);
  assert.match(html, /可增发/);
  assert.match(html, new RegExp(pool));
  assert.match(html, /scanner/);
  assert.match(html, /历史提示/);
  assert.match(html, /检测覆盖有限/);
});

test('risk summary separates incomplete detection from retained historical risk evidence', async () => {
  const html = await render('RiskSummary', { compact: true, asset: { riskFlags: ['holder_anomaly'] } });
  assert.match(summary(html), /地址异常.*历史/);
  assert.match(html, /检测覆盖/);
  assert.match(html, /0\/5/);
  assert.match(html, /检测覆盖有限/);
  assert.match(html, /缺少可复核检测值/);
  assert.match(html, /未检测/);
  assert.doesNotMatch(html, /安全结论/);
});

test('one native legend covers relation, quote and detection semantics without question marks', async () => {
  const html = await render('SignalLegend');
  assert.match(summary(html), /信号说明/);
  assert.match(html, /估值待更新/);
  assert.match(html, /部分 \/ 未检测/);
  assert.match(html, /未知|不能当作安全/);
  assert.equal((html.match(/<details/g) ?? []).length, 1);
  assert.doesNotMatch(summary(html), /[?？]/);
});
