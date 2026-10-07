<template>
  <div class="theme-sparkline-wrap" :title="historyTitle">
    <span class="theme-sparkline-label">{{ tr('股票代币价格走势','Stock-token price history') }} · USD</span>
    <svg v-if="path" class="theme-sparkline" viewBox="0 0 140 40" role="img" :aria-label="historyTitle"><title>{{ historyTitle }}</title><path :d="path" fill="none" stroke="currentColor" stroke-width="1.8" /></svg>
    <span v-else class="theme-sparkline-empty">{{ tr('暂无走势','No history') }}</span>
  </div>
</template>
<script setup>
import { computed } from 'vue';
import { tr } from '../i18n';
import { date } from '../utils/format';
const props=defineProps({points:{type:Array,default:()=>[]}});
const rows=computed(()=>props.points.map((point,index)=>typeof point==='number'?{t:index,value:point}:point)
  .filter(point=>point?.value!=null&&point.value!==''&&Number.isFinite(Number(point.value))&&Number.isFinite(Number(point.t)))
  .slice().sort((a,b)=>Number(a.t)-Number(b.t)));
const historyTitle=computed(()=>{
  const points=rows.value;
  if(points.length<2||Number(points.at(-1).t)<=Number(points[0].t))return tr('股票代币历史价格（USD）；有效样本不足，暂无走势，不代表价格为零。','Stock-token historical price (USD); insufficient valid observations, not a zero price.');
  const range=Number(points[0].t)>1e11?`${date(Number(points[0].t))} → ${date(Number(points.at(-1).t))}`:tr('时间范围未知','Time range unknown');
  return `${tr('股票代币历史价格（USD）','Stock-token historical price (USD)')} · ${range} · ${points.length} ${tr('个观测点；纵轴按已有样本缩放','observations; Y axis scaled to available samples')}`;
});
const path=computed(()=>{
  const points=rows.value;
  if(points.length<2)return '';
  const lo=Math.min(...points.map(row=>Number(row.value))),hi=Math.max(...points.map(row=>Number(row.value))),start=Number(points[0].t),end=Number(points.at(-1).t);
  if(end<=start)return '';
  return points.map((row,index)=>`${index?'L':'M'}${(4+(Number(row.t)-start)/(end-start)*132).toFixed(2)},${(hi===lo?20:35-(Number(row.value)-lo)/(hi-lo)*30).toFixed(2)}`).join(' ');
});
</script>
<style scoped>
.theme-sparkline-wrap{display:grid;gap:3px;width:100%;min-width:0}.theme-sparkline-label{font-size:12px;line-height:1.4;color:var(--muted);overflow-wrap:anywhere}.theme-sparkline{display:block;width:100%;height:40px;color:var(--accent)}.theme-sparkline-empty{display:flex;align-items:center;height:40px;font-size:12px;color:var(--muted)}
</style>
