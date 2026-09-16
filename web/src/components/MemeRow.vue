<template>
  <tr :data-asset="a.token" :class="{ 'v2-member-row': child }">
    <td>
      <RouterLink :to="detailLink(a)"><strong>{{ a.symbol }}</strong></RouterLink>
      <button v-if="extra > 0" class="v2-group-toggle" @click="emit('toggle-group')">{{ tr('+', '+') }} {{ extra }} {{ tr('个同名合约', 'same-name contracts') }}</button>
      <small>{{ chainName(a) }} · {{ short(a.token) }} · {{ a.provider ?? 'OKX' }}</small>
    </td>
    <td>
      <template v-if="relOf.length">
        <RouterLink v-for="r in relOf.slice(0, 2)" :key="r.id" :to="pairLink(r)">{{ r.ticker }} · {{ r.status === 'verified' ? '✓' : '?' }}</RouterLink>
        <small v-if="relOf.length > 2">+{{ relOf.length - 2 }}</small>
      </template>
      <template v-else>{{ tr('待核实', 'Pending') }}</template>
    </td>
    <td data-field="liquidity" :class="{ 'kpi-flash': flashKeys.has('liquidity') }">{{ usd(a.liquidity) }}</td>
    <td data-field="volume24h" :class="{ 'kpi-flash': flashKeys.has('volume24h') }">{{ usd(a.volume24h) }}</td>
    <td>{{ num(a.txs24h) }}<small>{{ tr('买 / 卖', 'Buys / sells') }}: {{ num(a.buys24h) }} / {{ num(a.sells24h) }}</small></td>
    <td data-field="price" :class="{ 'kpi-flash': flashKeys.has('price') }">{{ usd(a.price) }}</td>
    <td>{{ num(a.holders) }}</td>
    <td>{{ age(a.fieldTimes?.price ?? a.updatedAt) }}</td>
  </tr>
</template>

<script setup>
import { computed, reactive, watchEffect } from 'vue';
import { useDashboardStore } from '../stores/dashboard';
import { tr } from '../i18n';
import { age, chainName, detailLink, num, pairLink, short, usd } from '../utils/format';

const props = defineProps({
  a: { type: Object, required: true },
  relations: { type: Array, default: () => [] },
  store: { type: Object, required: true },
  child: { type: Boolean, default: false },
  extra: { type: Number, default: 0 },
});

const emit = defineEmits(['toggle-group']);

const relOf = computed(() => props.relations.filter((r) => r.token === props.a.token));

// Flash per field when the value changed since the last render.
const flashKeys = reactive(new Set());
watchEffect(() => {
  for (const field of ['price', 'volume24h', 'liquidity']) {
    const key = `${props.a.token}:${field}`;
    const text = props.a[field];
    if (props.store.diffCells(key, String(text ?? '')) && text != null) {
      flashKeys.add(field);
      setTimeout(() => flashKeys.delete(field), 1300);
    }
  }
});
</script>
