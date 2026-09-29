<template>
  <tr :data-asset="a.token" :class="{ 'v2-member-row':child, 'is-stale':isStale }">
    <td class="meme-identity-cell">
      <div class="meme-name-line"><RouterLink :to="detailLink(a)"><strong>{{ a.name || a.symbol || short(a.token) }}</strong></RouterLink></div>
      <div class="meme-address-line"><span>{{ chainName(a) }}</span><code>{{ short(a.token) }}</code><button type="button" :aria-label="tr('复制合约地址','Copy contract address')" @click="copyAddress">⧉</button><small v-if="copied">{{ tr('已复制','Copied') }}</small></div>
      <button v-if="extra > 0" class="v2-group-toggle" @click="emit('toggle-group')">+{{ extra }} {{ tr('个同名合约', 'same-name contracts') }}</button>
    </td>
    <td class="meme-relations-cell">
      <template v-if="relOf.length">
        <span v-for="r in relOf.slice(0,3)" :key="r.id" class="meme-relation"><RouterLink :to="r.pool ? pairLink(r) : {path:'/stock',query:{q:r.ticker,chain:a.chainId}}">{{ r.ticker }}</RouterLink><RelationBadge :relation="r" /></span>
        <small v-if="relOf.length > 3">+{{ relOf.length - 3 }}</small>
      </template>
      <template v-else>—</template>
    </td>
    <td data-field="price" :class="{ 'kpi-flash':flashKeys.has('price') }"><LiveNumber :value="a.price" :currency="a.priceCurrency ?? ''" format="price" /></td>
    <td :class="Number(a.change24h) > 0 ? 'up' : Number(a.change24h) < 0 ? 'down' : ''"><LiveNumber :value="a.change24h" format="percent" /></td>
    <td data-field="volume24h" :class="{ 'kpi-flash':flashKeys.has('volume24h') }"><LiveNumber :value="a.volume24h" :currency="a.volumeCurrency ?? a.priceCurrency ?? ''" /></td>
    <td data-field="totalLiquidityUsd" :class="{ 'kpi-flash':flashKeys.has('totalLiquidityUsd') }"><LiveNumber :value="a.totalLiquidityUsd" /></td>
    <td>{{ ratio == null ? '—' : `${Math.round(ratio)}x` }}</td>
    <td><LiveNumber :value="a.holders" format="number" /></td>
    <td><RiskBadge :asset="a" /></td>
    <td class="meme-updated" :title="date(observedAt)">{{ age(observedAt) }}</td>
  </tr>
</template>

<script setup>
import { computed, reactive, ref, watchEffect } from 'vue';
import LiveNumber from './LiveNumber.vue';
import RelationBadge from './RelationBadge.vue';
import RiskBadge from './RiskBadge.vue';
import { tr } from '../i18n';
import { age, chainName, date, detailLink, pairLink, short } from '../utils/format';
import { relationMatchesAsset } from '../utils/relations';
import { relationLevel, volumeLiquidityRatio } from '../utils/product-labels';

const props=defineProps({ a:{type:Object,required:true}, relations:{type:Array,default:()=>[]}, store:{type:Object,required:true}, child:{type:Boolean,default:false}, extra:{type:Number,default:0} });
const emit=defineEmits(['toggle-group']);
const relOf=computed(()=>props.relations.filter(r=>relationMatchesAsset(r,props.a)).sort((a,b)=>({A:0,C:1,B:2}[relationLevel(a)]??3)-({A:0,C:1,B:2}[relationLevel(b)]??3) || Number(b.liquidityUsd??0)-Number(a.liquidityUsd??0)));
const ratio=computed(()=>volumeLiquidityRatio(props.a));
const observedAt=computed(()=>props.a.fieldTimes?.price ?? props.a.quoteAt ?? props.a.updatedAt);
const isStale=computed(()=>!observedAt.value || Date.now()-Number(observedAt.value)>900000);
const copied=ref(false);
let copyTimer;
async function copyAddress() { try { await navigator.clipboard.writeText(props.a.token); copied.value=true; clearTimeout(copyTimer); copyTimer=setTimeout(()=>copied.value=false,1500); } catch {} }
const flashKeys=reactive(new Set());
watchEffect(()=>{ for(const field of ['price','volume24h','totalLiquidityUsd']) { const key=`${props.a.chainId}:${props.a.token}:${field}`; const value=props.a[field]; if(props.store.diffCells(key,String(value??''))&&value!=null){flashKeys.add(field);setTimeout(()=>flashKeys.delete(field),1300);} } });
</script>
