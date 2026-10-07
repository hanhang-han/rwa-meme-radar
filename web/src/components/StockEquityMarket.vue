<template>
  <section class="panel theme-equity-market" :aria-label="tr('正股行情','Underlying stock market')">
    <div class="panel-head"><div><h2>{{ tr('正股行情','Underlying stock') }}</h2><p class="equity-caption">{{ stock?.stockIdentity?.marketCode || stock?.stockIdentity?.code || ticker }} · {{ currency || tr('币种待确认','Currency unconfirmed') }}</p></div><span class="equity-session">{{ sessionLabel }}</span></div>
    <div v-if="stock?.stockPrice!=null" class="equity-quote">
      <strong><LiveNumber :value="stock.stockPrice" :currency="currency" format="price" :label="tr('正股价格','Underlying share price')" /></strong>
      <span v-if="stock.referenceChange24h!=null" :class="Number(stock.referenceChange24h)>0?'up':Number(stock.referenceChange24h)<0?'down':''">{{ pct(stock.referenceChange24h) }} <small>{{ tr('较前收盘','vs. previous close') }}</small></span>
    </div>
    <p v-else class="equity-caption">{{ stock?.referenceStatus==='entitlement-required'?tr('正股行情源需要额外权限。','The equity source requires additional access.'):tr('正股报价暂不可用。','The underlying quote is unavailable.') }}</p>
    <p v-if="stock?.referenceAt" class="equity-caption" :class="{'equity-old':quoteOld}">{{ tr('报价时间','Quote time') }} {{ date(stock.referenceAt) }}<span v-if="quoteOld"> · {{ tr('历史参考价','Historical reference') }}</span> · {{ stock.referenceProvider }} · {{ stock.referenceDelayMs?tr('延迟','Delayed')+' '+Math.round(stock.referenceDelayMs/60000)+' min':tr('公开参考行情，延迟未确认','Public reference; delay unconfirmed') }}</p>
    <div v-if="stock?.referenceTurnover!=null||stock?.referenceVolume!=null" class="equity-metrics">
      <div v-if="stock.referenceTurnover!=null"><span>{{ tr('当日成交额','Session turnover') }}</span><strong>{{ money(stock.referenceTurnover,currency) }}</strong></div>
      <div v-if="stock.referenceVolume!=null && stock.referenceVolumeUnit==='shares'"><span>{{ tr('当日成交量','Session volume') }}</span><strong>{{ num(stock.referenceVolume) }} {{ tr('股','shares') }}</strong></div>
    </div>
    <div class="equity-controls"><div><button type="button" :aria-pressed="mode==='candle'" @click="mode='candle'">{{ tr('日 K','Daily candles') }}</button><button type="button" :aria-pressed="mode==='line'" @click="mode='line'">{{ tr('收盘走势','Closing prices') }}</button></div><select v-model="range" :aria-label="tr('正股图表时间范围','Equity chart range')"><option value="1m">{{ tr('1 个月','1 month') }}</option><option value="3m">{{ tr('3 个月','3 months') }}</option><option value="6m">{{ tr('6 个月','6 months') }}</option></select><button type="button" :disabled="loading" @click="load">{{ tr('刷新','Refresh') }}</button></div>
    <svg v-if="rows.length" class="equity-chart" viewBox="0 0 800 270" role="img" :aria-label="tr('正股历史日 K，计价','Underlying daily history, in ')+history.currency">
      <title>{{ tr('正股日 K','Underlying daily candles') }} · {{ history.currency }} · {{ tr('不复权','Unadjusted') }}</title>
      <g class="equity-grid"><line v-for="tick in ticks" :key="tick.y" x1="18" x2="712" :y1="tick.y" :y2="tick.y" /></g>
      <g class="equity-axis"><text v-for="tick in ticks" :key="tick.y" x="720" :y="tick.y+4">{{ axisNumber(tick.value) }}</text><text x="18" y="262">{{ day(rows[0].t) }}</text><text x="712" y="262" text-anchor="end">{{ day(rows.at(-1).t) }}</text></g>
      <g v-if="mode==='candle'" class="equity-bars"><g v-for="(bar,index) in rows" :key="bar.t" :class="bar.c>=bar.o?'equity-up':'equity-down'"><title>{{ day(bar.t) }} · {{ tr('开','O') }} {{ bar.o }} · {{ tr('高','H') }} {{ bar.h }} · {{ tr('低','L') }} {{ bar.l }} · {{ tr('收','C') }} {{ bar.c }} {{ history.currency }}</title><line :x1="x(index)" :x2="x(index)" :y1="y(bar.h)" :y2="y(bar.l)" /><rect :x="x(index)-barWidth/2" :y="Math.min(y(bar.o),y(bar.c))" :width="barWidth" :height="Math.max(1,Math.abs(y(bar.o)-y(bar.c)))" /></g></g>
      <path v-else class="equity-line" :d="path" />
      <g v-if="volumeMax>0" class="equity-volume"><rect v-for="(bar,index) in rows.filter(r=>r.volumeShares!=null)" :key="bar.t" :x="x(rows.indexOf(bar))-barWidth/2" :y="242-bar.volumeShares/volumeMax*34" :width="barWidth" :height="bar.volumeShares/volumeMax*34"><title>{{ day(bar.t) }} · {{ num(bar.volumeShares) }} {{ tr('股','shares') }}</title></rect></g>
    </svg>
    <p v-else class="equity-empty" role="status">{{ loading?tr('正在加载正股历史行情…','Loading equity history…'):error?tr('正股历史行情暂时无法加载。','Equity history could not be loaded.'):tr('当前来源尚未覆盖正股历史。','The current source does not cover equity history.') }}</p>
    <p v-if="error&&rows.length" class="equity-caption warn" role="status">{{ tr('更新未成功，保留上次图表。','Refresh failed; the previous chart is retained.') }}</p>
    <p v-if="rows.length" class="equity-caption">{{ history.provider }} · {{ tr('不复权日 K','Unadjusted daily candles') }} · {{ history.currency }} · {{ tr('截至','Through') }} {{ day(rows.at(-1).t) }}<a v-if="history.sourceUrl" :href="history.sourceUrl" target="_blank" rel="noopener">{{ tr('来源','Source') }} ↗</a></p>
  </section>
</template>
<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { tr, useI18n } from '../i18n';
import { date, money, num, pct } from '../utils/format';
import { getEquityChart } from '../api/product';
import LiveNumber from './LiveNumber.vue';
const props=defineProps({stock:Object,ticker:String,chain:{type:String,default:'all'},marketStatus:Object});
const {lang}=useI18n(),range=ref('6m'),mode=ref('candle'),history=ref(null),loading=ref(false),error=ref(false);
let request=0,timer;
const currency=computed(()=>props.stock?.referenceCurrency||props.stock?.stockIdentity?.currency);
const quoteOld=computed(()=>props.stock?.referenceStatus==='stale'&&(props.marketStatus?.status!=='closed'||Date.now()-props.stock.referenceAt>4*86400000));
const sessionLabel=computed(()=>({closed:tr('休市','Closed'),open:tr('交易中','Open'),halted:tr('暂停交易','Halted')}[props.marketStatus?.status]??tr('交易状态待确认','Session unconfirmed')));
const rows=computed(()=>history.value?.rows??[]);
const bounds=computed(()=>{const lo=Math.min(...rows.value.map(r=>r.l)),hi=Math.max(...rows.value.map(r=>r.h)),pad=Math.max((hi-lo)*.08,hi*.001);return {lo:lo-pad,hi:hi+pad};});
const ticks=computed(()=>[0,1,2,3].map(i=>({y:14+i*60,value:bounds.value.hi-(bounds.value.hi-bounds.value.lo)*i/3})));
const barWidth=computed(()=>Math.min(12,694/Math.max(rows.value.length,1)*.65));
const volumeMax=computed(()=>Math.max(0,...rows.value.map(r=>r.volumeShares??0)));
const path=computed(()=>rows.value.map((r,i)=>`${i?'L':'M'}${x(i)},${y(r.c)}`).join(' '));
function x(index){return 18+(index+.5)/Math.max(rows.value.length,1)*694;}
function y(value){return 194-(value-bounds.value.lo)/(bounds.value.hi-bounds.value.lo)*180;}
function axisNumber(value){return Number(value).toLocaleString('en-US',{maximumFractionDigits:3});}
function day(at){return new Date(at).toLocaleDateString(lang.lang==='en'?'en-US':'zh-CN',{timeZone:'Asia/Hong_Kong',month:'2-digit',day:'2-digit'});}
async function load(){const current=++request;loading.value=true;error.value=false;try{const next=await getEquityChart(props.ticker,props.chain,range.value);if(current===request)history.value=next;}catch{if(current===request)error.value=true;}finally{if(current===request)loading.value=false;}}
watch([()=>props.ticker,()=>props.chain,range],()=>{request++;history.value=null;load();},{immediate:true});
watch(()=>props.stock?.referenceObservedAt,at=>{if(at&&!rows.value.length&&!loading.value)load();});
onMounted(()=>timer=setInterval(()=>{if(!document.hidden&&!loading.value)load();},120000));
onUnmounted(()=>{request++;clearInterval(timer);});
</script>
<style scoped>
.theme-equity-market{margin-bottom:18px}.equity-caption{font-size:12px;color:var(--muted);margin:6px 0;line-height:1.6}.equity-caption a{margin-left:8px;color:var(--accent)}.equity-session{font-size:12px;color:var(--muted);background:var(--bg);padding:5px 10px;border-radius:20px}.equity-quote{display:flex;align-items:baseline;gap:14px;margin-top:12px;flex-wrap:wrap}.equity-quote strong{font-size:30px;letter-spacing:-.6px}.equity-quote small{font-size:11px;color:var(--muted)}.equity-metrics{display:flex;gap:32px;margin:14px 0;flex-wrap:wrap}.equity-metrics div{display:flex;flex-direction:column;gap:4px}.equity-metrics span{font-size:11px;color:var(--muted)}.equity-metrics strong{font-size:14px}.equity-controls{display:flex;align-items:center;gap:8px;margin-top:18px;flex-wrap:wrap}.equity-controls div{display:flex;gap:4px;margin-right:auto}.equity-controls button,.equity-controls select{font:inherit;font-size:12px;padding:5px 9px;border:1px solid var(--border);background:var(--panel);color:var(--muted);border-radius:5px;cursor:pointer}.equity-controls button[aria-pressed=true]{background:var(--bg);color:var(--accent);border-color:var(--accent)}.equity-chart{width:100%;height:auto;max-height:320px;display:block;margin-top:14px;overflow:visible}.equity-grid line{stroke:var(--border);stroke-width:.7}.equity-axis text{fill:var(--muted);font-size:12px}.equity-bars line{stroke-width:1}.equity-up{fill:var(--up,#229b7c);stroke:var(--up,#229b7c)}.equity-down{fill:var(--down,#dc5866);stroke:var(--down,#dc5866)}.equity-line{fill:none;stroke:var(--accent);stroke-width:2;vector-effect:non-scaling-stroke}.equity-volume{fill:var(--muted);opacity:.2}.equity-empty{padding:32px 0;font-size:13px;color:var(--muted)}.equity-old{color:var(--muted)}@media(max-width:600px){.equity-chart{min-height:150px}.equity-quote strong{font-size:26px}.equity-metrics{gap:18px}}
</style>
