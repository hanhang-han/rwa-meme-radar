<template>
  <article class="event-row" :class="`kind-${kind}`" :data-navigation-anchor="navigationAnchor || 'event:'+eventKey(ev)">
    <span class="event-marker" aria-hidden="true"></span>
    <div class="event-top"><time :datetime="isoTime" :title="date(ev.t)">{{ timeLabel }}</time><span class="event-type">{{ kindLabel }}</span><span v-if="records.length>1" class="event-count" :title="tr('同日、同资产和同池的原始记录数；不表示交易笔数','Raw records for the same day, asset and pool; separate from trade counts')">{{ records.length }} {{ tr('条记录','records') }}</span></div>
    <div class="event-identity">
      <RouterLink v-if="asset" :to="detailTo" class="event-asset">{{ ev.symbol || short(asset) }}</RouterLink><strong v-else>{{ ev.symbol || tr('未知资产','Unknown asset') }}</strong>
      <RouterLink v-if="ticker" :to="themeNavigationLink(ticker,{route,scope})" class="event-ticker">{{ ticker }}</RouterLink>
      <span v-if="kind==='discovered' && ev.match?.ticker" class="event-hint">{{ tr('名称匹配','Name match') }}</span>
    </div>
    <p class="event-context" :title="tr('交易池流动性以USD计；历史值使用记录的实际观测，缺失不表示零','Pool liquidity is in USD; historical values retain recorded observations, missing does not mean zero')"><RouterLink :to="chainTo">{{ chainName({chainId}) }}</RouterLink><span v-if="fact"> · {{ fact }}</span></p>
    <details class="event-raw" :open="expanded" @toggle="emit('update:expanded',$event.target.open)"><summary>{{ tr('记录详情','Record details') }} <span aria-hidden="true">▾</span></summary>
      <RouterLink v-if="pool && asset" :to="pairTo">{{ tr('查看配对依据','View pair evidence') }} →</RouterLink>
      <dl class="event-evidence"><div v-if="asset"><dt>{{ tr('资产地址','Asset address') }}</dt><dd>{{ asset }} <button type="button" @click="copy(asset)">{{ copied===asset?tr('已复制','Copied'):tr('复制','Copy') }}</button> <a :href="explorer(asset,'address',chainId)" target="_blank" rel="noopener">↗</a></dd></div><div v-if="pool"><dt>{{ tr('交易池','Pool') }}</dt><dd>{{ pool }} <button type="button" @click="copy(pool)">{{ copied===pool?tr('已复制','Copied'):tr('复制','Copy') }}</button> <a :href="explorer(pool,'address',chainId)" target="_blank" rel="noopener">↗</a></dd></div><div v-if="ev.poolCreatedAt"><dt>{{ tr('池创建','Pool created') }}</dt><dd>{{ date(ev.poolCreatedAt) }}</dd></div></dl>
      <ol><li v-for="record in records" :key="eventKey(record)"><time :title="date(record.t)">{{ date(record.t) }}</time><span>{{ record.label || recordLabel(record.kind) }}</span></li></ol>
    </details>
  </article>
</template>

<script setup>
import { computed, ref } from 'vue';
import { useRoute } from 'vue-router';
import { assetNavigationLink, themeNavigationLink, pageNavigationLink } from '../utils/navigation-context';
import { tr, useI18n } from '../i18n';
import { age, chainName, date, explorer, short, usd } from '../utils/format';
import { eventKey, normalizeEventAddress } from '../utils/event-records';
import { useMinuteClock } from '../composables/useMinuteClock';

const props=defineProps({ev:{type:Object,required:true},records:{type:Array,default:()=>[]},scope:{type:String,default:'all'},back:{type:String,default:''},expanded:Boolean,navigationAnchor:{type:String,default:''}});
const emit=defineEmits(['update:expanded']);
const route=useRoute();
const {lang}=useI18n();
const chainId=computed(()=>String(props.ev.chainId??'196'));
const asset=computed(()=>normalizeEventAddress(props.ev.asset??props.ev.token??props.ev.relation?.token,chainId.value));
const pool=computed(()=>props.ev.pool??props.ev.relation?.pool);
const ticker=computed(()=>props.ev.ticker??props.ev.relation?.ticker??props.ev.match?.ticker);
const copied=ref(''),now=useMinuteClock();
async function copy(value){try{await navigator.clipboard.writeText(value);copied.value=value;setTimeout(()=>copied.value='',1500);}catch{copied.value='';}}
const fact=computed(()=>{const relation=props.ev.relation??{},dex=props.ev.dex??relation.protocol,liq=props.ev.liquidityUsd??relation.liquidityUsd;const at=Number(relation.liquidityAt);return [dex,pool.value?short(pool.value):null,ticker.value?tr('与 '+ticker.value+' 配对','paired with '+ticker.value):null,liq!=null?tr((at&&now.value-at<=900000?'池流动性 ':'历史池流动性 ')+usd(liq),(at&&now.value-at<=900000?'Pool liquidity ':'Historical pool liquidity ')+usd(liq)):null].filter(Boolean).join(' · ');});
const kind=computed(()=>props.ev.kind??'other');
const kindLabel=computed(()=>recordLabel(kind.value));
const detailTo=computed(()=>assetNavigationLink({chainId:chainId.value,token:asset.value},{route,scope:props.scope}));
const pairTo=computed(()=>assetNavigationLink({chainId:chainId.value,token:asset.value},{route,scope:props.scope,tab:'relation',pool:pool.value}));
const chainTo=computed(()=>route.path==='/events'?{path:'/events',query:{...route.query,chain:chainId.value}}:pageNavigationLink('/events',{route,scope:chainId.value}));
const observedAt=computed(()=>{const time=Number(props.ev.t);return time>0&&Number.isFinite(time)&&Number.isFinite(new Date(time).getTime())?new Date(time):null;});
const isoTime=computed(()=>observedAt.value?.toISOString());
const timeLabel=computed(()=>{now.value;return observedAt.value?age(observedAt.value.getTime()):'—';});
function recordLabel(value){
  if(value==='discovered')return tr('首次收录','First indexed');
  if(['verified','relation-verified'].includes(value))return tr('同池关系记录','Pool relation recorded');
  if(value==='invalidated')return tr('关系证据变化','Relationship evidence changed');
  if(value==='pool-created')return tr('池创建记录','Pool creation record');
  if(['pair-observed','relation-observed'].includes(value))return tr('交易池记录','Pool recorded');
  return tr('其他记录','Other record');
}
</script>

<style scoped>
.event-row{position:relative;padding:13px 0 15px;border-bottom:1px solid var(--border);min-width:0}.event-row:last-child{border-bottom:0}.event-marker{position:absolute;left:-24px;top:18px;width:10px;height:10px;background:var(--surface-raised);border:2px solid var(--accent);border-radius:50%}.kind-verified .event-marker,.kind-relation-verified .event-marker{border-radius:2px;background:var(--accent)}.kind-invalidated .event-marker{border-color:var(--warning);transform:rotate(45deg)}.event-top,.event-identity{display:flex;align-items:center;flex-wrap:wrap;gap:7px 10px}.event-top{font-size:12px;color:var(--muted)}.event-type{color:var(--text)}.event-count{padding:2px 6px;border-radius:4px;background:var(--surface-raised)}.event-identity{margin-top:6px}.event-asset{font-size:15px;color:var(--text);font-weight:650}.event-ticker,.event-hint{font-size:12px;color:var(--accent);padding:2px 6px;border:1px solid var(--border);border-radius:4px}.event-hint{color:var(--muted)}.event-context{margin:5px 0 0;font-size:12px;color:var(--muted)}.event-raw{margin-top:9px;font-size:12px}.event-raw summary{width:max-content;color:var(--muted);cursor:pointer;list-style:none}.event-raw summary::-webkit-details-marker{display:none}.event-raw summary span{font-size:12px}.event-evidence{display:grid;gap:5px;margin:10px 0;font-size:12px;color:var(--muted)}.event-evidence div{display:grid;grid-template-columns:75px minmax(0,1fr);gap:8px}.event-evidence dd{overflow-wrap:anywhere;color:var(--text)}.event-raw ol{margin:9px 0 0;padding:6px 0 6px 16px;border-left:1px solid var(--border);list-style:none}.event-raw li{display:grid;grid-template-columns:minmax(130px,auto) minmax(0,1fr);gap:3px 12px;margin:6px 0;color:var(--muted)}@media(max-width:600px){.event-raw li{grid-template-columns:1fr}}
.event-context a,.event-evidence a {color:var(--text);text-decoration:underline;text-underline-offset:3px;}.event-evidence button{background:none;border:0;color:var(--text);font:inherit;cursor:pointer;text-decoration:underline;padding:0 4px;}
</style>
