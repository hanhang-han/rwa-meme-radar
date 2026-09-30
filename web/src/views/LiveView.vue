<template>
  <div class="home-page">
    <div class="home-heading"><h2>{{ tr('市场概览','Market overview') }}</h2><span>{{ tr('股票主题与链上 Meme','Stock themes and on-chain memes') }}</span></div>
    <div class="kpis">
      <KpiCard kpi-key="actionableAssets" :title="tr('活跃 Meme','Active memes')" :value="num(scopeMetrics.active)" :note="tr('价格 15 分钟内 · 总流动性 ≥ $1K','Price within 15m · Total liquidity ≥ $1K')" :href="kpiHref({qualified:'1',rel:'all'})" />
      <KpiCard kpi-key="verifiedPools" :title="tr('池子配对','Pool pairs')" :value="num(scopeMetrics.pools)" :note="tr('池估值 15 分钟内 · 流动性 ≥ $1K','Pool quote within 15m · Liquidity ≥ $1K')" :href="kpiHref({view:'pool',qualified:'1'})" />
      <KpiCard kpi-key="pairedLiquidityUsd" :title="tr('配对池总流动性','Pair liquidity')" :value="usd(scopeMetrics.liquidity)" :note="tr('股票配对池合计','Total across stock pair pools')" :href="kpiHref({view:'pool',qualified:'1'})" />
      <KpiCard kpi-key="newAssets24h" :title="tr('近 24h 新发现','Discovered in 24h')" :value="num(scopeMetrics.newAssets)" :note="tr('按首次收录时间统计','Counted by first indexing time')" :href="kpiHref({new:'24h',sort:'firstSeen',rel:'all',fresh:'0',showMissing:'1'})" />
    </div>

    <section v-if="hotStocks.length" class="panel">
      <div class="panel-head"><h2>{{ tr('热门股票主题','Trending stock themes') }}</h2><RouterLink :to="{path:'/stock',query:{chain:scope}}">{{ tr('全部股票','All stocks') }} →</RouterLink></div>
      <div class="home-stock-grid" :style="{'--hot-columns':Math.min(4,hotStocks.length)}">
        <RouterLink v-for="card in hotStocks" :key="card.ticker" class="home-stock-card" :to="{path:'/stock/'+card.ticker,query:{chain:scope}}">
          <strong>{{ card.ticker }} <small>{{ stockName(card.stock,card.ticker) }}</small></strong>
          <span><b :title="stockQuoteTitle(card.stock)">{{ price(card.stock?.price,card.stock?.priceCurrency) }}</b><b :class="Number(card.stock?.change24h)>0?'up':Number(card.stock?.change24h)<0?'down':''">{{ pct(card.stock?.change24h) }}</b></span>
          <small :title="tr('按币统计 24h 成交，含其他池；并非股票配对池成交。','24h token volume includes other pools; it is not stock-pair pool volume.')">{{ num(card.assetCount) }} Meme · {{ tr('关联 Meme 总成交 · 24h','Related meme total volume · 24h') }} {{ usd(card.volume) }}</small>
          <small>{{ tr('成交覆盖','Volume coverage') }} {{ card.volumeKnown }} / {{ card.volumeTotal }}</small>
        </RouterLink>
      </div>
    </section>

    <ThemeMarketMap v-if="themeMap?.bubbles?.length || !themeMapReady" :theme-map="themeMap" :name-clues="nameClues" :name-clues-loading="!fullSnapshotReady" :stock-tokens="store.stockTokens" :scope="scope" :loading="!themeMapReady" @load-clues="loadMarket" />

    <div class="home-market-grid" :class="{'single-column':!related.length}">
      <section v-if="related.length" class="panel home-leaders">
        <div class="panel-head"><h2>{{ tr('Meme 24h 成交榜','Meme 24h volume') }}</h2><RouterLink :to="{path:'/meme',query:{chain:scope,rel:'all',sort:'volume24h',rank:'volume24h',category:'meme',fresh:'0',showMissing:'1'}}">{{ tr('全部','All') }} →</RouterLink></div>
        <p class="home-subline">{{ tr('显示','Showing') }} {{ related.length }} / {{ tr('共','of') }} {{ leaderTotal }} · USD</p>
        <div class="home-rank-head"><span>#</span><span>Meme</span><span>{{ tr('股票','Stock') }}</span><span>{{ tr('风险','Risk') }}</span><span>{{ tr('24h 成交','24h volume') }}</span></div>
        <div v-for="(asset,index) in related" :key="asset.chainId+':'+asset.token" class="home-leader-row">
          <span class="rank-position">{{ index+1 }}</span><RouterLink :to="assetPath(asset)"><strong>{{ asset.name || asset.symbol || short(asset.token) }}</strong><small>{{ chainName(asset) }}</small></RouterLink><span class="rank-tickers">{{ asset.tickers?.join(' · ') || relatedTickers(asset) }}</span><span class="rank-risk"><RiskBadge :asset="asset" /></span><strong class="rank-amount" :title="assetVolumeTitle(asset)">{{ usd(asset.volume24h) }}</strong>
        </div>
      </section>
      <ImportantChanges v-if="importantChanges?.items?.length || feed.relationships.length" :changes="importantChanges" :observations="feed.relationships" :unified="store.snapshot?.unified" :assets="store.assets" :scope="scope" :loading="!importantChangesReady" />
    </div>

    <section v-if="briefingItems.length" class="panel home-activity-summary">
      <div class="panel-head"><h2>{{ tr('需留意','Worth checking') }}</h2><time class="home-subline">{{ briefing?.at ? clockTime(briefing.at) : '—' }}</time></div>
      <div class="briefing-list"><article v-for="item in briefingItems" :key="item.id" class="briefing-item"><span class="activity-warning" aria-hidden="true">!</span><RouterLink v-if="item.asset?.address && item.asset?.chainId" :to="assetPath({chainId:item.asset.chainId,token:item.asset.address})" class="briefing-asset">{{ item.asset.name || item.asset.symbol || short(item.asset.address) }}</RouterLink><strong v-else>{{ item.asset?.name ?? tr('市场动态','Market activity') }}</strong><span>{{ briefingMessageFor(item) }}</span></article></div>
    </section>

    <section v-if="displayedTrades.length" class="panel home-trades">
      <div class="panel-head"><div><h2>{{ tr('链上成交','On-chain trades') }}</h2><span v-if="tradeDelay>30000" class="trade-delay">{{ tr('成交延迟','Trade delay') }} {{ Math.ceil(tradeDelay/1000) }}s</span></div><div class="home-feed-controls"><select v-model.number="minTrade" :aria-label="tr('最小成交额','Minimum trade amount')"><option :value="0">{{ tr('全部金额','All amounts') }}</option><option :value="100">≥ $100</option><option :value="1000">≥ $1K</option><option :value="10000">≥ $10K</option></select><select v-model="direction" :aria-label="tr('买卖方向','Trade direction')"><option value="all">{{ tr('全部方向','All directions') }}</option><option value="buy">{{ tr('买入','Buy') }}</option><option value="sell">{{ tr('卖出','Sell') }}</option></select></div></div>
      <div class="home-trade-list">
        <div v-for="trade in displayedTrades.slice(0,10)" :key="trade.chainId+':'+trade.token+':'+trade.id" class="home-trade-row" :class="{'is-large':tradeDisplayAmount(trade)?.currency==='USD' && (tradeDisplayAmount(trade)?.value??0)>=10000}">
          <time :title="date(trade.t)">{{ age(trade.t) }}</time><RouterLink :to="assetPath(trade)">{{ trade.symbol || short(trade.token) }}<small>{{ chainName(trade) }} · {{ tradeSource(trade) }}</small></RouterLink><span :class="trade.type==='buy'?'up':trade.type==='sell'?'down':''">{{ trade.type==='buy'?tr('买入','Buy'):trade.type==='sell'?tr('卖出','Sell'):tr('未能识别','Unknown') }}</span><strong><TradeAmount :trade="trade" /></strong><a v-if="trade.wallet" :href="explorer(trade.wallet,'address',String(trade.chainId))" target="_blank" rel="noopener" :title="trade.wallet">{{ short(trade.wallet) }} ↗</a><small v-else>{{ tr('钱包未能识别','Wallet unavailable') }}</small><a v-if="trade.hash" :href="explorer(trade.hash,'tx',String(trade.chainId))" target="_blank" rel="noopener" :aria-label="tr('查看交易','View transaction')">↗</a>
        </div>
      </div>
    </section>

    <section v-if="scope==='all' && distribution.length" class="panel">
      <div class="panel-head"><h2>{{ tr('链上分布','Chain distribution') }}</h2></div>
      <RouterLink v-for="item in distribution" :key="item.chainId" class="v2-distribution" :to="{path:'/meme',query:{chain:item.chainId,rel:'all'}}"><strong>{{ chainName(item) }}</strong><span>{{ num(item.assets) }} Meme<span class="chain-share" aria-hidden="true"><i :style="{width:distributionMax?(Number(item.assets||0)/distributionMax*100)+'%':'0%'}"></i></span></span><span>{{ tr('24h 成交','24h volume') }} {{ usd(item.volume?.value) }}<small> · {{ tr('覆盖','Coverage') }} {{ num(item.volume?.known) }}/{{ num(item.volume?.total) }}</small></span><span>{{ tr('池流动性','Pool liquidity') }} {{ usd(item.liquidity?.value) }}</span></RouterLink>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import KpiCard from '../components/KpiCard.vue';
import RiskBadge from '../components/RiskBadge.vue';
import TradeAmount from '../components/TradeAmount.vue';
import ThemeMarketMap from '../components/ThemeMarketMap.vue';
import ImportantChanges from '../components/ImportantChanges.vue';
import { useDashboardStore } from '../stores/dashboard';
import { useFeedStore } from '../stores/feed';
import { getBriefing } from '../api/client';
import { tr, useI18n } from '../i18n';
import { age, chainName, date, explorer, num, pct, price, short, usd } from '../utils/format';
import { chainScope, inChainScope } from '../utils/chain-scope';
import { homeStockCards, metricsForScope, onchainRecordedTrades, tradeDisplayAmount } from '../utils/home-model';
import { useMinuteClock } from '../composables/useMinuteClock';
import { relationMatchesAsset } from '../utils/relations';
import { comparableVolume, stockThemeName } from '../utils/stock-theme-model';
import { buildNameClues, fallbackRelationEvents, fallbackThemeMap } from '../utils/theme-map-model';

const route = useRoute();
const store = useDashboardStore();
const feed = useFeedStore();
const { lang } = useI18n();
const scope = computed(() => chainScope(route.query));
const now=useMinuteClock();
const fullSnapshotReady = computed(() => !!store.snapshot?.unified && store.snapshot.unified.snapshotScope !== 'overview');
const themeMapReady = computed(() => !!store.snapshot?.unified?.themeMap || fullSnapshotReady.value);
const importantChangesReady = computed(() => !!store.snapshot?.unified?.importantChanges || fullSnapshotReady.value);
const themeMap = computed(() => {
  const unified = store.snapshot?.unified;
  if (!unified) return null;
  return unified.themeMap ?? fallbackThemeMap(unified);
});
const nameClues = computed(() => fullSnapshotReady.value ? buildNameClues(store.snapshot.unified,scope.value) : []);
const importantChanges = computed(() => {
  const unified = store.snapshot?.unified;
  if (!unified) return null;
  return unified.importantChanges ?? fallbackRelationEvents(feed.relationships, unified);
});
const scopeMetrics = computed(() => metricsForScope(store.snapshot?.unified, scope.value, now.value));
const hotStocks = computed(() => homeStockCards(store.snapshot?.unified,scope.value,now.value));
const minTrade = ref(0);
const direction = ref('all');
const displayedTrades = computed(() => onchainRecordedTrades(feed.trades,null,scope.value,minTrade.value,direction.value));
const tradeDelay=computed(()=>{const trade=displayedTrades.value[0];return trade?.delayMs??(trade?.t&&trade.receivedAt?Math.max(0,Number(trade.receivedAt)-Number(trade.t)):0);});
const distribution = computed(() => [...(store.snapshot?.unified?.distribution ?? [])].sort((a,b)=>(b.volume?.value??-1)-(a.volume?.value??-1)));
const distributionMax = computed(() => Math.max(0, ...distribution.value.map(row => Number(row.assets) || 0)));
const eligibleRelations = computed(() => store.relations.filter(r => r.level === 'A'));
const publishedLeaders=computed(()=>scope.value==='all'?store.snapshot?.unified?.memeLeaders:store.snapshot?.unified?.memeLeadersByChain?.[scope.value]);
const eligibleLeaders=computed(()=>fullSnapshotReady.value ? store.assets.filter(asset=>asset.kind==='candidate' && asset.assetCategory!=='derivative' && inChainScope(asset,scope.value) && comparableVolume(asset,now.value)!=null).sort((a,b)=>Number(b.volume24h)-Number(a.volume24h)) : []);
const related=computed(()=>publishedLeaders.value?.items ?? eligibleLeaders.value.slice(0,10));
const leaderTotal=computed(()=>publishedLeaders.value?.total ?? eligibleLeaders.value.length);
function kpiHref(query){return '#/meme?'+new URLSearchParams({chain:scope.value,...query}).toString();}
function assetPath(asset){return {path:`/asset/${asset.chainId}/${asset.token}`,query:{chain:scope.value,from:'live'}};}
function stockQuoteTitle(stock){return `${stock?.fieldSources?.price??stock?.provider??'—'} · ${date(stock?.fieldTimes?.price??stock?.quoteAt)}`;}
function loadMarket(){return store.poll();}

const briefing = ref(null);
const briefingLoading = ref(true);
const briefingError = ref(false);
const briefingStatus = ref('scheduled');
const briefingCache = new Map();
let briefingRequest = 0;
const briefingItems = computed(() => (briefing.value?.items ?? []).filter(item => scope.value === 'all' || String(item.asset?.chainId) === scope.value).slice(0,3));
function briefingMessageFor(item) {
  const f = item.fields ?? {};
  const observedLiquidity = f.totalLiquidityUsd == null
    ? tr('暂无总流动性数据', 'total liquidity unavailable')
    : tr('总流动性 ' + usd(f.totalLiquidityUsd), 'total liquidity ' + usd(f.totalLiquidityUsd));
  if (item.type === 'wash_suspect') {
    if (f.volumeLiquidityRatio != null) return tr('24h 成交 / 已观测流动性：' + Math.round(f.volumeLiquidityRatio) + ' 倍',
      '24h volume / observed liquidity: ' + Math.round(f.volumeLiquidityRatio) + 'x');
    if (f.transactionsPerHolder != null) return tr('成交笔数 / 持币地址数：' + Math.round(f.transactionsPerHolder),
      'Trades / holder addresses: ' + Math.round(f.transactionsPerHolder));
  }
  if (item.type === 'thin_spike') return tr('24h 涨幅 ' + pct(f.change24hPercent) + ' · ' + observedLiquidity,
    '24h change ' + pct(f.change24hPercent) + ' · ' + observedLiquidity);
  return tr('24h 成交 ' + usd(f.volume24hUsd) + ' · ' + observedLiquidity,
    '24h volume ' + usd(f.volume24hUsd) + ' · ' + observedLiquidity);
}
function tradeSource(trade) {
  const venue = String(trade.venue ?? '').toLowerCase();
  if (venue === 'binance-alpha') return 'Binance Alpha';
  if (venue === 'binance') return 'Binance';
  if (venue === 'dex') return tr('链上', 'Onchain');
  return String(trade.provider ?? trade.source ?? trade.venue ?? tr('来源未能识别', 'Unknown source'));
}
function assetVolumeTitle(asset) {
  const at = asset.fieldTimes?.volume24h ?? asset.volumeAt;
  return `${asset.fieldSources?.volume24h ?? asset.provider ?? tr('来源未能识别', 'Unknown source')} · ${date(at)}`;
}
function stockName(stock,ticker) { return stockThemeName(stock,ticker,lang.lang); }
function relatedTickers(asset) { return [...new Set(eligibleRelations.value.filter(r => relationMatchesAsset(r,asset)).map(r => r.ticker).filter(Boolean))].slice(0,3).join(' · '); }
function clockTime(value) { return value ? new Date(value).toLocaleTimeString(lang.lang === 'en' ? 'en-US' : 'zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}) : '—'; }
async function loadBriefing() {
  const request = ++briefingRequest, language = lang.lang;
  briefingLoading.value = !briefing.value;
  try {
    const result = await getBriefing(language);
    if (request !== briefingRequest) return;
    briefingError.value = false;
    briefingStatus.value = result?.status ?? result?.reason ?? 'scheduled';
    if (result?.text || result?.items?.length) { briefing.value = result; briefingCache.set(language,result); }
  } catch { if (request === briefingRequest) briefingError.value = true; }
  finally { if (request === briefingRequest) briefingLoading.value = false; }
}
let feedTimer;
function loadFeed() { return feed.load(scope.value === 'all' ? null : scope.value); }
function onResourceChange(event) {
  const kind = event.detail?.kind;
  if (kind === 'briefing' || kind === 'all') loadBriefing();
  if (kind === 'feed' || kind === 'all') loadFeed();
}
onMounted(() => {
  loadBriefing(); loadFeed();
  window.addEventListener('resource-change',onResourceChange);
  feedTimer = setInterval(() => { if (!document.hidden && !store.hasProjectionStream) { loadFeed(); loadBriefing(); } },30000);
});
onUnmounted(() => { window.removeEventListener('resource-change',onResourceChange); clearInterval(feedTimer); briefingRequest++; feed.setPaused(false); });
watch(() => lang.lang, () => { briefing.value = briefingCache.get(lang.lang) ?? null; briefingError.value = false; loadBriefing(); });
watch(scope,loadFeed);
</script>

<style scoped>
.home-heading { display:flex; align-items:baseline; flex-wrap:wrap; gap:8px 16px; margin:12px 0 18px; }
.home-heading h2 { font-size:24px; }
.home-heading span,.home-subline { font-size:12px; color:var(--muted); }
.home-page .panel-head h2 { font-size:17px; letter-spacing:0; }
.home-stock-grid { grid-template-columns:repeat(3,minmax(0,1fr)); }
.home-stock-card { min-width:0; }
.home-stock-card > strong small { font-size:12px; }
.home-stock-card small { font-size:12px; }
.home-market-grid { display:grid; grid-template-columns:minmax(0,1.1fr) minmax(0,1fr); gap:14px; align-items:stretch; }
.home-market-grid.single-column { grid-template-columns:1fr; }
.home-market-grid > * { min-width:0; }
.home-rank-head,.home-leader-row { display:grid; grid-template-columns:22px minmax(90px,1fr) 66px 46px 105px; align-items:center; gap:10px; }
.home-rank-head { font-size:12px; color:var(--muted); border-bottom:1px solid var(--border); padding:13px 0 8px; }
.home-leader-row { padding:14px 0; border-bottom:1px solid var(--border); font-size:13px; }
.home-leader-row:last-child { border-bottom:0; }
.home-leader-row a { color:var(--text); overflow:hidden; }
.home-leader-row strong { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.home-leader-row small { display:block; color:var(--muted); font-size:12px; margin-top:3px; }
.rank-position,.rank-tickers { color:var(--muted); font-size:12px; }
.rank-risk { width:46px; }
.rank-amount,.home-rank-head > span:last-child { text-align:right; font-family:ui-monospace,monospace; font-variant-numeric:tabular-nums; }
.activity-warning { color:var(--warning); border:1px solid var(--warning); border-radius:50%; width:17px; height:17px; line-height:15px; text-align:center; font-size:12px; flex:none; }
.home-activity-summary .briefing-asset { color:var(--text); }
.home-trade-list { overflow:visible; }
.home-trade-row { grid-template-columns:110px minmax(130px,1fr) 60px 110px 125px 20px; }
.home-trade-row a { color:var(--text); }
.home-trade-row time,.home-trade-row small { font-size:12px; }
.trade-delay { color:var(--warning); font-size:12px; }
.chain-share { display:block;height:4px;margin-top:9px;background:var(--surface-raised);border-radius:2px;overflow:hidden; }
.chain-share i { display:block;height:100%;background:var(--accent);opacity:.7; }
@media(max-width:1100px) { .home-market-grid { grid-template-columns:1fr; } }
@media(max-width:720px) { .home-stock-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .home-rank-head,.home-leader-row { grid-template-columns:18px minmax(75px,1fr) 44px 32px 90px; gap:7px; } .rank-risk { width:32px; } .home-trade-row { grid-template-columns:70px minmax(90px,1fr) 44px 85px; } .home-trade-row > :nth-child(n+5) { display:none; } .home-trades .panel-head { flex-wrap:wrap; gap:12px; } }
</style>
