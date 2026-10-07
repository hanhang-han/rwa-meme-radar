<template>
  <article class="detail-relation-record">
    <div class="detail-relation-heading">
      <RouterLink v-if="relation.ticker" :to="themeLink">{{ companyName }}</RouterLink>
      <strong v-else>{{ tr('股票主题', 'Stock theme') }}</strong>
      <RelationBadge :relation="relation" />
    </div>
    <p v-if="state.recorded" class="detail-relation-meta">
      {{ chainName({chainId:relation.chainId??chain}) }} · {{ relation.protocol || tr('链上交易池', 'Onchain pool') }} · {{ short(relation.pool) }}
      <template v-if="liquidityKnown"> · {{ tr('池流动性', 'Pool liquidity') }} {{ usd(relation.liquidityUsd) }}<span v-if="!liquidityCurrent"> · {{ tr('历史估值', 'Historical valuation') }}</span></template>
    </p>
    <p v-else-if="state.key==='name-only'" class="detail-relation-meta">{{ tr('仅名称相近，不计入同池成交。', 'Name similarity only; excluded from same-pool volume.') }}</p>
    <details class="detail-relation-evidence">
      <summary>{{ tr('完整地址与配对依据', 'Addresses and pairing evidence') }}</summary>
      <dl>
        <div v-for="row in addresses" :key="row.key"><dt>{{ tr(...row.label) }}</dt><dd><a v-if="row.url" :href="row.url" target="_blank" rel="noopener">{{ row.value }} ↗</a><span v-else>{{ row.value || '—' }}</span><RouterLink v-if="row.detailLink" class="detail-relation-asset-link" :to="row.detailLink" :replace="row.detailLink.path===route.path">{{ tr('查看行情','View market') }} →</RouterLink></dd></div>
        <div><dt>{{ tr('核验来源', 'Verification source') }}</dt><dd>{{ relation.provider || relation.source || '—' }} · {{ date(relation.checkedAt) }}</dd></div>
        <div v-if="state.recorded"><dt>{{ tr('流动性观测', 'Liquidity observed') }}</dt><dd>{{ liquidityKnown ? usd(relation.liquidityUsd) : '—' }} · {{ date(relation.liquidityAt) }} · {{ tr(...state.label) }}</dd></div>
      </dl>
    </details>
  </article>
</template>
<script setup>
import {computed} from 'vue';
import RelationBadge from './RelationBadge.vue';
import {useRoute} from 'vue-router';
import {tr} from '../i18n';
import {chainName,date,explorer,short,usd} from '../utils/format';
import {detailRelationState} from '../utils/detail-disclosure-presentation';
import {stockThemeName,stockTicker} from '../utils/stock-theme-model';
import {relatedStockThemeLink,relationshipAssetLink} from '../utils/stock-navigation';
import {useDashboardStore} from '../stores/dashboard';
import {useMinuteClock} from '../composables/useMinuteClock';
const props=defineProps({relation:{type:Object,required:true},stock:{type:Object,default:null},chain:{type:String,required:true},scope:{type:String,default:'all'}});
const route=useRoute(),dashboard=useDashboardStore(),clock=useMinuteClock();
const state=computed(()=>detailRelationState(props.relation,clock.value));
const stockRow=computed(()=>stockTicker(props.stock)===stockTicker(props.relation)?props.stock:dashboard.stockIndex?.get(`${props.relation.chainId??props.chain}:${String(props.relation.stock).toLowerCase()}`));
const companyName=computed(()=>tr(stockThemeName(stockRow.value,props.relation.ticker,'zh'),stockThemeName(stockRow.value,props.relation.ticker,'en')));
const themeLink=computed(()=>relatedStockThemeLink(props.relation.ticker,route,props.scope));
const liquidityKnown=computed(()=>typeof props.relation.liquidityUsd==='number'&&Number.isFinite(props.relation.liquidityUsd)&&props.relation.liquidityUsd>=0&&props.relation.liquidityAt>0&&props.relation.liquidityAt<=clock.value);
const liquidityCurrent=computed(()=>liquidityKnown.value&&clock.value-props.relation.liquidityAt<=900000&&props.relation.liquidityStatus!=='stale');
const addresses=computed(()=>[['token',['Meme 合约','Meme contract']],['stock',['股票侧合约','Stock-side contract']],['pool',['交易池','Pool']]].filter(([key])=>key!=='pool'||state.value.recorded).map(([key,label])=>{
  const valid=/^0x[0-9a-f]{40}$/i.test(String(props.relation[key]));
  return {key,label,value:props.relation[key],url:valid?explorer(props.relation[key],'address',String(props.relation.chainId??props.chain)):null,
    detailLink:valid&&key!=='pool'?relationshipAssetLink({...props.relation,chainId:props.relation.chainId??props.chain},key==='stock'?'stock':'meme',route,props.scope,state.value.recorded?props.relation.pool:null):null};
}));
</script>
<style scoped>
.detail-relation-record{min-width:0;padding:12px 0;border-bottom:1px solid var(--border)}.detail-relation-record:last-child{border-bottom:0;padding-bottom:0}.detail-relation-record:first-child{padding-top:0}.detail-relation-heading{display:flex;align-items:baseline;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:13px}.detail-relation-heading a,.detail-relation-heading strong{font-weight:650;text-decoration:none;overflow-wrap:anywhere}.detail-relation-heading>span{color:var(--muted);font-size:11px;line-height:1.5}.detail-relation-heading>span.current{color:var(--accent)}.detail-relation-meta{margin:6px 0;color:var(--muted);font-size:12px;line-height:1.5;overflow-wrap:anywhere}.detail-relation-evidence{margin-top:7px;font-size:12px;color:var(--muted)}summary{cursor:pointer;line-height:1.5}dl{margin:8px 0 0}dl>div{display:grid;grid-template-columns:90px minmax(0,1fr);gap:9px;padding:6px 0;border-top:1px solid var(--border)}dd{margin:0;overflow-wrap:anywhere;line-height:1.5}.detail-relation-asset-link{display:block;margin-top:3px}a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
</style>
