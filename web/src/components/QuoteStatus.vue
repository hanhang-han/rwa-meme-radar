<template><small class="quote-status" :class="[`quote-${freshness.state}`, { 'quote-stale': freshness.stale }]" :title="stamp">{{ label }}<span v-if="at"> · {{ age(at) }}</span><span v-if="currency && freshness.state !== 'no-verified-market'"> · {{ currency }}</span></small></template>
<script setup>
import { computed } from 'vue';
import { useMinuteClock } from '../composables/useMinuteClock';
import { tr } from '../i18n';
import { age, date } from '../utils/format';
import { quoteAt, quoteFreshness } from '../utils/quote-status';
const props = defineProps({ row: {type:Object,required:true}, reference: Boolean });
const now = useMinuteClock();
const at = computed(() => quoteAt(props.row, props.reference));
const freshness = computed(() => quoteFreshness(props.row, props.reference, now.value));
const label = computed(() => ({
  missing: tr('暂无报价', 'No quote'),
  'no-verified-market': tr('暂无可验证的市场报价', 'No verifiable market quote'),
  stale: tr('历史报价', 'Historical quote'),
  delayed: tr('延迟参考', 'Delayed reference'),
  live: tr('实时行情', 'Live quote'),
  scheduled: tr('定时更新', 'Scheduled'),
  market_closed: tr('市场休市', 'Market closed'),
  unsupported: tr('暂不支持', 'Unsupported'),
  'unknown-time': tr('报价时间待核实', 'Quote time unverified'),
  'issuer-reference': tr('发行方参考', 'Issuer reference'),
  'entitlement-required': tr('行情授权不足', 'Market data entitlement required'),
  'identity-unverified': tr('证券身份待核实', 'Security identity unverified'),
  'quota-exhausted': tr('数据源额度用尽', 'Source quota exhausted'),
  'budget-exhausted': tr('数据源额度用尽', 'Source quota exhausted'),
})[freshness.value.state] ?? tr('行情状态未知', 'Quote status unknown'));
const currency = computed(() => props.reference ? props.row.referenceCurrency : props.row.priceCurrency);
const stamp = computed(() => `${props.reference ? props.row.referenceProvider ?? '' : props.row.fieldSources?.price ?? props.row.provider ?? ''} · ${date(at.value)}`);
</script>
