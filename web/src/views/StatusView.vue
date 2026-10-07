<template>
  <div class="status-page">
    <div class="page-heading status-heading">
      <div><h2>{{ tr('数据状态', 'Data status') }}</h2><p>{{ tr('最近检查', 'Last checked') }} {{ date(checkedAt) }}</p></div>
      <button type="button" @click="load">{{ tr('重新检查', 'Check again') }}</button>
    </div>
    <p v-if="error" role="alert" class="panel">{{ tr('状态读取失败。', 'Could not load status.') }} <button @click="load">{{ tr('重试', 'Retry') }}</button></p>
    <div class="status-summary">
      <div class="panel"><span>{{ tr('整体数据状态', 'Overall data status') }}</span><strong :class="healthTone">{{ overallLabel }}</strong><small>{{ tr('服务运行与数据覆盖', 'Service health and data coverage') }}</small></div>
      <div class="panel"><span>{{ tr('行情采集', 'Data collection') }}</span><strong :class="processTone(health?.collector)">{{ processLabel(health?.collector) }}</strong><small v-if="health" :class="collectorDegraded ? 'down' : ''">{{ collectorDegraded ? tr('部分采集异常', 'Collection errors reported') : tr('未报告采集异常', 'No collection errors reported') }}</small><small>{{ tr('上次资产更新', 'Last asset update') }} {{ age(health?.latestAssetAt) }}</small></div>
      <div class="panel"><span>{{ tr('数据统计', 'Data processing') }}</span><strong :class="processTone(health?.projection)">{{ processLabel(health?.projection) }}</strong><small v-if="health" :class="projectionDegraded ? 'down' : ''">{{ projectionDegraded ? tr('部分统计异常', 'Processing errors reported') : tr('未报告统计异常', 'No processing errors reported') }}</small></div>
      <div class="panel"><span>{{ tr('磁盘剩余', 'Disk free') }}</span><strong :class="diskTone" :title="tr('磁盘可用容量占总容量的百分比','Available disk capacity as a percentage of total capacity')">{{ health?.disk?.freePercent == null ? '—' : `${health.disk.freePercent}%` }}</strong><div class="status-mini-track" aria-hidden="true"><span :style="{width:diskWidth}"></span></div><small>{{ bytes(health?.disk?.freeBytes) }} {{ tr('可用', 'available') }}</small><small>{{ diskTrend }}</small></div>
    </div>

    <section v-if="health?.issues?.length || health?.warnings?.length" class="panel status-alerts">
      <h3>{{ tr('需要关注', 'Needs attention') }}</h3>
      <p v-if="health?.issues?.length" class="down">{{ tr('故障或数据未达标', 'Failures or data below target') }}：{{ health.issues.map(issueLabel).join(' · ') }}</p>
      <p v-if="health?.warnings?.length" class="warn">{{ tr('数据限制', 'Data limitations') }}：{{ health.warnings.map(issueLabel).join(' · ') }}</p>
    </section>

    <section class="panel">
      <h3>{{ tr('数据源', 'Sources') }}</h3>
      <ul v-if="visibleSources.length" class="status-source-list">
        <li v-for="source in visibleSources" :key="source.id ?? source.provider">
          <strong>{{ source.provider }}</strong>
          <span class="status-source-use">{{ sourceUse(source) }}</span>
          <span :class="['status-source-state',sourceTone(source)]">{{ sourceStatus(source.status) }}</span>
          <time :title="date(source.updatedAt)">{{ age(source.updatedAt) }}</time>
        </li>
      </ul>
      <p v-if="!visibleSources.length" class="x-empty">{{ tr('尚无来源状态', 'Source status unavailable') }}</p>
      <p v-if="okxSource?.budget" class="hint">OKX {{ tr('当日额度', 'daily budget') }}：{{ num(okxSource.budget.daily) }} / {{ num(okxSource.budget.dailyLimit) }}；{{ tr('剩余', 'Remaining') }} {{ num(okxSource.budget.remaining) }}</p>
      <p v-if="okxSource?.budget?.lanes?.length" class="hint">{{ okxSource.budget.lanes.map(lane => `${lane.name} ${num(lane.used)}/${num(lane.limit)}`).join(' · ') }}</p>
      <p class="hint">{{ tr('这里显示数据源的检查时间；行情更新时间见各项数据。', 'These are source check times. Market metrics show their own update times.') }}</p>
    </section>

    <section class="panel">
      <h3>{{ tr('数据覆盖', 'Data coverage') }}</h3>
      <ul class="status-coverage-list">
        <li v-for="row in coverageRows" :key="row.key">
          <div class="status-coverage-top"><strong>{{ row.label }}</strong><span :title="tr('目标时效内的观测数量 / 该指标全部目标数量；缺失不表示零','Observations within the freshness target / all target observations for this metric; missing does not mean zero')" :class="row.total && row.fresh / row.total < .9 ? 'down' : 'up'">{{ percent(row.fresh, row.total) }}</span></div>
          <div class="status-coverage-track" aria-hidden="true"><span :class="row.total && row.fresh / row.total < .9 ? 'is-low' : ''" :style="{width:coverageWidth(row.fresh,row.total)}"></span></div>
          <small>{{ row.window }} · {{ tr('有值', 'Available') }} {{ num(row.known) }} · {{ tr('目标内', 'Within target') }} {{ num(row.fresh) }} / {{ num(row.total) }}</small>
        </li>
      </ul>
      <details class="status-coverage-notes">
        <summary>{{ tr('查看覆盖口径与缺失项', 'Coverage details and gaps') }}</summary>
        <p v-for="row in unavailableQuoteRows" :key="`unavailable-${row.key}`" class="hint">{{ row.label }}：{{ tr('已发现', 'Discovered') }} {{ num(row.total) }} · {{ tr('可报价', 'Quotable') }} {{ num(row.quotableTotal) }} · {{ tr('暂无可验证报价', 'No verifiable quote') }} {{ num(row.quoteUnavailable) }} · {{ tr('可报价部分达标', 'Quotable within target') }} {{ percent(row.quotableWithinTarget, row.quotableTotal) }}</p>
        <p v-if="unavailableQuoteRows.length" class="hint">{{ tr('达标率包含全部已发现资产；有报价资产的覆盖率另列。', 'Coverage includes all discovered assets. Coverage among quotable assets is listed separately.') }}</p>
        <p v-if="liquidityCoverage.total" class="hint">{{ tr('总流动性数据', 'Total liquidity data') }}：{{ tr('当前', 'Current') }} {{ num(liquidityCoverage.fresh) }} · {{ tr('过期', 'Stale') }} {{ num(liquidityCoverage.stale) }} · {{ tr('证据待核实', 'Evidence unverified') }} {{ num(liquidityCoverage.unverified) }} · {{ tr('时间不明', 'Unknown time') }} {{ num(liquidityCoverage.unknownTime) }} · {{ tr('无数值', 'No value') }} {{ num(liquidityCoverage.missing) }}</p>
        <p class="hint">{{ tr('时效以来源的行情时间为准；未提供时标记报价接收时间，不等同于成交时间。', 'Freshness uses the source timestamp, or the quote receipt time when unavailable. Receipt time is not trade time.') }}</p>
      </details>
    </section>

    <section class="panel discovery-coverage" aria-labelledby="discovery-coverage-title">
      <div class="panel-head"><h2 id="discovery-coverage-title">{{ tr('新池发现对账', 'New pool discovery check') }}</h2></div>
      <div class="discovery-coverage-rows">
        <div v-for="row in discoveryRows" :key="row.chainId" class="discovery-coverage-row">
          <strong>{{ chainName(row.chainId) }}</strong>
          <span v-if="row.status === 'complete' && Number(row.eligible) > 0" class="mono" :title="tr('已覆盖的可比池数量 / DexScreener索引的白名单可比池总数','Covered comparable pools / allowlisted comparable pools indexed by DexScreener')" :class="Number(row.missing) > 0 ? 'warn' : 'up'">{{ percent(row.covered, row.eligible) }} <small>{{ num(row.covered) }} / {{ num(row.eligible) }}</small></span>
          <span v-else-if="row.status === 'complete'" class="muted">{{ tr('无可比较池', 'No comparable pools') }}</span>
          <span v-else class="muted" :title="discoveryStatusReason(row.status)">{{ tr('未验证', 'Unverified') }}<small v-if="row.status === 'partial' && Number(row.missing) > 0" class="discovery-known-missing"> · {{ tr('已发现缺池 ' + num(row.missing), num(row.missing) + ' indexed pools missing') }}</small></span>
          <small :title="date(row.checkedAt)">{{ row.checkedAt ? age(row.checkedAt) : tr('暂无检查记录', 'No check recorded') }}</small>
        </div>
      </div>
      <p class="hint">{{ tr('仅对照 DexScreener 已索引的白名单交易池，不代表全链池子覆盖。', 'This checks allowlisted pools indexed by DexScreener; it does not measure full-chain coverage.') }}</p>
    </section>

    <details v-if="chainStreams.length" :open="health?.issues?.includes('chain-stream-degraded') ? true : undefined" class="panel status-tasks">
      <summary>{{ tr('链上成交与 K 线覆盖', 'On-chain trades and candle coverage') }}</summary>
      <p class="hint">{{ tr('实时成交和历史记录分别统计。存在历史缺口时，K 线只覆盖已收录区间。', 'Live and historical coverage are listed separately. Candles cover recorded intervals only.') }}</p>
      <div class="scroll"><table class="tbl status-table"><thead><tr><th>{{ tr('链', 'Chain') }}</th><th>{{ tr('实时流', 'Live stream') }}</th><th :title="tr('最近链头与已处理区块的距离，单位为区块','Distance from the recent chain head to processed blocks, in blocks')">{{ tr('近端落后（区块）', 'Recent gap (blocks)') }}</th><th :title="tr('尚未覆盖的历史区块数量','Historical blocks not yet covered')">{{ tr('历史缺口（区块）', 'Historical gap (blocks)') }}</th><th :title="tr('等待处理的项目数 / 队列容量','Items waiting to be processed / queue capacity')">{{ tr('待处理（项）', 'Queued (items)') }}</th></tr></thead>
        <tbody><tr v-for="row in chainStreams" :key="row.chainId"><td>{{ chainName(row.chainId) }}</td><td :class="row.degraded ? 'down' : row.status === 'live' ? 'up' : 'warn'">{{ sourceStatus(row.status) }}<span v-if="row.degraded"> · {{ tr('延迟', 'Delayed') }}</span> <small>{{ lag(row.sourceLagMs) }}<span v-if="row.updatedAt"> · {{ tr('状态采样', 'Status sampled') }} {{ age(row.updatedAt) }}</span></small></td><td :class="row.degraded ? 'down' : ''">{{ blocks(row.nearTipLagBlocks) }}</td><td :class="row.historicalGapBlocks > 0 ? 'warn' : ''">{{ blocks(row.historicalGapBlocks ?? (row.lastHead != null && row.lastProcessedBlock != null ? row.lastHead - row.lastProcessedBlock : null)) }}<small v-if="row.historicalScan?.lastSuccessAt">{{ tr('上次历史进展', 'Last history progress') }} {{ age(row.historicalScan.lastSuccessAt) }}</small><small v-if="row.historicalScan?.lastErrorAt > (row.historicalScan?.lastSuccessAt ?? 0)" class="warn">{{ tr('最近扫描失败', 'Recent scan failed') }}</small></td><td :class="row.degraded ? 'down' : row.queueDepth >= 1024 ? 'warn' : ''">{{ num(row.queueDepth) }} <small>/ 4,096</small></td></tr></tbody>
      </table></div>
    </details>

    <section class="panel">
      <h3>{{ tr('股票标的参考价', 'Underlying stock reference prices') }}</h3>
      <p><strong :class="reference?.status === 'available' ? 'up' : 'warn'">{{ capabilityStatus(reference?.status) }}</strong> · {{ tr('独立交易所参考价', 'Independent exchange references') }} {{ num(reference?.independent) }} / {{ num(reference?.total) }}（{{ percent(reference?.independent, reference?.total) }}）</p>
      <p class="hint">{{ tr('发行方参考价', 'Issuer references') }} {{ num(reference?.issuer) }}；{{ tr('有任意参考价', 'Any reference') }} {{ num(reference?.known) }}。{{ tr('缺少独立参考价时，真实股票与代币的价差不可计算。', 'Without an independent reference, the underlying stock-to-token spread cannot be calculated.') }}</p>
      <p v-if="reference?.reason === 'entitlement-required'" class="warn">{{ tr('EODHD 盘中报价授权不足，部分股票参考价不可用。', 'EODHD intraday access is limited. Some stock reference prices are unavailable.') }}</p>
    </section>

    <details class="panel status-tasks"><summary>{{ tr('查看采集与统计任务', 'View collection and projection jobs') }}</summary>
      <div class="scroll"><table class="tbl status-table"><thead><tr><th>{{ tr('任务', 'Job') }}</th><th>{{ tr('进程', 'Process') }}</th><th>{{ tr('结果', 'Result') }}</th><th>{{ tr('上次成功', 'Last success') }}</th></tr></thead>
        <tbody><tr v-for="task in taskRows" :key="task.owner + task.name"><td>{{ task.name }}</td><td>{{ task.owner === 'collector' ? tr('采集', 'Collection') : tr('统计', 'Projection') }}</td><td :class="taskTone(task.status)">{{ taskStatus(task) }}</td><td :title="date(task.lastSuccessAt)">{{ age(task.lastSuccessAt) }}</td></tr></tbody>
      </table></div>
    </details>

    <section class="panel">
      <h3>{{ tr('指标定义', 'Metric definitions') }}</h3>
      <dl class="status-definitions">
        <div><dt>{{ tr('符合条件的 Meme', 'Qualified memes') }}</dt><dd>{{ tr('总流动性至少 $1,000、流动性来源观测不超过 30 分钟、价格观测不超过 15 分钟的候选币。来源未覆盖或观测过期的币不计入；计数为 0 也不表示网站没有成交。', 'Candidates with at least $1,000 in total liquidity, a liquidity observation within 30 minutes and a price observation within 15 minutes. Missing or expired evidence excludes a token; a zero count does not mean no trades occurred.') }}</dd></div>
        <div><dt>{{ tr('股票配对池', 'Stock pairs') }}</dt><dd>{{ tr('仅统计股票代币身份已确认、关系为 A 级、池流动性至少 $1,000 且池估值观测不超过 15 分钟的配对池；按链与池地址去重。', 'Only grade-A pairs with confirmed stock-token identity, at least $1,000 in pool liquidity and a pool valuation observed within 15 minutes; deduplicated by chain and pool address.') }}</dd></div>
        <div><dt>{{ tr('配对池流动性', 'Pair liquidity') }}</dt><dd>{{ tr('以上符合条件的配对池的双边流动性之和，区别于代币在全部池中的总流动性。', 'The sum of two-sided liquidity in the qualifying pairs above, separate from a token’s liquidity across all pools.') }}</dd></div>
        <div><dt>{{ tr('可排名资产', 'Rankable assets') }}</dt><dd>{{ tr('价格、成交、配对证据和池估值均达到排名时效要求的资产。', 'Assets whose price, volume, pair evidence and pool valuation meet ranking freshness rules.') }} {{ num(rankable) }} / {{ num(qualitySummary.total) }}</dd></div>
        <div><dt>{{ tr('缺失与过期', 'Missing and stale') }}</dt><dd>{{ tr('“—”表示无可用值；灰色历史值附上实际更新时间。真实的零显示为 0。', 'A dash means no available value. Historical values show their actual update time. A real zero is shown as zero.') }}</dd></div>
      </dl>
    </section>

    <section class="panel"><h3>{{ tr('数据版本', 'Data version') }}</h3><p class="hint">{{ tr('公开存证未启用。', 'Public anchoring is not enabled.') }} {{ tr('快照时间', 'Snapshot time') }} {{ date(snapshotAt) }} · Revision {{ store.revision || '—' }} · {{ tr('白名单版本', 'Identity list') }} {{ identityVersion }}</p></section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { getDataHealth } from '../api/client';
import { useMinuteClock } from '../composables/useMinuteClock';
import { useDashboardStore } from '../stores/dashboard';
import { tr } from '../i18n';
import { age, date, num } from '../utils/format';

const store = useDashboardStore();
const minuteNow = useMinuteClock();
const health = ref(null);
const error = ref(false);
const checkedAt = ref(null);
const qualitySummary = computed(() => store.snapshot?.unified?.quality?.summary ?? {});
const rankable = computed(() => qualitySummary.value.eligible?.relationRanking);
const snapshotAt = computed(() => store.snapshot?.unified?.snapshotAt ?? store.snapshot?.at ?? store.updatedAt);
const identityVersion = computed(() => store.snapshot?.unified?.identityCatalogVersion ?? store.snapshot?.unified?.identityCatalog?.version ?? tr('未发布', 'Not published'));
const visibleSources = computed(() => health.value?.sources ?? store.sources);
const okxSource = computed(() => visibleSources.value.find(source => source.id === 'okx:dex'));
const chainStreams = computed(() => [...(health.value?.chainStreams ?? [])].sort((a, b) => ['196','56','4663'].indexOf(String(a.chainId)) - ['196','56','4663'].indexOf(String(b.chainId))));
const discoveryRows = computed(() => {
  const byChain = new Map((health.value?.discoveryCoverage?.chains ?? []).map(row => [String(row.chainId), row]));
  return ['56','196','4663'].map(chainId => byChain.get(chainId) ?? {chainId,status:'unverified'});
});
const reference = computed(() => health.value?.capabilities?.stockReferences ?? health.value?.references);
const collectorDegraded = computed(() => health.value?.issues?.includes('collector-degraded'));
const projectionDegraded = computed(() => health.value?.issues?.includes('projection-degraded'));
const healthTone = computed(() => health.value?.status === 'healthy' ? 'up' : health.value?.status === 'limited' ? 'warn' : health.value?.status === 'degraded' ? 'down' : '');
const diskTone = computed(() => health.value?.disk?.freePercent == null ? '' : health.value.disk.freePercent < 20 ? 'down' : health.value.disk.freePercent < 25 ? 'warn' : 'up');
const diskWidth = computed(() => boundedWidth(health.value?.disk?.freePercent, 100));
const overallLabel = computed(() => ({healthy:tr('正常', 'Healthy'),limited:tr('部分能力受限', 'Some capabilities limited'),degraded:tr('需要处理', 'Needs attention')})[health.value?.status] ?? tr('检查中', 'Checking'));
function processTone(process) { return process?.ok === true ? 'up' : process?.ok === false ? 'down' : ''; }
function processLabel(process) { return process?.ok === true ? tr('运行中', 'Running') : process?.ok === false ? tr('需要检查', 'Check needed') : tr('检查中', 'Checking'); }
const liquidityCoverage = computed(() => {
  const rows = (store.snapshot?.unified?.assets ?? []).filter(row => row.kind === 'candidate');
  const now = minuteNow.value;
  const known = rows.filter(row => row.totalLiquidityUsd != null && Number.isFinite(Number(row.totalLiquidityUsd)));
  const unknownTime = known.filter(row => !Number.isFinite(Number(row.totalLiquidityAt)) || Number(row.totalLiquidityAt)<=0).length;
  const fresh = known.filter(row => {
    const at = Number(row.totalLiquidityAt);
    return row.totalLiquidityStatus === 'current' && Number.isFinite(at) && at > 0 && at <= now + 1000 && now - at <= 1_800_000;
  }).length;
  const stale = known.filter(row => {
    const at=Number(row.totalLiquidityAt);
    return Number.isFinite(at) && at>0 && (row.totalLiquidityStatus==='stale' || (row.totalLiquidityStatus==='current' && now-at>1_800_000));
  }).length;
  return {total:rows.length,known:known.length,fresh,stale,unverified:known.length-fresh-stale-unknownTime,unknownTime,missing:rows.length-known.length};
});
const coverageRows = computed(() => {
  const c = health.value?.coverage ?? {};
  const q = store.snapshot?.unified?.quality?.assets ?? {};
  return [
    {key:'candidates',label:tr('候选币价格', 'Candidate prices'),window:tr('30 分钟', '30 minutes'),known:c.candidates?.known,fresh:c.candidates?.withinTarget,total:c.candidates?.total},
    {key:'stocks',label:tr('股票代币价格', 'Stock-token prices'),window:tr('60 分钟', '60 minutes'),known:c.stocks?.known,fresh:c.stocks?.withinTarget,total:c.stocks?.total},
    ...[['volume24h',tr('Meme 24h 成交', 'Meme 24h volume')],['observedVolume5m',tr('已观测 5m 成交', 'Observed 5m volume')],['holders',tr('持币地址', 'Holder addresses')]].map(([key,label]) => ({key,label,window:tr('快照口径', 'Snapshot measure'),known:q[key]?.known,fresh:q[key]?.fresh,total:q[key]?.total})),
  ];
});
const unavailableQuoteRows = computed(() => ['candidates','stocks'].map(key => ({
  key,
  label:key === 'candidates' ? tr('候选币', 'Candidate tokens') : tr('股票代币', 'Stock tokens'),
  ...(health.value?.coverage?.[key] ?? {}),
})).filter(row => row.quoteUnavailable != null && row.quotableTotal != null));
const taskRows = computed(() => ['collector','projection'].flatMap(owner => Object.entries(health.value?.[owner]?.tasks ?? {}).map(([name, task]) => ({owner,name,...task}))));
const diskTrend = computed(() => {
  const rows = health.value?.diskSamples ?? [];
  if (rows.length < 2) return tr('磁盘趋势采集中', 'Collecting disk trend');
  const first = rows[0], last = rows[rows.length - 1];
  const change = last.freeBytes - first.freeBytes;
  return `${tr('近', 'Last')} ${Math.max(1, Math.round((last.at - first.at) / 3600000))}h ${tr('空闲变化', 'free-space change')} ${change >= 0 ? '+' : '−'}${bytes(Math.abs(change))}`;
});
function percent(part, total) { return Number.isFinite(Number(part)) && Number(total) > 0 ? `${(Number(part) / Number(total) * 100).toFixed(1)}%` : '—'; }
function boundedWidth(part, total) { return Number.isFinite(Number(part)) && Number.isFinite(Number(total)) && Number(total) > 0 ? `${Math.max(0, Math.min(100, Number(part) / Number(total) * 100))}%` : '0%'; }
function coverageWidth(part, total) { return boundedWidth(part, total); }
function bytes(value) { return value != null && Number.isFinite(Number(value)) ? `${(Number(value) / 1024 ** 3).toFixed(2)} GB` : '—'; }
function blocks(value) { return value != null && Number.isFinite(Number(value)) ? num(Math.max(0, Number(value))) : '—'; }
function lag(value) { return value != null && Number.isFinite(Number(value)) ? `${Math.round(Number(value) / 1000)} ${tr('秒延迟', 's delay')}` : '—'; }
function chainName(id) { return ({'196':'X Layer','56':'BNB Chain','4663':'Robinhood Chain'})[String(id)] ?? String(id); }
function discoveryStatusReason(status) { return ({partial:tr('部分对照未完成', 'Comparison is incomplete'),unsupported:tr('当前链暂无可用对照', 'No comparable index for this chain'),stale:tr('对照已过期', 'Comparison has expired'),unverified:tr('尚无完整对照', 'No complete comparison yet')})[status] ?? tr('尚无完整对照', 'No complete comparison yet'); }
function sourceTone(source) { if (source.status === 'ready') return 'up'; if (!source.required || ['partial','reserved','critical-only','starting'].includes(source.status)) return 'warn'; return 'down'; }
function sourceUse(source) { return ({'dex-quotes':tr('关键 · 链上行情', 'Core · on-chain quotes'),'exchange-token-quotes':tr('关键 · 交易所代币行情', 'Core · exchange token quotes'),'issuer-quotes':tr('关键 · 发行方行情', 'Core · issuer quotes'),'stock-references':tr('可选 · 股票标的参考', 'Optional · underlying stock references'),'supplemental-quotes':tr('可选 · 补充行情', 'Optional · supplemental quotes')})[source.kind] ?? tr('补充来源', 'Supplemental source'); }
function sourceStatus(status) { return ({ready:tr('正常', 'Ready'),live:tr('在线', 'Live'),'catching-up':tr('回补中', 'Catching up'),reconnecting:tr('重新连接中', 'Reconnecting'),stale:tr('过期', 'Stale'),partial:tr('部分可用', 'Partial'),error:tr('异常', 'Error'),'entitlement-required':tr('授权不足', 'Entitlement required'),'quota-exhausted':tr('额度用尽', 'Quota exhausted'),'budget-exhausted':tr('日额度用尽', 'Daily budget exhausted'),'critical-only':tr('仅保留关键请求', 'Critical requests only'),reserved:tr('背景额度不足', 'Background budget low'),starting:tr('启动中', 'Starting'),unconfigured:tr('未配置', 'Not configured'),unknown:tr('状态未知', 'Unknown')})[status] ?? status ?? tr('状态未知', 'Unknown'); }
function capabilityStatus(status) { return ({available:tr('可用', 'Available'),partial:tr('仅部分覆盖', 'Partial coverage'),unavailable:tr('暂无独立参考价', 'No independent references'),'not-applicable':tr('暂无标的', 'No stock assets')})[status] ?? tr('待检查', 'Pending'); }
function taskTone(status) { return ['error','partial','quota-blocked'].includes(status) ? 'down' : status === 'waiting' || status === 'idle' ? 'up' : 'warn'; }
function taskStatus(task) { if (task.status === 'waiting' && task.outcome === 'no-change') return tr('检查完成，无新观测', 'Checked, no new observation'); if (task.status === 'waiting' && task.outcome === 'partial-data') return tr('已更新，部分数据缺失', 'Updated, some data unavailable'); if (task.status === 'deferred') return task.outcome === 'live-backlog' ? tr('链流积压，等待重试', 'Waiting for chain backlog') : tr('延后执行', 'Deferred'); if (task.status === 'error' && task.outcome === 'source-unavailable') return tr('链上数据流不可用', 'Chain stream unavailable'); return ({waiting:tr('正常', 'Ready'),idle:tr('暂无待处理', 'Nothing due'),partial:tr('部分失败', 'Partial failure'),error:tr('失败', 'Failed'),'quota-blocked':tr('额度阻断', 'Quota blocked'),running:tr('执行中', 'Running'),scheduled:tr('待启动', 'Scheduled'),stopped:tr('已停止', 'Stopped')})[task.status] ?? task.status; }
function issueLabel(code) {
  const unsupported = Object.values(health.value?.discovery ?? {}).reduce((total, chain) => total + Number(chain.scanQuality?.unsupportedPools ?? 0), 0);
  const labels = {
    'assets-unavailable': tr('资产数据不可用', 'Asset data unavailable'),
    'worker-unavailable': tr('采集进程不可用', 'Collection process unavailable'),
    'projection-unavailable': tr('统计进程不可用', 'Projection process unavailable'),
    'projection-snapshot-unavailable': tr('统计快照尚未同步，数据状态暂不可核验', 'Projection snapshot is out of sync; data health is temporarily unverified'),
    'health-storage-unavailable': tr('数据存储暂时不可读取，健康状态无法核验', 'Data storage cannot be read; health status is unverified'),
    'disk-low': tr('磁盘空间低于 20%', 'Disk free below 20%'),
    'collector-degraded': tr('采集任务异常', 'Collection job failure'),
    'projection-degraded': tr('统计任务异常', 'Projection job failure'),
    'chain-stream-degraded': tr('链上成交追赶延迟，K 线可能落后', 'On-chain trades are delayed; candles may lag'),
    'candidates-coverage-degraded': tr('候选币报价覆盖不足 90%', 'Candidate quote coverage below 90%'),
    'stocks-coverage-degraded': tr('股票代币报价覆盖不足 90%', 'Stock-token quote coverage below 90%'),
    'stock-references-coverage-limited': tr('独立股票参考价覆盖有限', 'Independent stock references are limited'),
    'candidate-no-verified-market': tr('部分候选币暂无可验证市场报价', 'Some candidate tokens have no verifiable market quote'),
    'stock-no-verified-market': tr('部分股票代币暂无可验证交易报价', 'Some stock tokens have no verifiable trading quote'),
    'collector-unsupported-observations': tr('部分行情缺失或不受支持', 'Some market observations are unavailable or unsupported'),
    'discovery-coverage-missing': tr('已索引新池仍有收录缺口', 'Indexed pools are missing from discovery'),
    'discovery-coverage-unverified': tr('新池发现对账未完成', 'New pool discovery check is unverified'),
    'discovery-unsupported-pools': `${tr('有不兼容的池接口', 'Pool interfaces unsupported')} (${num(unsupported)})`,
  };
  if (labels[code]) return labels[code];
  const source = visibleSources.value.find(row => code === `${row.provider}-${row.status}`);
  return source ? `${source.provider} ${sourceStatus(source.status)}` : code;
}
async function load() { error.value = false; try { health.value = await getDataHealth(); checkedAt.value = Date.now(); } catch { error.value = true; } }
onMounted(load);
</script>

<style scoped>
.status-page .up { color: var(--up); }
.status-page .warn { color: var(--warning); }
.status-page .down { color: var(--down); }
.status-page { display:grid; gap:0; max-width:1180px; margin:0 auto; }
.status-page .status-heading { margin-bottom:22px; }
.status-page .status-heading button { border-radius:100px; background:transparent; }
.status-page .status-summary { grid-template-columns:repeat(4,minmax(0,1fr)); gap:0; border-top:1px solid var(--border); border-bottom:1px solid var(--border); }
.status-page .status-summary .panel { min-width:0; margin:0; padding:19px 22px 20px; border:0; border-right:1px solid var(--border); border-radius:0; background:transparent; }
.status-page .status-summary .panel:first-child { padding-left:0; }
.status-page .status-summary .panel:last-child { border-right:0; }
.status-page .status-summary span { color:var(--muted); font-size:11px; }
.status-page .status-summary strong { margin:8px 0 3px; font-family:var(--display-font); font-size:27px; line-height:1.2; font-weight:400; }
.status-page .status-summary small { line-height:1.5; }
.status-mini-track,.status-coverage-track { height:5px; background:var(--border); overflow:hidden; }
.status-mini-track { margin:5px 0 3px; }
.status-mini-track span,.status-coverage-track span { display:block; height:100%; background:var(--up); }
.status-mini-track span { background:var(--accent); }
.status-page section.panel,.status-page details.panel { margin:0; padding:25px 0; border:0; border-bottom:1px solid var(--border); border-radius:0; background:transparent; overflow:visible; }
.status-page section.panel h3 { margin:0 0 18px; font-family:var(--display-font); font-size:21px; font-weight:400; }
.status-alerts p + p { margin-top:8px; }
.status-page .status-source-list,.status-page .status-coverage-list { margin:0; padding:0; list-style:none; }
.status-source-list li { display:grid; grid-template-columns:minmax(110px,1fr) minmax(170px,1.4fr) minmax(110px,.8fr) auto; align-items:center; gap:12px; padding:14px 0; border-top:1px solid var(--border); font-size:12px; }
.status-source-list strong { color:var(--text); font-size:13px; font-weight:600; }
.status-source-use,.status-source-list time { color:var(--muted); }
.status-source-list time { text-align:right; font-size:11px; white-space:nowrap; }
.status-source-state { display:inline-flex; align-items:center; gap:7px; }
.status-source-state::before { content:""; display:block; width:6px; height:6px; flex:none; border-radius:50%; background:currentColor; }
.status-coverage-list { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); column-gap:34px; }
.status-coverage-list li { display:grid; gap:7px; min-width:0; padding:14px 0 16px; border-top:1px solid var(--border); }
.status-coverage-top { display:flex; justify-content:space-between; gap:14px; align-items:baseline; }
.status-coverage-top strong { font-size:13px; font-weight:600; }
.status-coverage-top span { font-family:var(--display-font); font-size:21px; font-variant-numeric:tabular-nums; }
.status-coverage-track span.is-low { background:var(--warning); }
.status-coverage-list small { color:var(--muted); font-size:11px; line-height:1.5; }
.status-coverage-notes { margin-top:4px; padding-top:12px; border-top:1px solid var(--border); }
.status-coverage-notes summary { width:max-content; max-width:100%; color:var(--accent); font-size:12px; cursor:pointer; }
.status-coverage-notes[open] summary { margin-bottom:12px; }
.status-table td small { display:block; color:var(--muted); font-size:11px; }
.status-tasks summary { cursor:pointer; font-family:var(--display-font); font-size:19px; }
.status-tasks .scroll { margin-top:14px; }
.discovery-coverage-rows { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); border-top:1px solid var(--border); border-bottom:1px solid var(--border); }
.discovery-coverage-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:6px 10px; min-width:0; padding:17px 18px; border-right:1px solid var(--border); }
.discovery-coverage-row:first-child { padding-left:0; }
.discovery-coverage-row:last-child { border-right:0; }
.discovery-coverage-row>strong { font-size:13px; }
.discovery-coverage-row>span { font-family:var(--display-font); font-size:16px; text-align:right; }
.discovery-coverage-row small { color:var(--muted); font-family:inherit; font-size:11px; }
.discovery-coverage-row>small { grid-column:1/-1; }
.status-definitions > div { grid-template-columns:180px minmax(0,1fr); padding:12px 0; }
@media(max-width:1100px) { .status-page .status-summary { grid-template-columns:repeat(2,minmax(0,1fr)); } .status-page .status-summary .panel:nth-child(2) { border-right:0; } .status-page .status-summary .panel:nth-child(-n+2) { border-bottom:1px solid var(--border); } .status-page .status-summary .panel:nth-child(3) { padding-left:0; } }
@media(max-width:700px) { .status-page .status-summary,.status-coverage-list,.discovery-coverage-rows { grid-template-columns:1fr; } .status-page .status-summary .panel { padding:15px 0; border-right:0; border-bottom:1px solid var(--border); } .status-page .status-summary .panel:last-child { border-bottom:0; } .status-source-list li { grid-template-columns:minmax(0,1fr) auto; gap:5px 12px; } .status-source-use { grid-column:1; grid-row:2; } .status-source-state { grid-column:2; grid-row:1; justify-self:end; } .status-source-list time { grid-column:2; grid-row:2; } .discovery-coverage-row { padding:14px 0; border-right:0; border-bottom:1px solid var(--border); } .discovery-coverage-row:last-child { border-bottom:0; } .status-definitions > div { grid-template-columns:1fr; } }
</style>
