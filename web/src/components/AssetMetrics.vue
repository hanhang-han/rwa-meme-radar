<template>
  <div class="asset-metrics">
    <div class="metric-glance">
      <div :title="metricTitle(metrics.fdvUsd,tr('完全稀释估值，单位 USD；不是流通市值。','Fully diluted valuation in USD; not circulating market cap.'))"><span>FDV · USD</span><strong>{{ usd(current(metrics.fdvUsd)) }}</strong></div>
      <div :title="metricTitle(metrics.poolAgeHours,tr('主池自已确认链上创建时间起的时长。','Main-pool age since its confirmed onchain creation.'))"><span>{{ tr('主池池龄','Main-pool age') }}</span><strong>{{ poolAge }}</strong></div>
      <div :title="metricTitle(metrics.buyShare24h,tr('近 24 小时买入笔数 ÷ 买卖总笔数；只在统计范围可比时显示。','24h buy count divided by total buy and sell count; shown only with comparable coverage.'))"><span>{{ tr('买入笔数占比 · 24h','Buy count share · 24h') }}</span><strong>{{ share == null ? '—' : share.toFixed(1)+'%' }}</strong><i v-if="share!=null" class="share-track"><i :style="{width:share+'%'}"></i></i></div>
    </div>
    <template v-if="changeRows.some(r=>r.value!=null)"><p class="metric-caption">{{ tr('主池价格涨跌 · %','Main-pool price change · %') }}</p><div class="metric-changes"><div v-for="row in changeRows" :key="row.window" :title="metricTitle(row.metric,tr('主池报价相对该时间窗口起点的涨跌百分比。','Main-pool price change from the start of this window.'))"><span>{{ row.window }}</span><strong :class="row.value>0?'up':row.value<0?'down':''">{{ pct(row.value) }}</strong></div></div></template>
    <p class="metric-caption">{{ tr('— 表示当前数据不足或已过期','— means insufficient or expired current data') }}</p>
    <div class="exit-impact">
      <div class="exit-heading"><span :title="tr('选择美元卖出金额，估算平均成交所得相对当前参考价值的损失。','Select a USD sale amount to estimate the reduction in average proceeds relative to the current reference value.')">{{ tr('卖出估算 · USD','Estimated sale impact · USD') }}</span><div role="group" :aria-label="tr('卖出金额','Sale amount')"><button v-for="value in amounts" :key="value" :aria-pressed="amount===value" :title="tr('按 '+value+' 美元卖出金额估算','Estimate for a sale amount of '+value+' USD')" @click="amount=value">${{ value>=1000?value/1000+'K':value }}</button></div></div>
      <strong v-if="impactValue!=null" :title="tr('平均成交影响，单位 %；估算时间','Average proceeds impact in %; estimated at')+' '+date(displayImpact?.at)">{{ impactValue.toFixed(2) }}%<small>{{ tr('平均成交影响','Average proceeds impact') }}</small></strong>
      <p v-else class="hint" role="status">{{ impactMessage }}</p>
      <button v-if="impact?.status==='unsupported' && metrics.mainPool" class="quoter-button" :disabled="quoteLoading" @click="loadQuote">{{ quoteLoading ? tr('读取报价…','Reading quote…') : quoteObservation.expired ? tr('更新报价','Refresh quote') : tr('读取 V3 / V4 报价','Read V3 / V4 quote') }}</button>
      <details>
        <summary>{{ tr('计算依据','Calculation basis') }}</summary>
        <p v-if="quoteObservation.isQuoter">{{ quoteObservation.quotedMethod==='v4-quoter' ? 'V4 Quoter' : 'V3 Quoter' }} · {{ tr('使用已确认的链上只读报价，与同一时间窗口的美元行情比较；包含池费率，不含代币税和路由费用。行情来源间的价格差也可能影响结果。','Uses a verified onchain quote and USD quotes from an aligned window; includes pool fees, excludes token taxes and routing costs. Price differences across sources may also affect the result.') }}</p>
        <p v-else-if="displayImpact?.method==='v2-balanced-reserves-estimate' && displayImpact?.status!=='unsupported'">{{ tr('已确认 V2 池的两侧流动性与费率估算；包含池手续费，不含代币税和其他路由费用。实际成交以 DEX 报价为准。','Uses confirmed V2 pool liquidity and fees; includes pool fees, excludes token taxes and other routing costs. Consult the DEX for an executable quote.') }}</p>
        <p v-else>{{ tr('取得有效的池流动性、费率或链上报价后，才会显示估算。','An estimate requires valid pool liquidity and fees or an onchain quote.') }}</p>
        <p v-if="displayImpact?.at">{{ displayImpact.source || '—' }} · {{ date(displayImpact.at) }} · {{ tr('池','Pool') }} {{ short(displayImpact.pool) }}</p>
        <p v-if="quoteObservation.isQuoter && displayImpact?.validUntil">{{ tr('报价有效至','Quote valid until') }} {{ date(displayImpact.validUntil) }}<template v-if="quoteObservation.expired"> · {{ tr('已过期','Expired') }}</template></p>
        <p v-if="displayImpact?.verificationUrl"><a :href="displayImpact.verificationUrl" target="_blank" rel="noopener">{{ tr('报价来源依据','Quoter verification') }} ↗</a></p>
        <p v-if="displayImpact?.feeSourceUrl"><a :href="displayImpact.feeSourceUrl" target="_blank" rel="noopener">{{ tr('费率依据','Fee source') }} ↗</a></p>
      </details>
    </div>
  </div>
</template>
<script setup>
import {computed,onBeforeUnmount,onMounted,ref,watch} from 'vue';
import {tr} from '../i18n';
import {date,pct,short,usd} from '../utils/format';
import {getJSON} from '../api/client';
import {exitImpactObservation} from '../utils/exit-impact-observation';
const props=defineProps({metrics:{type:Object,default:()=>({})},now:{type:Number,default:()=>Date.now()},chain:String,token:String});
const amounts=[100,1000,10000],amount=ref(1000);
const quoter=ref(null),quoteLoading=ref(false),quoteMessage=ref('');let request=0;
const clock=ref(Date.now());let clockTimer;
onMounted(()=>{clockTimer=setInterval(()=>{clock.value=Date.now();},1000);});
onBeforeUnmount(()=>{clearInterval(clockTimer);request++;});
watch(()=>[props.chain,props.token,props.metrics.mainPool,amount.value],()=>{request++;quoter.value=null;quoteMessage.value='';quoteLoading.value=false;});
async function loadQuote(){const id=++request;quoteLoading.value=true;quoteMessage.value='';try{const packet=await getJSON(`v2/assets/${encodeURIComponent(props.chain)}/${encodeURIComponent(props.token)}/exit-impact?${new URLSearchParams({pool:props.metrics.mainPool,sellUsd:String(amount.value)})}`,15000);if(id!==request)return;clock.value=Date.now();quoter.value=packet;if(packet.status!=='current')quoteMessage.value=packet.reason==='verified-quoter-unconfigured'?tr('此池报价服务尚未接入','This pool has no configured Quoter'):tr('当前报价条件不足，暂未计算','A current quote is unavailable');}catch{if(id===request)quoteMessage.value=tr('报价暂时无法读取','The quote could not be loaded');}finally{if(id===request)quoteLoading.value=false;}}
function current(metric){return metric?.status==='current' && typeof metric.value==='number' && Number.isFinite(metric.value) && typeof metric.at==='number' && metric.at<=clock.value && clock.value-metric.at<=1_800_000 ? metric.value : null;}
const share=computed(()=>current(props.metrics.buyShare24h));
const poolAge=computed(()=>{const m=props.metrics.poolAgeHours;if(m?.status!=='current'||!(m.at>0&&m.at<=clock.value))return '—';const hours=(clock.value-m.at)/3_600_000;return hours<24?tr(`${Math.floor(hours)} 小时`,`${Math.floor(hours)}h`):tr(`${Math.floor(hours/24)} 天`,`${Math.floor(hours/24)}d`);});
const changeRows=computed(()=>[['5m','m5'],['1h','h1'],['6h','h6'],['24h','h24']].map(([window,key])=>({window,value:current(props.metrics.changes?.[key]),metric:props.metrics.changes?.[key]})));
function metricTitle(metric,description){return `${description} · ${metric?.source||tr('来源待核实','Source unverified')} · ${date(metric?.at)}`;}
const impact=computed(()=>props.metrics.exitImpacts?.[String(amount.value)]??(amount.value===1000?props.metrics.exitImpact1k:null));
const quoteObservation=computed(()=>exitImpactObservation(impact.value,quoter.value,clock.value));
const displayImpact=computed(()=>quoteObservation.value.packet);
const impactValue=computed(()=>quoteObservation.value.valuePercent);
const impactMessage=computed(()=>quoteMessage.value || (quoteObservation.value.expired
  ? tr('估算已过期，请更新报价','The estimate expired; refresh the quote')
  : impact.value?.status==='unsupported' ? tr('此池需要独立报价','This pool needs a Quoter estimate')
  : tr('费率或当前池流动性不足，暂未计算','Current pool liquidity or verified fees are unavailable')));
</script>
<style scoped>
.asset-metrics{margin-top:20px}.metric-glance{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;border-top:1px solid var(--border);padding-top:16px}.metric-glance>div{min-width:0}.metric-glance span,.metric-changes span{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}.metric-glance strong{font-size:16px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.share-track{display:block;height:3px;margin-top:7px;background:var(--border)}.share-track>i{display:block;height:100%;background:var(--gain)}.metric-changes{display:flex;justify-content:space-between;gap:12px;margin-top:18px;font-size:13px}.metric-changes strong{font-variant-numeric:tabular-nums}.exit-impact{margin-top:20px;padding-top:16px;border-top:1px solid var(--border)}.exit-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:12px}.exit-heading>span{color:var(--muted)}.exit-heading button{font-size:12px;border:0;background:none;color:var(--muted);padding:4px 6px;cursor:pointer}.exit-heading button[aria-pressed=true]{color:var(--text);border-bottom:1px solid var(--text)}.exit-impact>strong{display:block;margin-top:12px;font-size:24px;font-variant-numeric:tabular-nums}.exit-impact>strong small{margin-left:8px;font-size:12px;font-weight:400;color:var(--muted)}.exit-impact details{font-size:12px;color:var(--muted);margin-top:10px;line-height:1.6}.exit-impact summary{cursor:pointer}.exit-impact p{overflow-wrap:anywhere}.exit-impact a{color:inherit;text-decoration:underline}.exit-heading button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.metric-caption{margin:12px 0 0;font-size:11px;line-height:1.5;color:var(--muted)}.metric-caption+.metric-changes{margin-top:8px}.quoter-button{font:inherit;font-size:12px;border:1px solid var(--border);background:var(--panel);color:var(--text);padding:6px 10px;border-radius:4px;cursor:pointer}.quoter-button:disabled{opacity:.6}
</style>
