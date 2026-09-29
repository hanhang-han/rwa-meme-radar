<template>
  <div class="comparison-chart">
    <div class="chart-legend"><span v-for="s in series" :key="s.key" :style="{ color: s.color }">{{ s.label }}</span></div>
    <p v-if="rows.length < 2" class="x-empty">{{ tr('真实历史样本积累中；不会补画缺失行情。', 'Collecting real observations; missing prices are not fabricated.') }}</p>
    <svg v-else viewBox="0 0 800 220" role="img" :aria-label="label">
      <title>{{ label }}</title>
      <line x1="68" y1="20" x2="68" y2="184" stroke="currentColor" opacity=".25" />
      <line x1="68" y1="184" x2="780" y2="184" stroke="currentColor" opacity=".25" />
      <text x="4" y="25">{{ bounds.max.toFixed(2) }}</text><text x="4" y="181">{{ bounds.min.toFixed(2) }}</text>
      <template v-for="s in series" :key="s.key">
        <polyline v-for="(segment, i) in lineSegments(rows, s.key)" :key="i" :points="points(segment, s.key)" fill="none" :stroke="s.color" stroke-width="2" />
        <circle v-for="p in rows" :key="p.at" :cx="x(p.at)" :cy="y(p[s.key])" r="2" :fill="s.color"><title>{{ date(p.at) }} · {{ s.label }} {{ p[s.key]?.toFixed(2) }}</title></circle>
      </template>
      <text x="68" y="212">{{ time(rows[0].at) }}</text><text x="780" y="212" text-anchor="end">{{ time(rows.at(-1).at) }}</text>
    </svg>
    <small v-if="rows.length">{{ tr('本地时间 · 缺口断线显示', 'Local time · gaps are not joined') }} · {{ date(rows[0].at) }} — {{ date(rows.at(-1).at) }}</small>
  </div>
</template>
<script setup>
import { computed } from 'vue';
import { tr } from '../i18n';
import { date } from '../utils/format';
import { lineSegments } from '../utils/comparisons';
const props = defineProps({ rows: { type: Array, default: () => [] }, series: { type: Array, required: true }, label: String });
const bounds = computed(() => {
  const values = props.rows.flatMap(r => props.series.map(s => r[s.key])).filter(Number.isFinite);
  let min = Math.min(...values), max = Math.max(...values);
  if (min === max) { min -= 1; max += 1; }
  return { min, max };
});
const x = at => 68 + 712 * (at - props.rows[0].at) / Math.max(1, props.rows.at(-1).at - props.rows[0].at);
const y = value => 184 - 164 * (value - bounds.value.min) / (bounds.value.max - bounds.value.min);
const points = (rows, key) => rows.map(r => `${x(r.at)},${y(r[key])}`).join(' ');
const time = at => new Date(at).toLocaleTimeString([], { hour12: false });
</script>
<style scoped>
.comparison-chart svg { width: 100%; max-height: 270px; color: var(--muted, #8795a7); }
.comparison-chart text { fill: currentColor; font-size: 11px; }
.chart-legend { display: flex; gap: 20px; font-size: 12px; margin-top: 12px; }
.comparison-chart small { color: var(--muted, #8795a7); font-size: 11px; }
</style>
