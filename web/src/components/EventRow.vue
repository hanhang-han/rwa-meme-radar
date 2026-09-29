<template>
  <a class="x-signal v2-event" :href="detailHref">
    <span class="v2-pair-line"><b>{{ ev.symbol ?? short(asset) }}</b><template v-if="ev.ticker"><i>↔</i><b>{{ ev.ticker }}</b></template></span>
    <span class="tag" :class="{ match: ev.kind === 'verified' }">{{ label }}</span>
    <span class="v2-muted">{{ date(ev.t) }} · {{ chainName({ chainId: ev.chainId ?? '196' }) }}</span>
  </a>
</template>

<script setup>
import { computed } from 'vue';
import { tr } from '../i18n';
import { chainName, date, short } from '../utils/format';
import { normalizeEventAddress } from '../utils/event-records';

const props = defineProps({
  ev: { type: Object, required: true },
});

const label = computed(() => props.ev.kind === 'discovered' ? tr('新候选 · 股票关联待核验', 'New candidate · stock link unverified') : props.ev.kind === 'verified' ? (props.ev.level === 'A' || props.ev.relation?.level === 'A' ? tr('股票配对已核验', 'Stock pair verified') : tr('池结构已核验 · 股票身份待核验', 'Pool structure verified · stock identity unverified')) : props.ev.kind === 'invalidated' ? tr('关系证据变化', 'Relationship evidence changed') : props.ev.label);
const chainId = computed(() => String(props.ev.chainId ?? '196'));
const asset = computed(() => normalizeEventAddress(props.ev.asset, chainId.value));
const detailHref = computed(() => `#/detail/${encodeURIComponent(chainId.value)}/${encodeURIComponent(asset.value)}`);
</script>
