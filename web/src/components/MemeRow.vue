<template>
  <tr :data-asset="a.token" :class="{ 'v2-member-row':child, 'is-stale':isStale }" class="meme-clickable-row" tabindex="0" role="link" :aria-label="tr('查看资产详情', 'View asset details') + ' ' + (a.name || a.symbol || short(a.token))" @click="openDetail" @keydown.enter="openDetail">
    <td class="meme-identity-cell">
      <div class="meme-name-line"><RouterLink :to="detailLink(a)"><strong>{{ a.name || a.symbol || short(a.token) }}</strong></RouterLink><RouterLink class="meme-detail-link" :to="detailLink(a)">{{ tr('详情', 'Details') }} →</RouterLink></div>
      <div class="meme-address-line"><span>{{ chainName(a) }}</span><code>{{ short(a.token) }}</code><button type="button" :aria-label="tr('复制合约地址','Copy contract address')" @click="copyAddress">⧉</button><small v-if="copied">{{ tr('已复制','Copied') }}</small></div>
      <button v-if="extra > 0" class="v2-group-toggle" @click="emit('toggle-group')">+{{ extra }} {{ tr('个同名合约', 'same-name contracts') }}</button>
    </td>
    <td class="meme-relations-cell">
      <template v-if="relOf.length">
        <span v-for="r in relOf.slice(0,3)" :key="r.id" class="meme-relation"><RouterLink :to="{path:'/stock/'+encodeURIComponent(r.ticker),query:{chain:a.chainId}}">{{ r.ticker }}</RouterLink><RelationBadge :relation="r" /></span>
        <small v-if="relOf.length > 3">+{{ relOf.length - 3 }}</small>
      </template>
      <template v-else>—</template>
    </td>
    <td :title="fieldTitle('price')" data-field="price" :class="{ 'kpi-flash':flashKeys.has('price') }"><LiveNumber :value="a.price" :currency="a.priceCurrency ?? ''" format="price" /><small class="meme-field-meta" :class="{ 'is-historical':fieldState('price')==='historical' }" :title="fieldTitle('price')">{{ fieldState('price')==='historical'?tr('历史值','Historical'):fieldState('price')==='missing'?tr('暂无数据','Unavailable'):'' }}</small></td>
    <td :title="fieldTitle('change24h')" :class="Number(a.change24h) > 0 ? 'up' : Number(a.change24h) < 0 ? 'down' : ''"><LiveNumber :value="a.change24h" format="percent" /><small class="meme-field-meta" :class="{ 'is-historical':fieldState('change24h')==='historical' }" :title="fieldTitle('change24h')">{{ fieldState('change24h')==='historical'?tr('历史值','Historical'):fieldState('change24h')==='missing'?tr('暂无数据','Unavailable'):'' }}</small></td>
    <td :title="fieldTitle('volume24h')" data-field="volume24h" :class="{ 'kpi-flash':flashKeys.has('volume24h') }"><LiveNumber :value="a.volume24h" :currency="a.volumeCurrency ?? a.priceCurrency ?? ''" /><small class="meme-field-meta" :class="{ 'is-historical':fieldState('volume24h')==='historical' }" :title="fieldTitle('volume24h')">{{ fieldState('volume24h')==='historical'?tr('历史值','Historical'):fieldState('volume24h')==='missing'?tr('暂无数据','Unavailable'):'' }}</small></td>
    <td :title="fieldTitle('totalLiquidityUsd')" data-field="totalLiquidityUsd" :class="{ 'kpi-flash':flashKeys.has('totalLiquidityUsd') }"><LiveNumber :value="a.totalLiquidityUsd" /><small class="meme-field-meta" :class="{ 'is-historical':fieldState('totalLiquidityUsd')==='historical' }" :title="fieldTitle('totalLiquidityUsd')">{{ fieldState('totalLiquidityUsd')==='historical'?tr('历史值','Historical'):fieldState('totalLiquidityUsd')==='missing'?tr('暂无数据','Unavailable'):'' }}</small></td>
    <td>{{ ratio == null ? '—' : `${Math.round(ratio)}x` }}<small v-if="ratio == null" class="meme-field-meta" :title="tr('需要相同美元口径且处于有效时段的资产 24h 成交额与总流动性', 'Requires comparable, fresh USD asset-wide volume and total liquidity')">{{ tr('未能识别', 'Unknown') }}</small></td>
    <td :title="fieldTitle('holders')"><LiveNumber :value="a.holders" format="number" /><small class="meme-field-meta" :title="fieldTitle('holders')">{{ fieldState('holders')==='historical'?tr('历史值','Historical'):fieldState('holders')==='missing'?tr('暂无数据','Unavailable'):'' }}</small></td>
    <td><RiskBadge :asset="a" /></td>
    <td class="meme-updated" :title="date(observedAt)">{{ age(observedAt) }}</td>
  </tr>
</template>

<script setup>
import { computed, reactive, ref, watchEffect } from 'vue';
import { useRouter } from 'vue-router';
import LiveNumber from './LiveNumber.vue';
import RelationBadge from './RelationBadge.vue';
import RiskBadge from './RiskBadge.vue';
import { tr } from '../i18n';
import { age, chainName, date, detailLink, pairLink, short } from '../utils/format';
import { relationMatchesAsset } from '../utils/relations';
import { relationLevel, volumeLiquidityRatio } from '../utils/product-labels';

const props=defineProps({ a:{type:Object,required:true}, relations:{type:Array,default:()=>[]}, store:{type:Object,required:true}, child:{type:Boolean,default:false}, extra:{type:Number,default:0} });
const router=useRouter();
const emit=defineEmits(['toggle-group']);
function openDetail(event) {
  if (event.target !== event.currentTarget && event.target.closest?.('a, button, input, select, textarea, summary')) return;
  router.push(detailLink(props.a));
}
const relOf=computed(()=>{
  const ranked=props.relations.filter(r=>relationMatchesAsset(r,props.a)).sort((a,b)=>({A:0,C:1,B:2}[relationLevel(a)]??3)-({A:0,C:1,B:2}[relationLevel(b)]??3) || Number(b.liquidityUsd??0)-Number(a.liquidityUsd??0));
  // A stock theme may have several pools for the same token. Show its strongest
  // relationship once in the asset list; the detail page retains every pool.
  const seen=new Set();
  return ranked.filter(r=>{
    const ticker=String(r.ticker??'').toUpperCase();
    if(!ticker || seen.has(ticker)) return false;
    seen.add(ticker);
    return true;
  });
});
const ratio=computed(()=>volumeLiquidityRatio(props.a));
const observedAt=computed(()=>props.a.fieldTimes?.price ?? props.a.quoteAt ?? props.a.updatedAt);
const isStale=computed(()=>!observedAt.value || Date.now()-Number(observedAt.value)>900000);
function fieldAt(field) {
  if (field === 'totalLiquidityUsd') return props.a.totalLiquidityAt ?? props.a.fieldTimes?.totalLiquidityUsd;
  if (field === 'price') return props.a.fieldTimes?.price ?? props.a.quoteAt;
  return props.a.fieldTimes?.[field] ?? null;
}
function fieldSource(field) {
  return props.a.fieldSources?.[field] ?? (field === 'price' ? props.a.provider : field === 'totalLiquidityUsd' ? props.a.totalLiquidityCoverage?.provider : null) ?? null;
}
function fieldState(field) {
  const value=props.a[field];
  if (value==null || !Number.isFinite(Number(value))) return 'missing';
  const at=Number(fieldAt(field));
  const now=Date.now();
  if (!Number.isFinite(at) || at<=0 || at>now+1000) return 'unknown';
  if (field === 'totalLiquidityUsd' && props.a.totalLiquidityStatus === 'stale') return 'historical';
  if (field === 'totalLiquidityUsd' && props.a.totalLiquidityStatus !== 'current') return 'unverified';
  const window=field === 'totalLiquidityUsd' ? 1800000 : 900000;
  return now-at>window ? 'historical' : 'current';
}
function fieldSummary(field) {
  const state=fieldState(field);
  if (state==='missing') return tr('暂无数据', 'Unavailable');
  const source=fieldSource(field) ?? tr('来源未能识别', 'Source unknown');
  const status=state==='historical' ? `${tr('历史值', 'Historical')} · ${age(fieldAt(field))}`
    : state==='unverified' ? tr('未能识别', 'Unknown')
    : state==='unknown' ? tr('时间未能识别', 'Time unknown') : age(fieldAt(field));
  return `${source} · ${status}`;
}
function fieldTitle(field) {
  return `${fieldSource(field) ?? tr('来源未能识别', 'Source unknown')} · ${date(fieldAt(field))}`;
}
const copied=ref(false);
let copyTimer;
async function copyAddress() { try { await navigator.clipboard.writeText(props.a.token); copied.value=true; clearTimeout(copyTimer); copyTimer=setTimeout(()=>copied.value=false,1500); } catch {} }
const flashKeys=reactive(new Set());
watchEffect(()=>{ for(const field of ['price','volume24h','totalLiquidityUsd']) { const key=`${props.a.chainId}:${props.a.token}:${field}`; const value=props.a[field]; if(props.store.diffCells(key,String(value??''))&&value!=null){flashKeys.add(field);setTimeout(()=>flashKeys.delete(field),1300);} } });
</script>

<style scoped>
.meme-field-meta { display:block; margin-top:3px; color:var(--muted); font-size:12px; font-weight:400; white-space:nowrap; }
.meme-field-meta.is-historical { color:var(--accent); }
</style>
