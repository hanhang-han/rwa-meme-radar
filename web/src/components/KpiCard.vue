<template>
  <component :is="href ? 'a' : 'div'" class="kpi" :href="href || undefined" :data-kpi-key="kpiKey">
    <span class="kpi-label">{{ title }}</span>
    <strong class="kpi-value mono" :class="{ 'kpi-flash': flashing }">{{ value }}</strong>
    <span class="kpi-note">{{ note }}</span>
    <span v-if="href" class="kpi-link-cue" aria-hidden="true">↗</span>
  </component>
</template>

<script setup>
import { ref, watchEffect } from 'vue';
import { useDashboardStore } from '../stores/dashboard';

const props = defineProps({
  title: { type: String, required: true },
  value: { type: String, default: '' },
  note: { type: String, default: '' },
  href: { type: String, default: '' },
  kpiKey: { type: String, default: '' },
});

const flashing = ref(false);
const store = useDashboardStore();
watchEffect(() => {
  if (props.kpiKey && props.value && store.diffKpi(props.kpiKey, props.value)) {
    flashing.value = false;
    requestAnimationFrame(() => (flashing.value = true));
    setTimeout(() => (flashing.value = false), 1200);
  }
});
</script>

<style scoped>
a.kpi { position: relative; color: inherit; text-decoration: none; padding-right: 34px; cursor: pointer; }
a.kpi:hover, a.kpi:focus-visible { border-color: var(--accent); }
a.kpi:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.kpi-link-cue { position: absolute; right: 14px; top: 12px; color: var(--muted); }
a.kpi:hover .kpi-link-cue, a.kpi:focus-visible .kpi-link-cue { color: var(--accent); }
</style>
