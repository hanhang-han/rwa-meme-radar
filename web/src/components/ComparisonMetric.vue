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
  pending: ['等待采集计算', 'Waiting for observations'],
  stale: ['报价已过期', 'Quotes expired'],
  'quota-exhausted': ['行情采集额度已用尽', 'Market data quota exhausted'],
  'reference-not-observed': ['尚未采到对应证券的独立参考价', 'No independent reference observed for this security'],
  'entitlement-required': ['当前股票行情授权不支持此指标', 'Current equity data entitlement does not support this metric'],
  'adjustment-unverified': ['缺少股票公司行动调整版本，暂不计算跨期收益', 'Corporate-action adjustment version unavailable; returns are not calculated'],
  'issuer-reference': ['发行方参考值不作为独立股票行情', 'Issuer reference is not an independent equity market quote'],
  'identity-unverified': ['底层证券身份待核实', 'Underlying security identity unverified'],
  'unverified-ratio': ['代币换算比例尚未核验', 'Token conversion ratio is unverified'],
  'stale-ratio': ['代币换算比例已过期或缺少有效期', 'Token conversion ratio expired or missing validity period'],
  'currency-mismatch': ['两个独立市场的计价单位不一致', 'Independent markets use different quote units'],
  'missing-aligned-history': ['等待积累与延迟股价同刻的币价', 'Waiting for token observations aligned with delayed equity quotes'],
  'waiting-aligned-history': ['同刻历史报价积累中', 'Collecting time-aligned historical observations'],
  'delayed-reference': ['股票参考价为延迟行情', 'Equity reference is delayed'],
  unaligned: ['报价时间未对齐', 'Quote times not aligned'],
  'future-quote': ['报价时间异常', 'Invalid quote timestamp'],
  'missing-time': ['缺少报价时间', 'Missing quote timestamp'],
  'derived-price': ['推导估值，不能计算溢价', 'Derived value, not a market premium'],
  'unknown-market': ['价格市场待核实', 'Market scope unverified'],
  'missing-token-currency': ['计价币种或美元换算待核实', 'Quote currency / USD conversion unverified'],
  'missing-price': ['缺少市场报价', 'Missing market quote'],
  'invalid-price': ['价格数据异常', 'Invalid price'],
  'missing-reference': ['缺少股票参考价', 'Missing stock reference'],
  'missing-reference-source': ['参考价格来源待核实', 'Reference source unverified'],
  'missing-ratio': ['每枚代币对应股数待核实', 'Shares per token unverified'],
  'missing-fx': ['缺少币种或美元汇率', 'Missing currency or USD FX rate'],
  'market-closed': ['股票休市或停牌', 'Stock market closed or halted'],
  'missing-pool-quote': ['等待交易池兑换比例', 'Waiting for pool exchange ratio'],
  'missing-independent-quote': ['缺少独立市场报价', 'Missing independent market quote'],
  'unknown-price-path': ['价格来源路径未核实', 'Price dependency path unverified'],
  'circular-price': ['同池推导价格，无法比较', 'Circular pool-derived price'],
  'wrong-instrument': ['资产或包装单位不匹配', 'Instrument or wrapper units mismatch'],
  'unverified-pair': ['配对关系尚未核验', 'Pair not verified'],
  'insufficient-history': ['同窗口历史不足', 'Insufficient aligned history'],
};
const description = computed(() => metric.value.value == null
  ? tr(...(reasons[metric.value.reason] ?? ['数据暂不可用', 'Data unavailable']))
  : metric.value.status === 'realtime' ? tr('实时对齐报价', 'Aligned live quotes') : metric.value.status === 'delayed' ? tr('延迟行情 · 同刻历史比较', 'Delayed data · same-time historical comparison') : tr('对齐快照 · 非实时', 'Aligned snapshot · not realtime'));
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
