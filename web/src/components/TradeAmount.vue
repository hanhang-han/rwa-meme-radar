<template>
  <span class="trade-amount" :title="amount.usd ? tr('美元成交额', 'USD trade amount') : tr('缺少成交时点的美元换算依据', 'No verified USD conversion at trade time')">
    <span>{{ amount.value == null ? '—' : amount.usd ? usd(amount.value) : money(amount.value, amount.unit) }}</span>
    <small v-if="!amount.usd">{{ tr('美元金额未计价', 'USD value not priced') }}</small>
  </span>
</template>

<script>
const numeric = value => value != null && value !== '' && typeof value !== 'boolean' && Number.isFinite(Number(value)) ? Number(value) : null;

export function describeTradeAmount(trade) {
  const volumeUnit = String(trade?.volumeCurrency ?? '').trim();
  const priceUnit = String(trade?.priceCurrency ?? '').trim();
  const volume = numeric(trade?.volume);
  // A persisted USD volume is valued with dated evidence. The raw pool quote
  // remains in quoteQuantity/quoteVolume and keeps its original price unit.
  if (volumeUnit === 'USD' && volume != null) return { value:volume, unit:'USD', usd:true };
  const quote = numeric(trade?.quoteQuantity ?? trade?.quoteVolume);
  const quoteUnit = priceUnit && priceUnit !== 'USD' ? priceUnit
    : volumeUnit && volumeUnit !== 'USD' ? volumeUnit : priceUnit || volumeUnit;
  if (quote != null && quoteUnit) return { value:quote, unit:quoteUnit, usd:quoteUnit === 'USD' };
  if (volume != null && volumeUnit) return { value:volume, unit:volumeUnit, usd:volumeUnit === 'USD' };
  return { value:null, unit:null, usd:false };
}
</script>

<script setup>
import { computed } from 'vue';
import { tr } from '../i18n';
import { money, usd } from '../utils/format';
const props=defineProps({ trade:{type:Object,required:true} });
const amount=computed(()=>describeTradeAmount(props.trade));
</script>

<style scoped>
.trade-amount {display:inline-flex;flex-direction:column;gap:1px;min-width:0;line-height:1.3;font-variant-numeric:tabular-nums;}
.trade-amount>span {overflow-wrap:anywhere;}
.trade-amount small {font-size:10px;font-weight:400;color:var(--muted);white-space:normal;}
</style>
