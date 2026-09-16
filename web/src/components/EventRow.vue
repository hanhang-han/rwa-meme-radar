<template>
  <a class="x-signal v2-event" :href="'#/detail/' + encodeURIComponent(ev.chainId ?? '196') + '/' + encodeURIComponent(ev.asset)">
    <span class="v2-pair-line"><b>{{ ev.symbol ?? short(ev.asset) }}</b><i>↔</i><b>{{ ev.ticker }}</b></span>
    <span class="tag" :class="{ match: ev.kind === 'verified' }">{{ ev.label }}</span>
    <span class="v2-muted">{{ date(ev.t) }} · {{ chainName({ chainId: ev.chainId ?? '196' }) }}</span>
  </a>
</template>

<script setup>
import { tr } from '../i18n';
import { chainName, date, short } from '../utils/format';

defineProps({
  ev: { type: Object, required: true },
});
</script>
