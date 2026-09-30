<template>
  <section class="panel meme-page">
    <div class="meme-heading">
      <h2>Meme</h2>
      <div class="meme-view-switch" :aria-label="tr('查看方式','View')">
        <button type="button" :class="{active:view==='coin'}" :aria-pressed="view==='coin'" @click="switchView('coin')">{{ tr('按币','By token') }}</button>
        <button type="button" :class="{active:view==='pool'}" :aria-pressed="view==='pool'" @click="switchView('pool')">{{ tr('按池','By pool') }}</button>
      </div>
    </div>
    <PoolListView v-if="view==='pool'" :scope="scope" :qualified="qualified" />
    <template v-else>
      <div class="meme-toolbar">
        <button type="button" class="mobile-filter-toggle" :aria-expanded="filtersOpen" @click="filtersOpen=!filtersOpen">{{ tr('筛选','Filters') }} {{ filtersOpen?'−':'+' }}</button>
        <div class="meme-filter-options" :class="{'is-open':filtersOpen}">
          <select :value="relationFilter" :aria-label="tr('关系','Relationship')" @change="setQuery({rel:$event.target.value,qualified:undefined})"><option value="A">{{ tr('池子配对','Pool pair') }}</option><option value="B">{{ tr('名称匹配','Name match') }}</option><option value="A,B,C">{{ tr('全部关系','All relationships') }}</option><option value="all">{{ tr('全部资产','All assets') }}</option></select>
          <select :value="freshFilter" :aria-label="tr('价格时效','Price freshness')" @change="setQuery({fresh:$event.target.value,qualified:undefined})"><option value="1">{{ tr('价格 15 分钟内','Price within 15m') }}</option><option value="0">{{ tr('含历史报价','Include historical quotes') }}</option></select>
          <select :value="minLiquidity" :aria-label="tr('最低总流动性','Minimum total liquidity')" @change="setQuery({minLiq:$event.target.value,qualified:undefined})"><option value="0">{{ tr('不限总流动性','Any total liquidity') }}</option><option value="1000">≥ $1K</option><option value="10000">≥ $10K</option><option value="100000">≥ $100K</option></select>
          <select :value="String(route.query.category || 'all')" :aria-label="tr('资产类别','Asset category')" @change="setQuery({category:$event.target.value==='all'?undefined:$event.target.value,qualified:undefined})"><option value="all">{{ tr('全部类别','All categories') }}</option><option value="meme">Meme</option><option value="derivative">{{ tr('衍生品','Derivatives') }}</option></select>
          <label><input type="checkbox" :checked="hideRisk" @change="setQuery({risk:$event.target.checked?'hide':undefined,qualified:undefined})"> {{ tr('隐藏风险标记','Hide flagged') }}</label>
          <label><input type="checkbox" :checked="qualified" @change="setQuery({qualified:$event.target.checked?'1':undefined,rel:'all',fresh:'1',minLiq:undefined,risk:undefined})"> {{ tr('活跃 Meme','Active memes') }}</label>
        </div>
        <select id="v2Sort" :value="sort" :aria-label="tr('排序','Sort')" @change="setQuery({sort:$event.target.value})"><option value="volume24h">{{ tr('24h 成交','24h volume') }}</option><option value="price">{{ tr('价格','Price') }}</option><option value="change24h">{{ tr('24h 涨跌','24h change') }}</option><option value="totalLiquidityUsd">{{ tr('总流动性','Total liquidity') }}</option><option value="holders">{{ tr('持币地址','Holders') }}</option><option value="firstSeen">{{ tr('新发现','New discoveries') }}</option></select>
      </div>
      <div class="meme-list-meta">
        <span v-if="catalogReady">{{ tr('显示','Showing') }} {{ num(displayedCount) }} / {{ tr('共','of') }} {{ num(assets.length) }} {{ tr('个资产','assets') }}</span>
        <button v-if="search" type="button" @click="setQuery({q:undefined})">{{ search }} ×</button>
        <button v-if="route.query.new==='24h'" type="button" @click="setQuery({new:undefined})">{{ tr('近 24h 新发现','Discovered in 24h') }} ×</button>
        <button v-if="volumeRanking" type="button" @click="setQuery({rank:undefined})">{{ tr('24h 成交榜','24h volume ranking') }} ×</button>
        <button v-if="qualified" type="button" @click="setQuery({qualified:undefined})">{{ tr('活跃 Meme','Active memes') }} ×</button>
        <label v-if="catalogReady && !qualified && (hiddenMissingCount || showMissing)"><input type="checkbox" :checked="showMissing" @change="setQuery({showMissing:$event.target.checked?'1':'0'})"> {{ tr('含行情不完整资产','Include incomplete quotes') }}<template v-if="hiddenMissingCount"> ({{ num(hiddenMissingCount) }})</template></label>
      </div>
      <p v-if="qualified" class="meme-note">{{ tr('价格 15 分钟内，总流动性 30 分钟内且不低于 $1K。','Price within 15m; total liquidity within 30m and at least $1K.') }}</p>
      <div v-if="!catalogReady" class="meme-skeleton" role="status" :aria-label="tr('加载行情','Loading market data')"><p class="meme-note">{{ store.error?tr('列表暂时无法加载。','The list could not be loaded.'):tr('正在加载列表…','Loading the list…') }} <button type="button" @click="store.poll({view:'market'})">{{ tr('重试','Retry') }}</button></p><div v-for="n in 7" :key="n" class="skeleton-line"></div></div>
      <div v-else-if="!pageGroups.length" class="x-empty">{{ tr('当前筛选暂无资产。','No assets match these filters.') }} <button type="button" @click="resetFilters">{{ tr('查看全部资产','View all assets') }}</button></div>
      <template v-else>
        <div class="scroll meme-desktop" id="xMemeRows">
          <table class="tbl v2-meme-table">
            <thead><tr><th>{{ tr('资产','Asset') }}</th><th>{{ tr('关联股票','Stocks') }}</th><th>{{ tr('价格','Price') }}</th><th>24h</th><th>{{ tr('24h 成交','24h volume') }}</th><th>{{ tr('总流动性','Total liquidity') }}</th><th>{{ tr('成交 / 流动性','Vol / Liq') }}</th><th>{{ tr('持币地址','Holders') }}</th><th>{{ tr('风险','Risk') }}</th><th>{{ tr('报价时间','Quote time') }}</th></tr></thead>
            <tbody><template v-for="group in pageGroups" :key="group.symbol"><MemeRow :a="group.members[0]" :relations="relations" :store="store" :extra="group.members.length>1?group.members.length-1:0" @toggle-group="toggleGroup(group.symbol)" /><template v-if="openGroups.has(group.symbol)"><MemeRow v-for="member in group.members.slice(1)" :key="member.chainId+':'+member.token" :a="member" :relations="relations" :store="store" child /></template></template></tbody>
          </table>
        </div>
        <div class="meme-mobile">
          <template v-for="group in pageGroups" :key="group.symbol">
            <article v-for="asset in visibleMembers(group)" :key="asset.chainId+':'+asset.token" class="meme-mobile-card">
              <div class="meme-card-title"><RouterLink :to="assetLink(asset)"><strong>{{ asset.name || asset.symbol || short(asset.token) }}</strong></RouterLink><RiskBadge :asset="asset" /></div>
              <p>{{ chainLabel(asset.chainId) }} · {{ short(asset.token) }}</p>
              <div class="meme-card-relations"><RouterLink v-for="ticker in assetTickers(asset)" :key="ticker" :to="{path:'/stock/'+ticker,query:{chain:scope}}">{{ ticker }}</RouterLink><span v-if="!assetTickers(asset).length">{{ tr('暂无股票关联','No stock relationship') }}</span></div>
              <dl><div><dt>{{ tr('价格','Price') }}</dt><dd><LiveNumber :value="asset.price" :currency="asset.priceCurrency" format="price" /></dd></div><div><dt>{{ tr('1h 涨跌','1h change') }}</dt><dd :class="Number(asset.change1h)>0?'up':Number(asset.change1h)<0?'down':''">{{ pct(asset.change1h) }}</dd></div><div><dt>{{ tr('24h 成交','24h volume') }}</dt><dd><LiveNumber :value="asset.volume24h" :currency="asset.volumeCurrency ?? asset.priceCurrency" /></dd></div><div><dt>{{ tr('成交 / 流动性','Vol / Liq') }}</dt><dd>{{ ratioText(asset) }}</dd></div></dl>
              <div class="meme-card-footer"><time :title="date(asset.fieldTimes?.price)">{{ age(asset.fieldTimes?.price) }}</time><RouterLink :to="assetLink(asset)">{{ tr('详情','Details') }} →</RouterLink></div>
              <button v-if="asset===group.members[0] && group.members.length>1" class="meme-same-name" @click="toggleGroup(group.symbol)">{{ openGroups.has(group.symbol)?tr('收起同名合约','Hide same-name contracts'):`+${group.members.length-1} ${tr('个同名合约','same-name contracts')}` }}</button>
            </article>
          </template>
        </div>
        <div class="x-pager"><button :disabled="safePage<=0" @click="setQuery({page:safePage-1})">{{ tr('上一页','Previous') }}</button><span>{{ safePage+1 }} / {{ pages }}</span><button :disabled="safePage+1>=pages" @click="setQuery({page:safePage+1})">{{ tr('下一页','Next') }}</button></div>
      </template>
      <p class="meme-note">{{ tr('池子配对：同池交易。名称匹配：名称关联。','Pool pair: traded in one pool. Name match: a name association.') }}</p>
    </template>
  </section>
</template>

<script setup>
import { computed, reactive, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import MemeRow from '../components/MemeRow.vue';
import RiskBadge from '../components/RiskBadge.vue';
import LiveNumber from '../components/LiveNumber.vue';
import PoolListView from './PoolListView.vue';
import { useDashboardStore } from '../stores/dashboard';
import { useMinuteClock } from '../composables/useMinuteClock';
import { tr } from '../i18n';
import { age, date, num, pct, short } from '../utils/format';
import { chainScope, inChainScope } from '../utils/chain-scope';
import { relationLevel, riskFlags, volumeLiquidityRatio } from '../utils/product-labels';
import { marketCatalogReady, memeFilterValues, memeQuoteMatches } from '../utils/meme-filter-model';
import { comparableVolume, hasBasicMarketData, isQualifiedMeme, isRecentObservation, normalizeTicker } from '../utils/stock-theme-model';

const route=useRoute(), router=useRouter(), store=useDashboardStore(), now=useMinuteClock();
const openGroups=reactive(new Set()), filtersOpen=ref(false);
const scope=computed(()=>chainScope(route.query));
const view=computed(()=>route.query.view==='pool'?'pool':'coin');
const filterValues=computed(()=>memeFilterValues(route.query));
const qualified=computed(()=>filterValues.value.qualified);
const volumeRanking=computed(()=>filterValues.value.ranking);
const showMissing=computed(()=>filterValues.value.showMissing);
const legacy=computed(()=>String(route.query.filter??''));
const relationFilter=computed(()=>filterValues.value.relation);
const freshFilter=computed(()=>filterValues.value.fresh);
const minLiquidity=computed(()=>filterValues.value.minLiquidity);
const hideRisk=computed(()=>filterValues.value.hideRisk);
const search=computed(()=>String(route.query.q??'').trim().toLowerCase());
const sort=computed(()=>String(route.query.sort??'volume24h'));
const page=computed(()=>Math.max(0,Number(route.query.page)||0));
const relations=computed(()=>store.relations);
const catalogReady=computed(()=>marketCatalogReady(store.snapshot));
const relationMap=computed(()=>{
  const result=new Map();
  for(const relation of relations.value){const key=`${relation.chainId}:${String(relation.token).toLowerCase()}`;if(!result.has(key))result.set(key,[]);result.get(key).push(relation);}
  return result;
});
const assetRelations=asset=>relationMap.value.get(`${asset.chainId}:${String(asset.token).toLowerCase()}`)??[];
function isCurrentLiquidity(asset){return asset.totalLiquidityUsd!=null && Number.isFinite(Number(asset.totalLiquidityUsd)) && asset.totalLiquidityStatus==='current' && isRecentObservation(asset.totalLiquidityAt ?? asset.fieldTimes?.totalLiquidityUsd,1800000,now.value);}
const matchingAssets=computed(()=>{
  const selected=new Set(relationFilter.value.split(',')), floor=Number(minLiquidity.value)||0;
  const unique=new Map(store.assets.map(asset=>[`${asset.chainId}:${String(asset.token).toLowerCase()}`,asset]));
  return [...unique.values()].filter(asset=>{
    if(asset.kind!=='candidate'||!inChainScope(asset,scope.value))return false;
    if(route.query.category==='derivative'&&asset.assetCategory!=='derivative')return false;
    if((route.query.category==='meme'||volumeRanking.value)&&asset.assetCategory==='derivative')return false;
    if(volumeRanking.value&&comparableVolume(asset,now.value)==null)return false;
    if(route.query.new==='24h'&&!isRecentObservation(asset.firstSeen,86400000,now.value))return false;
    const related=assetRelations(asset);
    if(search.value && ![asset.name,asset.symbol,asset.token,...related.map(r=>r.ticker),asset.match?.ticker].some(v=>String(v??'').toLowerCase().includes(search.value)))return false;
    if(qualified.value)return isQualifiedMeme(asset,now.value);
    const levels=new Set(related.map(relationLevel).filter(Boolean));if(asset.relationLevel)levels.add(asset.relationLevel);
    if(relationFilter.value!=='all'&&![...levels].some(level=>selected.has(level)))return false;
    if(floor && !(isCurrentLiquidity(asset)&&Number(asset.totalLiquidityUsd)>=floor))return false;
    if(hideRisk.value&&riskFlags(asset).length)return false;
    if(legacy.value==='history'&&asset.dataQuality?.tier!=='historical')return false;
    return true;
  });
});
const hiddenMissingCount=computed(()=>matchingAssets.value.filter(asset=>!hasBasicMarketData(asset)).length);
const assets=computed(()=>matchingAssets.value.filter(asset=>memeQuoteMatches(asset,filterValues.value,now.value)));
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
const visibleMembers=group=>openGroups.has(group.symbol)?group.members:group.members.slice(0,1);
const displayedCount=computed(()=>pageGroups.value.reduce((total,group)=>total+visibleMembers(group).length,0));
function setQuery(patch){router.push({query:{...route.query,...(Object.hasOwn(patch,'qualified')?{rank:undefined}:{}),...patch,filter:undefined,minLiquidity:undefined,page:patch.page||undefined}});}
function switchView(next){router.push({query:{chain:scope.value,q:route.query.q,view:next==='pool'?'pool':undefined}});}
function resetFilters(){router.push({query:{chain:scope.value,rel:'all',fresh:'0',showMissing:'1',minLiq:'0'}});}
function toggleGroup(symbol){openGroups.has(symbol)?openGroups.delete(symbol):openGroups.add(symbol);}
function assetTickers(asset){return [...new Set([...assetRelations(asset).map(r=>r.ticker),asset.match?.ticker].filter(Boolean).map(normalizeTicker))].slice(0,3);}
function assetLink(asset){return {path:`/asset/${asset.chainId}/${asset.token}`,query:{chain:scope.value,from:'meme'}};}
function chainLabel(chain){return {'56':'BNB Chain','196':'X Layer','4663':'Robinhood Chain'}[chain]??chain;}
function ratioText(asset){const ratio=volumeLiquidityRatio(asset);return ratio==null?'—':`${Math.round(ratio)}x`;}
</script>

<style scoped>
.meme-heading { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:20px; }
.meme-heading h2 { font-size:20px; }
.meme-view-switch { display:flex; border:1px solid var(--border); border-radius:6px; padding:3px; gap:3px; }
.meme-view-switch button { border:0; background:transparent; color:var(--muted); padding:7px 14px; border-radius:4px; cursor:pointer; }
.meme-view-switch button.active { background:var(--surface-raised); color:var(--text); }
.meme-toolbar,.meme-filter-options,.meme-list-meta { display:flex; flex-wrap:wrap; align-items:center; gap:10px; }
.meme-toolbar { justify-content:space-between; margin-bottom:12px; }
.meme-toolbar select { min-height:34px; border:1px solid var(--border); border-radius:6px; background:var(--bg); color:var(--text); padding:5px 8px; }
.meme-toolbar label,.meme-list-meta label { display:flex; gap:5px; align-items:center; font-size:12px; color:var(--muted); }
.meme-list-meta { color:var(--muted); font-size:12px; margin-bottom:12px; }
.meme-list-meta button { color:var(--text); background:var(--surface-raised); border:1px solid var(--border); border-radius:4px; padding:3px 7px; cursor:pointer; }
.meme-note { margin:12px 0; color:var(--muted); font-size:12px; }
.meme-desktop { max-height:none; }
.meme-desktop :deep(th) { font-size:12px; }
.meme-mobile,.mobile-filter-toggle { display:none; }
@media(max-width:760px) {
  .meme-desktop { display:none; }
  .meme-mobile { display:grid; gap:10px; }
  .mobile-filter-toggle { display:block; padding:7px 12px; background:var(--surface-raised); border:1px solid var(--border); border-radius:6px; color:var(--text); }
  .meme-filter-options { display:none; order:3; width:100%; padding:10px 0; gap:12px; }
  .meme-filter-options.is-open { display:flex; }
  .meme-filter-options select { flex:1; min-width:145px; }
  .meme-mobile-card { border:1px solid var(--border); border-radius:8px; background:var(--bg); padding:14px; min-width:0; }
  .meme-card-title,.meme-card-footer { display:flex; align-items:center; justify-content:space-between; gap:10px; }
  .meme-card-title a { min-width:0; word-break:break-word; color:var(--text); }
  .meme-mobile-card > p { color:var(--muted); font-size:12px; margin:4px 0 8px; }
  .meme-card-relations { display:flex; gap:8px; color:var(--muted); font-size:12px; min-height:18px; margin-bottom:12px; }
  .meme-card-relations a { color:var(--accent); }
  .meme-mobile-card dl { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .meme-mobile-card dt { color:var(--muted); font-size:12px; }
  .meme-mobile-card dd { font-family:ui-monospace,monospace; font-variant-numeric:tabular-nums; margin-top:4px; overflow-wrap:anywhere; }
  .meme-card-footer { border-top:1px solid var(--border); margin-top:12px; padding-top:10px; font-size:12px; }
  .meme-card-footer time { color:var(--muted); }
  .meme-card-footer a { color:var(--text); }
  .meme-same-name { margin-top:10px; background:none; color:var(--muted); border:0; text-decoration:underline; }
}
</style>
