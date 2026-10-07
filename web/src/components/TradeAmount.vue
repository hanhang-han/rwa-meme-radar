<template>
  <span class="trade-amount" :title="amount.value==null ? tr('成交金额暂无数据；不表示零','Trade amount unavailable; does not mean zero') : (amount.usd ? tr('美元成交额', 'USD trade amount') : tr('暂无法折算为美元；显示原币金额', 'USD conversion unavailable; showing the original currency'))+': '+amount.value+' '+amount.unit">
    <span>{{ amount.value == null ? '—' : amount.usd ? usd(amount.value) : money(amount.value, amount.unit) }}</span>
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
.trade-amount {display:inline-flex;min-width:0;line-height:1.3;font-variant-numeric:tabular-nums;}
.trade-amount>span {overflow-wrap:anywhere;}
</style>
