<template>
  <div class="asset-page">
    <header class="asset-hero">
      <div class="asset-hero-main">
        <AssetAvatar :asset="asset" :size="38" class="asset-monogram" />
        <div class="asset-identity">
          <h2>{{ asset.name || asset.symbol || short(address) }} <small v-if="asset.name && asset.symbol && asset.name !== asset.symbol">{{ asset.symbol }}</small></h2>
          <p>{{ chainName({chainId:chain}) }} <span aria-hidden="true">·</span> <span :title="address">{{ short(address) }}</span>
            <button class="text-button" type="button" @click="copyAddress">{{ copied ? tr('已复制','Copied') : tr('复制地址','Copy') }}</button>
            <a v-if="explorer(address,'address',chain)" :href="explorer(address,'address',chain)" target="_blank" rel="noopener">{{ tr('链上记录','Onchain') }} ↗</a>
          </p>
          <RouterLink v-if="tickers.length" class="asset-relation-pill" :to="themeLink(tickers[0])">{{ themeLabel(tickers[0]) }} · {{ relationshipLabel }}</RouterLink>
        </div>
        <div class="asset-actions">
          <button :aria-pressed="followed" type="button" @click="account.toggle(watchKey)">{{ followed ? tr('★ 已关注','★ Following') : tr('☆ 关注','☆ Follow') }}</button>
          <a v-if="dex" class="asset-dex-action" :href="dex.url" :title="dex.provider" target="_blank" rel="noopener">{{ tr('去 DEX 交易','Trade on DEX') }} ↗</a>
        </div>
      </div>
      <div v-if="data" class="asset-headline" data-detail-headline>
        <p class="asset-price-label">{{ tr('资产主报价','Primary asset quote') }} · {{ headlineCurrency || tr('单位待核实','unit unverified') }}</p>
        <div class="asset-quote" :title="metricTitle('price')">
          <LiveNumber :value="asset.price" :currency="headlineCurrency" format="price" :label="tr('资产主报价','Primary asset quote')" :description="metricTitle('price')" />
          <b :class="Number(asset.change24h)>0?'up':Number(asset.change24h)<0?'down':''" :title="metricTitle('change24h')">{{ pct(asset.change24h) }} <small>{{ tr('24h 涨跌','24h change') }}</small></b>
          <QuoteStatus :row="asset" />
        </div>
        <p class="asset-quote-provenance" data-detail-quote-time>{{ quoteProvider }} · {{ tr('报价时间','Quote observed') }} {{ date(headlineAt) }}</p>
      </div>
      <p v-if="error && data" class="hint" role="status">{{ tr('更新暂时失败，保留上次行情。','Update failed; showing the previous quote.') }}</p>
    </header>

    <section v-if="error && !data" class="panel x-empty" data-summary-error role="alert">{{ tr('资产资料暂时无法加载。','Asset details could not be loaded.') }} <button @click="load(true)">{{ tr('重试','Retry') }}</button></section>
    <p v-else-if="!data && earlyLivePool" class="hint" data-summary-loading role="status">{{ tr('资产资料加载中。','Loading asset details.') }}</p>
    <section v-else-if="!data" class="panel loading-skeleton" data-summary-loading role="status" :aria-label="tr('加载资产','Loading asset')"><div v-for="n in 5" :key="n" class="skeleton-line"></div></section>
    <p v-if="liveMarketError" class="hint" data-live-market-error role="alert">{{ tr('实时市场连接暂时失败。','Live markets could not be connected.') }} <button @click="loadLiveMarkets()">{{ tr('重试','Retry') }}</button></p>
    <p v-else-if="liveMarketPending" class="hint" data-live-market-pending role="status">{{ tr('正在准备实时行情，稍后自动更新。','Preparing live market data; this page will update automatically.') }}</p>
    <section v-if="data || earlyLivePool" class="asset-market-context" data-detail-market-context :aria-label="tr('行情与成交市场','Chart and trade market')">
      <div class="asset-market-control">
        <label v-if="marketChoiceCount>1 || missingPoolChoice" for="detail-market">{{ tr('交易池 / 市场','Pool / market') }}</label>
        <select v-if="marketChoiceCount>1 || missingPoolChoice" id="detail-market" data-chart-market :value="missingPoolChoice || marketSelection.id" @change="selectChartMarket($event.target.value)">
          <option v-if="missingPoolChoice" :value="missingPoolChoice" disabled>{{ tr('所选池暂不可用','Selected pool unavailable') }}</option>
          <option v-if="data" :value="asset.priceScope==='exchange'?'base':'dex'">{{ chartBaseLabel }}</option>
          <option v-for="m in exchangeMarkets" :key="String(m.venue)+':'+String(m.marketId)" :value="String(m.venue)+':'+String(m.marketId)">{{ m.provider??m.venue }} · {{ m.marketId }} · {{ m.priceCurrency || tr('单位待核实','unit unverified') }}</option>
          <option v-for="p in poolMarkets" :key="p.poolId??p.pool??p.marketId" :value="'pool:'+(p.poolId??p.pool??p.marketId)">{{ short(p.poolId??p.pool??p.marketId) }} · {{ p.priceCurrency || tr('单位待核实','unit unverified') }}</option>
        </select>
        <strong v-else>{{ marketDisplayLabel }}</strong>
      </div>
      <p v-if="selectedPoolId" class="hint" data-pool-unit>{{ tr('所选池登记计价','Selected pool quote unit') }} {{ marketSelection.pool.priceCurrency || tr('单位待核实','unit unverified') }} · {{ short(selectedPoolId) }}<span v-if="marketSelection.pool.lastTradeAt" :title="tr('来自池采集记录，不代表已加载对应逐笔记录。','Recorded by the pool collector; does not imply that individual trades are loaded.')"> · {{ marketSelection.pool.source || marketSelection.pool.provider || tr('池采集记录','Pool observation') }} · {{ tr('链上最后成交','Last onchain trade') }} {{ date(marketSelection.pool.lastTradeAt) }}</span></p>
      <p v-else-if="marketSelection.kind==='exchange'" class="hint" data-exchange-unit>{{ marketSelection.exchange.provider??marketSelection.exchange.venue }} · {{ marketSelection.exchange.marketId }} · {{ marketSelection.exchange.priceCurrency || tr('单位待核实','unit unverified') }}</p>
      <p class="asset-market-note">{{ selectedPoolId || marketSelection.kind==='exchange' ? tr('上方为资产主报价；图表以所选市场实际计价为准，成交列表展示已加载的匹配记录。','The headline is the primary asset quote. The chart uses its market unit; the trade list shows loaded matching records.') : tr('上方为资产主报价；图表显示自己的来源与单位，近期成交来自已收录市场。','The headline is the primary asset quote. The chart identifies its own source and unit; trades come from recorded markets.') }}</p>
      <p v-if="missingPoolChoice && !liveMarketPending" class="hint" data-chart-market-unavailable role="status">{{ tr('所选交易池暂无可用图表。','The selected pool chart is unavailable.') }}</p>
      <p v-if="sectionErrors.markets" class="hint" role="alert">{{ tr('市场列表暂时无法更新。','Market list is temporarily unavailable.') }} <button @click="loadSection('markets',true)">{{ tr('重试','Retry') }}</button></p>
    </section>
      <nav v-if="data" class="asset-tabs" :aria-label="tr('资产栏目','Asset sections')">
        <RouterLink v-for="tab in tabs" :key="tab.key" replace :to="{query:{...route.query,tab:tab.key}}" :class="{active:activeTab===tab.key}" :aria-current="activeTab===tab.key?'page':undefined">{{ tr(tab.zh,tab.en) }}</RouterLink>
      </nav>

    <section v-if="data && activeTab==='overview' && keyMetrics.length" class="asset-key-strip" data-detail-key-metrics :aria-label="tr('关键行情数据','Key market data')">
      <div v-for="metric in keyMetrics" :key="metric.key" :title="metricTitle(metric.key)">
        <span>{{ tr(...metric.label) }}<template v-if="metric.currency"> · {{ metric.currency }}</template></span>
        <strong><LiveNumber :value="metric.value" :currency="metric.currency" :format="metric.key==='holders'?'number':'money'" :label="tr(...metric.label)" :description="metricTitle(metric.key)" /></strong>
        <small>{{ metric.status==='historical'?tr('历史数据','Historical data'):tr('观测时间','Observed') }} · {{ metric.source || tr('来源待核实','Source unverified') }} · {{ date(metric.at) }}</small>
      </div>
    </section>
    <div v-if="(data || earlyLivePool) && overviewVisited" v-show="activeTab==='overview'" class="asset-overview-grid" :class="{'asset-overview-pending':!data}">
      <div class="asset-story">
        <section class="panel asset-chart-panel">
          <div class="panel-head"><h2>{{ tr('价格走势','Price history') }}</h2><span class="hint">{{ tr('时间与单位以图表标注为准','Times and units follow the chart') }}</span></div>
          <CandleChart v-if="!missingPoolChoice" :asset="chartAsset" :samples="marketSelection.kind==='base'?(data?.samples??[]):[]" :pool="selectedPoolId" :native-pool="nativePool" @select-pool="selectNativePool" />
          <p v-else class="asset-chart-missing" role="status">{{ tr('当前交易池没有可用历史行情，请切换市场。','History is unavailable for this pool. Select another market.') }}</p>
        </section>
        <AssetInsight v-if="data" :chain="chain" :address="address" />
        <details v-if="data && hasExtraMetrics" class="panel asset-extra-metrics">
          <summary>{{ tr('更多行情指标','More market metrics') }}</summary>
          <AssetMetrics :metrics="asset.productMetrics" :now="now" :chain="chain" :token="address" />
        </details>
      </div>
      <aside v-if="data" class="asset-side">
        <section class="panel asset-live-trades">
          <div class="panel-head"><h2>{{ tr('近期成交','Recorded trades') }}</h2><RouterLink replace :to="{query:{...route.query,tab:'trades'}}">{{ tr('全部','All') }} →</RouterLink></div>
          <p class="hint">{{ tradeScopeLabel }} · {{ tradeStatus }}</p>
          <div v-if="trades.length" class="asset-trade-head"><span>{{ tr('成交时间','Time') }}</span><span>{{ tr('方向','Side') }}</span><span>{{ tr('成交额','Amount') }}</span></div>
          <div v-if="loading.trades&&!trades.length" class="loading-skeleton"><div v-for="n in 3" :key="n" class="skeleton-line"></div></div>
          <div v-for="t in trades.slice(0,4)" :key="tradeKey(t)" class="asset-trade-mini">
            <time :title="date(t.sourceEventAt??t.t)">{{ tradeTimeLabel(t) }}</time>
            <span :class="t.type==='buy'?'up':t.type==='sell'?'down':''">{{ t.type==='buy'?tr('买入','Buy'):t.type==='sell'?tr('卖出','Sell'):'—' }}</span>
            <b :title="tradeSourceTitle(t)"><TradeAmount :trade="t" /></b>
          </div>
          <p v-if="sectionErrors.trades" class="hint" role="alert">{{ tr('成交暂时无法更新。','Trades could not be updated.') }} <button @click="loadSection('trades',true)">{{ tr('重试','Retry') }}</button></p>
          <p v-else-if="!trades.length&&!loading.trades" class="asset-compact-empty">{{ tradeEmptyLabel }}</p>
        </section>
        <section class="panel asset-risk-panel" aria-labelledby="asset-risk-heading">
          <div class="panel-head"><h2 id="asset-risk-heading">{{ tr('风险与检查','Risk checks') }}</h2></div>
          <ul v-if="triggeredRisks.length" class="asset-risk-flags" data-detail-risk-flags>
            <li v-for="risk in triggeredRisks" :key="risk.key" :title="(risk.provider||tr('来源待核实','Source unverified'))+' · '+date(risk.at)"><strong>{{ tr(...risk.label) }}</strong><span v-if="risk.historical">{{ tr('历史提示','Historical flag') }}</span></li>
          </ul>
          <RiskSummary :asset="asset" compact />
        </section>
        <section class="panel asset-relationship-panel" aria-labelledby="asset-relationship-heading">
          <div class="panel-head"><h2 id="asset-relationship-heading">{{ tr('股票关联','Stock relationships') }}</h2><RouterLink v-if="relations.length || data.relationCount" replace :to="{query:{...route.query,tab:'relation'}}">{{ tr('全部','All') }} →</RouterLink></div>
          <DetailRelationshipRecord v-for="relation in relations.slice(0,3)" :key="relationKey(relation)" :relation="relation" :stock="data.stock" :chain="chain" :scope="String(route.query.chain??'all')" />
          <p v-if="loading.relations&&!relations.length" class="hint" role="status">{{ tr('正在读取关系…','Loading relationships…') }}</p>
          <p v-else-if="!relations.length" class="asset-compact-empty">{{ relationshipLabel }}</p>
          <p v-if="sectionErrors.relations" class="hint" role="alert">{{ tr('关系暂时无法更新。','Relationships could not be updated.') }} <button @click="loadSection('relations',true)">{{ tr('重试','Retry') }}</button></p>
        </section>
      </aside>
    </div>
    <template v-if="data">
      <template v-if="activeTab==='overview'">
        <details class="panel asset-facts">
          <summary>{{ tr('资产信息与来源','Asset details and sources') }}</summary>
          <dl>
            <div><dt>{{ tr('关联股票','Related stocks') }}</dt><dd><template v-if="tickers.length"><RouterLink v-for="ticker in tickers" :key="ticker" :to="themeLink(ticker)">{{ themeLabel(ticker) }}</RouterLink></template><span v-else>—</span></dd></div>
            <div><dt>{{ tr('关系类型','Relationship') }}</dt><dd>{{ relationshipLabel }}</dd></div>
            <div><dt>{{ tr('行情来源','Quote source') }}</dt><dd>{{ asset.primaryQuote?.provider??asset.fieldSources?.price??asset.provider??'—' }} · {{ date(asset.fieldTimes?.price) }}</dd></div>
            <div><dt>{{ tr('首次收录','First indexed') }}</dt><dd>{{ date(asset.firstSeen) }}</dd></div>
          </dl>
        </details>
        <details v-if="comparisonAvailable" class="panel asset-comparison">
          <summary>{{ tr('价格比较','Price comparison') }}</summary>
          <div v-if="verifiedPairs.length" class="comparison-market">
            <label>{{ tr('价格比较配对','Comparison pair') }}
              <select :value="comparisonPair?.pool" @change="comparisonPoolChoice=$event.target.value">
                <option v-for="pair in verifiedPairs" :key="String(pair.chainId??chain)+':'+pair.stock+':'+pair.pool" :value="pair.pool">{{ pair.ticker }} · {{ short(pair.pool) }} · {{ chainName({chainId:pair.chainId??chain}) }}</option>
              </select>
            </label>
            <details class="comparison-note"><summary>{{ tr('比较口径','Comparison scope') }}</summary><p>{{ tr('仅比较已核实配对的股票侧合约与交易池；缺失的价格历史保留缺口。','Only verified stock-side contracts and pools are compared; missing price history remains a gap.') }}</p></details>
          </div>
          <SpreadPanel v-if="comparisonPair" :key="String(comparisonPair.chainId??chain)+':'+comparisonPair.stock+':'+comparisonPair.pool" :chain="String(comparisonPair.chainId??chain)" :token="comparisonPair.stock" :pool="comparisonPair.pool" />
          <SpreadPanel v-else-if="asset.kind==='stock'" :chain="chain" :token="address" />
        </details>
      </template>

      <section v-else-if="activeTab==='trades'" class="panel asset-section">
        <div class="panel-head"><h2>{{ tr('最近成交','Recent trades') }}</h2><span class="hint">{{ tradeStatus }}</span></div>
        <details v-if="trades.length && tradeActivityAvailable" class="asset-trade-analysis"><summary>{{ tr('成交时段分析','Trade activity analysis') }}</summary><TradeActivity :trades="tradeActivityRows" :scope="tradeScopeLabel" /></details>
        <div v-if="loading.trades&&!trades.length" class="loading-skeleton" role="status"><div v-for="n in 6" :key="n" class="skeleton-line"></div></div>
        <div v-else-if="trades.length" class="table-wrap" data-market-trades><table class="tbl"><thead><tr><th>{{ tr('时间','Time') }}</th><th>{{ tr('方向','Side') }}</th><th>{{ tr('价格','Price') }}</th><th>{{ tr('成交额','Amount') }}</th><th>{{ tr('市场','Market') }}</th><th>{{ tr('钱包 / 交易','Wallet / transaction') }}</th></tr></thead>
          <tbody><tr v-for="t in trades" :id="'trade-'+encodeURIComponent(tradeKey(t))" :key="tradeKey(t)" tabindex="-1" :class="{'trade-new':recentTradeArrival(t)}">
            <td :title="date(t.sourceEventAt??t.t)">{{ tradeTimeLabel(t) }}</td><td :class="t.type==='buy'?'up':t.type==='sell'?'down':''">{{ t.type==='buy'?tr('买入','Buy'):t.type==='sell'?tr('卖出','Sell'):'—' }}</td><td :title="tradeSourceTitle(t)">{{ money(t.price,t.priceCurrency) }}</td><td :title="tradeSourceTitle(t)"><TradeAmount :trade="t" /></td><td>{{ t.source??t.provider??t.venue??'—' }}<small>{{ short(t.poolId??t.pool??t.marketId) }}</small></td><td><a v-if="t.wallet" :href="explorer(t.wallet,'address',chain)" target="_blank" rel="noopener">{{ short(t.wallet) }} ↗</a><span v-else>—</span><small><a v-if="t.hash" :href="explorer(t.hash,'tx',chain)" target="_blank" rel="noopener">{{ tr('查看交易','View transaction') }} ↗</a></small></td>
          </tr></tbody></table></div>
        <p v-else-if="!loading.trades" class="asset-compact-empty">{{ tradeEmptyLabel }}</p>
        <button v-if="data.sectionNext?.trades!=null" :disabled="loading.trades" @click="loadMoreTrades">{{ tr('加载更多','Load more') }}</button>
        <p v-if="sectionErrors.trades" role="alert">{{ tr('成交读取失败。','Trades could not be loaded.') }} <button @click="loadSection('trades',true)">{{ tr('重试','Retry') }}</button></p>
      </section>

      <section v-else-if="activeTab==='holders'" class="panel asset-section">
        <div class="panel-head"><h2>{{ tr('持仓分布','Holder distribution') }}</h2><span v-if="loading.holders" class="hint" role="status">{{ tr('加载中…','Loading…') }}</span></div>
        <HolderComposition :distribution="data.holdersSummary?.distribution" :count="data.holdersSummary?.count??asset.holders" :count-at="data.holdersSummary?.observedAt??asset.fieldTimes?.holders" :count-source="data.holdersSummary?.provider??asset.fieldSources?.holders??''" />
        <p v-if="sectionErrors.holders" role="alert">{{ tr('持仓读取失败。','Holdings could not be loaded.') }} <button @click="loadSection('holders',true)">{{ tr('重试','Retry') }}</button></p>
      </section>

      <section v-else class="panel asset-section">
        <div class="panel-head"><h2>{{ tr('股票关联','Stock relationships') }}</h2><span class="hint" :title="tr('分子为已加载关联数，分母为服务端记录的关联总数。','Numerator: loaded relationships. Denominator: total relationships recorded by the server.')">{{ tr('已加载 / 总关联','Loaded / total relationships') }} {{ relations.length }} / {{ data.relationCount??relations.length }}</span></div>
        <p v-if="loading.relations" role="status">{{ tr('加载中…','Loading…') }}</p>
        <DetailRelationshipRecord v-for="r in relations" :key="relationKey(r)" :relation="r" :stock="data.stock" :chain="chain" :scope="String(route.query.chain??'all')" />
        <p v-if="!relations.length&&!loading.relations" class="asset-compact-empty">{{ tr('暂无股票关联。','No stock relationships yet.') }}</p>
        <button v-if="data.sectionNext?.relations!=null" :disabled="loading.relations" @click="loadSection('relations',true,data.sectionNext.relations)">{{ tr('加载更多','Load more') }}</button>
        <p v-if="sectionErrors.relations" role="alert">{{ tr('关联读取失败。','Relationships could not be loaded.') }} <button @click="loadSection('relations',true)">{{ tr('重试','Retry') }}</button></p>
      </section>
    </template>
    <nav v-if="data" class="asset-mobile-actions" :aria-label="tr('资产操作','Asset actions')"><RouterLink :to="reminderLink">{{ tr('设置提醒','Set alert') }}</RouterLink><a v-if="dex" :href="dex.url" target="_blank" rel="noopener">{{ tr('去 DEX 交易','Trade on DEX') }} ↗</a><span v-else>{{ tr('暂无交易入口','No trading link') }}</span></nav>
  </div>
</template>
<script setup>
import AssetAvatar from '../components/AssetAvatar.vue';
import {computed,onBeforeUnmount,onMounted,reactive,ref,watch} from 'vue';
import {useRoute,useRouter} from 'vue-router';
import {useDetailStore} from '../stores/detail';
import {useDashboardStore} from '../stores/dashboard';
import {useAccountStore} from '../stores/account';
import {tr} from '../i18n';
import {age,chainName,date,explorer,money,pct,short} from '../utils/format';
import {selectDetailMarket} from '../utils/market-selection';
import {getLiveMarkets} from '../utils/live-market-stream';
import {tradeKey} from '../stores/feed';
import {detailAsset,detailTrades,dexAction,recordedPool} from '../utils/detail-presentation';
import {detailMetricObservation,detailTriggeredRisks,hasDetailExtraMetrics} from '../utils/detail-disclosure-presentation';
import {stockThemeName,stockTicker} from '../utils/stock-theme-model';
import {relatedStockThemeLink} from '../utils/stock-navigation';
import AssetMetrics from '../components/AssetMetrics.vue';
import {useComparisonStore} from '../stores/comparisons';
import LiveNumber from '../components/LiveNumber.vue';
import QuoteStatus from '../components/QuoteStatus.vue';
import CandleChart from '../components/CandleChart.vue';
import RiskSummary from '../components/RiskSummary.vue';
import SpreadPanel from '../components/SpreadPanel.vue';
import AssetInsight from '../components/AssetInsight.vue';
import TradeAmount from '../components/TradeAmount.vue';
import TradeActivity from '../components/TradeActivity.vue';
import HolderComposition from '../components/HolderComposition.vue';
import DetailRelationshipRecord from '../components/DetailRelationshipRecord.vue';
const props=defineProps({chain:{type:String,required:true},address:{type:String,required:true}});
const route=useRoute(),router=useRouter(),detail=useDetailStore(),dash=useDashboardStore(),account=useAccountStore();
const comparisons=useComparisonStore();
const error=ref(null),copied=ref(false),now=ref(Date.now()),choice=ref(null),comparisonPoolChoice=ref(''),loading=reactive({}),sectionErrors=reactive({});
const data=computed(()=>detail.cache.get(`${props.chain}:${props.address.toLowerCase()}`)?.data??null),asset=computed(()=>detailAsset(data.value?.asset,props.chain,props.address));
const headlineCurrency=computed(()=>asset.value.priceCurrency || asset.value.primaryQuote?.currency || null);
const headlineAt=computed(()=>asset.value.fieldTimes?.price ?? asset.value.quoteAt ?? asset.value.primaryQuote?.at);
const quoteProvider=computed(()=>asset.value.primaryQuote?.provider ?? asset.value.fieldSources?.price ?? asset.value.provider ?? tr('来源待核实','Source unverified'));
const keyMetrics=computed(()=>[['volume24h',['24h 成交额','24h volume']],['totalLiquidityUsd',['已收录池流动性','Indexed pool liquidity']],['holders',['持币地址数','Holder addresses']]].flatMap(([key,label])=>{
  // A stream observation can arrive between the 20s health-clock ticks.
  // Evaluate its timestamp at render time instead of treating it as future data.
  const observation=detailMetricObservation(asset.value,key,Math.max(now.value,Date.now()));
  return observation.value!=null ? [{key,label,...observation}] : [];
}));
const hasExtraMetrics=computed(()=>hasDetailExtraMetrics(asset.value.productMetrics,Math.max(now.value,Date.now())));
const triggeredRisks=computed(()=>detailTriggeredRisks(asset.value,Math.max(now.value,Date.now())));
const dex=computed(()=>dexAction(props.chain,props.address));
const tabs=[{key:'overview',zh:'概览',en:'Overview'},{key:'trades',zh:'成交',en:'Trades'},{key:'holders',zh:'持仓',en:'Holders'},{key:'relation',zh:'关联',en:'Relationships'}];
const activeTab=computed(()=>tabs.some(t=>t.key===route.query.tab)?route.query.tab:'overview');
const overviewVisited=ref(activeTab.value==='overview');
const watchKey=computed(()=>`${props.chain}:${props.address.toLowerCase()}`),followed=computed(()=>account.watches.includes(watchKey.value));
const navigationScope=computed(()=>String(route.query.chain??'all'));
const relations=computed(()=>data.value?.relations??[]),tickers=computed(()=>[...new Set([...relations.value.map(r=>r.ticker),asset.value.match?.ticker].filter(Boolean))]),hasPool=computed(()=>relations.value.some(r=>recordedPool(r)));
function relationKey(relation){return relation.id ?? `${relation.chainId??props.chain}:${relation.pool??relation.token}:${relation.stock}:${relation.ticker}`;}
function themeRecord(ticker){
  if(stockTicker(data.value?.stock)===stockTicker({ticker}))return data.value.stock;
  if(stockTicker(asset.value)===stockTicker({ticker}))return asset.value;
  const relation=relations.value.find(row=>stockTicker(row)===stockTicker({ticker}));
  return relation?dash.stockIndex?.get(`${relation.chainId??props.chain}:${String(relation.stock).toLowerCase()}`):null;
}
function themeLabel(ticker){const stock=themeRecord(ticker);return tr(stockThemeName(stock,ticker,'zh'),stockThemeName(stock,ticker,'en'));}
function themeLink(ticker){return relatedStockThemeLink(ticker,route,navigationScope.value);}
const verifiedPairs=computed(()=>{
  const seen=new Set();
  return relations.value.filter(r=>{
    const key=String(r.chainId??props.chain)+':'+String(r.stock).toLowerCase()+':'+String(r.pool).toLowerCase();
    if(r.level!=='A'||r.status!=='verified'||!/^0x[0-9a-f]{40}$/i.test(String(r.stock))||!/^0x[0-9a-f]{40}$/i.test(String(r.pool))||seen.has(key))return false;
    seen.add(key);return true;
  });
});
const comparisonPair=computed(()=>verifiedPairs.value.find(r=>String(r.pool).toLowerCase()===String(comparisonPoolChoice.value||selectedPoolId.value).toLowerCase())??verifiedPairs.value[0]??null);
const relationshipLabel=computed(()=>hasPool.value?tr('已记录同池关系','Recorded pool pairing'):asset.value.match?.level==='B'||relations.value.some(r=>r.level==='B')?tr('名称匹配 · 未确认同池','Name match · pool unconfirmed'):Number(data.value?.relationCount??0)>relations.value.length?tr('查看关联栏目','See relationships'):tr('暂无股票关联','No stock relationship'));
const liveMarkets=ref([]),liveMarketError=ref(null),liveMarketPending=ref(false),initialLivePool=ref(null);
const exchangeMarkets=computed(()=>asset.value.exchangeMarkets??data.value?.markets?.filter?.(m=>m.venue!=='dex')??[]),poolMarkets=computed(()=>{
  const legacy=data.value?.poolMarkets??asset.value.poolMarkets??data.value?.markets?.filter?.(m=>m.venue==='dex')??[];
  const merged=new Map(legacy.map(p=>[String(p.poolId??p.pool??p.marketId).toLowerCase(),p]));
  for(const p of liveMarkets.value){const key=String(p.poolId??p.pool??p.marketId).toLowerCase();merged.set(key,{...merged.get(key),...p});}
  return [...merged.values()];
});
const marketSelection=computed(()=>selectDetailMarket(asset.value,exchangeMarkets.value,poolMarkets.value,choice.value??initialLivePool.value,now.value)),selectedPoolId=computed(()=>marketSelection.value.kind==='pool'?String(marketSelection.value.pool.poolId??marketSelection.value.pool.pool??marketSelection.value.pool.marketId):'');
const marketChoiceCount=computed(()=>(data.value?1:0)+exchangeMarkets.value.length+poolMarkets.value.length);
const marketDisplayLabel=computed(()=>selectedPoolId.value?`${tr('交易池','Pool')} ${short(selectedPoolId.value)}`:marketSelection.value.kind==='exchange'?`${marketSelection.value.exchange.provider??marketSelection.value.exchange.venue} · ${marketSelection.value.exchange.marketId}`:chartBaseLabel.value);
const missingPoolChoice=computed(()=>choice.value?.startsWith('pool:') && !poolMarkets.value.some(p=>String(p.poolId??p.pool??p.marketId).toLowerCase()===choice.value.slice(5).toLowerCase())?choice.value:null);
// The dedicated registry can identify a chart before the research summary.
// It supplies no canonical quote, asset statistics, or risk assessment.
const earlyLivePool=computed(()=>selectedPoolId.value && liveMarkets.value.some(p=>
  String(p.poolId??p.pool??p.marketId).toLowerCase()===selectedPoolId.value.toLowerCase()
  && p.venue==='dex' && p.quoteType==='pool' && /^0x[0-9a-f]{40}$/i.test(selectedPoolId.value)
  && p.priceCurrency && p.volumeCurrency));
// Chart market selection never changes the canonical headline quote.
const chartAsset=computed(()=>marketSelection.value.kind==='exchange'?{...asset.value,...marketSelection.value.exchange,token:asset.value.token,chainId:asset.value.chainId,priceScope:'exchange'}:asset.value);
const nativePool=computed(()=>{
  // poolMarkets comes from the on-chain market registry. Require a real pool
  // address and a recent recorded trade before offering it as a live fallback.
  const candidates=poolMarkets.value.filter(p=>{
    const pool=String(p.poolId??p.pool??p.marketId??'');
    const last=Number(p.lastTradeAt);
    return p.venue==='dex' && p.quoteType==='pool' && /^0x[0-9a-f]{40}$/i.test(pool)
      && p.priceCurrency && p.priceCurrency!=='USD' && Number.isFinite(last)
      && last>0 && last<=now.value && now.value-last<=600_000;
  }).sort((a,b)=>Number(b.lastTradeAt)-Number(a.lastTradeAt));
  const best=candidates[0];
  return best?{poolId:String(best.poolId??best.pool??best.marketId),priceCurrency:String(best.priceCurrency)}:null;
});
function routeMarketChoice(){
  if(typeof route.query.pool==='string' && route.query.pool)return `pool:${route.query.pool}`;
  return typeof route.query.market==='string' && route.query.market ? route.query.market : null;
}
function selectChartMarket(value,syncRoute=true){
  if(choice.value!==value){choice.value=value;cancelLiveMarketLoad();liveMarketPending.value=false;loadLiveMarkets();}
  if(!syncRoute)return;
  const query={...route.query};
  delete query.pool;delete query.market;
  if(typeof value==='string' && value.startsWith('pool:'))query.pool=value.slice(5);
  else if(typeof value==='string' && value)query.market=value;
  if(route.query.pool!==query.pool || route.query.market!==query.market)router.replace({query});
}
function selectNativePool(poolId){selectChartMarket(`pool:${poolId}`);}
const trades=computed(()=>detailTrades(data.value,marketSelection.value));
watch(activeTab,tab=>{if(tab==='overview')overviewVisited.value=true;});
const tradeActivityRows=computed(()=>trades.value.map(t=>({...t,t:t.sourceEventAt??t.t})));
const tradeActivityAvailable=computed(()=>tradeActivityRows.value.length>=3 && tradeActivityRows.value.every(t=>typeof t.t==='number'&&Number.isFinite(t.t)&&t.t>0&&t.t<=Math.max(now.value,Date.now())));
async function loadMoreTrades(){await loadSection('trades',true,data.value?.sectionNext?.trades);}
const tradeScopeLabel=computed(()=>selectedPoolId.value?tr('所选池成交','Selected pool'):marketSelection.value.kind==='exchange'?tr('所选市场成交','Selected market'):tr('近期市场成交','Recent market trades'));
const tradeEmptyLabel=computed(()=>selectedPoolId.value?tr('当前已加载记录中没有该池的逐笔成交。','No individual trades for this pool in the currently loaded records.'):marketSelection.value.kind==='exchange'?tr('当前已加载记录中没有该市场的逐笔成交。','No individual trades for this market in the currently loaded records.'):tr('暂无已收录成交。','No recorded trades yet.'));
// The headline quote and the default chart can have different providers.
// CandleChart shows the actual chart source and unit after data is loaded.
const chartBaseLabel=computed(()=>tr('默认图表市场','Default chart market'));
function tradeSourceTitle(t){return `${tr('这笔成交的市场报价与金额，单位以记录标注为准。','Market quote and amount for this trade; units follow the record.')} · ${t.source??t.provider??t.venue??'—'} · ${date(t.sourceEventAt??t.t)}`;}
function tradeTimeLabel(trade){const at=trade.sourceEventAt??trade.t;return typeof at==='number'&&at>0&&at<=Date.now()?age(at):tr('时间待核实','Time unverified');}
function recentTradeArrival(trade){const current=Date.now(),received=trade.browserReceivedAt,event=trade.sourceEventAt??trade.t;return typeof received==='number'&&received<=current&&current-received<2000&&typeof event==='number'&&event<=current&&current-event<30000;}
const tradeStatus=computed(()=>{const row=trades.value[0];if(!row)return tr('暂无逐笔记录','No individual trade records');const at=row.sourceEventAt??row.t;return `${row.source??row.provider??tr('来源待核实','Source unverified')} · ${tr('最后成交','Last trade')} ${date(at)}${typeof at==='number'&&at>0&&now.value-at>900000?' · '+tr('历史记录','Historical records'):''}`;});
const reminderLink=computed(()=>({path:'/me',query:{kind:'alert',chain:props.chain,token:props.address}}));
const comparisonAvailable=computed(()=>{const current=comparisons.latest.get(`${props.chain}:${props.address.toLowerCase()}`);const pair=current?.pairs?.find(p=>String(p.pool).toLowerCase()===String(comparisonPair.value?.pool).toLowerCase());const metric=pair?.spread??current?.premium;return metric?.value!=null&&['current','valid'].includes(metric.status)&&(!metric.validUntil||metric.validUntil>=now.value);});
function metricTitle(name){
  const definitions={price:tr('每枚资产的主行情报价；单位以报价币种为准。','Primary market quote per asset unit, in the stated quote currency.'),change24h:tr('主行情价格近 24 小时涨跌，单位 %。','24h primary-market price change in %.'),totalLiquidityUsd:tr('已收录交易池流动性合计，单位 USD；未覆盖池不计入。','Liquidity of indexed pools in USD; pools outside coverage are excluded.'),volume24h:tr('来源近 24 小时成交额；统计市场及单位以来源为准。','Source-reported 24h trade volume; market coverage and units follow the source.'),txs24h:tr('近 24 小时买入 / 卖出笔数，不是金额。','24h buy and sell transaction counts.'),holders:tr('持币地址数量，一个人可有多个地址。','Holder address count; one person may own several addresses.')};
  const source=asset.value.fieldAvailability?.[name]?.source ?? asset.value.fieldSources?.[name]
    ?? (name==='txs24h'?asset.value.fieldSources?.buys24h:null)
    ?? (name==='price'?asset.value.primaryQuote?.provider??asset.value.provider:null)
    ?? (name==='totalLiquidityUsd'?asset.value.totalLiquidityCoverage?.provider:null);
  const observedAt=asset.value.fieldAvailability?.[name]?.at ?? asset.value.fieldTimes?.[name]
    ?? (name==='txs24h'?asset.value.fieldTimes?.buys24h:null)
    ?? (name==='totalLiquidityUsd'?asset.value.totalLiquidityAt:null);
  return `${definitions[name]??''} · ${source??tr('来源未确认','Source unconfirmed')} · ${date(observedAt)} · ${tr('— 表示缺少可用观测；历史值单独标注','— means no usable observation; historical values are labeled')}`;
}
let generation=0,release,timer,disposed=false,liveMarketRequest,liveMarketController,liveMarketRetryTimer,liveMarketRetryAttempt=0;
const initialRetryDelays=[1000,2000,4000,8000,15000];
function cancelLiveMarketLoad(){clearTimeout(liveMarketRetryTimer);liveMarketRetryTimer=null;liveMarketController?.abort();liveMarketController=null;liveMarketRequest=null;liveMarketRetryAttempt=0;}
function scheduleLiveMarketRetry(gen){
  if(liveMarketRetryAttempt>=initialRetryDelays.length)return;
  const delay=initialRetryDelays[liveMarketRetryAttempt++];
  liveMarketRetryTimer=setTimeout(()=>{liveMarketRetryTimer=null;if(gen===generation&&!disposed&&!document.hidden)loadLiveMarkets();},delay);
}
function loadLiveMarkets(){
  if(disposed)return Promise.resolve();
  if(liveMarketRequest)return liveMarketRequest;
  clearTimeout(liveMarketRetryTimer);liveMarketRetryTimer=null;
  const gen=generation,c=props.chain,a=props.address,controller=new AbortController();
  liveMarketController=controller;
  liveMarketRequest=(async()=>{
    try{
      const requestChoice=choice.value??initialLivePool.value;
      const requestedPool=requestChoice?.startsWith('pool:')?requestChoice.slice(5):undefined;
      const result=await getLiveMarkets(c,a,{signal:controller.signal,pool:requestedPool,bar:'5m'});
      if(gen!==generation||disposed||controller.signal.aborted)return;
      liveMarkets.value=(result.markets??[]).filter(p=>p.liveMarket===true&&String(p.chainId)===c&&String(p.token).toLowerCase()===a.toLowerCase());
      liveMarketError.value=null;
      liveMarketPending.value=result.selectionStatus==='pending'&&result.watchRequested===true;
      if(liveMarketPending.value)scheduleLiveMarketRetry(gen);
      if(liveMarkets.value.length && !liveMarketPending.value){
        liveMarketRetryAttempt=0;
        // Keep the observed pool selected when its health clock ages or the
        // independent summary arrives; CandleChart owns its live connection.
        if(choice.value==null && initialLivePool.value==null){
          const selected=selectDetailMarket(asset.value,[],liveMarkets.value,null,Date.now());
          if(selected.kind==='pool')initialLivePool.value=selected.id;
        }
      }
    }catch(e){
      if(gen!==generation||disposed||controller.signal.aborted)return;
      liveMarketError.value=e.message;
      if(!liveMarkets.value.length || liveMarketPending.value)scheduleLiveMarketRetry(gen);
    }finally{if(gen===generation&&liveMarketController===controller){liveMarketRequest=null;liveMarketController=null;}}
  })();
  return liveMarketRequest;
}
async function loadSection(section,force=false,offset=0){const gen=generation,c=props.chain,a=props.address;loading[section]=true;sectionErrors[section]=false;try{await detail.fetchSection(c,a,section,{force,offset});}catch{if(gen===generation)sectionErrors[section]=true;}finally{if(gen===generation)loading[section]=false;}}
function loadActive(force=false){if(!data.value)return;const section=activeTab.value==='overview'?'markets':activeTab.value==='relation'?'relations':activeTab.value;loadSection(section,force);if(section==='trades')loadSection('markets',force);if(section==='markets'){loadSection('trades',force);loadSection('relations',force);}}
async function load(force=false){const gen=generation;try{await detail.fetch(props.chain,props.address,{force});if(gen!==generation||disposed)return;error.value=null;loadActive(force);}catch(e){if(gen===generation)error.value=e.message;}}
function switchAsset(){++generation;cancelLiveMarketLoad();release?.();detail.watch(props.chain,props.address);release=detail.activate(props.chain,props.address);error.value=null;choice.value=routeMarketChoice();comparisonPoolChoice.value='';overviewVisited.value=activeTab.value==='overview';liveMarkets.value=[];initialLivePool.value=null;liveMarketError.value=null;liveMarketPending.value=false;Object.keys(loading).forEach(k=>delete loading[k]);Object.keys(sectionErrors).forEach(k=>delete sectionErrors[k]);loadLiveMarkets();load();}
async function copyAddress(){try{await navigator.clipboard.writeText(props.address);copied.value=true;setTimeout(()=>copied.value=false,1500);}catch{copied.value=false;}}
function invalidate(e){const r=e.detail??{};if(r.chainId&&String(r.chainId)!==props.chain||r.token&&String(r.token).toLowerCase()!==props.address.toLowerCase())return;loadActive(true);}
function manual(){load(true);}
watch(()=>`${props.chain}:${props.address}`,switchAsset);watch(activeTab,()=>loadActive());watch(()=>[route.query.pool,route.query.market],()=>selectChartMarket(routeMarketChoice(),false));
onMounted(()=>{switchAsset();timer=setInterval(()=>{now.value=Date.now();if(!document.hidden){loadLiveMarkets();if(!dash.hasProjectionStream||Date.now()-(detail.cache.get(watchKey.value)?.at??0)>60000)load(true);}},20000);window.addEventListener('resource-change',invalidate);window.addEventListener('manual-refresh',manual);});
onBeforeUnmount(()=>{disposed=true;++generation;cancelLiveMarketLoad();release?.();detail.unwatch();clearInterval(timer);window.removeEventListener('resource-change',invalidate);window.removeEventListener('manual-refresh',manual);});
</script>
<style scoped>
.asset-page{display:grid;gap:18px;min-width:0;align-content:start}
.asset-hero{display:block;min-width:0;padding:4px 0 0}
.asset-identity a:hover,.asset-side .panel-head a:hover{text-decoration:underline}
.asset-hero-main{display:grid;grid-template-columns:44px minmax(0,1fr) auto;align-items:start;gap:14px}
.asset-monogram{display:grid;place-items:center;width:38px;height:38px;margin-top:5px;border-radius:50%;background:var(--accent-soft);color:var(--accent);font-family:inherit;font-size:16px;font-weight:700}
.asset-identity{min-width:0}
.asset-identity h2{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:0;font-family:inherit;font-size:28px;font-weight:600;line-height:1.2;letter-spacing:-.025em;overflow-wrap:anywhere}
.asset-identity h2 small{font-family:inherit;font-size:13px;font-weight:400;letter-spacing:0;color:var(--muted)}
.asset-identity p{display:flex;align-items:center;gap:6px 8px;flex-wrap:wrap;margin:7px 0 0;color:var(--muted);font-size:12px}
.asset-identity .text-button{border:0;padding:0;background:none;color:var(--accent);cursor:pointer;font:inherit}
.asset-relation-pill{display:inline-flex;max-width:100%;margin-top:11px;padding:3px 10px;border-radius:5px;background:var(--accent-soft);color:var(--accent);font-size:12px;line-height:1.35;text-decoration:none;overflow-wrap:anywhere}
.asset-actions{display:flex;align-items:center;justify-content:flex-end;gap:9px;flex-wrap:wrap;margin-top:2px}
.asset-actions button,.asset-dex-action{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:6px 16px;border:1px solid var(--border);border-radius:7px;background:transparent;color:var(--text);font-size:12px;font-weight:600;text-decoration:none;cursor:pointer;white-space:nowrap}
.asset-actions button[aria-pressed="true"]{color:var(--accent);border-color:var(--accent)}
.asset-actions .asset-dex-action{background:var(--accent);border-color:var(--accent);color:#fff}
.asset-actions button:hover,.asset-actions .asset-dex-action:hover{transform:translateY(-1px)}
.asset-tabs{display:flex;gap:28px;min-width:0;border-bottom:1px solid var(--border);overflow-x:auto;white-space:nowrap}
.asset-tabs a{position:relative;padding:11px 0 12px;border-bottom:2px solid transparent;color:var(--muted);font-size:13px;text-decoration:none}
.asset-tabs a.active{border-color:var(--accent);color:var(--text);font-weight:650}
.asset-overview-grid{display:grid;grid-template-columns:minmax(0,1.9fr) minmax(275px,.88fr);gap:20px;min-width:0;align-items:start}
.asset-overview-pending{grid-template-columns:minmax(0,1fr)}
.asset-story,.asset-side{display:grid;min-width:0;gap:16px;align-content:start}
.asset-page .panel{min-width:0;margin:0;padding:20px;border:1px solid var(--border);border-radius:11px;background:var(--panel);box-shadow:none;overflow:visible}
.asset-story>.panel:first-child,.asset-side>.panel:first-child{border-top:1px solid var(--border);padding-top:20px}
.asset-page .panel-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;min-height:0;margin-bottom:16px}
.asset-page .panel-head h2,.asset-page .panel>h2{margin:0;font-family:inherit;font-size:14px;font-weight:650;line-height:1.3;letter-spacing:-.015em}
.asset-page .panel-head a{font-size:12px;color:var(--accent);text-decoration:none;white-space:nowrap}
.asset-quote{display:flex;align-items:baseline;gap:6px 15px;flex-wrap:wrap;margin-bottom:16px;min-width:0}
.asset-quote>span:first-child{font-family:var(--number-font,sans-serif);font-size:40px;line-height:1.13;font-weight:400;font-variant-numeric:tabular-nums;letter-spacing:-.035em;overflow-wrap:anywhere}
.asset-quote>b{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}
.asset-quote>b small{margin-left:3px;color:var(--muted);font-size:12px;font-weight:400}
.asset-quote :deep(.quote-status){flex-basis:100%;font-size:12px;color:var(--muted)}
.asset-quote-compact{margin:19px 0 0 58px}
.asset-quote-compact>span:first-child{font-size:30px}
.asset-price-label{margin:0 0 7px;color:var(--muted);font-size:12px;line-height:1.5}.asset-price-label-compact{margin:18px 0 -12px 58px}
.asset-chart-panel .panel-head{align-items:center;margin-bottom:11px}
.asset-chart-panel .panel-head h2{font-size:17px}
.asset-chart-panel label{display:flex;align-items:center;gap:8px;min-width:0;color:var(--muted);font-size:12px;white-space:nowrap}
.asset-chart-panel select{max-width:min(260px,27vw);min-width:0;padding:5px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);font-size:12px}
.asset-chart-panel :deep(.chart.x-candles){height:350px}
.asset-chart-panel :deep(.tab-group){margin-bottom:8px;border-bottom:1px solid var(--border);padding-bottom:8px}
.asset-chart-panel :deep(.tab-group .tab){border-radius:5px;padding:5px 10px}
.asset-chart-panel :deep(.tab-group .tab.active){border-color:transparent;background:var(--accent-soft);color:var(--accent)}
.asset-chart-panel :is([data-pool-unit],[data-exchange-unit]){margin:4px 0 9px;line-height:1.5}
.asset-story :deep(.asset-insight){margin:0;padding:20px;border:1px solid var(--border);border-radius:11px;background:var(--panel)}
.asset-story :deep(.asset-insight .panel-head h2){font-size:14px;font-family:inherit}
.asset-risk-panel .panel-head .hint{font-size:12px}
.asset-relationship-panel .relationship-path{padding:0}
.asset-key-data dl{margin:0}
.asset-key-data dl>div{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:baseline;gap:12px;padding:10px 0;border-bottom:1px solid var(--border)}
.asset-key-data dt{color:var(--muted);font-size:12px}
.asset-key-data dd{margin:0;text-align:right;font-size:14px;font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.asset-key-data dl>div:first-child{padding-top:0}
.asset-data-provenance{margin:11px 0 0;color:var(--muted);font-size:12px;line-height:1.5;overflow-wrap:anywhere}
.asset-live-trades>.hint{margin:-5px 0 7px;color:var(--muted);font-size:12px;line-height:1.45}
.asset-trade-mini{display:grid;grid-template-columns:minmax(48px,1fr) 40px minmax(82px,auto);gap:7px;align-items:center;border-bottom:1px solid var(--border);padding:10px 0;font-size:12px}
.asset-trade-mini time{color:var(--muted)}
.asset-trade-mini b{text-align:right;font-variant-numeric:tabular-nums}
.asset-trade-head{display:grid;grid-template-columns:minmax(48px,1fr) 40px minmax(82px,auto);gap:7px;padding:7px 0 3px;color:var(--muted);font-size:11px}.asset-trade-head>span:last-child{text-align:right}
.asset-page .asset-facts{padding:16px 20px}
.asset-facts>summary{cursor:pointer;color:var(--muted);font-size:12px;list-style-position:inside}
.asset-facts dl{margin:9px 0 0}
.asset-facts dl>div{display:grid;grid-template-columns:120px minmax(0,1fr);gap:14px;padding:7px 0;border-bottom:1px solid var(--border);font-size:12px}
.asset-facts dl>div:last-child{border-bottom:0}
.asset-facts dt{color:var(--muted)}
.asset-facts dd{margin:0;overflow-wrap:anywhere}
.asset-facts dd a{margin-right:10px}
.asset-page .asset-comparison{padding:16px 20px}
.asset-comparison>summary{cursor:pointer;color:var(--text);font-family:inherit;font-size:17px;font-weight:600}
.asset-comparison[open]>summary{margin-bottom:12px}
.asset-comparison .comparison-market{border-top:0;padding-top:0}
.asset-comparison :deep(.spread-panel){border-top:0;padding-top:10px}
.comparison-market{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:14px 0;border-top:1px solid var(--border)}
.comparison-market label{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:12px}
.comparison-market select{max-width:min(350px,70vw);background:var(--bg);border:1px solid var(--border);color:var(--text)}
.comparison-note{color:var(--muted);font-size:12px;line-height:1.4}
.comparison-note summary{cursor:pointer}
.comparison-note p{margin:7px 0 0;max-width:40ch}
.asset-section{min-width:0}
.asset-section :deep(.trade-activity){margin:15px 0}
.asset-section .table-wrap{max-width:100%;min-width:0;overflow-x:auto;margin-top:16px;overscroll-behavior-x:contain}
.asset-section .table-wrap table{min-width:690px}
.asset-section tr:focus{outline:2px solid var(--accent);outline-offset:-2px}
.asset-page :is(button,a,select,summary):focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(max-width:940px){.asset-overview-grid{grid-template-columns:minmax(0,1fr)}.asset-side{grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.asset-side>.panel:first-child,.asset-side>.panel:nth-child(2){border-top:1px solid var(--border);padding-top:20px}.asset-live-trades{grid-column:1/-1}}
@media(max-width:700px){.asset-page{gap:16px}.asset-hero-main{grid-template-columns:32px minmax(0,1fr);gap:10px}.asset-monogram{width:28px;height:28px;margin-top:4px;font-size:13px}.asset-identity h2{font-size:24px}.asset-actions{grid-column:1/-1;justify-content:stretch;margin-top:10px}.asset-actions button{flex:1}.asset-actions .asset-dex-action{flex:1.4}.asset-tabs{gap:23px}.asset-quote>span:first-child{font-size:34px}.asset-quote{margin-bottom:18px}.asset-quote-compact{margin:17px 0 0 42px}.asset-quote-compact>span:first-child{font-size:28px}.asset-chart-panel .panel-head{align-items:flex-start;flex-wrap:wrap}.asset-chart-panel label{width:100%;justify-content:space-between}.asset-chart-panel select{max-width:72%}.asset-chart-panel :deep(.chart.x-candles){height:270px}.asset-side{grid-template-columns:minmax(0,1fr)}.asset-side>.panel:nth-child(2){border-top:1px solid var(--border)}.asset-live-trades{grid-column:auto}.asset-facts dl>div{grid-template-columns:95px minmax(0,1fr)}.comparison-market label{width:100%;align-items:flex-start;flex-direction:column}.comparison-market select{width:100%;max-width:100%}}
@media(max-width:390px){.asset-actions{flex-direction:column;align-items:stretch}.asset-actions button,.asset-actions .asset-dex-action{width:100%}.asset-trade-mini{grid-template-columns:minmax(42px,1fr) 36px minmax(76px,auto)}.asset-quote>span:first-child{font-size:36px}.asset-quote-compact>span:first-child{font-size:27px}}
@media(max-width:700px){.asset-price-label-compact{margin-left:42px}}@media(max-width:390px){.asset-trade-head{grid-template-columns:minmax(42px,1fr) 36px minmax(76px,auto)}}
.asset-key-data dd small{display:block;color:var(--muted);font-size:12px;font-weight:400;margin-top:3px}.asset-mobile-actions{display:none}.asset-side>details>summary{font-family:inherit;font-size:14px;font-weight:650;cursor:pointer}.asset-side>details[open]>summary{margin-bottom:16px}.asset-side>details[open]>.panel-head{display:none}
@media(max-width:700px){.asset-page{padding-bottom:65px}.asset-mobile-actions{position:fixed;display:flex;align-items:center;gap:12px;left:0;right:0;bottom:calc(64px + env(safe-area-inset-bottom));padding:10px 18px;background:var(--panel);border-top:1px solid var(--border);z-index:30}.asset-mobile-actions>a,.asset-mobile-actions>span{flex:1;min-height:36px;display:flex;justify-content:center;align-items:center;border:1px solid var(--border);border-radius:7px;font-size:12px;color:var(--text);text-decoration:none}.asset-mobile-actions>a:nth-child(2){background:var(--accent);border-color:var(--accent);color:#fff}.asset-mobile-actions>span{color:var(--muted);border-color:var(--border)}.asset-side>.panel{padding:18px}}

.asset-live-trades { order:-1; }
.asset-trade-head { margin:0 -8px; padding:9px 8px; border-radius:5px; background:var(--bg); }
.asset-section :deep(th) { background:var(--bg); color:var(--muted); font-weight:500; }
@media(max-width:700px) { .asset-page .panel { padding:16px; } .asset-story>.panel:first-child,.asset-side>.panel:first-child { padding-top:16px; } .asset-story :deep(.asset-insight) { padding:16px; } .asset-hero-main { grid-template-columns:32px minmax(0,1fr); } }
.asset-headline{margin-top:20px}.asset-headline .asset-quote{margin-bottom:5px}.asset-quote-provenance{margin:0;color:var(--muted);font-size:12px;line-height:1.5;overflow-wrap:anywhere}.asset-headline .asset-quote :deep(.quote-status){flex-basis:auto}
.asset-market-context{display:flex;align-items:center;gap:6px 18px;flex-wrap:wrap;padding:12px 14px;border:1px solid var(--border);border-radius:8px;background:var(--panel);min-width:0}.asset-market-control{display:flex;align-items:center;gap:9px;min-width:0;font-size:12px}.asset-market-control label{color:var(--muted);white-space:nowrap}.asset-market-control select{max-width:330px;min-width:0;padding:6px 8px;border:1px solid var(--border);border-radius:5px;background:var(--bg);color:var(--text);font:inherit}.asset-market-control strong{font-size:12px;font-weight:600}.asset-market-context p{margin:0;font-size:12px;line-height:1.5;overflow-wrap:anywhere}.asset-market-note{flex-basis:100%;color:var(--muted)}
.asset-key-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px 20px;padding:15px 18px;border:1px solid var(--border);border-radius:9px;background:var(--panel);min-width:0}.asset-key-strip>div{min-width:0}.asset-key-strip>div>span,.asset-key-strip small{display:block;color:var(--muted);font-size:11px;line-height:1.5;overflow-wrap:anywhere}.asset-key-strip strong{display:block;margin:6px 0;font-size:21px;font-variant-numeric:tabular-nums;font-weight:600;overflow-wrap:anywhere}
.asset-compact-empty,.asset-chart-missing{margin:0;color:var(--muted);font-size:12px;line-height:1.7}.asset-risk-flags{display:flex;gap:6px;flex-wrap:wrap;list-style:none;padding:0;margin:0 0 12px}.asset-risk-flags li{display:flex;gap:6px;align-items:center;padding:5px 8px;border-radius:5px;background:var(--warning-soft,var(--accent-soft));color:var(--warning);font-size:12px}.asset-risk-flags strong{font-weight:600}.asset-risk-flags li>span{font-size:11px;color:var(--muted)}.asset-risk-panel .panel-head{margin-bottom:11px}.asset-risk-panel :deep(.risk-compact){border-top:0;padding-top:0;margin-top:0}.asset-risk-panel :deep(.risk-checks){grid-template-columns:repeat(2,minmax(0,1fr))}.asset-risk-panel :deep(.risk-checks details){padding:10px;border-bottom:1px solid var(--border)}
.asset-extra-metrics>summary,.asset-trade-analysis>summary{font-size:12px;color:var(--muted);cursor:pointer}
.asset-story :deep(.asset-insight:has(.insight-empty)){display:none}
.asset-chart-panel :deep(.chart-stage:is([data-chart-state="missing"],[data-chart-state="unavailable"])){min-height:0}
.asset-chart-panel :deep(.chart-stage:is([data-chart-state="missing"],[data-chart-state="unavailable"]) .chart.x-candles){height:0;min-height:0}
.asset-chart-panel :deep(.chart-stage:is([data-chart-state="missing"],[data-chart-state="unavailable"]) .chart-empty){position:static;inset:auto;min-height:0;padding:15px 8px;text-align:left;align-items:flex-start}
@media(max-width:700px){.asset-headline{margin-top:17px}.asset-headline .asset-quote>span:first-child{font-size:32px}.asset-market-context{padding:11px 12px}.asset-market-control{width:100%;flex-wrap:wrap}.asset-market-control select{max-width:100%;flex:1}.asset-key-strip{grid-template-columns:repeat(2,minmax(0,1fr));padding:12px;gap:12px}.asset-key-strip strong{font-size:19px}.asset-key-strip>div:last-child:nth-child(odd){grid-column:1/-1}.asset-chart-panel .panel-head .hint{font-size:11px;line-height:1.5}.asset-risk-panel :deep(.risk-checks){grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
