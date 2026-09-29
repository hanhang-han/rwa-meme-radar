<template>
  <section class="panel meme-page">
    <div class="page-heading"><h2>Meme</h2></div>
    <div class="meme-filter-bar">
      <input id="xMemeSearch" v-model="qInput" :placeholder="tr('搜索股票、Meme 或合约地址','Search stock, meme or address')" @input="onSearch">
      <select id="v2Chain" :value="scope" :aria-label="tr('网络','Chain')" @change="setQuery({chain:$event.target.value})"><option value="all">{{ tr('全部链','All chains') }}</option><option value="196">X Layer</option><option value="56">BNB Chain</option><option value="4663">Robinhood Chain</option></select>
      <select :value="relationFilter" :aria-label="tr('关系','Relationship')" @change="setQuery({rel:$event.target.value})"><option value="A">{{ tr('池配对','Paired') }}</option><option value="B">{{ tr('名称相关','Name match') }}</option><option value="A,B,C">{{ tr('全部关系','All relations') }}</option><option value="all">{{ tr('全部资产','All assets') }}</option></select>
      <select :value="freshFilter" :aria-label="tr('价格时效','Price freshness')" @change="setQuery({fresh:$event.target.value})"><option value="1">{{ tr('价格 15 分钟内','Price within 15m') }}</option><option value="0">{{ tr('含历史报价','Include historical quotes') }}</option></select>
      <select :value="minLiquidity" :aria-label="tr('最低总流动性','Minimum total liquidity')" @change="setQuery({minLiq:$event.target.value})"><option value="0">{{ tr('不限总流动性','Any total liquidity') }}</option><option value="1000">≥ $1K</option><option value="10000">≥ $10K</option><option value="100000">≥ $100K</option></select>
      <label class="meme-risk-filter"><input type="checkbox" :checked="hideRisk" @change="setQuery({risk:$event.target.checked?'hide':undefined})"> {{ tr('隐藏风险标记','Hide flagged') }}</label>
      <span class="meme-result-count">{{ num(assets.length) }} {{ tr('个资产','assets') }}<template v-if="isOverview"> · {{ tr('概览数据', 'overview data') }}</template></span>
    </div>
    <p class="hint">{{ tr('筛选“价格 15 分钟内”仅检查价格；成交、流动性、持币地址各有独立更新时间。', 'The 15-minute filter checks price only; volume, liquidity and holders have separate observation times.') }} {{ tr('资产总流动性证据', 'Token-wide liquidity evidence') }} {{ num(currentLiquidityCount) }}/{{ num(assets.length) }} · {{ tr('可比较成交 / 流动性', 'Comparable volume / liquidity') }} {{ num(comparableRatioCount) }}/{{ num(assets.length) }}。{{ tr('“—”表示缺少可比较证据，不是零。', 'A dash means comparable evidence is unavailable, not zero.') }}</p>
    <div class="meme-table-controls"><select id="v2Sort" :value="sort" :aria-label="tr('排序','Sort')" @change="setQuery({sort:$event.target.value})"><option value="volume24h">{{ tr('24h 成交','24h volume') }}</option><option value="price">{{ tr('价格','Price') }}</option><option value="change24h">{{ tr('24h 涨跌','24h change') }}</option><option value="totalLiquidityUsd">{{ tr('总流动性','Total liquidity') }}</option><option value="holders">{{ tr('持币地址','Holders') }}</option><option value="firstSeen">{{ tr('首次收录','First indexed') }}</option></select></div>
    <div class="scroll v2-table-scroll" id="xMemeRows">
      <div v-if="!pageGroups.length" class="x-empty">
        {{ !store.snapshot ? tr('正在加载行情数据…', 'Loading market data…') : isOverview ? tr('概览中暂无匹配资产，完整列表仍在同步。', 'No match in the overview; the full list is still syncing.') : tr('当前筛选暂无资产。','No assets match these filters.') }}
        <button v-if="store.snapshot" type="button" @click="resetFilters">{{ tr('清除筛选','Clear filters') }}</button>
      </div>
      <table v-else class="tbl v2-meme-table">
        <thead><tr><th>{{ tr('资产','Asset') }}</th><th>{{ tr('关联股票','Stocks') }}</th><th>{{ tr('价格','Price') }}</th><th>24h</th><th>{{ tr('24h 成交','24h volume') }}</th><th>{{ tr('总流动性','Total liquidity') }}</th><th>{{ tr('成交 / 流动性','Vol / Liq') }}</th><th>{{ tr('持币地址','Holders') }}</th><th>{{ tr('风险','Risk') }}</th><th>{{ tr('价格观测','Price observed') }}</th></tr></thead>
        <tbody><template v-for="group in pageGroups" :key="group.symbol"><MemeRow :a="group.members[0]" :relations="relations" :store="store" :extra="group.members.length>1?group.members.length-1:0" @toggle-group="toggleGroup(group.symbol)" /><template v-if="openGroups.has(group.symbol)"><MemeRow v-for="member in group.members.slice(1)" :key="member.chainId+':'+member.token" :a="member" :relations="relations" :store="store" child /></template></template></tbody>
      </table>
    </div>
    <div class="x-pager"><button :disabled="safePage<=0" @click="setQuery({page:safePage-1})">{{ tr('上一页','Previous') }}</button><span>{{ safePage+1 }} / {{ pages }}</span><button :disabled="safePage+1>=pages" @click="setQuery({page:safePage+1})">{{ tr('下一页','Next') }}</button></div>
  </section>
</template>

<script setup>
import { computed, onUnmounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import MemeRow from '../components/MemeRow.vue';
import { useDashboardStore } from '../stores/dashboard';
import { tr } from '../i18n';
import { num } from '../utils/format';
import { chainScope, inChainScope } from '../utils/chain-scope';
import { relationMatchesAsset } from '../utils/relations';
import { relationLevel, riskFlags, volumeLiquidityRatio } from '../utils/product-labels';

const route=useRoute(), router=useRouter(), store=useDashboardStore();
const qInput=ref(String(route.query.q??''));
const openGroups=reactive(new Set());
const scope=computed(()=>chainScope(route.query));
const legacy=computed(()=>String(route.query.filter??''));
const relationFilter=computed(()=>String(route.query.rel??({all:'all',name:'B',history:'all',verified:'A',related:'A'}[legacy.value]??'A')));
const freshFilter=computed(()=>String(route.query.fresh??(['all','verified','name','history'].includes(legacy.value)?'0':'1')));
const minLiquidity=computed(()=>String(route.query.minLiq??route.query.minLiquidity??'0'));
const hideRisk=computed(()=>route.query.risk==='hide');
const search=computed(()=>String(route.query.q??'').trim().toLowerCase());
const sort=computed(()=>String(route.query.sort??'volume24h'));
const page=computed(()=>Math.max(0,Number(route.query.page)||0));
const relations=computed(()=>store.relations);
const isOverview=computed(()=>store.snapshot?.unified?.snapshotScope==='overview');
function isCurrentLiquidity(asset){
  const at=Number(asset.totalLiquidityAt ?? asset.fieldTimes?.totalLiquidityUsd);
  return asset.totalLiquidityUsd!=null && Number.isFinite(Number(asset.totalLiquidityUsd))
    && asset.totalLiquidityStatus==='current' && Number.isFinite(at) && at>0 && at<=Date.now()+1000 && Date.now()-at<=1800000;
}
const assets=computed(()=>{
  const selected=new Set(relationFilter.value.split(','));
  const floor=Number(minLiquidity.value)||0;
  return store.assets.filter(asset=>{
    if(asset.kind!=='candidate'||!inChainScope(asset,scope.value))return false;
    if(search.value && ![asset.name,asset.symbol,asset.token,...relations.value.filter(r=>relationMatchesAsset(r,asset)).map(r=>r.ticker)].some(v=>String(v??'').toLowerCase().includes(search.value)))return false;
    const levels=new Set(relations.value.filter(r=>relationMatchesAsset(r,asset)).map(relationLevel).filter(Boolean));
    if(asset.relationLevel)levels.add(asset.relationLevel);
    if(relationFilter.value!=='all'&&![...levels].some(level=>selected.has(level)))return false;
    if(freshFilter.value==='1'){
      const at=asset.fieldTimes?.price??asset.quoteAt??0;
      if(!at||Number(at)>Date.now()+1000||Date.now()-Number(at)>900000)return false;
    }
    if(floor && !(isCurrentLiquidity(asset)&&Number(asset.totalLiquidityUsd)>=floor))return false;
    if(hideRisk.value&&riskFlags(asset).length)return false;
    if(legacy.value==='history'&&asset.dataQuality?.tier!=='historical')return false;
    return true;
  });
});
const currentLiquidityCount=computed(()=>assets.value.filter(isCurrentLiquidity).length);
const comparableRatioCount=computed(()=>assets.value.filter(asset=>volumeLiquidityRatio(asset)!=null).length);
const groups=computed(()=>{
  const bySymbol=new Map();
  for(const asset of assets.value){const symbol=String(asset.symbol||asset.name||asset.token).toUpperCase();if(!bySymbol.has(symbol))bySymbol.set(symbol,[]);bySymbol.get(symbol).push(asset);}
  const score=asset=>{const value=asset[sort.value];return value==null?Number.NEGATIVE_INFINITY:Number(value)||0;};
  const rows=[...bySymbol].map(([symbol,members])=>({symbol,members:members.sort((a,b)=>score(b)-score(a)||String(a.token).localeCompare(String(b.token)))}));
  return rows.sort((a,b)=>score(b.members[0])-score(a.members[0])||a.symbol.localeCompare(b.symbol));
});
const PAGE_SIZE=50;
const pages=computed(()=>Math.max(1,Math.ceil(groups.value.length/PAGE_SIZE)));
const safePage=computed(()=>Math.min(page.value,pages.value-1));
const pageGroups=computed(()=>groups.value.slice(safePage.value*PAGE_SIZE,(safePage.value+1)*PAGE_SIZE));
function setQuery(patch){router.push({query:{...route.query,...patch,filter:undefined,minLiquidity:undefined,page:patch.page??undefined}});}
function resetFilters(){router.push({query:{chain:scope.value,rel:'all',fresh:'0',minLiq:'0',q:undefined}});}
function toggleGroup(symbol){openGroups.has(symbol)?openGroups.delete(symbol):openGroups.add(symbol);}
let searchTimer;
function onSearch(event){clearTimeout(searchTimer);searchTimer=setTimeout(()=>setQuery({q:event.target.value||undefined}),300);}
watch(()=>route.query.q,value=>{qInput.value=String(value??'');});
onUnmounted(()=>clearTimeout(searchTimer));
</script>
