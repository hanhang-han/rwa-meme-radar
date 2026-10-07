<template>
  <div class="comparison-chart">
    <h3 v-if="label" class="comparison-chart-title">{{ label }}</h3>
    <p class="comparison-chart-units">{{ tr('纵轴','Value axis') }} · {{ unit }} <span> / {{ tr('横轴 · 本地时间','Time axis · local time') }}</span></p>
    <div class="chart-legend"><span v-for="s in series" :key="s.key" :style="{ color: s.color }" :title="s.label+' · '+unit">{{ s.label }}</span></div>
    <p v-if="!hasHistory" class="x-empty">{{ tr('真实历史样本积累中；不会补画缺失行情。', 'Collecting real observations; missing prices are not fabricated.') }}</p>
    <svg v-else viewBox="0 0 800 220" role="img" :aria-label="label">
      <title>{{ label }} · {{ unit }}</title>
      <line x1="68" y1="20" x2="68" y2="184" stroke="currentColor" opacity=".25" />
      <line x1="68" y1="184" x2="780" y2="184" stroke="currentColor" opacity=".25" />
      <text x="4" y="25">{{ bounds.max.toFixed(2) }}</text><text x="4" y="181">{{ bounds.min.toFixed(2) }}</text>
      <template v-for="s in series" :key="s.key">
        <polyline v-for="(segment, i) in lineSegments(rows, s.key)" :key="i" :points="points(segment, s.key)" fill="none" :stroke="s.color" stroke-width="2" />
        <circle v-for="p in observedComparisonRows(rows,s.key)" :key="p.at" :cx="x(p.at)" :cy="y(p[s.key])" r="2" :fill="s.color"><title>{{ date(p.at) }} · {{ s.label }} {{ p[s.key].toFixed(2) }} · {{ unit }}</title></circle>
      </template>
      <text x="68" y="212">{{ time(rows[0].at) }}</text><text x="780" y="212" text-anchor="end">{{ time(rows.at(-1).at) }}</text>
    </svg>
    <small v-if="hasHistory">{{ tr('本地时间 · 缺口断线显示', 'Local time · gaps are not joined') }} · {{ date(rows[0].at) }} — {{ date(rows.at(-1).at) }}</small>
  </div>
</template>
<script>
export function observedComparisonRows(rows, field) {
  return (rows ?? []).filter(row => Number.isFinite(row?.at) && Number.isFinite(row?.[field]));
}
</script>
<script setup>
import { computed } from 'vue';
import { tr } from '../i18n';
import { date } from '../utils/format';
import { lineSegments } from '../utils/comparisons';
const props = defineProps({ rows: { type: Array, default: () => [] }, series: { type: Array, required: true }, label: String, unit:{type:String,default:'%'} });
const hasHistory = computed(() => props.series.some(s => observedComparisonRows(props.rows,s.key).length >= 2));
const bounds = computed(() => {
  const values = props.series.flatMap(s => observedComparisonRows(props.rows,s.key).map(row => row[s.key]));
  if (!values.length) return { min:0, max:1 };
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
.comparison-chart-title{margin:16px 0 6px;font-size:14px;font-weight:600}.comparison-chart-units{margin:0;color:var(--muted);font-size:11px;line-height:1.5}
</style>
