<template>
  <div class="kpi" :data-kpi-key="kpiKey">
    <span class="kpi-label">{{ title }}</span>
    <a v-if="href" :href="href" class="kpi-value mono" :class="{ 'kpi-flash': flashing }">{{ value }}</a>
    <strong v-else class="kpi-value mono" :class="{ 'kpi-flash': flashing }">{{ value }}</strong>
    <span class="kpi-note">{{ note }}</span>
  </div>
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
