<template>
  <section class="trade-activity" aria-labelledby="activity-title">
    <div class="activity-heading">
      <div><h3 id="activity-title" :title="tr('下方已加载成交按实际成交时间分布；不代表全天成交。','The loaded trades below, placed by actual trade time; not a full day of volume.')">{{ tr('成交时间分布', 'Trade timeline') }}</h3><p :title="tr('只统计此页已加载记录，未加载成交不计入。','Only records loaded on this page are counted; unloaded trades are excluded.')">{{ tr(`${trades.length} 笔已加载`, `${trades.length} loaded trades`) }} · {{ scope }}</p></div>
    </div>
    <template v-if="valid.length">
      <div class="activity-plot" role="group" :aria-label="tr('按实际成交时间排列的已加载记录', 'Loaded records by trade time')">
        <div v-for="lane in lanes" :key="lane.key" class="activity-lane"><span>{{ lane.label }}</span><div class="activity-track">
          <button v-for="item in points.filter(p => p.lane === lane.key)" :key="item.key" type="button" :style="{left:`${item.position}%`}" class="activity-point" :class="lane.key" :title="`${date(item.trade.t)} · ${lane.label}`" :aria-label="`${date(item.trade.t)} · ${lane.label}`" @click="showTrade(item.key)">{{ lane.key === 'buy' ? '▲' : lane.key === 'sell' ? '▼' : '●' }}</button>
        </div></div>
        <div class="activity-axis"><span :title="tr('最早已加载成交 · 本地时间','Earliest loaded trade · local time')">{{ date(start) }}</span><span :title="tr('最近已加载成交 · 本地时间','Latest loaded trade · local time')">{{ date(end) }}</span></div>
      </div>
      <div class="activity-counts" :aria-label="tr('已加载记录的买卖笔数', 'Buy and sell counts in loaded records')">
        <div class="activity-count-bar"><span v-if="counts.buy" class="buy" :style="{width:`${counts.buy / trades.length * 100}%`}"></span><span v-if="counts.sell" class="sell" :style="{width:`${counts.sell / trades.length * 100}%`}"></span><span v-if="counts.unknown" class="unknown" :style="{width:`${counts.unknown / trades.length * 100}%`}"></span></div>
        <p :title="tr('已加载记录的买入 / 卖出笔数；未识别方向的成交单列。','Buy and sell counts in loaded records; trades with unidentified direction are listed separately.')">{{ tr('买入', 'Buys') }} {{ counts.buy }} {{ tr('笔','trades') }} · {{ tr('卖出', 'Sells') }} {{ counts.sell }} {{ tr('笔','trades') }}<template v-if="counts.unknown"> · {{ tr('方向未知', 'Unknown') }} {{ counts.unknown }} {{ tr('笔','trades') }}</template></p>
      </div>
      <p v-if="valid.length < trades.length" class="activity-omission" role="status">{{ tr(`${trades.length-valid.length} 笔缺少时间，未绘制`, `${trades.length-valid.length} without time; omitted`) }}</p>
      <details class="activity-notes"><summary>{{ tr('统计范围说明', 'Coverage notes') }}</summary><p>{{ tr('时间图和买卖笔数仅统计下方已加载的成交，不代表全天或全市场。', 'The timeline and buy/sell counts cover only the trades loaded below, not the entire day or market.') }}</p></details>
    </template>
    <p v-else class="activity-empty" role="status">{{ trades.length ? tr(`${trades.length} 笔缺少有效成交时间，无法绘制`, `${trades.length} loaded trades have no valid time to plot`) : tr('暂无已加载成交', 'No trades loaded yet') }}</p>
  </section>
</template>
<script setup>
import {computed} from 'vue';
import {tr} from '../i18n';
import {date} from '../utils/format';
import {tradeKey} from '../stores/feed';
const props=defineProps({trades:{type:Array,required:true},scope:{type:String,default:''}});
const lanes=computed(()=>[{key:'buy',label:tr('买入','Buy')},{key:'sell',label:tr('卖出','Sell')},{key:'unknown',label:tr('未知','Unknown')}]);
const valid=computed(()=>props.trades.filter(t=>Number.isFinite(Number(t.t))&&Number(t.t)>0));
const start=computed(()=>Math.min(...valid.value.map(t=>Number(t.t))));
const end=computed(()=>Math.max(...valid.value.map(t=>Number(t.t))));
const points=computed(()=>valid.value.map(t=>({trade:t,key:encodeURIComponent(tradeKey(t)),lane:t.type==='buy'||t.type==='sell'?t.type:'unknown',position:end.value===start.value?50:Math.max(1,Math.min(99,(Number(t.t)-start.value)/(end.value-start.value)*98+1))})));
const counts=computed(()=>props.trades.reduce((out,t)=>{out[t.type==='buy'||t.type==='sell'?t.type:'unknown']++;return out;},{buy:0,sell:0,unknown:0}));
function showTrade(key){const row=document.getElementById('trade-'+key);row?.focus();row?.scrollIntoView({block:'center',behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});}
</script>
<style scoped>
.trade-activity{min-width:0;padding:18px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border);background:transparent}
.activity-heading h3{margin:0 0 4px;font-family:var(--display-font,Georgia,serif);font-size:18px;font-weight:600}
.activity-heading p,.activity-counts p{margin:0;color:var(--muted);font-size:11px}
.activity-plot{margin:20px 0 13px}
.activity-lane{display:grid;grid-template-columns:68px minmax(0,1fr);align-items:center;min-height:39px;color:var(--muted);font-size:11px}
.activity-track{position:relative;height:28px;border-bottom:1px solid var(--border);background:linear-gradient(90deg,transparent 49.8%,var(--border) 50%,transparent 50.2%)}
.activity-point{position:absolute;top:3px;transform:translateX(-50%);display:grid;place-items:center;width:20px;height:20px;padding:0;border:0;border-radius:50%;background:var(--bg);font-size:11px;cursor:pointer}
.activity-point.buy{color:var(--up)}
.activity-point.sell{color:var(--down)}
.activity-point.unknown{color:var(--muted)}
.activity-point:hover{box-shadow:0 0 0 4px var(--accent-soft);z-index:2}
.activity-point:focus-visible{outline:2px solid var(--accent);outline-offset:2px;z-index:2}
.activity-axis{display:flex;justify-content:space-between;gap:10px;margin-left:68px;color:var(--muted);font-size:10px}
.activity-axis span{min-width:0;overflow-wrap:anywhere}
.activity-axis span:last-child{text-align:right}
.activity-counts{margin-top:18px}
.activity-count-bar{display:flex;overflow:hidden;height:12px;background:var(--border)}
.activity-count-bar .buy{background:var(--up)}
.activity-count-bar .sell{background:var(--down)}
.activity-count-bar .unknown{background:var(--muted)}
.activity-counts p{margin-top:8px}
.activity-omission,.activity-notes,.activity-empty{color:var(--muted);font-size:11px;line-height:1.6}
.activity-omission{margin:10px 0 0}
.activity-empty{margin:14px 0 0}
.activity-notes{margin-top:10px}
.activity-notes summary{cursor:pointer}
.activity-notes p{margin:6px 0 0}
@media(max-width:700px){.activity-lane{grid-template-columns:48px minmax(0,1fr)}.activity-axis{margin-left:48px}.activity-axis span{font-size:10px}}
</style>
