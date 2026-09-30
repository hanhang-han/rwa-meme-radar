<template>
  <span class="comparison-metric">
    <strong :class="{ 'kpi-flash': flash }">{{ metric.value == null ? '—' : `${metric.value >= 0 ? '+' : ''}${metric.value.toFixed(2)}${unit === 'pp' ? tr(' 个百分点', ' pp') : '%'}` }}</strong>
    <small>{{ description }}</small>
    <small v-if="metric.value != null">{{ date(metric.at) }}</small>
  </span>
</template>
<script setup>
import { computed, ref, watch, onUnmounted } from 'vue';
import { tr } from '../i18n';
import { date } from '../utils/format';
import { currentMetric } from '../utils/comparisons';
import { useMinuteClock } from '../composables/useMinuteClock';
const props = defineProps({ value: Object, unit: { type: String, default: '%' } });
const now = useMinuteClock();
const metric = computed(() => currentMetric(props.value, now.value));
const reasons = {
  pending: ['数据不足', 'Insufficient data'],
  stale: ['报价已过期', 'Quotes expired'],
  'quota-exhausted': ['行情采集额度已用尽', 'Market data quota exhausted'],
  'reference-not-observed': ['暂无独立股票参考价', 'Independent stock reference unavailable'],
  'entitlement-required': ['股票行情授权不足', 'Equity data entitlement required'],
  'adjustment-unverified': ['缺少拆股等调整信息，收益不可比', 'Missing corporate-action adjustments; returns not comparable'],
  'issuer-reference': ['仅有发行方参考价', 'Issuer reference only'],
  'identity-unverified': ['底层证券身份待核实', 'Underlying security identity unverified'],
  'unverified-ratio': ['代币换算比例尚未核验', 'Token conversion ratio is unverified'],
  'stale-ratio': ['代币换算比例已过期或缺少有效期', 'Token conversion ratio expired or missing validity period'],
  'currency-mismatch': ['计价单位不同', 'Different quote units'],
  'missing-aligned-history': ['缺少与延迟股价同期的币价', 'No token quote aligned with delayed stock price'],
  'waiting-aligned-history': ['同期历史报价不足', 'Insufficient time-aligned history'],
  'delayed-reference': ['股票参考价为延迟行情', 'Equity reference is delayed'],
  unaligned: ['报价时间未对齐', 'Quote times not aligned'],
  'future-quote': ['报价时间异常', 'Invalid quote timestamp'],
  'missing-time': ['缺少报价时间', 'Missing quote timestamp'],
  'derived-price': ['仅有推算价，无法计算溢价', 'Derived price only; premium unavailable'],
  'unknown-market': ['价格市场待核实', 'Market scope unverified'],
  'missing-token-currency': ['计价币种或美元换算待核实', 'Quote currency / USD conversion unverified'],
  'missing-price': ['缺少市场报价', 'Missing market quote'],
  'invalid-price': ['价格数据异常', 'Invalid price'],
  'missing-reference': ['缺少股票参考价', 'Missing stock reference'],
  'missing-reference-source': ['参考价格来源待核实', 'Reference source unverified'],
  'missing-ratio': ['每枚代币对应股数待核实', 'Shares per token unverified'],
  'missing-fx': ['缺少币种或美元汇率', 'Missing currency or USD FX rate'],
  'market-closed': ['股票休市或停牌', 'Stock market closed or halted'],
  'missing-pool-quote': ['暂无池内兑换比例', 'Pool exchange ratio unavailable'],
  'missing-independent-quote': ['缺少独立市场报价', 'Missing independent market quote'],
  'unknown-price-path': ['价格来源路径未核实', 'Price dependency path unverified'],
  'circular-price': ['同池推导价格，无法比较', 'Circular pool-derived price'],
  'wrong-instrument': ['资产或包装单位不匹配', 'Instrument or wrapper units mismatch'],
  'unverified-pair': ['配对关系尚未核验', 'Pair not verified'],
  'insufficient-history': ['同期历史数据不足', 'Insufficient aligned history'],
};
const description = computed(() => metric.value.value == null
  ? tr(...(reasons[metric.value.reason] ?? ['数据暂不可用', 'Data unavailable']))
  : metric.value.status === 'realtime' ? tr('实时报价 · 同期比较', 'Live quotes · time-aligned') : metric.value.status === 'delayed' ? tr('延迟报价 · 同期比较', 'Delayed quotes · time-aligned') : tr('快照报价 · 非实时', 'Snapshot quotes · not live'));
const flash = ref(false); let timer;
watch(() => metric.value.value, (n, o) => {
  if (n == null || o == null || n === o) return;
  flash.value = true; clearTimeout(timer); timer = setTimeout(() => { flash.value = false; }, 1200);
});
onUnmounted(() => clearTimeout(timer));
</script>
<style scoped>
.comparison-metric { display: inline-flex; flex-direction: column; gap: 4px; }
.comparison-metric small { font-size: 11px; color: var(--muted, #8795a7); font-weight: normal; }
</style>
