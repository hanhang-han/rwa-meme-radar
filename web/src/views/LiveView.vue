<template>
  <div>
    <div class="hero x-hero">
      <div class="v2-hero-copy">
        <h2>{{ tr('市场概览', 'Market overview') }}</h2>
        <p>{{ tr('股票主题、关联 Meme 与链上成交', 'Stock themes, related memes and onchain trades') }}</p>
      </div>
    </div>

    <div class="kpis">
      <KpiCard kpi-key="actionableAssets" :title="tr('活跃 Meme', 'Active memes')" :value="num(scopeMetrics.active)" :note="activeNote" href="#/meme" />
      <KpiCard kpi-key="verifiedPools" :title="tr('股票配对池', 'Stock pairs')" :value="num(scopeMetrics.pools)" :note="deltaNote('pools') || tr('已核验配对池', 'Verified pair pools')" href="#/pair" />
      <KpiCard kpi-key="pairedLiquidityUsd" :title="tr('配对池总流动性', 'Pair liquidity')" :value="usd(scopeMetrics.liquidity)" :note="deltaNote('liquidity') || tr('仅统计股票配对池', 'Stock pair pools only')" href="#/pair" />
      <KpiCard kpi-key="newRelations24h" :title="tr('24h 新增配对', 'New pairs (24h)')" :value="num(scopeMetrics.newPairs)" :note="scopeMetrics.newPairs == null ? tr('创建时间未知','Creation time unknown') : deltaNote('newPairs')" href="#/events" />
    </div>

    <ThemeMarketMap :theme-map="themeMap" :name-clues="nameClues" :name-clues-loading="!fullSnapshotReady" :stock-tokens="store.stockTokens" :scope="scope" :loading="!themeMapReady" />
    <ImportantChanges :changes="importantChanges" :observations="feed.relationships" :unified="store.snapshot?.unified" :assets="store.assets" :scope="scope" :loading="!importantChangesReady" />

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
      <p v-if="briefing && (briefing.stale || briefingError)" class="hint">{{ tr('上次简报 · 更新暂不可用', 'Previous briefing · update unavailable') }}</p>
    </section>

    <section class="panel">
      <div class="panel-head"><h2>{{ tr('热门股票', 'Trending stocks') }}</h2><RouterLink to="/stock">{{ tr('全部', 'All') }} →</RouterLink></div>
      <div v-if="hotStocks.length" class="home-stock-grid">
        <RouterLink v-for="card in hotStocks" :key="card.ticker" class="home-stock-card" :to="{path:'/stock',query:{chain:scope,q:card.ticker}}">
          <strong>{{ card.ticker }} <small>{{ stockName(card.stock) }}</small></strong>
          <span><b>{{ price(card.stock?.price,card.stock?.priceCurrency) }}</b><b :class="Number(card.stock?.change24h)>0?'up':Number(card.stock?.change24h)<0?'down':''">{{ pct(card.stock?.change24h) }}</b></span>
          <small>{{ num(card.memes.size) }} Meme · {{ tr('关联 Meme 成交', 'Related meme volume') }} {{ usd(card.volume) }} · {{ chainName(card.stock) }} {{ card.stock?.fieldSources?.price ?? card.stock?.provider ?? tr('来源待核验', 'Source unverified') }}</small>
          <QuoteStatus :row="card.stock" />
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
            <small :title="tradeWalletTitle(t)">{{ tradeWalletLabel(t) }}</small>
            <a v-if="t.hash" :href="explorer(t.hash,'tx',String(t.chainId))" target="_blank" rel="noopener" :aria-label="tr('查看交易', 'View transaction')">↗</a>
          </div>
          <div v-if="!displayedTrades.length" class="x-empty">{{ tr('当前条件下暂无成交。', 'No trades match these filters.') }}</div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>{{ tr('新收录关联', 'Recent discoveries') }}</h2><RouterLink to="/events">{{ tr('全部', 'All') }} →</RouterLink></div>
        <p class="hint">{{ tr('按首次收录时间排列', 'Ordered by first indexing time') }}</p>
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
          <span>{{ relatedTickers(asset) }}</span><span v-if="asset.riskFlags?.length" class="risk-mark">!</span><b :title="assetVolumeTitle(asset)">{{ money(asset.volume24h,asset.volumeCurrency ?? asset.priceCurrency) }}<small v-if="asset.volume24h != null && assetVolumeState(asset) !== 'current'"> · {{ assetVolumeState(asset) === 'historical' ? tr('历史值', 'Historical') : tr('时间待核实', 'Time unverified') }}</small></b>
        </RouterLink>
      </div>
      <div v-else class="x-empty">{{ tr('当前暂无合格关联资产。', 'No qualifying related memes.') }}</div>
    </section>

    <section v-if="scope === 'all'" class="panel">
      <h2>{{ tr('链上分布', 'Chain distribution') }}</h2>
      <RouterLink v-for="d in distribution" :key="d.chainId" class="v2-distribution" :to="{ path: '/meme', query: { chain: d.chainId } }">
        <strong>{{ d.name }}</strong>
        <span>{{ num(d.assets) }} {{ tr('个 Meme', 'memes') }}<span class="chain-share" aria-hidden="true"><i :style="{width: distributionMax ? (Number(d.assets || 0) / distributionMax * 100) + '%' : '0%'}"></i></span></span>
        <span>{{ tr('24h 成交', '24h volume') }} {{ usd(d.volume?.value) }}<small> · {{ tr('近期数据', 'Current data') }} {{ num(d.volume?.known) }}/{{ num(d.volume?.total) }}</small></span>
        <span>{{ tr('池流动性', 'Pool liquidity') }} {{ usd(d.liquidity?.value) }}<small> · {{ tr('数据覆盖', 'Coverage') }} {{ num(d.liquidity?.known) }}/{{ num(d.liquidity?.total) }}</small></span>
      </RouterLink>
    </section>

  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import KpiCard from '../components/KpiCard.vue';
import QuoteStatus from '../components/QuoteStatus.vue';
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
const scopeMetrics = computed(() => metricsForScope(store.snapshot?.unified, scope.value));
const activeNote = computed(() => scopeMetrics.value.active === 0
  ? tr('暂无同时满足报价与流动性条件的资产', 'No assets meet both quote and liquidity criteria')
  : deltaNote('active') || tr('报价与总流动性达标', 'Meets quote and total liquidity criteria'));
const hotStocks = computed(() => topStockCards(store.stockTokens, store.relations, store.assets, scope.value));
const minTrade = ref(100);
const direction = ref('all');
const displayedTrades = computed(() => filteredTrades(feed.trades, scope.value, minTrade.value, direction.value));
const feedScroll = ref(null);
const distribution = computed(() => store.snapshot?.unified?.distribution ?? []);
const distributionMax = computed(() => Math.max(0, ...distribution.value.map(row => Number(row.assets) || 0)));
const eligibleRelations = computed(() => store.relations.filter(r => r.level === 'A'));
const related = computed(() => store.assets.filter(a => a.kind === 'candidate' && inChainScope(a,scope.value) && eligibleRelations.value.some(r => relationMatchesAsset(r,a)))
  .sort((a,b) => Number(assetVolumeState(b)==='current')-Number(assetVolumeState(a)==='current') || Number(b.volume24h ?? -1) - Number(a.volume24h ?? -1)).slice(0,10));
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
}).filter(Boolean).sort((a,b)=>Number(b.at??0)-Number(a.at??0)).slice(0,8));

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
  if (briefingStatus.value === 'disabled') return tr('简报暂不可用。', 'Briefing unavailable.');
  if (briefingStatus.value === 'upstream_failed') return tr('简报暂不可用。', 'Briefing unavailable.');
  return tr('暂无简报。', 'No briefing yet.');
});
function briefingMessageFor(item) {
  const f = item.fields ?? {};
  const observedLiquidity = f.totalLiquidityUsd == null
    ? tr('总流动性待核验', 'total liquidity unverified')
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
  return String(trade.provider ?? trade.source ?? trade.venue ?? tr('来源待核验', 'Source unverified'));
}
function tradeWalletLabel(trade) {
  if (trade.wallet) return short(trade.wallet);
  return ['binance', 'binance-alpha'].includes(String(trade.venue ?? '').toLowerCase())
    ? tr('无链上钱包', 'No on-chain wallet') : tr('钱包未提供', 'Wallet unavailable');
}
function tradeWalletTitle(trade) {
  if (trade.wallet) return String(trade.wallet);
  return ['binance', 'binance-alpha'].includes(String(trade.venue ?? '').toLowerCase())
    ? tr('交易所成交不提供链上交易钱包', 'Exchange trades do not provide an on-chain trade wallet')
    : tr('此来源未提供交易钱包', 'This source did not provide a trade wallet');
}
function assetVolumeState(asset) {
  if (asset.volume24h == null) return 'missing';
  const at = Number(asset.fieldTimes?.volume24h ?? asset.volumeAt);
  if (!Number.isFinite(at) || at <= 0 || at > Date.now()+1000) return 'unknown';
  return Date.now() - at <= 900000 ? 'current' : 'historical';
}
function assetVolumeTitle(asset) {
  const at = asset.fieldTimes?.volume24h ?? asset.volumeAt;
  return `${asset.fieldSources?.volume24h ?? asset.provider ?? tr('来源待核验', 'Source unverified')} · ${date(at)}`;
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

<style scoped>
.radar-workspace .x-hero { padding: 10px 0 18px; }
.chain-share { display:block;height:4px;margin-top:9px;background:var(--surface-raised);border-radius:2px;overflow:hidden; }
.chain-share i { display:block;height:100%;background:var(--accent);opacity:.7; }
</style>
