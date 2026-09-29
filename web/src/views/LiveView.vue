<template>
  <div>
    <div class="hero x-hero">
      <div class="v2-hero-copy">
        <h2>{{ tr('探索股票主题与链上资产', 'Explore stock themes and onchain assets') }}</h2>
        <p>{{ tr('已核验配对与未核验名称线索分层展示，行情和证据均可追溯。', 'Verified pairs and unverified name clues are shown separately, with traceable market data and evidence.') }}</p>
      </div>
    </div>

    <div class="kpis">
      <KpiCard kpi-key="actionableAssets" :title="tr('数据达标的活跃 Meme', 'Qualified active memes')" :value="num(scopeMetrics.active)" :note="deltaNote('active') || tr('需有新鲜报价与资产总流动性', 'Requires a fresh quote and asset-wide liquidity')" href="#/meme" />
      <KpiCard kpi-key="verifiedPools" :title="tr('股票配对池', 'Stock pairs')" :value="num(scopeMetrics.pools)" :note="deltaNote('pools')" href="#/pair" />
      <KpiCard kpi-key="pairedLiquidityUsd" :title="tr('配对池总流动性', 'Pair liquidity')" :value="usd(scopeMetrics.liquidity)" :note="deltaNote('liquidity')" href="#/pair" />
      <KpiCard kpi-key="newRelations24h" :title="tr('24h 新增配对', 'New pairs (24h)')" :value="num(scopeMetrics.newPairs)" :note="scopeMetrics.newPairs == null ? tr('新池创建时间待采集','Pool creation time pending') : deltaNote('newPairs')" href="#/events" />
    </div>

    <ThemeMarketMap :theme-map="themeMap" :name-clues="nameClues" :stock-tokens="store.stockTokens" :scope="scope" :loading="!fullSnapshotReady" />
    <ImportantChanges :changes="importantChanges" :observations="feed.relationships" :unified="store.snapshot?.unified" :assets="store.assets" :scope="scope" :loading="!fullSnapshotReady" />

    <section class="panel x-ai">
      <div class="panel-head"><h2>{{ tr('今日异动', 'Today') }}</h2><time class="hint">{{ briefing?.at ? clockTime(briefing.at) : '—' }}</time></div>
      <div v-if="briefingItems.length" class="briefing-list">
        <article v-for="item in briefingItems" :key="item.id" class="briefing-item">
          <RouterLink v-if="item.asset?.address && item.asset?.chainId" :to="{path:`/detail/${item.asset.chainId}/${item.asset.address}`,query:{chain:scope}}" class="briefing-asset">{{ item.asset.name || item.asset.symbol || short(item.asset.address) }} <small>{{ chainName(item.asset) }}</small></RouterLink>
          <strong v-else>{{ item.asset?.name ?? tr('市场异动', 'Market activity') }}</strong>
          <span>{{ briefingMessageFor(item) }}</span>
        </article>
      </div>
      <div v-else-if="briefing?.items?.length" class="v2-ai-text">{{ tr('当前范围暂无符合条件的异动。', 'No qualifying changes in this scope.') }}</div>
      <div v-else-if="briefing?.text && scope === 'all'" class="v2-ai-text">{{ briefing.text }}</div>
      <div v-else class="v2-ai-text" role="status">{{ briefingMessage }}</div>
      <p v-if="briefing && (briefing.stale || briefingError)" class="hint">{{ tr('当前展示上一份简报，后台更新后自动替换。', 'Showing the last report; it will be replaced after the next successful update.') }}</p>
    </section>

    <section class="panel">
      <div class="panel-head"><h2>{{ tr('热门股票', 'Trending stocks') }}</h2><RouterLink to="/stock">{{ tr('全部', 'All') }} →</RouterLink></div>
      <div v-if="hotStocks.length" class="home-stock-grid">
        <RouterLink v-for="card in hotStocks" :key="card.ticker" class="home-stock-card" :to="{path:'/stock',query:{chain:scope,q:card.ticker}}">
          <strong>{{ card.ticker }} <small>{{ stockName(card.stock) }}</small></strong>
          <span><b>{{ price(card.stock?.price,card.stock?.priceCurrency) }}</b><b :class="Number(card.stock?.change24h)>0?'up':Number(card.stock?.change24h)<0?'down':''">{{ pct(card.stock?.change24h) }}</b></span>
          <small>{{ num(card.memes.size) }} Meme · {{ usd(card.volume) }} · {{ chainName(card.stock) }} {{ card.stock?.provider ?? '' }}</small>
        </RouterLink>
      </div>
      <div v-else class="x-empty">{{ tr('当前范围暂无关联股票。', 'No related stocks in this scope.') }}</div>
    </section>

    <div class="x-grid v21-feed-grid">
      <section class="panel x-feed">
        <div class="panel-head">
          <h2>{{ tr('实时成交', 'Live trades') }}</h2>
          <div class="home-feed-controls">
            <select v-model.number="minTrade" :aria-label="tr('最小成交额', 'Minimum trade amount')"><option :value="100">≥ 100 USD/USDT</option><option :value="1000">≥ 1K USD/USDT</option><option :value="10000">≥ 10K USD/USDT</option></select>
            <select v-model="direction" :aria-label="tr('买卖方向', 'Trade direction')"><option value="all">{{ tr('全部方向', 'All directions') }}</option><option value="buy">{{ tr('买入', 'Buy') }}</option><option value="sell">{{ tr('卖出', 'Sell') }}</option></select>
          </div>
        </div>
        <div class="scroll v2-feed-scroll" ref="feedScroll" @scroll="onFeedScroll" @mouseenter="feed.setPaused(true)" @mouseleave="feed.setPaused(false)">
          <button v-if="feed.hasNew" class="v2-new-pill" @click="jumpToNew">{{ feed.newCount }} {{ tr('条新成交', 'new trades') }} ↑</button>
          <div v-for="t in displayedTrades" :key="t.chainId + ':' + t.token + ':' + t.id" class="home-trade-row" :class="{ 'is-large': (tradeDisplayAmount(t)?.value ?? 0) >= 10000 }">
            <time :title="date(t.t)">{{ age(t.t) }}</time>
            <RouterLink :to="{path:'/detail/' + t.chainId + '/' + t.token,query:{chain:scope}}">{{ t.symbol || short(t.token) }}<small>{{ scope === 'all' ? chainName(t) + ' · ' : '' }}{{ tradeSource(t) }}</small></RouterLink>
            <span :class="t.type === 'buy' ? 'up' : 'down'">{{ t.type === 'buy' ? tr('买入', 'Buy') : tr('卖出', 'Sell') }}</span>
            <strong>{{ money(tradeDisplayAmount(t)?.value,tradeDisplayAmount(t)?.currency) }}</strong>
            <small>{{ t.wallet ? short(t.wallet) : '—' }}</small>
            <a v-if="t.hash" :href="explorer(t.hash,'tx',String(t.chainId))" target="_blank" rel="noopener" :aria-label="tr('查看交易', 'View transaction')">↗</a>
          </div>
          <div v-if="!displayedTrades.length" class="x-empty">{{ tr('当前条件下暂无成交。', 'No trades match these filters.') }}</div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>{{ tr('新发现', 'New') }}</h2><RouterLink to="/events">{{ tr('全部', 'All') }} →</RouterLink></div>
        <div v-if="discoveries.length">
          <RouterLink v-for="row in discoveries" :key="row.id" class="home-new-row" :to="{path:'/detail/' + row.chainId + '/' + row.token,query:{chain:scope}}">
            <strong>{{ row.name }}</strong><small>{{ row.label }} · {{ chainName(row) }}</small><time :title="date(row.at)">{{ row.timeKind === 'created' ? tr('创建于','Created') : tr('收录于','Indexed') }} {{ age(row.at) }}</time>
          </RouterLink>
        </div>
        <div v-else class="x-empty">{{ tr('当前范围暂无新关联。', 'No new relations in this scope.') }}</div>
      </section>
    </div>
    <section class="panel">
      <div class="panel-head"><h2>{{ tr('Meme 24h 成交榜', 'Top memes by 24h volume') }}</h2><RouterLink to="/meme">{{ tr('全部', 'All') }} →</RouterLink></div>
      <div v-if="related.length" class="home-rank-list">
        <RouterLink v-for="(asset,index) in related" :key="asset.chainId + ':' + asset.token" class="home-rank-row" :to="{path:'/detail/' + asset.chainId + '/' + asset.token,query:{chain:scope}}">
          <b>{{ index + 1 }}</b><strong>{{ asset.name || asset.symbol || short(asset.token) }} <small v-if="scope === 'all'">{{ chainName(asset) }}</small></strong>
          <span>{{ relatedTickers(asset) }}</span><span v-if="asset.riskFlags?.length" class="risk-mark">!</span><b>{{ money(asset.volume24h,asset.volumeCurrency ?? asset.priceCurrency) }}</b>
        </RouterLink>
      </div>
      <div v-else class="x-empty">{{ tr('当前暂无合格关联资产。', 'No qualifying related memes.') }}</div>
    </section>

    <section v-if="scope === 'all'" class="panel">
      <h2>{{ tr('各链对照', 'By chain') }}</h2>
      <RouterLink v-for="d in distribution" :key="d.chainId" class="v2-distribution" :to="{ path: '/meme', query: { chain: d.chainId } }">
        <strong>{{ d.name }}</strong>
        <span>{{ num(d.assets) }} {{ tr('个 Meme', 'memes') }}</span>
        <span>{{ tr('24h 成交', '24h volume') }} {{ usd(d.volume?.value) }}</span>
        <span>{{ tr('池流动性', 'Pool liquidity') }} {{ usd(d.liquidity?.value) }}</span>
      </RouterLink>
    </section>

  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import KpiCard from '../components/KpiCard.vue';
import ThemeMarketMap from '../components/ThemeMarketMap.vue';
import ImportantChanges from '../components/ImportantChanges.vue';
import { useDashboardStore } from '../stores/dashboard';
import { useFeedStore } from '../stores/feed';
import { getBriefing } from '../api/client';
import { tr, useI18n } from '../i18n';
import { age, chainName, date, explorer, money, num, pct, price, short, usd } from '../utils/format';
import { chainScope, inChainScope } from '../utils/chain-scope';
import { discoveryTime, filteredTrades, metricsForScope, topStockCards, tradeDisplayAmount } from '../utils/home-model';
import { relationMatchesAsset } from '../utils/relations';
import { normalizeEventAddress } from '../utils/event-records';
import { buildNameClues, fallbackRelationEvents, fallbackThemeMap } from '../utils/theme-map-model';

const route = useRoute();
const store = useDashboardStore();
const feed = useFeedStore();
const { lang } = useI18n();
const scope = computed(() => chainScope(route.query));
const fullSnapshotReady = computed(() => !!store.snapshot?.unified && store.snapshot.unified.snapshotScope !== 'overview');
const themeMap = computed(() => {
  if (!fullSnapshotReady.value) return null;
  const unified = store.snapshot.unified;
  return unified.themeMap ?? fallbackThemeMap(unified);
});
const nameClues = computed(() => fullSnapshotReady.value ? buildNameClues(store.snapshot.unified,scope.value) : []);
const importantChanges = computed(() => {
  if (!fullSnapshotReady.value) return null;
  const unified = store.snapshot.unified;
  return unified.importantChanges ?? fallbackRelationEvents(feed.relationships, unified);
});
const scopeMetrics = computed(() => metricsForScope(store.snapshot?.unified, scope.value));
const hotStocks = computed(() => topStockCards(store.stockTokens, store.relations, store.assets, scope.value));
const minTrade = ref(100);
const direction = ref('all');
const displayedTrades = computed(() => filteredTrades(feed.trades, scope.value, minTrade.value, direction.value));
const feedScroll = ref(null);
const distribution = computed(() => store.snapshot?.unified?.distribution ?? []);
const eligibleRelations = computed(() => store.relations.filter(r => r.level === 'A'));
const related = computed(() => store.assets.filter(a => a.kind === 'candidate' && inChainScope(a,scope.value) && eligibleRelations.value.some(r => relationMatchesAsset(r,a)))
  .sort((a,b) => Number(b.volume24h ?? -1) - Number(a.volume24h ?? -1)).slice(0,10));
const discoveries = computed(() => (feed.relationships ?? []).filter(r => inChainScope(r,scope.value)).map(r => {
  const relation = r.relation ?? r;
  const ticker = relation.ticker ?? r.ticker;
  const chainId = String(r.chainId ?? relation.chainId ?? '');
  const token = normalizeEventAddress(relation.token ?? r.asset,chainId);
  if (!ticker || !token || !chainId) return null;
  const time = discoveryTime(r);
  return { id:r.id ?? relation.id, chainId, token, name:r.symbol || relation.tokenSymbol || short(token),
    label:(relation.level === 'A' ? tr('池配对', 'Paired') : relation.level === 'B' ? tr('名称相关', 'Name match') : tr('关系待核验', 'Relation unverified')) + ' · ' + ticker,
    at:time.at, timeKind:time.kind };
}).filter(Boolean).slice(0,8));

const briefing = ref(null);
const briefingLoading = ref(true);
const briefingError = ref(false);
const briefingStatus = ref('scheduled');
const briefingCache = new Map();
let briefingRequest = 0;
const briefingItems = computed(() => (briefing.value?.items ?? []).filter(item => scope.value === 'all' || String(item.asset?.chainId) === scope.value).slice(0,3));
const briefingMessage = computed(() => {
  if (briefingLoading.value) return tr('正在读取简报…', 'Loading briefing…');
  if (briefingError.value) return tr('简报读取失败，稍后自动重试。', 'Could not load the briefing. Retrying shortly.');
  if (briefingStatus.value === 'disabled') return tr('自动简报暂未启用。', 'Automatic briefings are not enabled.');
  if (briefingStatus.value === 'upstream_failed') return tr('本轮生成失败，等待后台重试。', 'Generation failed; waiting for a background retry.');
  return tr('暂无简报，等待后台定时生成。', 'No briefing yet; waiting for the scheduled run.');
});
function briefingMessageFor(item) {
  const f = item.fields ?? {};
  const observedLiquidity = f.totalLiquidityUsd == null
    ? tr('总流动性待核验', 'total liquidity unverified')
    : tr('总流动性 ' + usd(f.totalLiquidityUsd), 'total liquidity ' + usd(f.totalLiquidityUsd));
  if (item.type === 'wash_suspect') {
    if (f.volumeLiquidityRatio != null) return tr('24h 成交是已观测流动性的 ' + Math.round(f.volumeLiquidityRatio) + ' 倍，建议核查成交结构',
      '24h volume is ' + Math.round(f.volumeLiquidityRatio) + 'x observed liquidity; review trade activity');
    if (f.transactionsPerHolder != null) return tr('成交笔数与持币地址数之比 ' + Math.round(f.transactionsPerHolder) + '，建议核查成交结构',
      'Trades-to-holder-address ratio ' + Math.round(f.transactionsPerHolder) + '; review trade activity');
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
  return String(trade.provider ?? trade.source ?? trade.venue ?? tr('来源待核验', 'Source unverified'));
}
function deltaNote(key) {
  const d = scopeMetrics.value.deltas?.[key];
  return d == null ? '' : (d >= 0 ? '+' : '') + d + ' ' + tr('较 24h 前', 'vs 24h ago');
}
function stockName(stock) {
  return lang.lang === 'en' ? stock?.stockIdentity?.nameEn ?? stock?.stockIdentity?.nameZh ?? ''
    : stock?.stockIdentity?.nameZh ?? stock?.stockIdentity?.nameEn ?? '';
}
function relatedTickers(asset) { return [...new Set(eligibleRelations.value.filter(r => relationMatchesAsset(r,asset)).map(r => r.ticker).filter(Boolean))].slice(0,3).join(' · '); }
function clockTime(value) { return value ? new Date(value).toLocaleTimeString(lang.lang === 'en' ? 'en-US' : 'zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}) : '—'; }
function onFeedScroll() { feed.setPaused((feedScroll.value?.scrollTop ?? 0) > 60); }
function jumpToNew() { feed.setPaused(false); feedScroll.value?.scrollTo({top:0,behavior:'smooth'}); }
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
