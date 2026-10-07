<template>
  <div class="stock-theme-page">
    <section class="theme-heading">
      <div><p class="theme-eyebrow">CLIPERX / {{ tr('股票主题','STOCK THEME') }}</p><div class="theme-heading-identity"><AssetAvatar :asset="selectedStock" :name="themeName" :symbol="theme.ticker" :identity="'stock:'+theme.ticker" :size="40" /><h2>{{ themeName }} <span v-if="themeName!==theme.ticker">{{ themeCodeLabel }}</span></h2></div><p class="theme-intro">{{ tr('正股、链上代币与关联 Meme','Underlying stock, on-chain tokens and related Memes') }}</p></div>
      <button type="button" class="theme-follow" :aria-pressed="followed" @click="toggleFollow">{{ followed?tr('★ 已关注','★ Following'):tr('☆ 关注','☆ Follow') }}</button>
    </section>

    <div v-if="loading" class="panel theme-skeleton" role="status" :aria-label="tr('加载股票主题','Loading stock theme')"><p class="theme-note">{{ themeError?tr('主题暂时无法加载。','The theme could not be loaded.'):loadSlow?tr('加载较慢，点此重试','Loading slowly, retry'):tr('正在加载股票主题…','Loading the stock theme…') }} <button type="button" :disabled="themeLoading" @click="loadTheme">{{ tr('重试','Retry') }}</button></p><div v-for="n in 4" :key="n" class="skeleton-line"></div></div>
    <template v-else>
      <div class="theme-refresh-status" :class="{'has-error':themeError}" :role="themeError?'alert':'status'">
        <span v-if="themeError">{{ tr('刷新失败，保留上次结果。','Refresh failed. Previous results are retained.') }}</span>
        <span v-else-if="themeLoading">{{ tr('正在更新…','Updating…') }}</span>
        <span>{{ tr('快照时间','Snapshot time') }} · {{ date(snapshotAt) }}</span>
        <span v-if="presentation.poolObservedAt">{{ tr('池成交观测','Pool volume observed') }} · {{ date(presentation.poolObservedAt) }}<template v-if="presentation.poolObservedLatest!==presentation.poolObservedAt"> – {{ date(presentation.poolObservedLatest) }}</template></span>
        <button type="button" :disabled="themeLoading" @click="loadTheme">{{ themeError?tr('重试','Retry'):tr('刷新','Refresh') }}</button>
      </div>
      <section v-if="!theme.versions.length && !theme.rows.length && !theme.recordedPools.length" class="panel theme-empty">
        <h2>{{ scope==='all'?tr('暂未收录该股票主题','This theme is not indexed yet'):tr('当前网络暂无该主题','No theme data on this chain') }}</h2>
        <p>{{ scope==='all'?tr('目前没有可展示的股票代币或关联 Meme。','No stock token or related Meme is available yet.'):tr('当前网络没有该主题的股票代币或关联 Meme；可查看全部链。','No stock token or related Meme for this theme is in the selected chain. View all chains.') }}</p>
        <RouterLink :replace="scope!=='all'" :to="scope==='all'?directoryLink:{path:route.path,query:{...route.query,chain:'all'}}">{{ scope==='all'?tr('返回股票主题','Back to stock themes'):tr('查看全部链','View all chains') }} →</RouterLink>
      </section>
      <template v-else>
        <StockEquityMarket v-if="theme.versions.length" :stock="equityStock" :ticker="ticker" :chain="scope" :market-status="themePayload?.marketStatus" />
        <section v-if="theme.versions.length" class="panel theme-reference" :aria-label="tr('股票代币行情','Stock-token market')">
          <div class="theme-reference-summary">
            <div class="theme-reference-identity"><span class="theme-note">{{ tr('股票代币行情','Stock-token market') }}</span><strong>{{ selectedStock?.tokenSymbol || tr('暂无已核实报价','No verified quote') }}</strong><small>{{ selectedStock ? chainLabel(selectedStock.chainId) : tr('暂无当前可展示的股票代币报价','No stock-token quote available') }}<template v-if="selectedStock"> · {{ selectedStock.priceCurrency || tr('单位待核实','Unit unverified') }}</template></small></div>
            <div v-if="selectedStock" class="theme-quote"><strong :title="stockPriceTitle"><LiveNumber :value="stockQuotePrice" :currency="selectedStock.priceCurrency" format="price" :label="tr('股票代币价格','Stock-token price')" :description="stockPriceTitle" /></strong><span :class="stockQuoteChange!=null&&fieldCurrent(selectedStock,'change24h')?(stockQuoteChange>0?'up':stockQuoteChange<0?'down':''):''"><LiveNumber :value="stockQuoteChange" format="percent" :label="tr('24h 价格涨跌','24h price change')" /> · 24h<small v-if="stockQuoteChange!=null&&!fieldCurrent(selectedStock,'change24h')"> · {{ tr('历史值','Historical') }}</small></span></div>
            <div v-if="selectedStock" class="theme-quote-source"><QuoteStatus :row="selectedStock" /><small>{{ stockPriceProvider }} · {{ tr('报价时间','Quote time') }} {{ date(stockPriceAt) }}</small><small v-if="selectedStock.priceScope==='issuer-derived'">{{ tr('发行方参考价','Issuer reference price') }}</small></div>
            <div v-if="selectedStock" class="theme-quote-actions"><button type="button" class="theme-chart-toggle" :aria-expanded="chartExpanded" aria-controls="theme-reference-chart" @click="chartExpanded=!chartExpanded">{{ chartExpanded?tr('收起走势','Hide chart'):tr('价格走势','Price history') }} {{ chartExpanded?'⌃':'⌄' }}</button><RouterLink :to="stockAssetLink(selectedStock)">{{ tr('行情详情','Market details') }} →</RouterLink></div>
          </div>
          <div v-if="chartExpanded && selectedStock" id="theme-reference-chart" class="theme-reference-body">
            <ThemeHistoryChart v-if="hasChartSeries" compact :range="chartRange" :data="chartData" :loading="chartLoading" :error="chartError" @update:range="selectChartRange" @refresh="loadChart" />
            <p v-else class="theme-note" :role="chartLoading?'status':undefined">{{ chartLoading?tr('正在加载历史行情…','Loading market history…'):chartError?tr('历史行情暂时无法加载。','Market history could not be loaded.'):tr('暂无可用历史行情。','No market history is available.') }} <button v-if="!chartLoading" type="button" class="theme-text-button" @click="loadChart">{{ tr('重试','Retry') }}</button></p>
          </div>
          <StockTokenLiveChart :options="themePayload?.tradingMarkets??[]" />
        </section>

        <SignalLegend />
        <section v-if="theme.pairedCount" class="theme-metrics" :aria-label="tr('主题数据摘要','Theme data summary')">
          <div><span>{{ tr('当前同池 Meme','Current pool-paired Memes') }}</span><strong>{{ num(theme.pairedCount) }}</strong></div>
          <div><span>{{ tr('股票配对池成交','Stock-pair pool volume') }} · 24h USD</span><strong>{{ usd(presentation.poolTotal) }}</strong><small>{{ poolVolumeKnown }} / {{ theme.pools.length }} {{ tr('个池有成交数据','pools with volume data') }}</small></div>
          <div><span>{{ tr('计入统计的配对池','Paired pools in current totals') }}</span><strong>{{ num(theme.pools.length) }}</strong></div>
        </section>
        <div v-else class="theme-pairing-status"><span>{{ scope==='all'?tr('当前暂无可计入统计的配对池。','No paired pools currently qualify for totals.'):tr('当前网络暂无可计入统计的配对池。','No paired pools currently qualify on this chain.') }}<template v-if="theme.recordedPools.length"> {{ tr('池已记录','Pools recorded') }} {{ theme.recordedPools.length }} · {{ tr('暂未计入统计，配对记录见下方。','Outside current totals; pairing records are below.') }}</template></span><RouterLink :replace="scope!=='all'" :to="scope==='all'?directoryLink:{path:route.path,query:{...route.query,chain:'all'}}">{{ scope==='all'?tr('其他股票主题','Other stock themes'):tr('查看全部链','View all chains') }} →</RouterLink></div>

        <section v-if="theme.rows.length" class="panel theme-related-panel">
          <div class="panel-head"><h2>{{ relationshipScope==='name'?tr('名称线索','Name clues'):tr('同池 Meme','Pool-paired Memes') }}</h2><span class="theme-note">{{ visibleRelatedRows.length }} Meme<template v-if="relationshipScope==='paired'"> · 24h USD</template></span></div>
          <div v-if="theme.pairedCount && theme.nameCount" class="theme-relation-tabs" :aria-label="tr('关联范围','Relationship scope')">
            <button type="button" :aria-pressed="relationshipScope==='paired'" @click="selectRelationshipScope('paired')">{{ tr('当前同池','Current pool pairs') }} <span>{{ theme.pairedCount }}</span></button>
            <button type="button" :aria-pressed="relationshipScope==='name'" @click="selectRelationshipScope('name')">{{ tr('名称匹配','Name matches') }} <span>{{ theme.nameCount }}</span></button>
          </div>
          <p class="theme-table-scope">{{ relationshipScope==='paired'?tr('成交仅含直接配对池 · 覆盖与观测时间见各行。','Direct paired-pool volume only · coverage and observation times are shown per row.'):tr('仅名称线索，尚未确认同池关系，不计入配对池成交。','Name clues only: shared-pool relationships are unconfirmed and excluded from paired-pool volume.') }}</p>
          <div v-if="visibleRelatedRows.length" class="scroll" tabindex="0" :aria-label="tr('关联 Meme 数据表，可横向滚动','Related Meme data table; scroll horizontally')">
            <table class="tbl theme-assets-table"><thead><tr><th>Meme</th><th class="numeric">{{ tr('价格 / 1h 涨跌','Price / 1h change') }}</th><th v-if="relationshipScope==='paired'" class="numeric">{{ tr('股票配对池成交','Stock-pair pool volume') }}<small>24h · USD</small></th><th>{{ tr('风险','Risk') }}</th><th>{{ tr('操作','Actions') }}</th></tr></thead><tbody>
              <tr v-for="row in visibleRelatedRows" :key="row.key">
                <td><RouterLink :to="assetLink(row)"><strong>{{ row.asset?.symbol || short(row.token) }}</strong></RouterLink><small>{{ chainLabel(row.chainId) }} · {{ short(row.token) }}</small><div class="theme-row-relation"><RelationBadge :relation="row.pools[0]?.relation || row.asset?.match || {level:row.level}" /><small v-if="relationshipScope==='paired'">{{ row.pools.length }} {{ tr('个直接配对池','direct paired pools') }}</small></div></td>
                <td class="numeric"><LiveNumber :value="row.asset?.price" :currency="row.asset?.priceCurrency" format="price" :label="tr('Meme 价格','Meme price')" /><small v-if="row.asset?.price!=null&&!fieldCurrent(row.asset,'price')" class="historical">{{ tr('历史价格','Historical price') }}</small><small :class="fieldCurrent(row.asset,'change1h')?(Number(row.asset?.change1h)>0?'up':Number(row.asset?.change1h)<0?'down':''):''" :title="fieldTitle(row.asset,'change1h')">{{ pct(row.asset?.change1h) }} · 1h<template v-if="row.asset?.change1h!=null&&!fieldCurrent(row.asset,'change1h')"> · {{ tr('历史值','Historical') }}</template></small></td>
                <td v-if="relationshipScope==='paired'" class="numeric"><LiveNumber :value="row.poolVolume" currency="USD" :label="tr('股票配对池 24h 成交','Stock-pair pool 24h volume')" /><small>{{ tr('池成交覆盖','Pool volume coverage') }} {{ row.poolKnownCount }} / {{ row.pools.length }}</small><small v-if="row.poolShare!=null">{{ shareText(row.poolShare) }} {{ tr('占已知池成交','of known pool volume') }}</small><small v-if="row.poolObservedAt" :title="date(row.poolObservedAt)">{{ tr('观测','Observed') }} {{ age(row.poolObservedAt) }}</small></td>
                <td><RiskBadge :asset="row.asset || {}" /></td>
                <td><div class="theme-row-actions"><RouterLink :to="assetLink(row)">{{ tr('查看行情','View market') }} →</RouterLink><RouterLink v-if="row.level==='A'" :to="assetLink(row,true)">{{ tr('配对依据','Pairing evidence') }}</RouterLink></div></td>
              </tr>
            </tbody></table>
          </div>
          <p v-else class="theme-note">{{ tr('所选分组当前暂无记录。','The selected group currently has no records.') }} <button type="button" class="theme-text-button" @click="selectRelationshipScope(theme.pairedCount?'paired':'name')">{{ tr('查看现有记录','View available records') }}</button></p>
        </section>

        <section v-if="theme.recordedPools.length" class="panel theme-recorded-relations">
          <div class="panel-head"><h2>{{ tr('已记录的配对池','Recorded paired pools') }}</h2><span class="theme-note">{{ theme.recordedPools.length }} · {{ tr('暂未计入统计','Outside current totals') }}</span></div>
          <article v-for="row in visibleRecordedPools" :key="row.key" class="recorded-pool-row">
            <div class="recorded-pool-heading"><strong>{{ row.asset?.symbol || row.asset?.name || short(row.token) }} / {{ poolStockSymbol(row) }}</strong><span>{{ chainLabel(row.chainId) }} · {{ row.relation.protocol || 'DEX' }}</span></div>
            <div class="recorded-pool-footer"><RelationBadge :relation="row.relation" /><span class="recorded-pool-reason">{{ recordedReason(row.relation) }}</span><RouterLink v-if="row.detailAvailable" :to="recordedAssetLink(row)">{{ tr('配对依据','Pairing evidence') }} →</RouterLink></div>
            <details class="recorded-pool-details"><summary>{{ tr('地址与记录','Addresses and records') }}</summary><div class="recorded-pool-addresses"><span>{{ tr('交易池','Pool') }} <a v-if="explorer(row.relation.pool,'address',row.chainId)" :href="explorer(row.relation.pool,'address',row.chainId)" target="_blank" rel="noopener">{{ row.relation.pool }} ↗</a><span v-else>{{ row.relation.pool }}</span></span><span>{{ tr('Meme 合约','Meme contract') }} <a v-if="explorer(row.token,'address',row.chainId)" :href="explorer(row.token,'address',row.chainId)" target="_blank" rel="noopener">{{ row.token }} ↗</a><span v-else>{{ row.token }}</span></span><span>{{ tr('股票代币合约','Stock-token contract') }} <a v-if="explorer(row.relation.stock,'address',row.chainId)" :href="explorer(row.relation.stock,'address',row.chainId)" target="_blank" rel="noopener">{{ row.relation.stock }} ↗</a><span v-else>{{ row.relation.stock }}</span></span></div><p v-if="row.relation.liquidityAt" class="theme-note">{{ tr('流动性观测','Liquidity observed') }} · {{ date(row.relation.liquidityAt) }}</p><p v-if="row.relation.checkedAt" class="theme-note">{{ tr('关系核验','Relation checked') }} · {{ date(row.relation.checkedAt) }}</p></details>
          </article>
          <button v-if="theme.recordedPools.length>3" type="button" class="theme-text-button" :aria-expanded="showAllRecordedPools" @click="showAllRecordedPools=!showAllRecordedPools">{{ showAllRecordedPools?tr('收起更多交易池','Show fewer pools'):tr('查看其余','Show')+' '+(theme.recordedPools.length-3)+' '+tr('个交易池','more pools') }}</button>
        </section>

        <details v-if="showVolumeDistribution" class="panel theme-volume-distribution">
          <summary><span>{{ tr('股票配对池成交分析','Stock-pair pool volume analysis') }}</span><small>{{ presentation.knownMemes }} / {{ presentation.pairedMemes }} Meme · 24h USD</small></summary>
          <p class="theme-table-scope">{{ tr('占比以已知的直接配对池成交为分母，缺失数据不补零。','Shares use known direct paired-pool volume; missing data is not filled with zero.') }}</p>
          <div v-for="row in presentation.contributions" :key="row.key" class="contribution-row"><strong>{{ row.asset?.symbol || short(row.token) }} <small>{{ chainLabel(row.chainId) }}</small></strong><span>{{ usd(row.poolVolume) }} · {{ shareText(row.poolShare) }}</span><div class="contribution-bar"><i :style="{width:((row.poolShare??0)*100)+'%'}"></i></div></div>
          <p v-if="metricValue('topTwoShare')!=null" class="theme-note">{{ tr('前两名占已知配对池成交','Top two shares of known paired-pool volume') }} {{ shareText(metricValue('topTwoShare')) }}</p>
          <p v-if="metricValue('volumeRatio7d')!=null" class="theme-note">{{ tr('7 日量比','7-day volume ratio') }} {{ metricValue('volumeRatio7d').toFixed(2) }}×</p>
        </details>
        <section v-if="theme.events.length" class="panel theme-events"><div class="panel-head"><h2>{{ tr('近期关联记录','Recent related records') }}</h2><RouterLink :to="pageNavigationLink('/events',{route,scope})">{{ tr('全部记录','All records') }} →</RouterLink></div><article v-for="event in visibleEvents" :key="event.key"><time :title="date(event.at)">{{ age(event.at) }}</time><RouterLink :to="eventAssetLink(event)"><strong>{{ event.asset?.symbol || short(event.token) }}</strong> · {{ eventFact(event) }}</RouterLink><small>{{ chainLabel(event.chainId) }}<template v-if="event.count>1"> · {{ event.count }} {{ tr('次记录','records') }}</template></small></article><button v-if="theme.events.length>3" type="button" class="theme-text-button" :aria-expanded="showAllEvents" @click="showAllEvents=!showAllEvents">{{ showAllEvents?tr('收起更多记录','Show fewer records'):tr('更多记录','More records') }}</button></section>
        <details class="theme-methods"><summary>{{ tr('统计口径与数据来源','Definitions and data sources') }}</summary><p class="theme-note">{{ tr('股票代币价格不是正股每股价格。同池统计仅纳入流动性估值不少于 $1,000 且最近 15 分钟有观测的已核实配对池；名称匹配和暂未计入的交易池不计入成交。','Stock-token prices are not underlying share prices. Totals include verified paired pools with liquidity of at least $1,000 observed within the last 15 minutes. Name matches and other recorded pools are excluded.') }}</p><p class="theme-note">{{ tr('配对池成交只计与本主题直接配对的池。Meme 全部市场成交请查看行情详情；未知数据不补零。','Paired-pool volume includes only pools directly paired with this theme. View market details for Meme all-market volume. Unknown values are not filled with zero.') }}</p><p v-if="theme.events.length" class="theme-note">{{ tr('记录时间为本站收录时间，池创建时间来自链上交易。','Record time is the site indexing time; pool creation time comes from on-chain transactions.') }}</p>
          <template v-if="theme.versions.length"><label v-if="quoteVersions.length>1" class="theme-version-select">{{ tr('报价版本','Quote version') }} <select :value="versionKey(selectedStock)" @change="selectStockVersion($event.target.value)"><option v-for="stock in quoteVersions" :key="versionKey(stock)" :value="versionKey(stock)">{{ stock.tokenSymbol || theme.ticker }} · {{ chainLabel(stock.chainId) }}</option></select></label><p class="theme-note">{{ tr('切换参考代币仅影响报价与走势，不改变关联 Meme 范围。','Changing the reference token affects quotes and history only, not related Memes.') }}</p><p class="theme-market-status">{{ tr('正股市场','Underlying stock market') }} · {{ marketLabel }} <small>{{ tr('股票代币可全天交易','Stock tokens may trade around the clock') }}</small></p><div class="theme-version-list"><div v-for="stock in theme.versions" :key="versionKey(stock)"><span>{{ stock.tokenSymbol }} · {{ chainLabel(stock.chainId) }}</span><RouterLink :to="stockAssetLink(stock)">{{ tr('查看代币','View token') }} {{ short(stock.tokenContractAddress) }} →</RouterLink></div></div></template>
        </details>
      </template>
    </template>
  </div>
</template>

<script setup>
import AssetAvatar from '../components/AssetAvatar.vue';
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useFeedStore } from '../stores/feed';
import { useMinuteClock } from '../composables/useMinuteClock';
import { tr, useI18n } from '../i18n';
import { age, date, explorer, num, pct, short, usd } from '../utils/format';
import { chainScope } from '../utils/chain-scope';
import { navigationSource, pageNavigationLink, safeInternalBack } from '../utils/navigation-context';
import { readPageState, writePageState } from '../utils/page-navigation-state';
import { buildStockTheme, isRecentObservation, normalizeTicker, stockThemeCodeLabel, stockThemeName } from '../utils/stock-theme-model';
import { canShowStockVolumeDistribution, currentStockTheme, recordedPoolReason, stockThemePresentation, stockThemeSnapshotTime } from '../utils/stock-theme-presentation';
import { stockDirectoryLink, themeAssetLink } from '../utils/stock-navigation';
import { themeChartSeries } from '../utils/theme-chart-model';
import { isOfficialStock, preferredOfficialStock } from '../utils/product-labels';
import { readThemeWatches, writeThemeWatches } from '../utils/theme-map-model';
import ThemeHistoryChart from '../components/ThemeHistoryChart.vue';
import StockEquityMarket from '../components/StockEquityMarket.vue';
import StockTokenLiveChart from '../components/StockTokenLiveChart.vue';
import { getStockTheme, getStockChart } from '../api/product';
import LiveNumber from '../components/LiveNumber.vue';
import QuoteStatus from '../components/QuoteStatus.vue';
import RelationBadge from '../components/RelationBadge.vue';
import SignalLegend from '../components/SignalLegend.vue';
import RiskBadge from '../components/RiskBadge.vue';

const route=useRoute(), router=useRouter(), feed=useFeedStore(), clock=useMinuteClock(), { lang }=useI18n();
// A response may arrive between clock ticks. Compare its observations with
// receipt time immediately, without replacing any provider timestamp.
const receivedAt=ref(0), now=computed(()=>Math.max(clock.value,receivedAt.value));
const scope=computed(()=>chainScope(route.query)), ticker=computed(()=>normalizeTicker(route.params.ticker));
const themePayload=ref(null),themeLoading=ref(true),themeError=ref(false),loadSlow=ref(false);
const chartData=ref(null),chartLoading=ref(false),chartError=ref(false),chartRange=ref('24h'),chartExpanded=ref(false);
let themeRequest=0,chartRequest=0,slowTimer,refreshTimer;
const loading=computed(()=>!themePayload.value);
const theme=computed(()=>currentStockTheme(buildStockTheme({ticker:ticker.value,stockTokens:themePayload.value?.unified?.stockTokens??[],assets:themePayload.value?.unified?.assets??[],relations:themePayload.value?.unified?.relations??[],events:themePayload.value?.events??themePayload.value?.unified?.events??feed.relationships,scope:scope.value,now:now.value}),now.value,themePayload.value?.unified?.relations??[]));
const presentation=computed(()=>stockThemePresentation(theme.value,themePayload.value?.themeMetrics,now.value));
const snapshotAt=computed(()=>stockThemeSnapshotTime(themePayload.value));
const showAllRecordedPools=ref(false),showAllEvents=ref(false),relationshipScope=ref('paired'),initialDefaultsApplied=ref(false);
const visibleRecordedPools=computed(()=>showAllRecordedPools.value?theme.value.recordedPools:theme.value.recordedPools.slice(0,3));
const visibleEvents=computed(()=>showAllEvents.value?theme.value.events:theme.value.events.slice(0,3));
const visibleRelatedRows=computed(()=>presentation.value.rows.filter(row=>row.level===(relationshipScope.value==='name'?'B':'A')));
const poolVolumeKnown=computed(()=>presentation.value.rows.reduce((sum,row)=>sum+row.poolKnownCount,0));
const showVolumeDistribution=computed(()=>canShowStockVolumeDistribution(presentation.value));
function metricValue(field){const metric=themePayload.value?.themeMetrics?.[field],value=metric?.value??metric;return isRecentObservation(themePayload.value?.themeMetrics?.at,900000,now.value)&&value!=null&&value!==''&&typeof value!=='object'&&Number.isFinite(Number(value))?Number(value):null;}
const marketLabel=computed(()=>({open:tr('交易中','Open'),closed:tr('休市','Closed'),pre:tr('盘前','Pre-market'),premarket:tr('盘前','Pre-market'),post:tr('盘后','After-hours'),afterhours:tr('盘后','After-hours'),halted:tr('暂停交易','Halted')}[themePayload.value?.marketStatus?.status]??tr('未能识别','Unknown')));
async function loadTheme(){
  const request=++themeRequest;themeLoading.value=true;themeError.value=false;loadSlow.value=false;
  clearTimeout(slowTimer);slowTimer=setTimeout(()=>{if(request===themeRequest)loadSlow.value=true;},8000);
  try{const result=await getStockTheme(ticker.value,scope.value);if(request===themeRequest){receivedAt.value=Date.now();themePayload.value=result;
    // Defaults belong to a route's first successful response, never to a refresh.
    if(!initialDefaultsApplied.value){initialDefaultsApplied.value=true;relationshipScope.value=routeRelationshipScope()??(theme.value.pairedCount?'paired':'name');selectedVersion.value=selectedStock.value?versionKey(selectedStock.value):'';chartExpanded.value=restoredChartExpanded??!!selectedStock.value;persistDisclosureState();}
  }}
  catch{if(request===themeRequest)themeError.value=true;}
  finally{if(request===themeRequest){themeLoading.value=false;clearTimeout(slowTimer);}}
}
async function loadChart(){
  if(!selectedStock.value)return;
  const request=++chartRequest;chartLoading.value=true;chartError.value=false;
  try{const result=await getStockChart(ticker.value,scope.value,'1h',chartRange.value,selectedStock.value);if(request===chartRequest)chartData.value=result;}
  catch{if(request===chartRequest)chartError.value=true;}
  finally{if(request===chartRequest)chartLoading.value=false;}
}
const selectedVersion=ref('');
const versionKey=stock=>`${stock?.chainId}:${stock?.tokenContractAddress}`;
const quoteVersions=computed(()=>theme.value.versions.filter(stock=>isOfficialStock(stock)&&stock.tokenContractAddress));
const selectedStock=computed(()=>quoteVersions.value.find(stock=>versionKey(stock)===selectedVersion.value)??preferredOfficialStock(quoteVersions.value));
const equityStock=computed(()=>[...theme.value.versions].sort((a,b)=>(b.referenceAt??0)-(a.referenceAt??0))[0]);
function routeRelationshipScope(){return ['paired','name'].includes(route.query.relationScope)?route.query.relationScope:null;}
function routeStockVersion(){return typeof route.query.version==='string' && /^\d{1,16}:0x[0-9a-f]{40}$/i.test(route.query.version)?route.query.version:'';}
function replaceThemeChoice(key,value){if(route.query[key]!==value)router.replace({query:{...route.query,[key]:value}});}
function selectRelationshipScope(value){if(!['paired','name'].includes(value))return;relationshipScope.value=value;replaceThemeChoice('relationScope',value);}
function selectStockVersion(value){if(!quoteVersions.value.some(stock=>versionKey(stock)===value))return;selectedVersion.value=value;replaceThemeChoice('version',value);}
function selectChartRange(value){if(!['24h','7d'].includes(value))return;chartRange.value=value;replaceThemeChoice('themeRange',value);}
const disclosureStateKey=computed(()=>`stock-theme-disclosures:${JSON.stringify([ticker.value,scope.value,
  safeInternalBack(route.query.back)??`source:${navigationSource(route)}`])}`);
let activeDisclosureKey='',restoredChartExpanded=null,restoringDisclosures=false;
function persistDisclosureState(){
  if(!activeDisclosureKey||restoringDisclosures||!initialDefaultsApplied.value)return;
  // Store only layout choices. Quotes, chart samples and timestamps are fetched
  // normally when returning, and never enter the page-state cache.
  writePageState(activeDisclosureKey,{showAllRecordedPools:showAllRecordedPools.value,
    showAllEvents:showAllEvents.value,chartExpanded:chartExpanded.value});
}
function restoreDisclosureState(){
  const key=disclosureStateKey.value;
  if(key===activeDisclosureKey)return;
  persistDisclosureState();activeDisclosureKey=key;
  const saved=readPageState(key)??{};
  restoredChartExpanded=typeof saved.chartExpanded==='boolean'?saved.chartExpanded:null;
  restoringDisclosures=true;
  showAllRecordedPools.value=saved.showAllRecordedPools===true;showAllEvents.value=saved.showAllEvents===true;
  chartExpanded.value=restoredChartExpanded??(initialDefaultsApplied.value&&!!selectedStock.value);
  restoringDisclosures=false;persistDisclosureState();
}
watch([ticker,scope],()=>{persistDisclosureState();initialDefaultsApplied.value=false;restoreDisclosureState();relationshipScope.value=routeRelationshipScope()??'paired';selectedVersion.value=routeStockVersion();chartRange.value=route.query.themeRange==='7d'?'7d':'24h';themePayload.value=null;chartData.value=null;chartError.value=false;chartRequest++;chartLoading.value=false;loadTheme();feed.load(scope.value==='all'?undefined:scope.value);},{immediate:true});
watch(disclosureStateKey,restoreDisclosureState,{flush:'post'});
watch([showAllRecordedPools,showAllEvents,chartExpanded],persistDisclosureState,{flush:'sync'});
watch(()=>route.query.relationScope,()=>{relationshipScope.value=routeRelationshipScope()??(theme.value.pairedCount?'paired':'name');});
watch(()=>route.query.version,()=>{selectedVersion.value=routeStockVersion();});
watch(()=>route.query.themeRange,()=>{chartRange.value=route.query.themeRange==='7d'?'7d':'24h';});
watch([chartRange,()=>selectedStock.value?versionKey(selectedStock.value):''],()=>{chartRequest++;chartData.value=null;chartError.value=false;chartLoading.value=false;if(chartExpanded.value)loadChart();});
watch(chartExpanded,open=>{if(open&&!chartData.value&&!chartLoading.value)loadChart();});
const hasChartSeries=computed(()=>themeChartSeries(chartData.value??{}).prices.length>0);
const themeName=computed(()=>stockThemeName(selectedStock.value??theme.value.versions[0],theme.value.ticker,lang.lang));
const themeCodeLabel=computed(()=>stockThemeCodeLabel(selectedStock.value??theme.value.versions[0],theme.value.ticker,lang.lang));
const stockQuotePrice=computed(()=>{const stock=selectedStock.value;return typeof stock?.price==='number'&&Number.isFinite(stock.price)&&stock.price>0&&stock.priceCurrency?stock.price:null;});
const stockQuoteChange=computed(()=>typeof selectedStock.value?.change24h==='number'&&Number.isFinite(selectedStock.value.change24h)?selectedStock.value.change24h:null);
const stockPriceTitle=computed(()=>`${tr('每枚股票代币价格，不是正股每股价格。','Price per stock token, not per underlying stock share.')} · ${selectedStock.value?.priceCurrency||tr('单位待核实','unit unverified')} · ${selectedStock.value?.fieldSources?.price??selectedStock.value?.provider??'—'} · ${date(selectedStock.value?.fieldTimes?.price??selectedStock.value?.quoteAt)}`);
const stockPriceAt=computed(()=>selectedStock.value?.fieldTimes?.price??selectedStock.value?.quoteAt);
const stockPriceProvider=computed(()=>selectedStock.value?.fieldSources?.price??selectedStock.value?.provider??tr('来源未能识别','Unknown source'));
const watched=ref(readThemeWatches()),watchKey=computed(()=>`stock:${theme.value.ticker}`),followed=computed(()=>watched.value.has(watchKey.value));
function syncWatches(){watched.value=readThemeWatches();}
function toggleFollow(){const next=new Set(watched.value);next.has(watchKey.value)?next.delete(watchKey.value):next.add(watchKey.value);writeThemeWatches(next);watched.value=next;}
onMounted(()=>{refreshTimer=setInterval(()=>{if(!document.hidden){loadTheme();if(chartExpanded.value)loadChart();}},30000);window.addEventListener('theme-watch-change',syncWatches);window.addEventListener('storage',syncWatches);});
onUnmounted(()=>{persistDisclosureState();themeRequest++;chartRequest++;clearTimeout(slowTimer);clearInterval(refreshTimer);window.removeEventListener('theme-watch-change',syncWatches);window.removeEventListener('storage',syncWatches);});
const directoryLink=computed(()=>stockDirectoryLink(route.query,scope.value));
function assetLink(row,relation=false){return themeAssetLink(row,{ticker:theme.value.ticker,chain:scope.value,themePath:route.fullPath,relation});}
function recordedAssetLink(row){return themeAssetLink(row,{ticker:theme.value.ticker,chain:scope.value,themePath:route.fullPath,relation:true,pool:row.relation.pool});}
function stockAssetLink(stock){return themeAssetLink({chainId:stock.chainId,token:stock.tokenContractAddress},{ticker:theme.value.ticker,chain:scope.value,themePath:route.fullPath});}
function eventAssetLink(event){return themeAssetLink(event,{ticker:theme.value.ticker,chain:scope.value,themePath:route.fullPath,relation:!!event.pool,pool:event.pool});}
function recordedReason(relation){return {'issuer-unverified':tr('股票代币待核实','Stock token unverified'),'liquidity-unknown':tr('估值待补齐','Valuation pending'),'liquidity-time-unknown':tr('估值时间待确认','Valuation time unconfirmed'),'liquidity-expired':tr('估值待更新','Valuation update pending'),'liquidity-below-threshold':tr('流动性未达到 $1,000','Liquidity below $1,000'),'criteria-unmet':tr('暂未满足统计条件','Outside current totals')}[recordedPoolReason(relation,now.value)];}
function poolStockSymbol(row){return theme.value.versions.find(stock=>String(stock.chainId)===String(row.chainId)&&String(stock.tokenContractAddress).toLowerCase()===String(row.relation.stock).toLowerCase())?.tokenSymbol||short(row.relation.stock);}
function shareText(value){return value==null?'—':`${(value*100).toFixed(1)}%`;}
function fieldTitle(asset,field){return `${field==='volume24h'?tr('该 Meme 的资产级 24h 成交；只展示当前可比美元成交。','This Meme’s asset-level 24h turnover; only current comparable USD volume is shown.'):tr('该 Meme 近 1 小时价格涨跌，单位 %。','This Meme’s one-hour price change, in %.')} · ${asset?.fieldSources?.[field]??'—'} · ${date(asset?.fieldTimes?.[field])}`;}
function fieldCurrent(asset,field){return isRecentObservation(asset?.fieldTimes?.[field],900000,now.value);}
function chainLabel(chain){return {'56':'BNB Chain','196':'X Layer','4663':'Robinhood Chain','5042':'Arc'}[chain]??chain;}
function eventFact(event){const pool=theme.value.pools.find(row=>String(row.relation.pool).toLowerCase()===String(event.pool).toLowerCase());return [pool?.relation?.protocol,event.kind==='pool-created'?tr('创建配对池','created a paired pool'):pool?eventLabel(event.kind):tr('收录交易池','pool indexed'),event.pool?short(event.pool):null].filter(Boolean).join(' · ');}
function eventLabel(kind){return kind==='pool-created'?tr('池创建记录','Pool creation record'):['verified','relation-verified'].includes(kind)?tr('同池关系记录','Pool relation recorded'):kind==='discovered'?tr('首次收录','First indexed'):tr('交易池记录','Pool recorded');}
</script>

<style scoped>
.stock-theme-page{display:grid;gap:18px;min-width:0}.theme-heading{display:flex;justify-content:space-between;align-items:center;gap:18px}.theme-heading h2{font-size:30px;line-height:1.25;font-weight:700;letter-spacing:-.7px;margin:3px 0}.theme-heading h2 span{font-size:13px;color:var(--muted);font-weight:400;margin-left:8px}.theme-eyebrow{color:var(--accent);font-size:12px;letter-spacing:.13em;margin:0 0 7px}.theme-intro{font-size:12px;color:var(--muted);margin:8px 0 0}.theme-follow{background:var(--panel);border:1px solid var(--border);border-radius:7px;padding:9px 13px;color:var(--text);font:inherit;font-size:12px;white-space:nowrap;cursor:pointer}.theme-follow[aria-pressed=true]{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}
.theme-refresh-status{display:flex;align-items:baseline;gap:6px 16px;flex-wrap:wrap;font-size:12px;color:var(--muted);line-height:1.7}.theme-refresh-status.has-error{padding:10px 12px;border:1px solid var(--warning);border-radius:7px;color:var(--warning)}.theme-refresh-status button,.theme-skeleton button{font:inherit;color:var(--accent);background:none;border:0;cursor:pointer;text-decoration:underline}.theme-refresh-status button{margin-left:auto}.theme-refresh-status button:disabled,.theme-skeleton button:disabled{cursor:wait;opacity:.6}
.stock-theme-page .panel{background:var(--panel);border:1px solid var(--border);border-radius:11px;padding:20px;margin:0;min-width:0}.stock-theme-page .panel-head h2{font-size:15px;font-weight:700}.theme-note,.theme-table-scope{font-size:12px;color:var(--muted);line-height:1.7}.theme-table-scope{margin:13px 0}.theme-empty{display:grid;gap:8px}.theme-empty h2{font-size:17px}.theme-empty p{font-size:13px;color:var(--muted)}
.theme-reference-summary{display:grid;grid-template-columns:minmax(130px,1fr) auto minmax(190px,1fr) auto;align-items:center;gap:12px 24px}.theme-reference-identity,.theme-quote-source{display:flex;flex-direction:column;gap:5px;min-width:0}.theme-reference-identity>strong{font-size:16px;overflow-wrap:anywhere}.theme-reference-identity>small,.theme-quote-source>small{font-size:11px;color:var(--muted);line-height:1.6}.theme-quote{display:flex;flex-direction:column;gap:5px;white-space:nowrap;font-variant-numeric:tabular-nums}.theme-quote>strong{font-size:28px;font-weight:650;letter-spacing:-.5px}.theme-quote>span{font-size:12px}.theme-quote-actions{display:flex;flex-direction:column;align-items:flex-start;gap:9px;white-space:nowrap;font-size:12px}.theme-quote-actions a{color:var(--accent);text-decoration:none}.theme-chart-toggle{color:var(--text);background:var(--panel);border:1px solid var(--border);padding:7px 10px;border-radius:6px;font:inherit;cursor:pointer}.theme-reference-body{border-top:1px solid var(--border);padding-top:15px;margin-top:16px}.theme-reference-body>.theme-note{margin:0}
.theme-metrics{display:grid;grid-template-columns:1fr 1.5fr 1fr;background:var(--panel);border:1px solid var(--border);border-radius:9px;padding:14px 18px;gap:16px}.theme-metrics>div{display:grid;grid-template-columns:1fr auto;align-items:baseline;gap:5px 12px;border-right:1px solid var(--border);padding-right:16px}.theme-metrics>div:last-child{border:0;padding-right:0}.theme-metrics span,.theme-metrics small{font-size:12px;color:var(--muted);line-height:1.6}.theme-metrics small{grid-column:1/-1}.theme-metrics strong{font-size:20px;font-weight:650;font-variant-numeric:tabular-nums}.theme-pairing-status{display:flex;justify-content:space-between;align-items:baseline;gap:10px 20px;flex-wrap:wrap;padding:12px 15px;background:var(--accent-soft);border-radius:8px;font-size:12px;line-height:1.7;color:var(--muted)}.theme-pairing-status a{color:var(--accent);text-decoration:none;white-space:nowrap}
.theme-relation-tabs{display:flex;gap:5px;margin-top:16px;border-bottom:1px solid var(--border)}.theme-relation-tabs button{color:var(--muted);background:none;border:0;border-bottom:2px solid transparent;padding:9px 12px 11px;font:inherit;font-size:13px;cursor:pointer}.theme-relation-tabs button[aria-pressed=true]{color:var(--accent);border-bottom-color:var(--accent)}.theme-relation-tabs button span{font-size:12px;margin-left:5px}.theme-assets-table{min-width:620px}.theme-assets-table th{white-space:nowrap;font-size:12px;background:var(--bg);padding:11px}.theme-assets-table th small,.theme-assets-table td small{display:block;font-size:11px;font-weight:400;color:var(--muted);line-height:1.6;margin-top:4px}.theme-assets-table td{padding:13px 11px;font-size:12px}.theme-assets-table .numeric{text-align:right;font-variant-numeric:tabular-nums}.theme-assets-table a{color:var(--text);text-decoration:none}.theme-assets-table a:hover{color:var(--accent)}.theme-assets-table .historical{color:var(--warning)}.theme-row-actions{display:flex;flex-direction:column;gap:8px;white-space:nowrap}.theme-row-actions a{color:var(--accent)}.theme-text-button{border:0;background:none;color:var(--accent);font:inherit;font-size:12px;cursor:pointer;padding:0;text-decoration:underline}
.stock-theme-page details>summary{display:flex;align-items:center;gap:9px;font-weight:600;font-size:12px;cursor:pointer;list-style:none}.stock-theme-page details>summary::-webkit-details-marker{display:none}.stock-theme-page details>summary::before{content:'▸';color:var(--accent)}.stock-theme-page details[open]>summary::before{content:'▾'}.stock-theme-page details>summary span{flex:1}.stock-theme-page details>summary small{font-size:11px;color:var(--muted);font-weight:400}.theme-methods{border-top:1px solid var(--border);padding:14px 4px}.theme-version-select{display:flex;align-items:center;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:12px}.theme-version-select select{max-width:260px;color:var(--text);background:var(--panel);border:1px solid var(--border);padding:7px 9px;border-radius:6px;font:inherit}.theme-market-status{display:flex;gap:6px 12px;flex-wrap:wrap;font-size:12px;color:var(--text);margin:12px 0 16px}.theme-market-status small{color:var(--muted);font-size:12px}.theme-version-list{margin-top:14px;border-top:1px solid var(--border)}.theme-version-list>div{display:flex;gap:8px 14px;justify-content:space-between;flex-wrap:wrap;font-size:12px;padding-top:12px;color:var(--muted)}.theme-version-list a{color:var(--accent)}
.contribution-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:7px 12px;padding:12px 0;border-bottom:1px solid var(--border);font-size:12px}.contribution-row>strong{min-width:0;overflow-wrap:anywhere}.contribution-row small{font-size:11px;font-weight:400;color:var(--muted);margin-left:5px}.contribution-row>span{text-align:right;font-variant-numeric:tabular-nums}.contribution-bar{grid-column:1/-1;height:4px;background:var(--bg);border-radius:3px;overflow:hidden}.contribution-bar i{display:block;height:100%;background:var(--accent);border-radius:3px}
.recorded-pool-row{padding:15px 0;border-bottom:1px solid var(--border)}.recorded-pool-row:last-of-type{border:0}.recorded-pool-heading,.recorded-pool-footer{display:flex;flex-wrap:wrap;gap:7px 14px;font-size:12px;line-height:1.6}.recorded-pool-heading{justify-content:space-between}.recorded-pool-heading>strong{overflow-wrap:anywhere}.recorded-pool-heading>span,.recorded-pool-footer{color:var(--muted)}.recorded-pool-footer{margin-top:8px;align-items:center}.recorded-pool-footer>a{margin-left:auto;color:var(--accent);white-space:nowrap;text-decoration:none}.recorded-pool-reason{color:var(--warning);background:color-mix(in srgb,var(--warning) 7%,transparent);padding:2px 7px;border-radius:4px}.recorded-pool-details{margin-top:11px}.recorded-pool-details>summary{color:var(--muted);font-weight:400!important}.recorded-pool-addresses{display:grid;gap:8px;margin-top:12px;color:var(--muted);font-size:11px;line-height:1.6;overflow-wrap:anywhere}.recorded-pool-addresses a{color:var(--accent)}.theme-recorded-relations>.theme-text-button,.theme-events>.theme-text-button{margin-top:12px}.theme-events article{display:grid;grid-template-columns:95px minmax(0,1fr) auto;gap:12px;padding:14px 0;border-bottom:1px solid var(--border);font-size:12px;line-height:1.6}.theme-events article time,.theme-events article small{color:var(--muted);font-size:11px}.theme-events a{font-size:12px;color:var(--accent)}
.stock-theme-page :is(a,button,summary,select,.scroll):focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(max-width:900px){.theme-reference-summary{grid-template-columns:minmax(130px,1fr) auto}.theme-quote-source{grid-column:1}.theme-quote-actions{flex-direction:row;align-items:center;justify-content:flex-end}.theme-metrics{grid-template-columns:1fr 1.4fr}.theme-metrics>div:nth-child(2){border:0;padding-right:0}.theme-metrics>div:last-child{grid-column:1/-1;border-top:1px solid var(--border);padding-top:9px}}
@media(max-width:600px){.stock-theme-page{gap:15px}.stock-theme-page .panel{padding:15px}.theme-heading{align-items:flex-start;gap:12px}.theme-heading h2{font-size:25px}.theme-heading h2 span{display:block;margin:4px 0 0;font-size:12px}.theme-follow{padding:8px 10px}.theme-reference-summary{gap:12px}.theme-reference-identity>strong{font-size:14px}.theme-quote>strong{font-size:25px}.theme-quote-source{grid-column:1/-1}.theme-quote-actions{grid-column:1/-1;justify-content:flex-start;gap:16px}.theme-metrics{grid-template-columns:1fr;padding:12px 15px;gap:9px}.theme-metrics>div{border:0;padding-right:0}.theme-metrics>div:last-child{grid-column:auto;border-top:1px solid var(--border);padding-top:8px}.theme-metrics strong{font-size:19px}.stock-theme-page details>summary{flex-wrap:wrap}.theme-events article{grid-template-columns:1fr auto;gap:7px}.theme-events article time{grid-column:1/-1}.theme-refresh-status button{margin-left:0}.theme-relation-tabs button{font-size:12px;padding:9px 7px}.theme-version-select select{max-width:100%}.recorded-pool-heading>span{font-size:11px}.recorded-pool-footer>a{margin-left:0}}
@media(prefers-reduced-motion:reduce){.stock-theme-page *{transition:none}}
.theme-row-relation{display:flex;align-items:center;flex-wrap:wrap;gap:5px 8px;margin-top:6px}
.theme-row-relation>small{margin:0}.theme-row-relation :deep(.relation-badge){font-size:11px}
.recorded-pool-footer :deep(.relation-badge){font-size:11px}.theme-quote{min-width:0;white-space:normal;overflow-wrap:anywhere}
.theme-quote>strong{font-size:25px;line-height:1.4}.theme-quote-source{align-self:start}
.stock-theme-page .panel-head{flex-wrap:wrap;gap:7px 14px}.theme-reference-summary{grid-template-columns:minmax(100px,1fr) minmax(120px,auto) minmax(150px,1fr) auto;gap:12px 18px}
@media(max-width:900px){.theme-reference-summary{grid-template-columns:minmax(0,1fr) minmax(0,auto)}}
@media(max-width:600px){.theme-quote>strong{font-size:23px}.theme-reference-summary{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.theme-row-relation{align-items:flex-start}}
.theme-heading-identity{display:flex;align-items:center;gap:12px;min-width:0}.theme-heading-identity h2{min-width:0;overflow-wrap:anywhere}
</style>
