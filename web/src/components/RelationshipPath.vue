<template>
  <article class="relationship-path">
    <div class="relationship-heading"><RouterLink v-if="relation.ticker" :to="themeLink"><h3>{{ companyName }} <small v-if="companyName!==relation.ticker">{{ relation.ticker }}</small></h3></RouterLink><h3 v-else>{{ tr('其他主题', 'Other theme') }}</h3><RelationBadge :relation="relation" /></div>
    <div class="relationship-nodes" :class="{unverified:!verifiedPool}">
      <div class="relationship-node"><small>{{ tr('Meme 资产', 'Meme asset') }}</small><RouterLink v-if="validMeme" :to="memeLink" :replace="memeLink.path===route.path">{{ memeLabel }}</RouterLink><strong v-else>{{ memeLabel }}</strong><span :title="relation.token">{{ short(relation.token) }} <a v-if="validMeme && explorer(relation.token,'address',String(relation.chainId??chain))" :href="explorer(relation.token,'address',String(relation.chainId??chain))" target="_blank" rel="noopener" :aria-label="tr('查看 Meme 链上记录','View Meme onchain record')">↗</a></span></div>
      <div class="relationship-link" :class="{dashed:!verifiedPool}"><span>{{ verifiedPool ? tr('同池', 'Same pool') : relation.level==='B' ? tr('名称关联', 'Name match') : tr('关系未确认', 'Unconfirmed relationship') }}</span></div>
      <div v-if="verifiedPool" class="relationship-node pool"><small>{{ tr('交易池', 'Trading pool') }}</small><a :href="explorer(relation.pool,'address',String(relation.chainId??chain))" target="_blank" rel="noopener">{{ relation.protocol || tr('链上交易池', 'Onchain pool') }} ↗</a><span :title="relation.pool">{{ short(relation.pool) }}</span></div>
      <div v-if="verifiedPool" class="relationship-link"><span>{{ tr('同池', 'Same pool') }}</span></div>
      <div class="relationship-node"><small>{{ tr('股票侧代币', 'Stock-side token') }}</small><RouterLink v-if="validStock" :to="stockLink" :replace="stockLink.path===route.path">{{ stockLabel }}</RouterLink><strong v-else>{{ stockLabel }}</strong><span :title="relation.stock">{{ short(relation.stock) }} <a v-if="validStock && explorer(relation.stock,'address',String(relation.chainId??chain))" :href="explorer(relation.stock,'address',String(relation.chainId??chain))" target="_blank" rel="noopener" :aria-label="tr('查看股票代币链上记录','View stock token onchain record')">↗</a></span></div>
    </div>
    <p v-if="verifiedPool" class="relationship-meta">{{ tr('池流动性', 'Pool liquidity') }} {{ currentLiquidity ? usd(relation.liquidityUsd) : tr('估值延迟','Valuation delayed') }}</p>
    <p v-else class="relationship-meta">{{ relation.level==='B' ? tr('名称相同，配对池尚未核实', 'Name match; paired pool not verified') : tr('暂无已核实交易池', 'No verified trading pool') }}</p>
    <details class="relationship-evidence"><summary>{{ tr('地址与关系依据', 'Addresses and evidence') }}</summary><dl><div><dt>Meme</dt><dd>{{ relation.token || '—' }}</dd></div><div v-if="relation.pool"><dt>{{ tr('池地址','Pool') }}</dt><dd>{{ relation.pool }}</dd></div><div><dt>{{ tr('股票侧代币','Stock-side token') }}</dt><dd>{{ relation.stock || '—' }}</dd></div><div><dt>{{ tr('关系状态','Status') }}</dt><dd>{{ evidenceLabel }}</dd></div><div><dt>{{ tr('记录时间','Recorded') }}</dt><dd>{{ date(relation.checkedAt || relation.createdAt) }}</dd></div><template v-if="verifiedPool"><div><dt>{{ tr('估值来源','Valuation source') }}</dt><dd>{{ relation.liquidityProvider || relation.liquiditySource || relation.provider || '—' }}</dd></div><div><dt>{{ tr('估值时间','Valuation observed') }}</dt><dd>{{ date(relation.liquidityAt) }}</dd></div></template></dl><RegistryBadge v-if="relation.pool" :relation="relation" /><p>{{ tr('同池关系不代表上市公司授权，也不表示价格走势相关。', 'Sharing a pool does not imply company authorization or price correlation.') }}</p></details>
  </article>
</template>
<script setup>
import {computed} from 'vue';
import {useRoute} from 'vue-router';
import {tr} from '../i18n';
import {date,explorer,short,usd} from '../utils/format';
import {recordedPool} from '../utils/detail-presentation';
import {assetKey} from '../utils/realtime';
import {stockThemeName} from '../utils/stock-theme-model';
import {relatedStockThemeLink,relationshipAssetLink} from '../utils/stock-navigation';
import {useDashboardStore} from '../stores/dashboard';
import {useMinuteClock} from '../composables/useMinuteClock';
import RelationBadge from './RelationBadge.vue';
import RegistryBadge from './RegistryBadge.vue';
const props=defineProps({relation:{type:Object,required:true},asset:{type:Object,required:true},stock:{type:Object,default:null},chain:{type:String,required:true},scope:{type:String,default:'all'}});
const dashboard=useDashboardStore();
const route=useRoute();
const clock=useMinuteClock();
const validMeme=computed(()=>/^0x[0-9a-f]{40}$/i.test(String(props.relation.token)));
const validStock=computed(()=>/^0x[0-9a-f]{40}$/i.test(String(props.relation.stock)));
const currentLiquidity=computed(()=>typeof props.relation.liquidityUsd==='number' && Number.isFinite(props.relation.liquidityUsd) && props.relation.liquidityUsd>=0 && typeof props.relation.liquidityAt==='number' && props.relation.liquidityAt>0 && clock.value-props.relation.liquidityAt>=0 && clock.value-props.relation.liquidityAt<=900000 && props.relation.liquidityStatus!=='stale');
const verifiedPool=computed(()=>recordedPool(props.relation));
const evidenceLabel=computed(()=>verifiedPool.value ? tr('池身份已核实','Pool identity verified') : props.relation.evidenceStatus==='name-only' || props.relation.level==='B' ? tr('名称匹配','Name match') : tr('尚未核实','Not verified'));
const memeRow=computed(()=>String(props.asset.token).toLowerCase()===String(props.relation.token).toLowerCase()?props.asset:dashboard.assetIndex.get(assetKey(props.relation.chainId??props.chain,props.relation.token)));
const stockRow=computed(()=>String(props.asset.token).toLowerCase()===String(props.relation.stock).toLowerCase()?props.asset:String(props.stock?.tokenContractAddress).toLowerCase()===String(props.relation.stock).toLowerCase()?props.stock:dashboard.stockIndex.get(assetKey(props.relation.chainId??props.chain,props.relation.stock)));
const companyName=computed(()=>tr(stockThemeName(stockRow.value,props.relation.ticker,'zh'),stockThemeName(stockRow.value,props.relation.ticker,'en')));
const themeLink=computed(()=>relatedStockThemeLink(props.relation.ticker,route,props.scope));
const navigationRelation=computed(()=>({...props.relation,chainId:props.relation.chainId??props.chain}));
const memeLink=computed(()=>relationshipAssetLink(navigationRelation.value,'meme',route,props.scope,verifiedPool.value?props.relation.pool:null));
const stockLink=computed(()=>relationshipAssetLink(navigationRelation.value,'stock',route,props.scope,verifiedPool.value?props.relation.pool:null));
const memeLabel=computed(()=>memeRow.value?.name || memeRow.value?.symbol || props.relation.tokenName || props.relation.tokenSymbol || short(props.relation.token));
const stockLabel=computed(()=>stockRow.value?.tokenName || stockRow.value?.tokenSymbol || stockRow.value?.name || stockRow.value?.symbol || props.relation.stockTokenName || props.relation.stockTokenSymbol || `${props.relation.ticker || ''} ${tr('股票代币','stock token')}`.trim());
</script>
<style scoped>
.relationship-path{min-width:0;padding:19px 0;border-bottom:1px solid var(--border)}
.relationship-path:last-child{border-bottom:0}
.relationship-heading{display:flex;align-items:center;justify-content:space-between;gap:9px;margin-bottom:14px}
.relationship-heading h3{margin:0;font-family:var(--display-font,Georgia,serif);font-size:19px;font-weight:600}
.relationship-heading h3 small{margin-left:6px;color:var(--muted);font-family:inherit;font-size:12px;font-weight:400}
.relationship-nodes{display:flex;flex-direction:column;min-width:0}
.relationship-node{display:grid;grid-template-columns:74px minmax(0,1fr);gap:2px 8px;min-width:0;padding:5px 0}
.relationship-node small{grid-row:1/3;align-self:start;padding-top:2px;color:var(--muted);font-size:12px}
.relationship-node a,.relationship-node strong{min-width:0;font-size:13px;font-weight:600;line-height:1.4;overflow-wrap:anywhere}
.relationship-node>span{grid-column:2;min-width:0;color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.relationship-node>span a{margin-left:4px;color:var(--muted);font-size:12px;font-weight:400}.relationship-path a:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.relationship-link{position:relative;min-height:23px;margin-left:9px;padding-left:16px;display:flex;align-items:center;color:var(--muted);font-size:12px}
.relationship-link:before{content:'';position:absolute;left:0;top:0;bottom:0;border-left:1px solid var(--accent)}
.relationship-link:after{content:'';position:absolute;left:-3px;bottom:1px;width:6px;height:6px;border-bottom:1px solid var(--accent);border-right:1px solid var(--accent);transform:rotate(45deg)}
.relationship-link.dashed:before{border-left-style:dashed}
.relationship-meta,.relationship-evidence{color:var(--muted);font-size:12px;line-height:1.6}
.relationship-meta{margin:12px 0 0;border-top:1px solid var(--border);padding-top:10px;overflow-wrap:anywhere}
.relationship-evidence{margin-top:10px}
.relationship-evidence summary{cursor:pointer;color:var(--muted)}
.relationship-evidence dl{margin:7px 0}
.relationship-evidence dl>div{display:grid;grid-template-columns:105px minmax(0,1fr);gap:10px;padding:5px 0;border-bottom:1px solid var(--border)}
.relationship-evidence dd{margin:0;overflow-wrap:anywhere}
.relationship-evidence p{margin:9px 0 0}
@media(max-width:700px){.relationship-node{grid-template-columns:75px minmax(0,1fr)}.relationship-evidence dl>div{grid-template-columns:86px minmax(0,1fr)}}
</style>
