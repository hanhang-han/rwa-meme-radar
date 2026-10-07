<template>
  <tr class="directory-row" :class="{'is-child':child,'is-new':added}" :data-asset-key="directoryAssetKey(asset)" :data-navigation-anchor="directoryAssetKey(asset)">
    <td class="identity-cell">
      <div class="asset-name"><RouterLink :to="assetLink"><AssetAvatar :asset="asset" :size="28" /><strong>{{ asset.name || asset.symbol || short(asset.token) }}</strong></RouterLink></div>
      <small class="asset-contract">{{ asset.symbol && asset.symbol!==asset.name?asset.symbol+' · ':'' }}{{ chainName(asset) }} · <code :title="asset.token">{{ short(asset.token) }}</code></small>
      <div class="asset-signals"><RiskBadge :asset="asset" compact /><small v-if="stale">{{ tr('上次记录','Previous record') }}</small></div>
      <button v-if="extra>0" type="button" class="group-toggle" :aria-expanded="expanded" @click="$emit('toggle-group')">{{ expanded?tr('收起同名合约','Hide same-name contracts'):`+${extra} ${tr('个同名合约','same-name contracts')}` }}</button>
    </td>
    <td class="quote-cell" :title="fieldTitle('price')" data-field="price">
      <strong><LiveNumber :value="observation('price').value" :currency="observation('price').currency" format="price" :label="tr('资产报价','Asset quote')" :description="fieldTitle('price')" /></strong>
      <span class="change" :class="changeClass('change24h')" :title="fieldTitle('change24h')" data-field="change24h"><LiveNumber :value="observation('change24h').value" format="percent" /> · 24h<small v-if="observation('change24h').value!=null&&observation('change24h').state!=='current'"> · {{ stateLabel(observation('change24h').state) }}</small></span>
      <small class="field-meta" :class="{'historical':observation('price').state==='historical'}">{{ fieldMeta('price') }}</small>
      <span v-if="exchangeQuote" class="exchange-quote" :title="exchangeTitle" data-field="exchangePrice">
        <small>{{ tr('交易所','Exchange') }} · {{ exchangeQuote.provider }}</small>
        <span class="exchange-values"><LiveNumber :value="exchangeQuote.price" :currency="exchangeQuote.priceCurrency" format="price" :label="tr('交易所报价','Exchange quote')" :description="exchangeTitle" /><span v-if="exchangeQuote.change24h!=null" :class="exchangeChangeClass"><LiveNumber :value="exchangeQuote.change24h" format="percent" /> · 24h<small v-if="!exchangeQuote.changeCurrent"> · {{ tr('历史','Historical') }}</small></span></span>
        <small :class="{'historical':!exchangeQuote.current}">{{ exchangeQuote.marketId }} · {{ !exchangeQuote.current?tr('历史 · ','Historical · '):'' }}{{ age(exchangeQuote.at) }}</small>
      </span>
    </td>
    <td class="numeric volume-cell" :title="fieldTitle('volume24h')" data-field="volume24h">
      <span class="mobile-field-label">{{ tr('资产 24h 成交','Asset 24h volume') }}</span>
      <strong><LiveNumber :value="observation('volume24h').value" :currency="observation('volume24h').currency" :label="tr('资产 24h 成交','Asset 24h volume')" :description="fieldTitle('volume24h')" /></strong>
      <small class="field-meta" :class="{'historical':observation('volume24h').state==='historical'}">{{ fieldMeta('volume24h') }}</small>
      <small v-if="observation('volume24h').value!=null&&!observation('volume24h').scopeKnown" class="field-meta historical">{{ tr('成交范围待核实','Volume scope unverified') }}</small>
    </td>
    <td class="numeric liquidity-cell" :title="fieldTitle('totalLiquidityUsd')" data-field="totalLiquidityUsd">
      <span class="mobile-field-label">{{ tr('总流动性','Total liquidity') }}</span>
      <strong><LiveNumber :value="observation('totalLiquidityUsd').value" currency="USD" :label="tr('总流动性','Total liquidity')" :description="fieldTitle('totalLiquidityUsd')" /></strong>
      <small class="field-meta" :class="{'historical':observation('totalLiquidityUsd').state!=='current'}">{{ fieldMeta('totalLiquidityUsd') }}</small>
    </td>
    <td class="stock-cell">
      <span class="mobile-field-label">{{ tr('股票关联','Stocks') }}</span>
      <span v-for="relation in stockRelations.slice(0,3)" :key="relation.ticker" class="stock-relation"><RouterLink :to="themeLink(relation.ticker)">{{ stockThemeName(null,relation.ticker,lang.lang) }}</RouterLink><RelationBadge :relation="relation" /></span>
      <RouterLink v-if="stockRelations.length>3" class="more-relations" :to="relationLink">+{{ stockRelations.length-3 }} {{ tr('项关联','relationships') }}</RouterLink>
      <span v-if="!stockRelations.length" class="unknown">—</span>
    </td>
    <td v-if="holders" class="numeric optional-cell" :title="fieldTitle('holders')"><LiveNumber :value="observation('holders').value" format="number" /><small class="field-meta">{{ fieldMeta('holders') }}</small></td>
    <td v-if="windows" class="optional-cell"><span v-for="[field,label] in changeWindows" :key="field" :title="fieldTitle(field)" class="window-value" :class="changeClass(field)">{{ label }} <LiveNumber :value="observation(field).value" format="percent" /><small v-if="observation(field).value!=null&&observation(field).state!=='current'"> · {{ stateLabel(observation(field).state) }}</small></span></td>
    <td class="action-cell">
      <div class="row-actions"><RouterLink :to="assetLink">{{ tr('查看行情','View market') }} →</RouterLink><button type="button" :aria-pressed="followed" :aria-label="(followed?tr('取消关注 ','Unfollow '):tr('关注 ','Follow '))+(asset.name||asset.symbol||asset.token)" @click="account.toggle(directoryAssetKey(asset))">{{ followed?tr('★ 已关注','★ Following'):tr('☆ 关注','☆ Follow') }}</button></div>
      <details v-if="holders||windows" class="mobile-more"><summary>{{ tr('补充数据','Extra data') }}</summary><dl><div v-if="holders"><dt>{{ tr('持币地址','Holder addresses') }}</dt><dd :title="fieldTitle('holders')"><LiveNumber :value="observation('holders').value" format="number" /><small>{{ fieldMeta('holders') }}</small></dd></div><div v-if="windows"><dt>{{ tr('其他涨跌','Other changes') }}</dt><dd><span v-for="[field,label] in changeWindows" :key="field" :title="fieldTitle(field)" :class="changeClass(field)">{{ label }} <LiveNumber :value="observation(field).value" format="percent" /><small v-if="observation(field).value!=null&&observation(field).state!=='current'"> · {{ stateLabel(observation(field).state) }}</small></span></dd></div></dl></details>
    </td>
  </tr>
</template>
<script setup>
import AssetAvatar from './AssetAvatar.vue';
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { tr, useI18n } from '../i18n';
import { age, date, chainName, short } from '../utils/format';
import { stockThemeName } from '../utils/stock-theme-model';
import { assetNavigationLink, themeNavigationLink } from '../utils/navigation-context';
import { directoryObservation, directoryAssetKey } from '../utils/meme-directory-presentation';
import { memeStockRelations } from '../utils/meme-scan-signals';
import { visibleExchangeObservation } from '../utils/visible-exchange-quote';
import { useMinuteClock } from '../composables/useMinuteClock';
import { useAccountStore } from '../stores/account';
import LiveNumber from './LiveNumber.vue';
import RelationBadge from './RelationBadge.vue';
import RiskBadge from './RiskBadge.vue';
const props=defineProps({asset:{type:Object,required:true},relations:{type:Array,default:()=>[]},scope:{type:String,default:'all'},child:Boolean,added:Boolean,extra:{type:Number,default:0},expanded:Boolean,stale:Boolean,holders:Boolean,windows:Boolean});
defineEmits(['toggle-group']);
const route=useRoute(),{lang}=useI18n(),now=useMinuteClock(),account=useAccountStore();
const changeWindows=[['change5m','5m'],['change1h','1h'],['change6h','6h']];
const assetLink=computed(()=>assetNavigationLink(props.asset,{route,scope:props.scope}));
const relationLink=computed(()=>assetNavigationLink(props.asset,{route,scope:props.scope,tab:'relation'}));
const themeLink=ticker=>themeNavigationLink(ticker,{route,scope:props.scope});
const followed=computed(()=>account.watches.includes(directoryAssetKey(props.asset)));
const stockRelations=computed(()=>memeStockRelations(props.asset,props.relations,Math.max(now.value,Date.now())));
const exchangeQuote=computed(()=>visibleExchangeObservation(props.asset,Math.max(now.value,Date.now())));
const exchangeChangeClass=computed(()=>exchangeQuote.value?.changeCurrent?(exchangeQuote.value.change24h>0?'up':exchangeQuote.value.change24h<0?'down':''):'');
const exchangeTitle=computed(()=>exchangeQuote.value?`${tr('交易所报价','Exchange quote')} · ${exchangeQuote.value.provider} · ${exchangeQuote.value.venue} · ${exchangeQuote.value.marketId} · ${exchangeQuote.value.priceCurrency} · ${tr('报价观测','Quote observed')} ${date(exchangeQuote.value.at)} · ${tr('24h 涨跌观测','24h change observed')} ${date(exchangeQuote.value.changeAt)} · ${tr('独立计价，不计入链上成交或价格排序','Separately quoted; excluded from on-chain volume and price sorting')}`:'');
const observation=field=>directoryObservation(props.asset,field,Math.max(now.value,Date.now()));
function stateLabel(state){return ({historical:tr('历史','Historical'),'unknown-time':tr('时间未知','Time unknown'),unverified:tr('待核实','Unverified'),missing:tr('暂无数据','Unavailable')})[state]??'';}
function fieldMeta(field){const item=observation(field);if(item.value==null)return tr('暂无数据','Unavailable');return [(['price','volume24h'].includes(field)&&!item.currency)?tr('币种待确认','Currency unverified'):null,item.source||tr('来源未知','Source unknown'),field==='totalLiquidityUsd'&&props.asset.totalLiquidityCoverage?.complete===false?tr('部分覆盖','Partial coverage'):null,stateLabel(item.state),item.at&&item.state!=='unknown-time'?age(item.at):null].filter(Boolean).join(' · ');}
function fieldTitle(field){const item=observation(field);const scope=field==='volume24h'?(item.scopeKnown?tr('资产市场范围；不等于股票配对池成交','Asset market scope; not stock-pair pool volume'):`${tr('成交范围待核实','Volume scope unverified')} · ${item.scope||tr('范围未知','Scope unknown')}`):field==='totalLiquidityUsd'?tr('已覆盖交易池的合计；可能不完整','Sum across covered pools; coverage may be incomplete'):'';return [fieldMeta(field),item.currency,date(item.at),scope].filter(Boolean).join(' · ');}
function changeClass(field){const item=observation(field);return item.state==='current'&&item.value!=null?item.value>0?'up':item.value<0?'down':'':'';}
</script>
<style scoped>
.directory-row.is-new td{animation:directory-arrival 2s ease-out}@keyframes directory-arrival{from{background:var(--accent-soft,var(--surface-raised))}to{background:transparent}}@media(prefers-reduced-motion:reduce){.directory-row.is-new td{animation:none;background:var(--accent-soft,var(--surface-raised))}}
.directory-row td{padding:14px 10px;vertical-align:top;border-bottom:1px solid var(--border);font-variant-numeric:tabular-nums}.directory-row.is-child td{background:var(--surface-raised)}.directory-row td:first-child{padding-left:0}.directory-row td:last-child{padding-right:0}.asset-name{min-width:0}.asset-name strong{font-size:14px;overflow-wrap:anywhere}.asset-name a{color:var(--text)}.asset-contract,.field-meta{display:block;color:var(--muted);font-size:11px;line-height:1.45;margin-top:4px;white-space:normal;overflow-wrap:anywhere}.identity-cell{width:23%;min-width:160px}.identity-cell code{font:inherit}.asset-signals{display:flex;align-items:center;flex-wrap:wrap;gap:4px 8px;margin-top:6px;min-width:0}.asset-signals>small{color:var(--muted);font-size:10px}.asset-signals :deep(.risk-badge){max-width:100%}.group-toggle{display:block;border:0;background:none;padding:5px 0 0;color:var(--accent);font-size:11px;cursor:pointer;text-align:left}.numeric,.quote-cell{text-align:right}.quote-cell>strong,.volume-cell>strong,.liquidity-cell>strong{font-size:14px;font-weight:600;white-space:nowrap}.exchange-quote{display:grid;gap:3px;margin-top:7px;padding-top:6px;border-top:1px solid var(--border);font-size:11px}.exchange-quote>small{color:var(--muted);font-size:10px;white-space:normal;overflow-wrap:anywhere}.exchange-values{display:flex;justify-content:flex-end;flex-wrap:wrap;gap:3px 8px;font-size:12px}.exchange-values>span small{display:inline;margin-top:0;font-size:10px}.change{display:block;margin-top:4px;font-size:12px;white-space:normal}.change small{display:inline;margin-top:0;font-size:11px}.historical{color:var(--warning)!important}.stock-cell{min-width:120px;max-width:190px}.stock-relation{display:flex;align-items:center;flex-wrap:wrap;gap:4px 6px;margin:2px 0 6px;min-width:0}.stock-relation>a,.more-relations,.row-actions>a{color:var(--accent);font-size:12px;overflow-wrap:anywhere}.unknown{color:var(--muted)}.action-cell{min-width:82px}.row-actions{display:flex;flex-direction:column;align-items:flex-start;gap:8px}.row-actions>a,.row-actions>button{white-space:nowrap}.row-actions>button{border:0;background:none;padding:0;color:var(--muted);font:inherit;font-size:11px;cursor:pointer}.row-actions>button[aria-pressed=true]{color:var(--accent)}.window-value{display:block;white-space:nowrap;font-size:11px}.window-value small{display:inline;margin-top:0;font-size:10px}.mobile-more,.mobile-field-label{display:none}button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(max-width:760px){.directory-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px 14px;padding:16px 0;border-bottom:1px solid var(--border)}.directory-row td{padding:0!important;border:0!important;min-width:0;max-width:100%;overflow-wrap:anywhere}.field-meta{font-size:10px}.identity-cell{width:auto;grid-row:1;grid-column:1}.quote-cell{grid-row:1;grid-column:2}.quote-cell>strong{font-size:13px;white-space:normal;overflow-wrap:anywhere}.volume-cell{grid-row:2;grid-column:1;text-align:left}.liquidity-cell{grid-row:2;grid-column:2;text-align:right}.mobile-field-label{display:block;margin-bottom:5px;font-size:10px;color:var(--muted);line-height:1.4}.stock-cell{grid-row:3;grid-column:1/-1;max-width:none;display:flex;align-items:center;flex-wrap:wrap;gap:4px 12px}.stock-cell>.mobile-field-label{margin-bottom:0}.stock-relation{margin:0;gap:4px}.optional-cell{display:none}.action-cell{grid-row:4;grid-column:1/-1}.row-actions{flex-direction:row;align-items:center;justify-content:space-between;gap:12px}.row-actions>a,.row-actions>button{display:inline-flex;align-items:center;min-height:32px;font-size:12px}.mobile-more{display:block;margin-top:5px;white-space:normal}.mobile-more summary{color:var(--muted);font-size:11px;cursor:pointer;min-height:24px}.mobile-more dl{display:grid;gap:9px;text-align:left;background:var(--surface-raised);padding:10px;border-radius:6px;font-size:11px}.mobile-more dt{color:var(--muted)}.mobile-more dd{margin:3px 0 0;overflow-wrap:anywhere}.mobile-more dd>span{display:block;margin-top:4px}.mobile-more dd small{color:var(--muted);font-size:10px}.mobile-more dd>small{display:block}.directory-row.is-child{background:var(--surface-raised);padding:16px 8px}.asset-contract{font-size:10px}.asset-name strong{font-size:13px}.group-toggle{font-size:10px;max-width:100%;overflow-wrap:anywhere}}
.asset-name a{display:flex;align-items:center;gap:8px;min-width:0}.asset-name strong{min-width:0}
</style>
