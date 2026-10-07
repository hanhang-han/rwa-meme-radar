<template>
  <div ref="chartElement" class="theme-history" :class="{'theme-history--compact':compact}">
    <div class="theme-history-controls"><div class="theme-chart-key"><span class="price-key" :title="tr('所选股票代币的美元价格；不是正股股价。','USD price of the selected stock token; not the underlying stock share price.')">{{ tr('股票代币价','Stock-token price') }} · USD</span><template v-if="!compact"><span class="volume-key" :title="tr('直接配对池每小时的实际美元成交额；覆盖不足时留空。','Actual hourly USD volume in direct stock-pair pools; incomplete coverage stays empty.')">{{ tr('关联池成交','Paired-pool volume') }} · USD/h</span><span class="event-key" :title="tr('已确认的链上池创建时间。','Confirmed onchain pool creation time.')">{{ tr('池创建','Pool created') }}</span></template></div><label>{{ tr('时间范围','Range') }} <select :value="range" :aria-label="tr('图表时间范围','Chart range')" @change="emit('update:range',$event.target.value)"><option value="24h">24h</option><option value="7d">7d</option></select></label><button type="button" :disabled="loading" @click="emit('refresh')">{{ loading?tr('更新中…','Updating…'):tr('刷新','Refresh') }}</button></div>
    <p v-if="!compact" class="theme-history-axis-note">{{ series.volumes.length?tr('左轴：代币价格 · 右轴：每小时成交额 · 横轴：本地时间','Left axis: token price · right axis: hourly volume · time axis: local time'):tr('纵轴：代币价格 · 横轴：本地时间','Price axis: token price · time axis: local time') }}</p>
    <div v-if="loading&&!hasSeries" class="theme-history-loading" role="status"><span>{{ tr('正在加载历史行情…','Loading historical quotes…') }}</span><div class="skeleton-line"></div><div class="skeleton-line"></div></div>
    <template v-else-if="hasSeries">
      <svg v-if="compact" class="theme-history-svg theme-history-price-svg" :viewBox="`0 0 ${chartWidth} 190`" role="img" :aria-label="tr('股票代币美元价格走势，时间为本地时间','Stock-token USD price history, in local time')">
        <title>{{ tr('股票代币美元价格走势','Stock-token USD price history') }} · {{ range }} · {{ tr('本地时间','Local time') }}</title>
        <g class="chart-grid"><line v-for="tick in priceTicks" :key="tick.y" :x1="plotLeft" :x2="plotRight" :y1="tick.y" :y2="tick.y" /></g>
        <path v-if="pricePath" class="theme-price-line" :d="pricePath" />
        <circle v-if="series.prices.length===1" class="theme-price-point" :cx="x(series.prices[0].t)" :cy="y(series.prices[0].value)" r="3"><title>{{ date(series.prices[0].t) }} · {{ price(series.prices[0].value,'USD') }}</title></circle>
        <g class="chart-axis"><text v-for="tick in priceTicks" :key="tick.y" :x="plotRight+8" :y="tick.y+4">{{ price(tick.value,'USD') }}</text><text :x="plotLeft" y="181">{{ axisTime(series.start) }}</text><text :x="plotRight" y="181" text-anchor="end">{{ axisTime(series.end) }}</text></g>
      </svg>
      <svg v-else class="theme-history-svg" viewBox="0 0 760 330" role="img" :aria-label="tr('股票代币价格、关联池成交与池创建时间组合图','Stock-token price, paired-pool volume and creation times')">
        <title>{{ tr('股票代币价格、每小时关联池成交与池创建时间','Stock-token price, hourly paired-pool volume and pool creation time') }} · {{ range }}</title>
        <g class="chart-grid"><line v-for="tick in [0,1,2,3]" :key="tick" x1="76" x2="700" :y1="25+tick*62" :y2="25+tick*62" /></g>
        <g v-if="series.prices.length" class="chart-axis"><text v-for="tick in priceTicks" :key="tick.y" x="68" :y="tick.y+4" text-anchor="end">{{ price(tick.value,'USD') }}</text></g>
        <g class="theme-volume-bars"><rect v-for="bar in bars" :key="bar.t" :x="bar.x" :y="bar.y" :width="bar.width" :height="bar.height"><title>{{ date(bar.t) }} · {{ tr('这一小时关联池成交额','Paired-pool volume in this hour') }} {{ usd(bar.volumeUsd) }}</title></rect></g>
        <path v-if="pricePath" class="theme-price-line" :d="pricePath" />
        <g v-for="(event,index) in series.events" :key="event.chainId+':'+event.pool+':'+event.t+':'+index" class="theme-creation-marker"><line :x1="x(event.t)" :x2="x(event.t)" y1="22" y2="286" /><circle :cx="x(event.t)" cy="24" r="3"><title>{{ event.symbol }} · {{ event.dex }} · {{ date(event.t) }}</title></circle></g>
        <g class="chart-axis"><text x="76" y="314">{{ axisTime(series.start) }}</text><text x="700" y="314" text-anchor="end">{{ axisTime(series.end) }}</text><template v-if="series.volumes.length"><text x="708" y="232">{{ usd(volumeMax) }}</text><text x="708" y="286">$0</text></template></g>
      </svg>
      <p class="theme-history-time"><span v-if="compact&&priceSources">{{ priceSources }} · </span>{{ tr('最后行情','Last quote') }} {{ date(lastPriceAt) }}<span v-if="lastPriceAt && now-lastPriceAt>900000" class="warn"> · {{ tr('图表延迟','Chart delayed') }} {{ Math.ceil((now-lastPriceAt)/60000) }} {{ tr('分钟','min') }}</span><template v-if="!compact&&!series.volumes.length"> · {{ tr('关联池成交暂无数据','Paired-pool volume unavailable') }}</template></p>
      <p v-if="error" class="theme-history-error warn" role="alert">{{ tr('历史行情更新失败，保留上次结果。','History update failed; the previous result is shown.') }}</p>
    </template>
    <p v-else class="x-empty" :role="error?'alert':'status'">{{ error?tr('图表暂时无法加载。','The chart could not be loaded.'):compact?tr('所选窗口暂无可用历史价格。','No historical prices in this window.'):tr('所选窗口暂无历史行情与关联池成交。','No historical quotes or paired-pool volume in this window.') }}</p>
    <details v-if="!compact" class="theme-history-source"><summary>{{ tr('图表数据','Chart data') }}</summary><p>{{ tr('价格为股票代币美元报价；成交柱只使用与该股票直接配对的池成交。缺失数据保留为空。虚线使用链上池创建时间。','Prices are stock-token USD quotes. Bars use only pools directly paired with this stock. Missing values remain empty. Dashed lines use on-chain pool creation times.') }}</p><p v-if="data?.coverage?.volume" :title="tr('分子为全部配对池都有有效美元成交覆盖的时段，分母为所选窗口的全部时段。','Numerator: intervals with complete valid USD trade coverage for all paired pools. Denominator: all intervals in the selected window.')">{{ tr('成交时段覆盖（完整 / 总时段）','Volume interval coverage (complete / total)') }} {{ data.coverage.volume.known??'—' }} / {{ data.coverage.volume.total??'—' }}</p><ul v-if="series.events.length"><li v-for="(event,index) in series.events" :key="index">{{ date(event.t) }} · {{ event.symbol||short(event.token) }} · {{ event.dex||'DEX' }}<a v-if="event.pool" :href="explorer(event.pool,'address',String(event.chainId))" target="_blank" rel="noopener">{{ short(event.pool) }} ↗</a></li></ul></details>
  </div>
</template>
<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { tr, useI18n } from '../i18n';
import { date, explorer, price, short, usd } from '../utils/format';
import { themeChartSeries } from '../utils/theme-chart-model';
import { useMinuteClock } from '../composables/useMinuteClock';
const props=defineProps({data:{type:Object,default:null},loading:Boolean,error:Boolean,range:{type:String,default:'24h'},compact:Boolean});
const emit=defineEmits(['update:range','refresh']),now=useMinuteClock(),{lang}=useI18n();
const chartElement=ref(null),chartWidth=ref(760);
let resizeObserver;
function updateWidth(width){if(Number.isFinite(width)&&width>0)chartWidth.value=Math.max(260,Math.round(width));}
onMounted(()=>{updateWidth(chartElement.value?.clientWidth);if(typeof ResizeObserver!=='undefined'){resizeObserver=new ResizeObserver(entries=>{updateWidth(entries[0]?.contentRect?.width);});resizeObserver.observe(chartElement.value);}});
onBeforeUnmount(()=>resizeObserver?.disconnect());
const series=computed(()=>themeChartSeries(props.data??{}));
const hasSeries=computed(()=>series.value.prices.length>0||(!props.compact&&series.value.volumes.length>0));
const plotLeft=computed(()=>props.compact?10:76),plotRight=computed(()=>props.compact?chartWidth.value-84:700);
function x(t){return plotLeft.value+(series.value.end>series.value.start?(t-series.value.start)/(series.value.end-series.value.start):.5)*(plotRight.value-plotLeft.value);}
const bounds=computed(()=>{const values=series.value.prices.map(row=>row.value);const lo=Math.min(...values),hi=Math.max(...values);const pad=hi===lo?Math.max(Math.abs(hi)*.02,1e-12):(hi-lo)*.08;return {lo:lo-pad,hi:hi+pad};});
function y(value){return props.compact?151-(value-bounds.value.lo)/(bounds.value.hi-bounds.value.lo)*133:210-(value-bounds.value.lo)/(bounds.value.hi-bounds.value.lo)*185;}
const priceTicks=computed(()=>props.compact?[0,1,2].map(index=>({y:18+index*66.5,value:bounds.value.hi-(bounds.value.hi-bounds.value.lo)*index/2})):[0,1,2,3].map(index=>({y:25+index*62,value:bounds.value.hi-(bounds.value.hi-bounds.value.lo)*index/3})));
const pricePath=computed(()=>series.value.prices.map((row,index,rows)=>`${index&&row.t-rows[index-1].t<=(props.range==='7d'?14_400_000:3_600_000)*1.8?'L':'M'}${x(row.t).toFixed(2)},${y(row.value).toFixed(2)}`).join(' '));
const volumeMax=computed(()=>Math.max(0,...series.value.volumes.map(row=>row.volumeUsd)));
const bars=computed(()=>{const rows=series.value.volumes,width=Math.max(1,Math.min(16,624/Math.max(rows.length,1)*.65));return rows.map(row=>{const height=volumeMax.value>0?row.volumeUsd/volumeMax.value*54:0;return {...row,x:x(row.t)-width/2,y:286-height,width,height};});});
const lastPriceAt=computed(()=>series.value.prices.at(-1)?.t??null);
const priceSources=computed(()=>props.data?.coverage?.prices?.source||[...new Set(series.value.prices.map(row=>row.source).filter(value=>typeof value==='string'&&value))].join(', '));
function axisTime(t){return t?new Date(t).toLocaleString(lang.lang==='en'?'en-US':'zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}):'—';}
</script>
<style scoped>
.theme-history-controls{display:flex;align-items:center;flex-wrap:wrap;gap:10px;font-size:12px}.theme-chart-key{display:flex;flex-wrap:wrap;gap:10px;margin-right:auto;color:var(--muted)}.theme-chart-key span{display:inline-flex;gap:5px;align-items:center}.theme-chart-key span::before{content:'';display:inline-block;width:12px;height:2px;background:var(--accent)}.theme-chart-key .volume-key::before{height:8px;background:var(--border)}.theme-chart-key .event-key::before{width:1px;height:10px;background:none;border-left:1px dashed var(--text)}.theme-history-controls button,.theme-history-controls select{padding:5px 8px;border:1px solid var(--border);background:var(--panel);color:var(--text);font:inherit;cursor:pointer}.theme-history-svg{display:block;width:100%;height:auto;min-height:210px;margin-top:14px;overflow:visible}.chart-grid line{stroke:var(--border);stroke-width:.8}.chart-axis text{fill:var(--muted);font:12px var(--number-font);font-variant-numeric:tabular-nums}.theme-price-line{fill:none;stroke:var(--accent);stroke-width:2}.theme-volume-bars rect{fill:var(--muted);opacity:.38}.theme-creation-marker line{stroke:var(--muted);stroke-dasharray:4 4;opacity:.6}.theme-creation-marker circle{fill:var(--text)}.theme-history-time,.theme-history-source{font-size:12px;color:var(--muted);margin:10px 0 0}.theme-history-source summary{cursor:pointer}.theme-history-source p{margin-top:7px}.theme-history-source ul{padding-left:16px;margin-top:8px}.theme-history-source li{margin-top:5px}.theme-history-source a{margin-left:8px;color:var(--text);text-decoration:underline}.theme-history-loading{min-height:260px;padding-top:40px}
@media(max-width:600px){.theme-history-svg{min-height:190px}.theme-chart-key{flex-basis:100%}}
.theme-history-axis-note{margin:10px 0 0;font-size:11px;line-height:1.6;color:var(--muted)}
.theme-price-point{fill:var(--accent)}.theme-history-error{font-size:12px;margin:6px 0 0}.theme-history--compact .theme-history-svg{height:190px;min-height:0;margin-top:6px;overflow:hidden}.theme-history--compact .theme-history-loading{min-height:0;padding:14px 0}.theme-history--compact .theme-history-loading .skeleton-line{margin-top:10px}.theme-history--compact .theme-history-time{margin-top:4px;line-height:1.5}.theme-history--compact .x-empty{padding:14px 0;margin:0}.theme-history--compact .theme-history-controls{gap:8px}.theme-history--compact .chart-axis text{font-size:11px}.theme-history--compact .theme-history-controls select{border-radius:5px}.theme-history--compact .theme-history-controls button{border-radius:5px}.theme-history--compact .theme-price-line{vector-effect:non-scaling-stroke}
@media(max-width:600px){.theme-history--compact .theme-chart-key{flex-basis:auto}.theme-history--compact .theme-history-controls{font-size:11px}}
</style>
