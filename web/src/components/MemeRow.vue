<template>
  <tr :data-asset="a.token" :data-asset-key="assetKey" :class="{ 'v2-member-row':child, 'is-stale':isStale, 'is-selected':selected }" class="meme-clickable-row" tabindex="0" role="link" :aria-label="tr('查看资产详情', 'View asset details') + ' ' + (a.name || a.symbol || short(a.token))" @click="openDetail" @keydown.enter="openDetail">
    <td class="meme-identity-cell">
      <div class="meme-name-line"><RouterLink :to="contextDetailLink(a)"><strong>{{ a.name || a.symbol || short(a.token) }}</strong></RouterLink></div>
      <div class="meme-address-line"><button type="button" class="chain-filter" @click="router.push({query:{...route.query,chain:String(a.chainId),page:undefined}})">{{ chainName(a) }}</button><code>{{ short(a.token) }}</code><button type="button" :aria-label="tr('复制合约地址','Copy contract address')" @click="copyAddress">⧉</button><a :href="explorer(a.token,'address',String(a.chainId))" target="_blank" rel="noopener" :aria-label="tr('在浏览器查看合约','View contract in explorer')">↗</a><small v-if="copied">{{ tr('已复制','Copied') }}</small></div>
      <button v-if="extra > 0" class="v2-group-toggle" @click="emit('toggle-group')">+{{ extra }} {{ tr('个同名合约', 'same-name contracts') }}</button>
    </td>
    <td :title="fieldTitle('price')" data-field="price" :class="{ 'kpi-flash':flashKeys.has('price') }"><LiveNumber :value="observation('price').value" :currency="observation('price').currency" format="price" :label="fieldLabel('price')" :description="fieldTitle('price')" /><small v-if="fieldTag('price')" class="meme-field-meta" :class="{ 'is-historical':fieldState('price')==='historical' }">{{ fieldTag('price') }}</small></td>
    <td :title="fieldTitle('change24h')" :class="fieldState('change24h')==='current'?(Number(observation('change24h').value) > 0 ? 'up' : Number(observation('change24h').value) < 0 ? 'down' : ''):''"><LiveNumber :value="observation('change24h').value" format="percent" :label="fieldLabel('change24h')" :description="fieldTitle('change24h')" /><small v-if="fieldTag('change24h')" class="meme-field-meta" :class="{ 'is-historical':fieldState('change24h')==='historical' }">{{ fieldTag('change24h') }}</small><details v-if="full" class="meme-window-changes"><summary>{{ tr('其他窗口','Other windows') }}</summary><span v-for="[field,label] in [['change5m','5m'],['change1h','1h'],['change6h','6h']]" :key="field" :title="fieldTitle(field)">{{ label }} <LiveNumber :value="observation(field).value" format="percent" /><small v-if="fieldTag(field)">{{ fieldTag(field) }}</small></span></details></td>
    <td :title="fieldTitle('volume24h')" data-field="volume24h" :class="{ 'kpi-flash':flashKeys.has('volume24h') }"><div v-if="volumeBar!=null" class="meme-value-bar"><span :style="{width:`${volumeBar}%`}"></span></div><LiveNumber :value="observation('volume24h').value" :currency="observation('volume24h').currency" :label="fieldLabel('volume24h')" :description="fieldTitle('volume24h')" /><small v-if="fieldTag('volume24h')" class="meme-field-meta" :class="{ 'is-historical':fieldState('volume24h')==='historical' }">{{ fieldTag('volume24h') }}</small></td>
    <td :title="fieldTitle('totalLiquidityUsd')" data-field="totalLiquidityUsd" :class="{ 'kpi-flash':flashKeys.has('totalLiquidityUsd') }"><div v-if="liquidityBar!=null" class="meme-value-bar"><span :style="{width:`${liquidityBar}%`}"></span></div><LiveNumber :value="observation('totalLiquidityUsd').value" currency="USD" :label="fieldLabel('totalLiquidityUsd')" :description="fieldTitle('totalLiquidityUsd')" /><small v-if="fieldTag('totalLiquidityUsd')" class="meme-field-meta" :class="{ 'is-historical':fieldState('totalLiquidityUsd')==='historical' }">{{ fieldTag('totalLiquidityUsd') }}</small></td>
    <td class="meme-relations-cell">
      <template v-if="relOf.length">
        <span v-for="r in relOf.slice(0,3)" :key="r.ticker" class="meme-relation"><RouterLink :to="themeNavigationLink(r.ticker,{route,scope:route.query.chain})">{{ r.ticker }}</RouterLink><RelationBadge :relation="r" /></span>
        <small v-if="relOf.length > 3" :title="tr('其余已匹配股票主题数量','Additional matched stock themes')">+{{ relOf.length - 3 }}</small>
      </template>
      <template v-else>—</template>
    </td>
    <td v-if="full" :title="tr('24h 美元成交 / 总流动性（倍）；要求同源同期数据，缺失时不计算','24h USD volume / total liquidity (multiple); requires matching source and time, unavailable if data is missing')">{{ ratio == null ? '—' : `${ratio.toFixed(2)}×` }}<small class="meme-field-meta" :title="tr('24h 买入笔数占买卖总笔数的比例；缺失值不表示零','24h buys divided by total buy and sell counts; missing does not mean zero')">{{ tr('买入占比','Buy share') }} {{ buyRatio==null?'—':(buyRatio*100).toFixed(1)+'%' }}</small></td>
    <td v-if="full" :title="fieldTitle('holders')"><LiveNumber :value="observation('holders').value" format="number" :label="fieldLabel('holders')" :description="fieldTitle('holders')" /><small v-if="fieldTag('holders')" class="meme-field-meta" :class="{ 'is-historical':fieldState('holders')==='historical' }">{{ fieldTag('holders') }}</small></td>
    <td><RiskBadge :asset="a" /></td>
    <td v-if="full" class="meme-updated" :title="date(observedAt)">{{ age(observedAt) }}</td>
  </tr>
</template>

<script setup>
import { computed, reactive, ref, watchEffect } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import LiveNumber from './LiveNumber.vue';
import RelationBadge from './RelationBadge.vue';
import RiskBadge from './RiskBadge.vue';
import { tr } from '../i18n';
import { age, chainName, date, explorer, short } from '../utils/format';
import { assetNavigationLink, themeNavigationLink } from '../utils/navigation-context';
import { directoryObservation } from '../utils/meme-directory-presentation';
import { memeStockRelations } from '../utils/meme-scan-signals';
import { useMinuteClock } from '../composables/useMinuteClock';
import { comparableVolume, isRecentObservation } from '../utils/stock-theme-model';
import { currentBuyRatio, currentVolumeLiquidityRatio } from '../utils/meme-row-metrics';

const props=defineProps({ a:{type:Object,required:true}, relations:{type:Array,default:()=>[]}, store:{type:Object,required:true}, child:{type:Boolean,default:false}, extra:{type:Number,default:0}, full:{type:Boolean,default:false}, selected:{type:Boolean,default:false}, volumeMax:{type:Number,default:0}, liquidityMax:{type:Number,default:0} });
const router=useRouter(),route=useRoute(),now=useMinuteClock();
function contextDetailLink(asset){return assetNavigationLink(asset,{route,scope:route.query.chain});}
const emit=defineEmits(['toggle-group']);
function openDetail(event) {
  if (event.target !== event.currentTarget && event.target.closest?.('a, button, input, select, textarea, summary')) return;
  router.push(contextDetailLink(props.a));
}
const relOf=computed(()=>memeStockRelations(props.a,props.relations,Math.max(now.value,Date.now())));
const ratio=computed(()=>currentVolumeLiquidityRatio(props.a));
const buyRatio=computed(()=>currentBuyRatio(props.a));
const assetKey=computed(()=>`${props.a.chainId}:${String(props.a.token).toLowerCase()}`);
const volumeBar=computed(()=>{const value=comparableVolume(props.a);return value!=null&&props.volumeMax>0?Math.max(0,Math.min(100,value/props.volumeMax*100)):null;});
const liquidityBar=computed(()=>{const asset=props.a,value=Number(asset.totalLiquidityUsd);return asset.totalLiquidityUsd!=null&&Number.isFinite(value)&&value>=0&&asset.totalLiquidityStatus==='current'&&isRecentObservation(asset.totalLiquidityAt ?? asset.fieldTimes?.totalLiquidityUsd,1800000)&&props.liquidityMax>0?Math.max(0,Math.min(100,value/props.liquidityMax*100)):null;});
const observedAt=computed(()=>observation('price').at);
const isStale=computed(()=>observation('price').state!=='current');
const observation=field=>directoryObservation(props.a,field,Math.max(now.value,Date.now()));
function fieldAt(field){return observation(field).at;}
function fieldSource(field){return observation(field).source;}
function fieldState(field){return observation(field).state;}
function fieldTag(field){const item=observation(field),state=item.state;return [item.value!=null&&['price','volume24h'].includes(field)&&!item.currency?tr('币种待确认','Currency unverified'):'',item.value!=null&&field==='volume24h'&&!item.scopeKnown?tr('成交范围待核实','Volume scope unverified'):'',state==='historical'?tr('历史','Historical'):state==='unverified'?tr('待核实','Unverified'):state==='unknown-time'?tr('时间未知','Time unknown'):''].filter(Boolean).join(' · ');}
function fieldSummary(field) {
  const state=fieldState(field);
  if (state==='missing') return tr('暂无数据', 'Unavailable');
  const source=fieldSource(field) ?? tr('来源未能识别', 'Source unknown');
  const status=state==='historical' ? `${tr('历史值', 'Historical')} · ${age(fieldAt(field))}`
    : state==='unverified' ? tr('未能识别', 'Unknown')
    : state==='unknown-time' ? tr('时间未能识别', 'Time unknown') : age(fieldAt(field));
  return `${source} · ${status}`;
}
function fieldLabel(field) {
  const labels={price:['代币报价','Token price'],change24h:['24h 涨跌（%）','24h change (%)'],volume24h:['资产 24h 成交','Asset volume 24h'],totalLiquidityUsd:['总流动性（USD）','Total liquidity (USD)'],holders:['持币地址数','Holder addresses'],change5m:['5m 涨跌（%）','5m change (%)'],change1h:['1h 涨跌（%）','1h change (%)'],change6h:['6h 涨跌（%）','6h change (%)']};
  return labels[field]?tr(...labels[field]):field;
}
function fieldTitle(field) {
  const item=observation(field);
  const scope=field==='volume24h'?(item.scopeKnown?tr('资产范围，非股票配对池成交','Asset scope; not stock-pair volume'):tr('成交范围待核实','Volume scope unverified')):'';
  return [fieldLabel(field),item.currency||(['price','volume24h'].includes(field)?tr('币种待确认','Currency unverified'):''),fieldSummary(field),`${tr('观测时间','Observed at')} ${date(item.at)}`,scope].filter(Boolean).join(' · ');
}
const copied=ref(false);
let copyTimer;
async function copyAddress() { try { await navigator.clipboard.writeText(props.a.token); copied.value=true; clearTimeout(copyTimer); copyTimer=setTimeout(()=>copied.value=false,1500); } catch {} }
const flashKeys=reactive(new Set());
watchEffect(()=>{ for(const field of ['price','volume24h','totalLiquidityUsd']) { const key=`${props.a.chainId}:${props.a.token}:${field}`; const value=props.a[field]; if(props.store.diffCells(key,String(value??''))&&value!=null){flashKeys.add(field);setTimeout(()=>flashKeys.delete(field),600);} } });
</script>

<style scoped>
.meme-name-line{white-space:normal;overflow-wrap:anywhere}.meme-relations-cell{min-width:0;max-width:210px}.meme-relation{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;margin:3px 0;}
.meme-field-meta { display:block; margin-top:3px; color:var(--muted); font-size:12px; font-weight:400; white-space:nowrap; }
.meme-field-meta.is-historical { color:var(--warning); }
.meme-value-bar { height:3px; width:100%; background:var(--surface-raised); border-radius:999px; margin-bottom:5px; overflow:hidden; }
.meme-value-bar span { display:block; height:100%; background:var(--accent); border-radius:inherit; }
.meme-clickable-row.is-selected td { background:color-mix(in srgb,var(--accent) 9%,var(--panel)); }
.chain-filter {border:0;background:none;color:var(--text);font:inherit;text-decoration:underline;cursor:pointer;padding:0;}
.meme-window-changes {font-size:12px;color:var(--muted);margin-top:5px;}.meme-window-changes summary {cursor:pointer;}.meme-window-changes>span {display:block;white-space:nowrap;margin-top:3px;}
</style>
