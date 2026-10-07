<template>
  <div class="stock-index-page">
    <div class="stock-index-heading"><div><p class="stock-eyebrow">02 / {{ tr('股票主题','STOCK THEMES') }}</p><h2>{{ tr('股票主题','Stock themes') }}</h2><p>{{ tr('查看公司主题、股票代币报价与关联 Meme。','Explore company themes, stock-token quotes and related Memes.') }}</p></div></div>
    <section class="panel stock-filter-panel"><StockDirectoryFilters :query="route.query" :busy="directoryRefreshing" @apply="setQuery" /></section>
    <LiveDataStatus :stream="quoteStream" :quote-at="latestVisibleQuoteAt" :snapshot-at="directory?.now" @refresh="refreshNow" />
    <section v-if="!loading" class="stock-overview" :aria-label="tr('已收录股票主题概览','Indexed stock themes overview')">
      <div class="stock-overview-copy"><span>{{ tr('已收录股票主题','Indexed stock themes') }}</span><strong :title="tr('当前网络范围已收录的股票主题总数，包含尚无同池 Meme 的主题。','All indexed stock themes in this chain scope, including themes without paired memes.')">{{ num(totalThemes) }}</strong><small>{{ scopeLabel }}</small><p class="stock-paired-count" :title="tr('该目录快照的筛选结果中，至少有一个同池 Meme 的股票主题数。','Themes with at least one paired meme in the selected directory snapshot.')">{{ pairedCountLabel }} <b>{{ num(pairedThemeCount) }}</b> {{ tr('个主题','themes') }}<template v-if="directoryFiltered"> · {{ tr('当前筛选','Current filter') }}</template></p><small class="stock-statistics-time">{{ tr('统计观测','Statistics observed') }} · <time v-if="statisticsAt" :datetime="new Date(statisticsAt).toISOString()" :title="date(statisticsAt)">{{ date(statisticsAt) }}</time><span v-else>{{ tr('时间待确认','Time unconfirmed') }}</span></small></div>
      <div v-if="topVolumeThemes.length" class="stock-overview-plot"><div class="stock-overview-chart-heading"><strong>{{ tr('股票配对池成交排行','Stock-paired pool volume ranking') }}</strong><span>24h · USD · {{ scopeLabel }}<template v-if="directoryFiltered"> · {{ tr('当前筛选','Current filter') }}</template></span></div><p class="stock-overview-method">{{ tr('仅统计与股票代币直接配对的池，按池去重。','Only pools paired directly with stock tokens are counted, once per pool.') }}</p><div class="stock-volume-ranking"><RouterLink v-for="row in topVolumeThemes" :key="row.ticker" :to="themeLink(row.ticker)" :data-navigation-anchor="'stock:'+row.ticker" class="stock-volume-row" :title="volumeTitle(row)"><span class="stock-volume-identity"><strong>{{ cardTitle(row) }}</strong><small>{{ codeLabel(row) }} · {{ tr('成交池','Pools') }} {{ row.theme.volume.known }}/{{ row.theme.volume.total }}</small></span><span class="stock-volume-track" aria-hidden="true"><i :style="{width:themeBarWidth(row.theme.volume.value)}"></i></span><strong class="stock-volume-value"><LiveNumber :value="row.theme.volume.value" currency="USD" :compact="false" :label="tr('股票配对池 24h 成交','Stock-paired pool 24h volume')" /><span aria-hidden="true"> ↗</span></strong></RouterLink></div></div>
      <p v-else class="stock-overview-empty">{{ tr('当前筛选暂无可用的股票配对池成交数据。','No usable stock-paired pool volume in the current selection.') }}</p>
    </section>
    <section class="panel stock-directory-results">
      <div class="stock-index-toolbar">
        <div class="stock-segments" role="group" :aria-label="tr('展示方式','Display mode')"><button type="button" :aria-pressed="mode==='browse'" :class="{active:mode==='browse'}" @click="setQuery({mode:undefined})">{{ tr('主题卡片','Theme cards') }}</button><button type="button" :aria-pressed="mode==='compare'" :class="{active:mode==='compare'}" @click="setQuery({mode:'compare'})">{{ tr('对比列表','Comparison list') }}</button></div>
        <div v-if="!loading" class="stock-directory-meta"><span>{{ tr('筛选结果','Results') }} {{ num(totalCount) }} · {{ tr('本页','This page') }} {{ num(cards.length) }}</span></div>
      </div>
      <SignalLegend />
      <p v-if="directory && (directoryRefreshing || directoryError)" class="stock-index-note" role="status"><span>{{ directoryError?tr('更新失败，暂时显示上次结果。','Update failed; showing the previous result.'):directorySlow?tr('更新较慢，点此重试','Updating slowly, retry'):tr('正在更新列表…','Updating the list…') }} · {{ tr('数据时间','Data time') }} {{ date(directory.now) }}</span> <button v-if="directoryError || directorySlow" type="button" @click="loadDirectory(true)">{{ tr('重试','Retry') }}</button></p>
      <div v-if="loading && directoryError" class="x-empty" role="alert">{{ tr('主题目录暂时无法加载。','The theme catalog could not be loaded.') }} <button type="button" @click="loadDirectory(true)">{{ tr('重试','Retry') }}</button></div>
      <div v-else-if="loading" role="status" :aria-label="tr('加载股票','Loading stocks')"><p v-if="directorySlow" class="stock-index-note">{{ tr('加载较慢，点此重试','Loading slowly, retry') }} <button type="button" @click="loadDirectory(true)">{{ tr('重试','Retry') }}</button></p><div v-for="n in 8" :key="n" class="skeleton-line"></div></div>
      <p v-else-if="!cards.length" class="x-empty">{{ search?tr('没有匹配的主题。','No matching themes.'):showAll?tr('暂无股票主题。','No stock themes yet.'):tr('暂无同池主题。','No pool-paired themes yet.') }}</p>
      <template v-else>
        <section v-if="mode==='browse'" class="stock-atlas" :aria-label="tr('股票主题卡片','Stock theme cards')">
          <p class="stock-atlas-method">{{ tr('股票代币报价 · 直接配对池成交','Stock-token quotes · direct paired-pool volume') }}</p>
          <div class="stock-atlas-grid">
            <RouterLink v-for="card in cards" :key="card.ticker" :to="themeLink(card.ticker)" :data-navigation-anchor="'stock:'+card.ticker" class="stock-atlas-tile" :class="{'is-arriving':arrivingThemes.has(card.ticker)}" :aria-label="tr('查看主题','View theme')+' '+cardTitle(card)">
              <div class="stock-atlas-tile-top"><span>{{ codeLabel(card) }}</span><span aria-hidden="true">→</span></div>
              <div class="stock-atlas-identity"><AssetAvatar :asset="card.stock" :name="cardTitle(card)" :symbol="card.ticker" :identity="'stock:'+card.ticker" :size="32" /><strong class="stock-atlas-ticker">{{ cardTitle(card) }}</strong></div>
              <div class="stock-atlas-price-row">
                <div class="stock-atlas-value"><small>{{ tr('股票代币价','Stock-token price') }}<template v-if="card.stock?.priceCurrency"> · {{ card.stock.priceCurrency }}</template></small><strong v-if="cardSignal(card).price!=null" :title="quoteTitle(card.stock)"><LiveNumber :value="cardSignal(card).price" :currency="card.stock.priceCurrency" format="price" :label="tr('股票代币价格','Stock-token price')" /></strong><span v-else class="stock-atlas-pending">{{ tr('报价待更新','Quote pending') }}</span></div>
                <div class="stock-atlas-baseline"><small>{{ tr('24h 涨跌','24h change') }}</small><span :class="changeClass(card.stock)"><LiveNumber :value="cardSignal(card).change24h" format="percent" :label="tr('股票代币 24h 涨跌','Stock-token 24h change')" :description="changeDescription()" /></span><small v-if="historicalChange(card.stock)" class="historical">{{ tr('历史','Historical') }}</small></div>
              </div>
              <dl class="stock-atlas-coverage" :title="poolCoverageTitle(card)">
                <div><dt :title="pairedDescription()">{{ tr('同池 Meme','Paired memes') }}</dt><dd>{{ signalCount(cardSignal(card).pairedCount) }}</dd></div>
                <div><dt>{{ tr('配对池成交','Paired-pool volume') }} <small>24h USD</small></dt><dd :title="volumeTitle(card)"><LiveNumber v-if="cardSignal(card).poolVolume!=null" :value="cardSignal(card).poolVolume" currency="USD" :compact="false" :label="tr('股票配对池 24h 成交','Stock-paired pool 24h volume')" /><span v-else>{{ tr('待获取','Pending') }}</span></dd></div>
                <div><dt :title="coverageDescription()">{{ tr('成交池覆盖','Pool coverage') }}</dt><dd>{{ coverageText(card) }}<small v-if="cardMarket(card).poolState==='partial'"> · {{ tr('部分','Partial') }}</small></dd></div>
              </dl>
              <small v-if="cardSignal(card).nameCount>0" class="stock-atlas-name-clues" :title="nameDescription()">{{ tr('名称线索','Name clues') }} {{ cardSignal(card).nameCount }} · {{ tr('未确认同池','Shared pool unconfirmed') }}</small>
              <div class="stock-atlas-pool-state" :title="poolCoverageTitle(card)"><template v-if="cardMarket(card).poolState==='recorded-pending'"><span>{{ tr('池已记录','Pools recorded') }} {{ signalCount(cardSignal(card).poolRecorded) }}</span><span>{{ poolPendingLabel(card) }}</span></template><span v-else-if="cardMarket(card).poolState==='no-eligible-pools'">{{ cardSignal(card).poolTotal===0?tr('尚无计入统计的配对池','No paired pools in current totals'):tr('配对池数据待确认','Paired-pool data unconfirmed') }}</span></div>
              <div v-if="card.stock?.priceCurrency==='USD' && card.sparkline?.length>1" class="stock-atlas-history"><ThemeSparkline :points="card.sparkline" /></div>
              <div v-if="cardMarket(card).equity" class="stock-atlas-equity" :title="equityTitle(card)"><span>{{ tr('正股参考价','Underlying reference') }}</span><strong><LiveNumber :value="cardMarket(card).equity.stockPrice" :currency="cardMarket(card).equity.referenceCurrency" format="price" :label="tr('正股参考价','Underlying reference price')" /></strong></div>
              <div class="stock-atlas-quote-time" :class="{'is-historical':cardMarket(card).state==='historical'}" :title="quoteTitle(card.stock)"><span><template v-if="card.stock?.chainId">{{ chainLabel(card.stock.chainId) }} · </template>{{ quoteStateLabel(card) }}</span><time v-if="cardMarket(card).at" :datetime="new Date(cardMarket(card).at).toISOString()">{{ quoteAge(cardMarket(card).at) }}</time></div>
            </RouterLink>
          </div>
        </section>
        <div v-else class="stock-index-table scroll" tabindex="0" :aria-label="tr('股票主题对比表，可横向滚动','Stock theme comparison table; scroll horizontally')"><table class="tbl"><thead><tr><th>{{ tr('股票主题','Stock theme') }}</th><th class="numeric" :title="tr('股票代币报价，币种按每行记录显示。','Stock-token quote in the currency recorded for each row.')">{{ tr('股票代币价','Stock-token price') }}</th><th class="numeric" :title="changeDescription()">{{ tr('股票代币 24h 涨跌（%）','Stock-token 24h change (%)') }}</th><th class="numeric" :title="pairedDescription()">{{ tr('同池 Meme','Paired memes') }}</th><th class="numeric" :title="volumeDescription()">{{ tr('股票配对池成交 · 24h USD','Stock-paired pool volume · 24h USD') }}</th><th class="numeric" :title="ratioDescription()">{{ tr('7 日量比','7-day volume ratio') }}</th><th :title="coverageDescription()">{{ tr('成交池覆盖','Pool volume coverage') }}</th><th>{{ tr('报价时间','Quote time') }}</th></tr></thead><tbody>
          <tr v-for="card in cards" :key="card.ticker" :data-navigation-anchor="'stock:'+card.ticker"><td><RouterLink :to="themeLink(card.ticker)" class="stock-list-identity"><AssetAvatar :asset="card.stock" :name="cardTitle(card)" :symbol="card.ticker" :identity="'stock:'+card.ticker" :size="32" /><span><strong>{{ cardTitle(card) }}</strong><small>{{ codeLabel(card) }}</small></span></RouterLink></td><td class="numeric" :title="quoteTitle(card.stock)"><LiveNumber :value="cardSignal(card).price" :currency="card.stock?.priceCurrency ?? ''" format="price" :label="tr('股票代币价','Stock-token price')" :description="quoteTitle(card.stock)" /><small v-if="card.stock">{{ chainLabel(card.stock.chainId) }} · {{ card.stock.tokenSymbol }}</small></td><td class="numeric" :class="changeClass(card.stock)"><LiveNumber :value="cardSignal(card).change24h" format="percent" :label="tr('股票代币 24h 涨跌','Stock-token 24h change')" :description="changeDescription()+' · '+date(card.stock?.fieldTimes?.change24h)" /><small v-if="historicalChange(card.stock)" class="historical">{{ tr('历史','Historical') }}</small></td><td class="numeric" :title="pairedDescription()">{{ signalCount(cardSignal(card).pairedCount) }}<small v-if="cardSignal(card).nameCount>0" :title="nameDescription()">{{ tr('名称线索','Name clues') }} {{ cardSignal(card).nameCount }}</small></td><td class="numeric" :title="volumeTitle(card)"><LiveNumber :value="cardSignal(card).poolVolume" currency="USD" :compact="false" :label="tr('股票配对池 24h 成交','Stock-paired pool 24h volume')" /></td><td class="numeric" :title="ratioTitle(card)">{{ ratioText(card) }}</td><td :title="poolCoverageTitle(card)">{{ coverageText(card) }}<small v-if="cardMarket(card).poolState==='recorded-pending'">{{ tr('池已记录','Pools recorded') }} {{ signalCount(cardSignal(card).poolRecorded) }} · {{ poolPendingLabel(card) }}</small></td><td class="stock-table-quote-time" :title="quoteTitle(card.stock)"><span :class="{'historical':cardMarket(card).state==='historical'}">{{ quoteStateLabel(card) }}</span><small>{{ quoteAge(cardMarket(card).at) }}</small></td></tr>
        </tbody></table></div>
        <div v-if="mode==='compare'" class="stock-index-mobile">
          <RouterLink v-for="card in cards" :key="card.ticker" :to="themeLink(card.ticker)" :data-navigation-anchor="'stock:'+card.ticker" class="stock-index-card">
            <div><span class="stock-list-identity"><AssetAvatar :asset="card.stock" :name="cardTitle(card)" :symbol="card.ticker" :identity="'stock:'+card.ticker" :size="32" /><strong>{{ cardTitle(card) }} <small>{{ codeLabel(card) }}</small></strong></span><span aria-hidden="true">→</span></div>
            <div class="stock-mobile-quotes"><span><small>{{ tr('股票代币价','Stock-token price') }}</small><LiveNumber :value="cardSignal(card).price" :currency="card.stock?.priceCurrency ?? ''" format="price" :label="tr('股票代币价','Stock-token price')" :description="quoteTitle(card.stock)" /></span><span :class="changeClass(card.stock)"><small>{{ tr('24h 涨跌（%）','24h change (%)') }}</small><LiveNumber :value="cardSignal(card).change24h" format="percent" :label="tr('股票代币 24h 涨跌','Stock-token 24h change')" :description="changeDescription()" /><small v-if="historicalChange(card.stock)" class="historical">{{ tr('历史','Historical') }}</small></span></div>
            <small :title="pairedDescription()">{{ pairedCountLabel }} {{ signalCount(cardSignal(card).pairedCount) }}<template v-if="cardSignal(card).nameCount>0"> · <span :title="nameDescription()">{{ tr('名称线索','Name clues') }} {{ cardSignal(card).nameCount }}</span></template></small>
            <small :title="volumeTitle(card)">{{ tr('配对池成交 · 24h USD','Paired-pool volume · 24h USD') }} <LiveNumber :value="cardSignal(card).poolVolume" currency="USD" :compact="false" :label="tr('股票配对池 24h 成交','Stock-paired pool 24h volume')" /></small>
            <small :title="coverageDescription()">{{ tr('成交池覆盖','Pool volume coverage') }} {{ coverageText(card) }} · <span :title="ratioTitle(card)">{{ tr('7 日量比','7-day volume ratio') }} {{ ratioText(card) }}</span></small>
            <small v-if="cardMarket(card).poolState==='recorded-pending'">{{ tr('池已记录','Pools recorded') }} {{ signalCount(cardSignal(card).poolRecorded) }} · {{ poolPendingLabel(card) }}</small>
            <div v-if="cardMarket(card).equity" class="stock-atlas-equity" :title="equityTitle(card)"><span>{{ tr('正股参考价','Underlying reference') }}</span><strong><LiveNumber :value="cardMarket(card).equity.stockPrice" :currency="cardMarket(card).equity.referenceCurrency" format="price" :label="tr('正股参考价','Underlying reference price')" /></strong></div>
            <small class="stock-mobile-quote-time" :class="{'historical':cardMarket(card).state==='historical'}" :title="quoteTitle(card.stock)">{{ quoteStateLabel(card) }} · {{ quoteAge(cardMarket(card).at) }}</small>
          </RouterLink>
        </div>
        <div class="x-pager"><button :disabled="safePage<=0" @click="setQuery({page:safePage-1})">{{ tr('上一页','Previous') }}</button><span>{{ safePage+1 }} / {{ pages }}</span><button :disabled="safePage+1>=pages" @click="setQuery({page:safePage+1})">{{ tr('下一页','Next') }}</button></div>
      </template>
    </section>
    <section v-if="!loading && sectors.length" class="panel stock-themes-index">
      <div class="stock-index-section-head"><h2>{{ tr('主题指数','Theme indexes') }}</h2><span>{{ num(currentSectors.length) }}/{{ num(sectors.length) }} {{ tr('可用','available') }}</span></div>
      <details class="stock-index-method"><summary>{{ tr('指数说明','About these indexes') }}</summary><p>{{ tr('仅显示有当前有效报价的指数值。成分横条是权重，不是收益贡献；过期或报价不足的篮子收在下方。','Index values require current quotes. Constituent bars show weights, not return contribution. Stale or insufficient baskets are grouped below.') }}</p></details>
      <div v-if="currentSectors.length" class="stock-index-live-grid">
        <article v-for="sector in currentSectors" :key="sectorKey(sector)" class="stock-index-live-card">
          <div class="stock-live-heading"><div><strong>{{ themeLabel(sector.sector, lang.lang) }}</strong><small>{{ sector.scopeLabel || chainLabel(sector.chainId) }}</small></div><span :title="tr('当前主题指数值；基期与成分见下方，不是单个代币报价。','Current theme index value; base date and constituents are listed below. This is not an individual token quote.')">{{ num(sector.value) }}</span></div>
          <div class="stock-coverage-line"><span :title="tr('当前有效报价的成分数 / 指数成分总数。','Constituents with current valid quotes / all index constituents.')">{{ tr('报价覆盖','Quote coverage') }} {{ num(sector.quoteCoverage?.fresh) }}/{{ num(sector.quoteCoverage?.total) }}</span><time :title="tr('指数观测时间','Index observation time')+' · '+date(sector.at)">{{ date(sector.at) }}</time></div>
          <div class="stock-coverage-track" :title="tr('当前有效报价的成分占比。','Share of index constituents with current valid quotes.')"><span :style="{width:coverageWidth(sector)}"></span></div>
          <p class="stock-basket-meta">{{ tr('成分权重','Constituent weights') }}</p>
          <div v-for="part in sortedComponents(sector).slice(0,3)" :key="part.token" class="stock-component" :title="weightTitle(part)"><RouterLink :to="constituentLink(sector,part)" :data-navigation-anchor="`${sector.chainId}:${String(part.token).toLowerCase()}`">{{ part.symbol }}</RouterLink><div class="stock-component-track"><span v-if="weightOf(part)!=null" :style="{width:`${weightOf(part)*100}%`}"></span></div><strong>{{ weightOf(part)!=null?pct(weightOf(part)*100):'—' }}</strong></div>
          <details v-if="sortedComponents(sector).length>3" class="stock-component-more"><summary>{{ tr('查看其余成分','View remaining constituents') }} ({{ sortedComponents(sector).length-3 }})</summary><div v-for="part in sortedComponents(sector).slice(3)" :key="part.token" class="stock-component" :title="weightTitle(part)"><RouterLink :to="constituentLink(sector,part)" :data-navigation-anchor="`${sector.chainId}:${String(part.token).toLowerCase()}`">{{ part.symbol }}</RouterLink><div class="stock-component-track"><span v-if="weightOf(part)!=null" :style="{width:`${weightOf(part)*100}%`}"></span></div><strong>{{ weightOf(part)!=null?pct(weightOf(part)*100):'—' }}</strong></div></details>
          <p class="stock-basket-meta" :title="tr('指数的基期及成分总数','Index base date and constituent count')">{{ tr('基期','Base') }} {{ date(sector.baseAt) }} · {{ num(sector.members) }} {{ tr('成分','constituents') }}</p>
        </article>
      </div>
      <details v-if="unavailableSectors.length" class="stock-index-unavailable"><summary>{{ tr('无当前值的指数','Indexes without a current value') }} <strong>{{ num(unavailableSectors.length) }}</strong> <span>⌄</span></summary><div class="v2-baskets"><details v-for="sector in unavailableSectors" :key="sectorKey(sector)" class="x-basket"><summary class="stock-basket-summary"><strong>{{ themeLabel(sector.sector, lang.lang) }} <small>{{ sector.scopeLabel || chainLabel(sector.chainId) }}</small></strong><span>—</span><small>{{ tr('报价','Quotes') }} {{ num(sector.quoteCoverage?.fresh) }}/{{ num(sector.quoteCoverage?.total) }} · {{ tr('最后有效','Last valid') }} {{ date(sector.lastAt) }}</small></summary><div class="stock-basket-meta">{{ tr('基期','Base') }} {{ date(sector.baseAt) }} · {{ num(sector.members) }} {{ tr('成分','constituents') }}</div><div v-for="part in sortedComponents(sector)" :key="part.token" class="stock-component" :title="weightTitle(part)"><RouterLink :to="constituentLink(sector,part)" :data-navigation-anchor="`${sector.chainId}:${String(part.token).toLowerCase()}`">{{ part.symbol }}</RouterLink><div class="stock-component-track"><span v-if="weightOf(part)!=null" :style="{width:`${weightOf(part)*100}%`}"></span></div><strong>{{ weightOf(part)!=null?pct(weightOf(part)*100):'—' }}</strong></div></details></div></details>
    </section>
  </div>
</template>

<script setup>
import AssetAvatar from '../components/AssetAvatar.vue';
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useMinuteClock } from '../composables/useMinuteClock';
import { useVisibleQuotes } from '../composables/useVisibleQuotes';
import { tr, useI18n } from '../i18n';
import { date, fullUsd, num, pct } from '../utils/format';
import { chainScope, inChainScope } from '../utils/chain-scope';
import { isRecentObservation, stockThemeCodeLabel, stockThemeName } from '../utils/stock-theme-model';
import { themeLabel } from '../utils/theme-labels';
import LiveNumber from '../components/LiveNumber.vue';
import LiveDataStatus from '../components/LiveDataStatus.vue';
import SignalLegend from '../components/SignalLegend.vue';
import StockDirectoryFilters from '../components/StockDirectoryFilters.vue';
import ThemeSparkline from '../components/ThemeSparkline.vue';
import { directoryCardMarket } from '../utils/stock-directory-presentation';
import { stockDirectorySignals } from '../utils/stock-signal-presentation';
import { getStockDirectory, peekStockDirectory } from '../api/product';
import { stockDirectoryQuery, STOCK_DIRECTORY_PAGE_SIZE } from '../utils/stock-directory-query';
import { assetNavigationLink, themeNavigationLink } from '../utils/navigation-context';
import { applyStockDirectoryQuote, mergeStockDirectorySnapshot, stockQuoteAgeLabel } from '../utils/stock-directory-refresh';
import { applyVisibleQuote, visibleQuoteAt } from '../utils/visible-quotes';

const route=useRoute(), router=useRouter(), minuteNow=useMinuteClock(),liveNow=ref(Date.now());
const now=computed(()=>Math.max(minuteNow.value,liveNow.value));
const {lang}=useI18n();
const scope=computed(()=>chainScope(route.query));
const scopeLabel=computed(()=>scope.value==='all'?tr('全部链','All chains'):chainLabel(scope.value));
const directory=ref(null),directoryError=ref(false),directorySlow=ref(false),directoryRefreshing=ref(false);
// The directory's now is its published statistics snapshot, independent of streamed quotes.
const statisticsAt=computed(()=>{const at=directory.value?.now;return typeof at==='number'&&Number.isFinite(at)&&at>0?at:null;});
const statisticsCurrent=computed(()=>isRecentObservation(statisticsAt.value,900000,now.value));
const pairedCountLabel=computed(()=>statisticsCurrent.value?tr('当前同池','Paired now'):tr('快照同池','Snapshot pairs'));
const arrivingThemes=ref(new Set());
const hidden=ref(typeof document!=='undefined'&&document.hidden);
const directoryQuery=computed(()=>stockDirectoryQuery(route.query));
let directoryRequest=0,directoryTimer,clockTimer,slowTimer,arrivalTimer,mounted=false;
function acceptDirectory(result){
  const before=new Set((directory.value?.unified?.stockThemes??[]).map(row=>row.ticker));
  directory.value=mergeStockDirectorySnapshot(directory.value,result);
  const arrivals=(directory.value?.unified?.stockThemes??[]).filter(row=>!before.has(row.ticker)).map(row=>row.ticker);
  if(before.size&&arrivals.length){clearTimeout(arrivalTimer);arrivingThemes.value=new Set(arrivals);arrivalTimer=setTimeout(()=>{arrivingThemes.value=new Set();},1500);}
}
async function loadDirectory(force=false,{automatic=false}={}){
  if(hidden.value||automatic&&directoryRefreshing.value)return;
  const request=++directoryRequest;directoryError.value=false;directorySlow.value=false;directoryRefreshing.value=true;
  const {chain,options}=directoryQuery.value;
  clearTimeout(slowTimer);slowTimer=setTimeout(()=>{if(request===directoryRequest)directorySlow.value=true;},8000);
  try{const result=await getStockDirectory(chain,{...options,force});if(request===directoryRequest&&!hidden.value)acceptDirectory(result);}
  catch{if(request===directoryRequest)directoryError.value=true;}
  finally{if(request===directoryRequest){clearTimeout(slowTimer);directoryRefreshing.value=false;}}
}
function refreshNow(){if(!hidden.value)loadDirectory(true);}
function visibilityChanged(){
  hidden.value=document.hidden;
  if(hidden.value){directoryRequest++;clearTimeout(slowTimer);directoryRefreshing.value=false;}
  else{liveNow.value=Date.now();loadDirectory(true);}
}
const search=computed(()=>String(route.query.q??'').trim().toLowerCase());
const sort=computed(()=>directoryQuery.value.options.sort);
const mode=computed(()=>route.query.mode==='compare'?'compare':'browse');
const showAll=computed(()=>route.query.catalog!=='paired');
const directoryFiltered=computed(()=>!showAll.value||!!search.value);
const PAGE_SIZE=STOCK_DIRECTORY_PAGE_SIZE;
const requestedPage=computed(()=>{const value=Number(route.query.page);return Number.isFinite(value)?Math.max(0,Math.floor(value)):0;});
watch(()=>JSON.stringify(directoryQuery.value),()=>{
  directoryRequest++;arrivingThemes.value=new Set();clearTimeout(arrivalTimer);
  const {chain,options}=directoryQuery.value;directory.value=peekStockDirectory(chain,options);loadDirectory();
},{immediate:true});
onMounted(()=>{mounted=true;document.addEventListener('visibilitychange',visibilityChanged);directoryTimer=setInterval(()=>{if(!hidden.value)loadDirectory(true,{automatic:true});},30000);clockTimer=setInterval(()=>{if(!hidden.value)liveNow.value=Date.now();},1000);});
onUnmounted(()=>{mounted=false;directoryRequest++;clearTimeout(slowTimer);clearTimeout(arrivalTimer);clearInterval(directoryTimer);clearInterval(clockTimer);document.removeEventListener('visibilitychange',visibilityChanged);});
const loading=computed(()=>!directory.value);
const sectors=computed(()=>(directory.value?.unified?.sectors??[]).filter(row=>inChainScope(row,scope.value)));
const currentSectors=computed(()=>sectors.value.filter(sector=>sector.dataStatus==='current'&&sector.value!=null&&Number.isFinite(Number(sector.value))&&Number(sector.quoteCoverage?.fresh)>0));
const unavailableSectors=computed(()=>sectors.value.filter(sector=>!currentSectors.value.includes(sector)));
function sectorKey(sector){return `${sector.chainId}:${sector.sector}:${sector.basketVersion}`;}
function coverageWidth(sector){const fresh=Number(sector.quoteCoverage?.fresh),total=Number(sector.quoteCoverage?.total);return Number.isFinite(fresh)&&Number.isFinite(total)&&total>0?`${Math.max(0,Math.min(100,fresh/total*100))}%`:'0%';}
function sortedComponents(sector){return [...(sector.components??[])].sort((a,b)=>Number(weightOf(b)!=null)-Number(weightOf(a)!=null)||(weightOf(b)??0)-(weightOf(a)??0)||String(a.symbol??'').localeCompare(String(b.symbol??'')));}
function poolPendingLabel(card){
  const reason=cardMarket(card).pending.slice().sort((a,b)=>b.count-a.count)[0]?.reason;
  return ({unknown:tr('估值待补齐','Valuation pending'),stale:tr('估值待更新','Valuation update pending'),identityPending:tr('股票合约待核验','Stock contract unverified'),belowMinimum:tr('流动性不足','Insufficient liquidity')})[reason]||tr('数据待补齐','Data pending');
}
function poolCoverageTitle(card){
  const market=cardMarket(card),signal=cardSignal(card),labels={unknown:tr('待估值','Valuation pending'),stale:tr('估值待更新','Valuation update pending'),identityPending:tr('合约待核验','Contract unverified'),belowMinimum:tr('流动性不足','Insufficient liquidity'),other:tr('其他待核验','Other unverified')};
  return [tr('池已记录','Pools recorded')+' '+signalCount(signal.poolRecorded),tr('计入统计','In current totals')+' '+signalCount(signal.poolTotal),...market.pending.map(row=>labels[row.reason]+' '+row.count)].join(' · ');
}
function directoryRow(row){return {...row,list:row.list??(row.stock?[row.stock]:[]),theme:row.theme??{pairedCount:row.assetCount??0,nameCount:row.nameCount??0,volume:{value:row.volume24h,known:row.volumeKnown,total:row.volumeTotal}}};}
const allRows=computed(()=>(directory.value?.unified?.stockThemes??[]).map(directoryRow));
const directoryStats=computed(()=>directory.value?.directory??{});
const totalCount=computed(()=>directoryStats.value.total??allRows.value.length);
const totalThemes=computed(()=>directoryStats.value.totalThemes??totalCount.value);
const pages=computed(()=>Math.max(1,Math.ceil(totalCount.value/PAGE_SIZE)));
const safePage=computed(()=>Math.min(requestedPage.value,pages.value-1));
// Filtering, ordering and page slicing are owned by the directory endpoint.
const cards=computed(()=>allRows.value);
const visibleStocks=computed(()=>cards.value.map(card=>card.stock).filter(Boolean));
const latestVisibleQuoteAt=computed(()=>Math.max(0,...visibleStocks.value.map(visibleQuoteAt)));
function applyQuote(packet){
  if(hidden.value||!mounted)return false;
  // A token observation is never substituted for an underlying exchange quote
  // or for the volume of pools directly paired with the stock token.
  let changed=false;
  const snapshot=directory.value;
  if(snapshot)for(const card of [...(snapshot.unified?.stockThemes??[]),...(snapshot.directory?.topThemes??[])]){
    if(card.stock)changed=applyStockDirectoryQuote(card,packet,applyVisibleQuote)||changed;
  }
  liveNow.value=Date.now();
  return changed;
}
const {status:quoteStream}=useVisibleQuotes({rows:visibleStocks,onQuote:applyQuote,enabled:computed(()=>!hidden.value),getCursor:()=>directory.value?.realtime?.cursor,onReset:()=>loadDirectory(true)});
const pairedThemeCount=computed(()=>directoryStats.value.pairedThemeCount??allRows.value.filter(row=>row.theme.pairedCount>0).length);
const maxKnownVolume=computed(()=>directoryStats.value.maxKnownVolume??allRows.value.reduce((max,row)=>Math.max(max,Number(row.theme.volume.value)||0),0));
const topVolumeThemes=computed(()=>{
  const serverRanked=Array.isArray(directoryStats.value.topThemes),rows=serverRanked?directoryStats.value.topThemes.map(directoryRow):allRows.value;
  const valid=rows.filter(row=>row.theme.pairedCount>0&&row.theme.volume.value!=null&&row.theme.volume.value!==''&&Number.isFinite(Number(row.theme.volume.value))&&Number(row.theme.volume.value)>=0).slice();
  return (serverRanked?valid:valid.sort((a,b)=>Number(b.theme.volume.value)-Number(a.theme.volume.value))).slice(0,4);
});
watch(directory,packet=>{if(packet&&requestedPage.value>=pages.value)router.replace({query:{...route.query,page:pages.value>1?pages.value-1:undefined}});});
function themeBarWidth(volume){return maxKnownVolume.value>0?`${Math.max(0,Math.min(100,Number(volume)/maxKnownVolume.value*100))}%`:'0%';}
function weightOf(part){const weight=Number(part?.weight);return part?.weight!=null&&Number.isFinite(weight)&&weight>=0&&weight<=1?weight:null;}
function weightTitle(part){const weight=weightOf(part);return `${part.symbol} · ${tr('指数成分权重（%），不是收益贡献','Index constituent weight (%), not return contribution')}: ${weight!=null?(weight*100).toFixed(4)+'%':missingDescription()}`;}
function cardTitle(card){return stockThemeName(card.stock??card.list[0],card.ticker,lang.lang);}
function cardMarket(card){return directoryCardMarket(card,now.value);}
function cardSignal(card){return stockDirectorySignals(card);}
function signalCount(value){return value==null?'—':num(value);}
function coverageText(card){const signal=cardSignal(card);return `${signalCount(signal.poolKnown)} / ${signalCount(signal.poolTotal)}`;}
function quoteStateLabel(card){return ({current:tr('报价时间','Quote time'),historical:tr('历史报价','Historical quote'),unconfirmed:tr('报价时间待确认','Quote time unconfirmed'),missing:tr('尚未取得有效报价','No valid quote yet')})[cardMarket(card).state];}
function quoteAge(at){return stockQuoteAgeLabel(at,now.value,lang.lang);}
function equityTitle(card){const reference=cardMarket(card).equity;return reference?`${tr('正股参考价','Underlying reference')} · ${reference.referenceCurrency} · ${reference.referenceProvider} · ${date(reference.referenceAt)} · ${reference.referenceRealtime?tr('实时','Realtime'):tr('参考行情，延迟以来源为准','Reference data; delay depends on source')}`:'';}
function codeLabel(card){return stockThemeCodeLabel(card.stock??card.list[0],card.ticker,lang.lang);}
function themeLink(ticker){return themeNavigationLink(ticker,{route,scope:scope.value});}
function constituentLink(sector,part){return assetNavigationLink({chainId:sector.chainId,token:part.token},{route,scope:scope.value});}
function setQuery(patch){const query={...route.query,...patch};if(Object.hasOwn(patch,'page'))query.page=patch.page>0?patch.page:undefined;router.push({query});}
function ratioText(card){const ratio=cardSignal(card).ratio7d;return ratio!=null?ratio.toFixed(2)+'×':'—';}
function missingDescription(){return tr('— 表示暂无可用数据，不代表 0。','— means no usable data, not zero.');}
function volumeDescription(){return tr('与该主题股票代币直接配对的池，24h 有效美元成交合计；同一链上的同一个池只计一次。','Valid 24h USD volume from pools paired directly with this theme’s stock token; each pool is counted once per chain.')+' '+missingDescription();}
function volumeTitle(card){return `${tr('股票配对池成交 · 24h USD','Stock-paired pool volume · 24h USD')}: ${fullUsd(card.theme.volume.value)} · ${volumeDescription()}`;}
function pairedDescription(){return tr('与该主题股票代币共用交易池的 Meme 资产数。','Number of meme assets sharing a trading pool with this theme’s stock token.');}
function nameDescription(){return tr('仅名称相似的 Meme 资产数；不表示已确认同池。','Number of memes with similar names; this does not confirm a shared pool.');}
function coverageDescription(){return tr('有可用美元成交数据的配对池数 / 当前配对池总数；未覆盖部分不补 0。','Stock-paired pools with usable USD volume / current stock-paired pools; uncovered data is not filled with zero.');}
function ratioDescription(){return tr('当前同池组 24h 成交 / 前 7 日 UTC 同小时、同池组、同源成交的中位数，单位为倍。','Current paired-pool 24h volume / median volume for the same pools and provider at the same UTC hour over the previous seven days, in multiples.');}
function ratioTitle(card){return ratioDescription()+' '+(ratioText(card)==='—'?tr('暂无完整可比数据；可能缺少历史或当前成交，或基准为 0。','No complete comparable data; history or current volume may be missing, or the baseline may be zero.'):ratioText(card));}
function changeDescription(){return tr('股票代币 24 小时价格变化，单位为百分比；历史值另行标注。','Stock-token price change over 24 hours, in percent; historical values are marked.')+' '+missingDescription();}
function chainLabel(chain){return {'56':'BNB Chain','196':'X Layer','4663':'Robinhood Chain','5042':'Arc'}[chain]??chain;}
function quoteTitle(stock){return `${tr('股票代币报价','Stock-token quote')} · ${stock?.priceCurrency||tr('币种未知','Currency unknown')} · ${stock?.fieldSources?.price??stock?.provider??tr('来源未知','Source unknown')} · ${date(stock?.fieldTimes?.price??stock?.quoteAt)} · ${missingDescription()}`;}
function currentChange(stock){return isRecentObservation(stock?.fieldTimes?.change24h,900000,now.value);}
function historicalChange(stock){return typeof stock?.change24h==='number'&&Number.isFinite(stock.change24h)&&!currentChange(stock);}
function changeClass(stock){return typeof stock?.change24h!=='number'||!Number.isFinite(stock.change24h)||!currentChange(stock)?'':stock.change24h>0?'up':stock.change24h<0?'down':'';}
</script>

<style scoped>
.stock-index-page{display:grid;gap:20px;min-width:0}
.stock-index-page>.panel{padding:22px;border:1px solid var(--border);border-radius:12px;background:var(--panel);min-width:0}
.stock-index-heading{padding:12px 0 6px}.stock-index-heading h2{font-size:28px;font-weight:700;letter-spacing:-.7px;line-height:1.25;margin:3px 0 9px}
.stock-eyebrow{font-size:12px;letter-spacing:.14em;color:var(--accent);margin:0}.stock-index-heading p:last-child{font-size:12px;color:var(--muted);line-height:1.65;margin:10px 0 0}
.stock-index-page>.stock-filter-panel{padding:16px 20px}.stock-filter-panel :deep(.stock-directory-filters){padding:0;border:0}
.stock-overview{display:grid;grid-template-columns:190px minmax(0,1fr);gap:28px;padding:16px 20px;background:var(--panel);border:1px solid var(--border);border-radius:12px;align-items:center}
.stock-overview-copy{border-right:1px solid var(--border);padding-right:24px}.stock-overview-copy>span,.stock-overview-copy>small{font-size:12px;color:var(--muted)}
.stock-overview-copy>small{display:block}.stock-overview-copy>strong{display:block;font-size:32px;font-weight:650;font-variant-numeric:tabular-nums;line-height:1.1;margin:7px 0}
.stock-paired-count{font-size:12px;color:var(--muted);margin:12px 0 0;line-height:1.65}.stock-paired-count b{color:var(--text);font-weight:600}.stock-statistics-time{margin-top:6px;line-height:1.6;overflow-wrap:anywhere}
.stock-overview-plot{min-width:0}.stock-overview-chart-heading{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:5px 16px;margin-bottom:10px;font-size:12px}
.stock-overview-chart-heading strong{font-weight:600}.stock-overview-chart-heading>span,.stock-overview-empty{color:var(--muted);font-size:12px;line-height:1.6}.stock-overview-empty{margin:0}
.stock-overview-method{font-size:12px;color:var(--muted);line-height:1.5;margin:0 0 10px}.stock-volume-ranking{display:grid}
.stock-volume-row{display:grid;grid-template-columns:minmax(120px,.8fr) minmax(90px,1.2fr) minmax(95px,.7fr);gap:18px;align-items:center;padding:9px 0;color:var(--text);text-decoration:none;min-width:0}
.stock-volume-row:hover .stock-volume-identity strong{color:var(--accent)}.stock-volume-identity{display:grid;gap:3px;min-width:0}.stock-volume-identity strong{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stock-volume-identity small{font-size:11px;color:var(--muted)}.stock-volume-track{display:block;height:6px;border-radius:3px;background:var(--surface-raised);overflow:hidden}.stock-volume-track i{display:block;height:100%;border-radius:3px;background:var(--accent)}
.stock-volume-value{text-align:right;min-width:0;font-size:14px;font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.stock-volume-value>span{font-size:12px;color:var(--muted);font-weight:400}
.stock-index-toolbar{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:14px}
.stock-segments{display:flex;gap:3px;padding:3px;background:var(--bg);border-radius:6px;margin-right:auto}.stock-segments button{font:inherit;font-size:12px;border:0;border-radius:5px;background:none;color:var(--muted);padding:7px 12px;cursor:pointer}
.stock-segments button.active{background:var(--panel);color:var(--accent);box-shadow:0 1px 3px color-mix(in srgb,var(--text) 6%,transparent)}
.stock-directory-meta{font-size:12px;color:var(--muted);line-height:1.7}.stock-index-note{font-size:12px;color:var(--muted);line-height:1.7;margin:12px 0}
.stock-atlas{margin:14px 0 20px}.stock-atlas-method{font-size:11px;margin:0 0 10px;color:var(--muted);line-height:1.5}
.stock-atlas-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,260px),1fr));gap:10px}
.stock-atlas-tile{display:flex;flex-direction:column;min-width:0;padding:14px;border:1px solid var(--border);border-radius:8px;background:var(--panel);color:var(--text);text-decoration:none;cursor:pointer}
.stock-atlas-tile:hover{border-color:var(--accent)}.stock-atlas-tile.is-arriving{animation:stock-theme-arrival 1.5s ease-out}
.stock-atlas-tile-top{display:flex;justify-content:space-between;gap:10px;font-size:11px;color:var(--muted);line-height:1.5;min-height:17px}
.stock-atlas-ticker{display:block;font-size:17px;font-weight:650;line-height:1.4;letter-spacing:0;margin:7px 0 11px;min-height:24px;overflow-wrap:anywhere}
.stock-atlas-price-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(70px,auto);align-items:start;gap:8px}
.stock-atlas-value{display:flex;flex-direction:column;min-width:0;gap:4px}.stock-atlas-value>small{font-size:11px;line-height:1.5;color:var(--muted)}
.stock-atlas-value>strong{font-size:20px;font-weight:650;line-height:1.4;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.stock-atlas-pending{font-size:15px;line-height:1.8;color:var(--muted);font-weight:500}
.stock-atlas-baseline{display:grid;gap:4px;justify-items:end;font-variant-numeric:tabular-nums;line-height:1.5}.stock-atlas-baseline>small{font-size:11px;color:var(--muted)}.stock-atlas-baseline>span{font-size:14px;font-weight:600}
.stock-atlas-baseline .historical,.stock-atlas-quote-time.is-historical{color:var(--warning)}
.stock-atlas-coverage{display:grid;gap:8px;margin:11px 0 0;padding:11px 0 0;border-top:1px solid var(--border);font-size:12px;line-height:1.5}
.stock-atlas-coverage>div{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr);align-items:baseline;gap:8px}
.stock-atlas-coverage dt{min-width:0;color:var(--muted);overflow-wrap:anywhere}.stock-atlas-coverage dd{min-width:0;margin:0;text-align:right;color:var(--text);font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.stock-atlas-coverage dt small{display:block;font-size:10px}.stock-atlas-coverage dd small{font-size:11px;font-weight:400;color:var(--muted)}
.stock-atlas-name-clues{margin-top:7px;color:var(--muted);font-size:11px;line-height:1.5}
.stock-atlas-pool-state{display:flex;flex-wrap:wrap;gap:3px 7px;font-size:11px;line-height:1.5;color:var(--muted);margin-top:7px}.stock-atlas-pool-state:empty{display:none}
.stock-atlas-history{flex:none;margin:12px 0 0}
.stock-atlas-equity{display:flex;align-items:baseline;justify-content:space-between;gap:8px;flex-wrap:wrap;margin:10px 0 0;padding-top:8px;border-top:1px dashed var(--border);font-size:11px;line-height:1.6}
.stock-atlas-equity>span{color:var(--muted)}.stock-atlas-equity>strong{font-weight:600;font-variant-numeric:tabular-nums}
.stock-atlas-quote-time{display:flex;flex-wrap:wrap;gap:3px 8px;justify-content:space-between;margin:10px 0 0;padding-top:8px;border-top:1px solid var(--border);color:var(--muted);font-size:11px;line-height:1.5}
.stock-index-table{max-width:100%}.stock-index-table .tbl{min-width:1020px}.stock-index-table .tbl th{font-size:12px;background:var(--bg)}.stock-index-table .tbl td{font-size:12px;padding-top:15px;padding-bottom:15px}
.stock-index-table small{display:block;font-size:12px;color:var(--muted);margin-top:3px}.stock-index-table a{color:var(--text)}.stock-index-table .numeric{text-align:right;font-family:var(--number-font);font-variant-numeric:tabular-nums}
.stock-index-table .historical,.stock-index-card .historical{color:var(--warning)}.stock-table-quote-time{font-size:11px;min-width:135px}
.stock-index-mobile{display:none}.x-pager{font-size:12px}
.stock-index-section-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}.stock-index-section-head h2{font-size:16px;margin-bottom:14px}.stock-index-section-head>span{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.stock-index-method{color:var(--muted);font-size:12px;margin:0 0 14px}.stock-index-method summary{cursor:pointer}.stock-index-method p{max-width:650px;margin:7px 0 0;line-height:1.55}
.stock-index-live-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:0 30px}
.stock-index-live-card{padding:18px 0 22px;border-top:1px solid var(--border);min-width:0}.stock-live-heading{display:flex;justify-content:space-between;align-items:start;gap:10px}.stock-live-heading>div{display:grid;gap:3px;min-width:0}
.stock-live-heading strong{font-size:14px}.stock-live-heading small{font-size:12px;color:var(--muted)}.stock-live-heading>span{font:500 28px var(--number-font);font-variant-numeric:tabular-nums}
.stock-coverage-line{display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px 12px;margin-top:14px;color:var(--muted);font-size:12px}.stock-coverage-track{height:5px;border-radius:999px;background:var(--surface-raised);overflow:hidden;margin:7px 0 14px}.stock-coverage-track span{display:block;height:100%;background:var(--accent)}
.stock-basket-meta{margin:12px 0 5px;font-size:12px;color:var(--muted)}.stock-component{display:grid;grid-template-columns:minmax(70px,1fr) minmax(60px,2fr) 65px;gap:12px;align-items:center;padding:9px 0;border-top:1px solid var(--border)}
.stock-component a{color:var(--text);min-width:0;overflow:hidden;text-overflow:ellipsis;font-size:12px}.stock-component strong{text-align:right;font:600 12px var(--number-font);font-variant-numeric:tabular-nums}
.stock-component-track{height:6px;background:var(--surface-raised);overflow:hidden}.stock-component-track span{display:block;height:100%;background:var(--accent)}
.stock-component-more{border-top:1px solid var(--border);padding-top:8px}.stock-component-more summary{cursor:pointer;color:var(--muted);font-size:12px}
.stock-index-unavailable{margin-top:14px;padding:12px 0;border-block:1px solid var(--border)}.stock-index-unavailable>summary{display:flex;align-items:center;gap:7px;cursor:pointer;color:var(--muted);font-size:13px;list-style:none}.stock-index-unavailable>summary::-webkit-details-marker{display:none}
.stock-index-unavailable>summary strong{color:var(--text)}.stock-index-unavailable>summary span{margin-left:auto}.stock-index-unavailable .v2-baskets{margin-top:12px}
.stock-index-page :is(a,button,summary,.scroll):focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@keyframes stock-theme-arrival{from{background:var(--accent-soft);border-color:var(--accent)}to{}}
@media(max-width:850px){.stock-volume-row{grid-template-columns:minmax(110px,1fr) minmax(50px,1fr) 88px;gap:12px}.stock-volume-value{font-size:13px}}
@media(max-width:760px){.stock-index-table{display:none}.stock-index-mobile{display:grid;gap:10px}.stock-index-card{display:flex;flex-direction:column;gap:9px;padding:15px;border:1px solid var(--border);border-radius:8px;background:var(--panel);color:var(--text);text-decoration:none;min-width:0}
.stock-index-card>div{display:flex;align-items:center;justify-content:space-between;gap:10px;min-width:0}.stock-index-card small{font-size:12px;color:var(--muted);line-height:1.6}.stock-index-card strong{min-width:0;overflow-wrap:anywhere}.stock-index-card strong small{display:block;margin-top:3px}
.stock-mobile-quotes>span{display:grid;gap:4px;min-width:0;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.stock-mobile-quote-time{padding-top:8px;border-top:1px solid var(--border)}.stock-index-card .stock-atlas-equity{margin:0}
}
@media(max-width:600px){.stock-index-page{gap:16px}.stock-index-page>.panel,.stock-index-page>.stock-filter-panel{padding:16px}.stock-index-heading h2{font-size:25px}.stock-overview{grid-template-columns:1fr;gap:14px;padding:16px}.stock-overview-copy{border-right:0;padding-right:0;display:grid;grid-template-columns:auto 1fr;gap:0 14px;align-items:baseline}.stock-overview-copy>strong{font-size:30px;margin:0}.stock-overview-copy>small,.stock-paired-count{grid-column:1/-1}.stock-overview-copy>small{margin-top:4px}.stock-overview-copy>.stock-statistics-time{margin-top:6px}.stock-paired-count{margin-top:5px}
.stock-volume-row{grid-template-columns:minmax(0,1fr) minmax(35px,.65fr) minmax(80px,.7fr);gap:10px}.stock-overview-method{font-size:11px}.stock-index-toolbar{align-items:flex-start;gap:10px}.stock-index-toolbar .stock-directory-meta{width:100%}.stock-segments button{padding:7px 10px}
.stock-atlas-grid{grid-template-columns:1fr}.stock-atlas-price-row{grid-template-columns:minmax(0,1fr) minmax(85px,auto)}.stock-atlas-tile-top{flex-wrap:wrap;gap:3px}.stock-overview-chart-heading{line-height:1.6}.stock-component{gap:8px;grid-template-columns:minmax(60px,1fr) minmax(50px,2fr) 60px}
}
@media(prefers-reduced-motion:reduce){.stock-atlas-tile.is-arriving{animation:none;border-color:var(--accent)}.stock-index-page *{transition:none}}
.stock-atlas-identity{display:flex;align-items:center;gap:10px;margin:9px 0 13px;min-height:36px}.stock-atlas-identity .stock-atlas-ticker{margin:0;min-height:0;min-width:0}.stock-list-identity{display:flex;align-items:center;gap:9px;min-width:0}.stock-list-identity>span,.stock-list-identity>strong{min-width:0;overflow-wrap:anywhere}
</style>
