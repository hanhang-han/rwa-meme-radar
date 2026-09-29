<template>
  <div v-if="data">
    <div class="page-heading">
      <RouterLink :to="backLink">← {{ backLabel }}</RouterLink>
      <h2>{{ asset.symbol }} <span v-if="asset.kind === 'stock'" class="x-badge">{{ asset.issuerIdentity?.verificationStatus === 'official' ? tr('身份已核验','Identity verified') : asset.issuerIdentity?.verificationStatus === 'legacy' ? tr('旧版官方部署','Legacy official deployment') : tr('身份未核验','Unverified identity') }}</span></h2>
      <p>{{ asset.name }} · {{ chainName(asset) }} · {{ tr('首次发现', 'First seen') }} {{ date(asset.firstSeen) }}</p>
    </div>

    <section class="panel">
      <div class="address-row">
        <code>{{ asset.token }}</code>
        <button :data-copy="asset.token" @click="copyAddress">{{ tr('复制合约地址', 'Copy contract') }}</button>
        <a :href="explorer(asset.token, 'address', chain(asset))" target="_blank" rel="noopener">{{ tr('区块浏览器', 'Block explorer') }} ↗</a>
        <a :href="okxUrl" target="_blank" rel="noopener">{{ tr('在 OKX 查看资产', 'View asset on OKX') }} ↗</a>
      </div>
      <p>{{ tr('行情来源', 'Quote source') }} {{ asset.provider ?? 'OKX' }} · {{ age(asset.quoteAt ?? asset.fieldTimes?.price ?? asset.updatedAt) }}{{ asset.error ? ' · ' + tr('部分采集失败，历史保留', 'Partial collection failure; history retained') : '' }}</p>
    </section>

    <SpreadPanel v-if="asset.kind === 'stock'" :chain="String(asset.chainId)" :token="asset.token" />
    <section class="panel x-verdict">
      <h2>{{ verdictTitle }}</h2>
      <div v-if="relations.length">
        <article v-for="r in relations" :key="r.id" class="x-proof">
          <div class="panel-head">
            <h3>{{ r.ticker }} <RelationBadge :relation="r" /></h3>
            <span class="x-badge" :class="{ verified: relationStockIdentityStatus(r) === 'official' }">{{ relationStockIdentityStatus(r) === 'official' ? tr('股票侧身份已核验','Stock identity verified') : relationStockIdentityStatus(r) === 'legacy' ? tr('股票侧为旧版官方部署','Stock side is a legacy official deployment') : tr('股票侧身份未核验','Stock identity unverified') }} · {{ age(r.checkedAt) }}</span>
          </div>
          <p>
            {{ tr('代币', 'Token') }} <RouterLink :to="detailLink({ token: r.token, chainId: r.chainId ?? '196' })">{{ short(r.token) }}</RouterLink>
            <template v-if="r.pool"> → {{ tr('池', 'Pool') }} <a :href="explorer(r.pool, 'address', r.chainId ?? '196')" target="_blank" rel="noopener">{{ short(r.pool) }}</a></template>
            <template v-if="r.stockSide"> → {{ tr('股票侧', 'Stock side') }} <a :href="explorer(r.stockSide, 'address', r.chainId ?? '196')" target="_blank" rel="noopener">{{ short(r.stockSide) }}</a></template>
          </p>
          <div class="x-coverage">
            <span>{{ r.protocol }}</span>
            <span>{{ tr('池总流动性', 'Total pool liquidity') }} {{ usd(r.liquidityUsd) }}</span>
            <span>{{ tr('核验区块', 'Verified block') }} {{ num(r.block) }}</span>
          </div>
          <RegistryBadge v-if="r.pool" :relation="r" />
        </article>
      </div>
      <div v-else class="x-empty">{{ tr('尚未完成配对池核验。', 'Pair-pool verification is not complete yet.') }}</div>
    </section>

    <div class="kpis">
      <div class="kpi" data-kpi-key="price">
        <span class="kpi-label">{{ tr('最新价格', 'Latest price') }}</span>
        <strong class="kpi-value mono" :class="{ 'kpi-flash': priceFlash }"><LiveNumber :value="asset.price" :currency="asset.priceCurrency" format="price" /></strong>
        <span class="kpi-note">{{ pct(asset.change24h) }} · 24h</span><QuoteStatus :row="asset" />
      </div>
      <div class="kpi"><span class="kpi-label">{{ tr('24h 成交额', '24h volume') }}</span><strong class="kpi-value mono"><LiveNumber :value="asset.volume24h" :currency="asset.volumeCurrency ?? asset.priceCurrency" /></strong><span class="kpi-note">{{ volumeScopeLabel }}</span></div>
      <div class="kpi"><span class="kpi-label">{{ tr('24h 交易', '24h trades') }}</span><strong class="kpi-value mono">{{ num(asset.priceScope === 'exchange' ? asset.exchangeTrades24h : asset.txs24h) }}</strong><span class="kpi-note">{{ asset.priceScope === 'exchange' ? tr('交易所成交总数；未提供买卖方向', 'Exchange trades; buy/sell split unavailable') : `${num(asset.buys24h)} / ${num(asset.sells24h)}` }}</span></div>
      <div class="kpi"><span class="kpi-label">{{ tr('持币地址数', 'Holder addresses') }}</span><strong class="kpi-value mono"><LiveNumber :value="asset.holders" format="number" /></strong><span class="kpi-note">{{ age(asset.fieldTimes?.holders) }}</span></div>
      <div class="kpi"><span class="kpi-label">{{ tr('总流动性','Total liquidity') }}</span><strong class="kpi-value mono"><LiveNumber :value="asset.totalLiquidityUsd" /></strong><span class="kpi-note">{{ asset.totalLiquidityUsd == null ? tr('总量待核实','Total pending verification') : age(asset.fieldTimes?.totalLiquidityUsd ?? asset.totalLiquidityAt) }}</span></div>
    </div>

    <div class="x-grid">
      <section class="panel">
        <div class="panel-head">
          <h2>{{ tr('价格走势', 'Price history') }}<template v-if="selectedPoolId"> · {{ tr('池内成交价', 'Pool trade price') }}</template></h2>
          <label v-if="exchangeMarkets.length || poolMarkets.length">{{ tr('图表市场', 'Chart market') }} <select data-chart-market :value="marketSelection.id" @change="marketChoice=$event.target.value">
            <option v-if="baseAsset.priceScope==='exchange'" value="base">{{ baseAsset.provider ?? baseAsset.venue }} · {{ baseAsset.marketId ?? baseAsset.symbol }} · {{ baseAsset.priceCurrency }}</option>
            <option v-else value="dex">{{ tr('DEX 聚合行情', 'Aggregated DEX') }}</option>
            <option v-for="market in exchangeMarkets" :key="`${market.venue}:${market.marketId}`" :value="`${market.venue}:${market.marketId}`">{{ market.provider ?? market.venue }} · {{ market.marketId }} · {{ market.priceCurrency }}</option>
            <option v-for="pool in poolMarkets" :key="pool.poolId ?? pool.marketId" :value="`pool:${pool.poolId ?? pool.pool ?? pool.marketId}`">{{ tr('链上池', 'On-chain pool') }} {{ short(pool.poolId ?? pool.pool ?? pool.marketId) }} · {{ pool.priceCurrency }}</option>
          </select></label>
        </div>
        <p v-if="selectedPoolId" class="hint" data-pool-unit>{{ tr('每枚代币以', 'Each token is quoted in') }} {{ marketSelection.pool.priceCurrency }}{{ tr('计价；上方指标仍为资产整体行情。', '; metrics above remain asset-wide quotes.') }}</p>
        <CandleChart :asset="asset" :samples="data.samples ?? []" :pool="selectedPoolId" />
      </section>
      <section class="panel">
        <div class="panel-head">
          <h2>{{ tr('最新市场成交', 'Recent market trades') }}</h2>
          <span class="hint">{{ marketFreshnessLabel }}</span>
        </div>
        <p class="hint">{{ tr('仅展示已接入市场的逐笔记录；新成交到达时自动插入，不代表全部市场成交。时间为实际成交时间。', 'Per-trade records from connected markets only; new trades appear automatically. This is not all market activity. Times are trade times.') }}</p>
        <div v-if="marketTrades.length" class="scroll" data-market-trades>
          <table class="tbl">
            <thead><tr><th>{{ tr('时间', 'Time') }}</th><th>{{ tr('来源 / 市场', 'Source / market') }}</th><th>{{ tr('方向', 'Side') }}</th><th>{{ tr('成交价', 'Price') }}</th><th>{{ tr('成交额', 'Quote amount') }}</th></tr></thead>
            <tbody>
              <tr v-for="t in marketTrades" :key="marketTradeKey(t)" :class="{ 'trade-new': newMarketIds.has(marketTradeKey(t)) }">
                <td>{{ date(t.t) }}</td>
                <td>{{ t.source ?? t.venue ?? tr('来源待核实', 'Source unverified') }}<small>{{ t.marketId ?? tr('市场待核实', 'Market unverified') }}</small></td>
                <td :class="t.type==='buy'?'up':t.type==='sell'?'down':''">{{ t.type==='buy'?tr('买入','Buy'):t.type==='sell'?tr('卖出','Sell'):tr('未提供','Unknown') }}</td>
                <td>{{ money(t.price,t.priceCurrency) }}</td>
                <td>{{ money(t.quoteQuantity,t.volumeCurrency??t.priceCurrency) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="x-empty">{{ tr('当前没有已接入市场的逐笔成交记录；不代表没有交易。', 'No per-trade records from connected markets yet; this does not mean there were no trades.') }}</div>
      </section>
    </div>

    <section class="panel">
      <div class="panel-head">
        <h2>{{ tr('链上成交采集记录', 'Collected on-chain trades') }}</h2>
        <span class="hint">{{ tr('按后台采集和历史回补进度更新', 'Updated as collection and historical backfill progress') }}</span>
      </div>
      <p class="hint">{{ statText }}</p>
      <div v-if="bucketSummary.length" class="v3-bucket-strip">
        <div v-for="bucket in bucketSummary" :key="bucket.bar">
          <span>{{ bucket.bar }} {{ tr('真实成交桶', 'observed bucket') }}</span>
          <strong>{{ usd(bucket.volumeUsd) }}</strong>
          <small>{{ num(bucket.tradeCount) }} {{ tr('笔', 'trades') }} · {{ bucket.complete ? tr('覆盖完整', 'complete') : `${coveragePct(bucket.coverageRatio)} ${tr('覆盖', 'covered')}` }} · {{ age(bucket.closeTime) }}</small>
        </div>
      </div>
      <div v-if="trades.length" class="scroll">
        <table class="tbl">
          <thead><tr><th>{{ tr('时间', 'Time') }}</th><th>{{ tr('方向', 'Side') }}</th><th>{{ tr('美元成交额', 'USD volume') }}</th><th>DEX</th><th>{{ tr('交易', 'Transaction') }}</th></tr></thead>
          <tbody id="v2TradeBody">
            <tr v-for="t in trades" :key="t.id" :class="{ 'trade-new': newIds.has(t.id) }">
              <td>{{ date(t.t) }}</td>
              <td :class="t.type === 'buy' ? 'up' : 'down'">{{ t.type === 'buy' ? tr('买入', 'Buy') : tr('卖出', 'Sell') }}</td>
              <td>{{ usd(t.volume) }}</td>
              <td>{{ t.dex }}</td>
              <td><a v-if="t.hash" :href="explorer(t.hash, 'tx', chain(asset))" target="_blank" rel="noopener">{{ short(t.hash) }} ↗</a><template v-else>{{ tr('未提供哈希', 'Hash unavailable') }}</template></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="x-empty">{{ tr('当前保存范围内暂无成交记录。', 'No trades in the saved coverage.') }}</div>
    </section>

    <section class="panel">
      <h2>{{ tr('前10大地址持仓占比', 'Top-10 address share') }}</h2>
      <template v-if="asset.risk?.top10 != null">
        <strong class="kpi-value">{{ num(asset.risk.top10) }}%</strong>
        <progress max="100" :value="Math.max(0, Math.min(100, asset.risk.top10))"></progress>
        <p>{{ date(asset.risk.checkedAt) }} · {{ asset.risk.provider ?? asset.fieldSources?.holderTop10 ?? tr('来源待核实', 'Source unverified') }}</p>
      </template>
      <div v-else class="x-empty">{{ tr('上游尚未提供集中度数据。', 'Holder concentration is not available from the source yet.') }}</div>
    </section>

    <section class="panel x-ai">
      <h2>{{ tr('数据解读', 'Data readout') }}</h2>
      <p class="hint">{{ tr('由 DeepSeek 基于本站已采集字段生成，只解读已有数据，不预测价格。', 'Generated by DeepSeek from collected fields only; no price forecasts.') }}</p>
      <div class="v2-ai-text">{{ insight ?? insightPlaceholder }}</div>
    </section>

    <div class="x-grid">
      <section class="panel">
        <h2>{{ tr('风险信息', 'Risk information') }}</h2>
        <template v-if="riskFlags(asset).length"><p v-for="flag in riskFlags(asset)" :key="flag">{{ riskLabel(flag) }}</p></template>
        <p v-else>{{ asset.riskStatus === 'clear' ? tr('当前规则未触发，不代表无风险。','No current rules triggered; this is not a safety guarantee.') : tr('未检测或数据不足。','Not checked or insufficient data.') }}</p>
      </section>
      <section class="panel">
        <h2>{{ tr('关系时间线', 'Relationship timeline') }}</h2>
        <EventRow v-for="ev in events" :key="ev.id" :ev="ev" />
        <p v-if="!events.length">{{ tr('等待首条关系核验事件。', 'Waiting for the first relationship-verification event.') }}</p>
      </section>
    </div>
  </div>
  <section v-else-if="error" class="panel x-empty">{{ error }}</section>
  <section v-else class="panel x-empty">{{ tr('正在读取已保存的数据…', 'Loading saved data…') }}</section>
</template>

<script setup>
import LiveNumber from '../components/LiveNumber.vue';
import QuoteStatus from '../components/QuoteStatus.vue';
import SpreadPanel from '../components/SpreadPanel.vue';
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import CandleChart from '../components/CandleChart.vue';
import RegistryBadge from '../components/RegistryBadge.vue';
import RelationBadge from '../components/RelationBadge.vue';
import EventRow from '../components/EventRow.vue';
import { useDetailStore } from '../stores/detail';
import { useDashboardStore } from '../stores/dashboard';
import { getInsight } from '../api/client';
import { tr, useI18n } from '../i18n';
import { age, chain, chainName, date, detailLink, explorer, num, pct, short, usd, money } from '../utils/format';
import { applyQuote } from '../utils/realtime';
import { selectDetailMarket } from '../utils/market-selection';
import { RISK_LABELS, riskFlags, relationStockIdentityStatus } from '../utils/product-labels';

const props = defineProps({
  chain: { type: String, required: true },
  address: { type: String, required: true },
});

const route = useRoute();
const router = useRouter();
const detail = useDetailStore();
const dash=useDashboardStore();
const data = ref(null);
const error = ref(null);
const insight = ref(null);
const insightStatus = ref('queued');
const { lang } = useI18n();
const riskLabel = flag => tr(...RISK_LABELS[flag]);
const newIds = reactive(new Set());
const newMarketIds = reactive(new Set());
const knownMarketIds = new Set();
const marketFlashTimers = new Set();
let marketRowsPrimed = false;
const marketClock = ref(Date.now());
const priceFlash = ref(false);
const knownTradeIds = new Set();
// Raw numeric SSE values: assignment compares numbers (formatted strings
// discard real micro-moves); the flash still keys off the displayed text.
let lastPriceNum = null;
let lastPriceText = '';
let lastStockSse = null;
let lastSse = null; // { price, at } — a poll older than this never wins
let insightRequest = 0;

const marketChoice=ref(null);
const baseAsset=computed(()=>data.value?.asset??{});
const exchangeMarkets=computed(()=>baseAsset.value.exchangeMarkets??[]);
const poolMarkets=computed(()=>data.value?.poolMarkets??baseAsset.value.poolMarkets??[]);
const marketSelection=computed(()=>selectDetailMarket(baseAsset.value,exchangeMarkets.value,poolMarkets.value,marketChoice.value));
const selectedPoolId=computed(()=>marketSelection.value.kind==='pool'?String(marketSelection.value.pool.poolId??marketSelection.value.pool.pool??marketSelection.value.pool.marketId):'');
const activeMarket=computed(()=>marketSelection.value.kind==='exchange'?marketSelection.value.exchange:null);
const asset = computed(() => {
  const base=baseAsset.value,market=activeMarket.value;if(!market)return base;
  return {...base,...market,token:base.token,chainId:base.chainId,priceScope:'exchange',volumeScope:'exchange',quoteType:'exchange-token',txs24h:market.exchangeTrades24h??null,buys24h:null,sells24h:null,
    fieldTimes:{...base.fieldTimes,price:market.quoteAt,volume24h:market.quoteAt,change24h:market.quoteAt,txs24h:market.quoteAt},fieldSources:{...base.fieldSources,price:market.provider??market.venue},priceProvenance:{timeKind:'market',venue:market.venue,marketAt:market.sourceEventAt??market.quoteAt},quoteStatus:'live'};
});
const relations = computed(() => data.value?.relations ?? []);
const trades = computed(() => data.value?.trades ?? []);
const marketTrades = computed(() => data.value?.marketTrades ?? []);
const marketTradeKey = t => `${t.venue ?? ''}:${t.marketId ?? ''}:${t.id}`;
const marketFreshnessLabel = computed(() => {
  marketClock.value;
  const latest = marketTrades.value[0];
  return latest?.t
    ? `${tr('最近成交', 'Latest trade')} ${age(latest.t)}`
    : tr('暂无逐笔记录', 'No per-trade records');
});
const events = computed(() => data.value?.events ?? []);
const bucketSummary = computed(() => ['1m', '5m', '1h'].flatMap((bar) => {
  const rows = data.value?.tradeBuckets?.[bar] ?? [];
  const row = [...rows].reverse().find((item) => item.closeTime <= Date.now());
  return row ? [{ ...row, bar }] : [];
}));
function coveragePct(value) {
  const n = Math.max(0, Math.min(1, Number(value) || 0)) * 100;
  return `${n < 10 && n > 0 ? n.toFixed(1) : Math.round(n)}%`;
}
const backLink = computed(() => (route.query.from === 'stock' ? '/stock' : '/meme'));
const backLabel = computed(() => (route.query.from === 'stock' ? tr('股票', 'Stock') : tr('Meme', 'Meme')) + ' Radar');
const verdictTitle = computed(() =>
  relations.value.some((r) => relationStockIdentityStatus(r) === 'official')
    ? tr('官方股票代币池配对', 'Approved stock-token pair')
    : relations.value.some((r) => r.status === 'verified')
      ? tr('池结构已核验 · 股票身份待核验', 'Pool structure verified · stock identity pending')
    : asset.value.kind === 'stock'
      ? tr('已收录股票代币', 'Stock token catalogued')
      : tr('关系待核实', 'Relationship pending verification'),
);
const statText = computed(() =>
  asset.value.tradeAt
    ? tr('此处仅展示链上交易池成交；可能存在分页缺口，不包含交易所成交。', 'Only on-chain pool trades are shown here; pagination gaps may exist. Exchange trades are not included.')
    : tr('成交采集排队中，缺失不表示零成交。', 'Trade collection is queued; missing data does not mean zero trades.'),
);
const insightPlaceholder = computed(() => {
  if (insightStatus.value === 'disabled') return tr('AI 解读暂未启用。', 'AI readouts are not enabled.');
  if (insightStatus.value === 'upstream_failed') return tr('解读生成失败，后台稍后会重试。', 'Readout generation failed; the worker will retry.');
  return tr('解读已排队，生成后会自动显示。', 'Readout queued and will appear automatically.');
});
const volumeScopeLabel = computed(() => {
  if (asset.value.volumeScope === 'exchange' || asset.value.priceScope === 'exchange') {
    return asset.value.provider ?? asset.value.venue ?? tr('交易所', 'Exchange');
  }
  return asset.value.provider ?? asset.value.venue ?? 'DEX';
});
watch(() => marketTrades.value.map(marketTradeKey), keys => {
  if (!marketRowsPrimed) {
    keys.forEach(key => knownMarketIds.add(key));
    marketRowsPrimed = true;
    return;
  }
  for (const key of keys) {
    if (knownMarketIds.has(key)) continue;
    knownMarketIds.add(key);
    newMarketIds.add(key);
    const timer = setTimeout(() => {
      newMarketIds.delete(key);
      marketFlashTimers.delete(timer);
    }, 2000);
    marketFlashTimers.add(timer);
  }
  while (knownMarketIds.size > 1000) knownMarketIds.delete(knownMarketIds.values().next().value);
});

const okxUrl = computed(() => {
  const cid = chain(asset.value);
  const host = { 196: 'xlayer', 56: 'bsc', 4663: 'robinhood' }[cid] ?? 'xlayer';
  return `https://www.okx.com/web3/detail?chain=${host}&address=${encodeURIComponent(asset.value.token ?? '')}`;
});
function flashPrice() {
  priceFlash.value = false;
  requestAnimationFrame(() => (priceFlash.value = true));
  setTimeout(() => (priceFlash.value = false), 1200);
}
let disposed = false;
async function load(force = false) {
  try {
    const d = await detail.fetch(props.chain, props.address.toLowerCase(), { force });
    if (disposed) return;
    if (lastStockSse && d.stock && (d.stock.quoteAt ?? 0) < lastStockSse.marketAt) {
      applyQuote(d.stock, lastStockSse);
    }
    if (d.stock && d.asset && d.stock.price != null && d.asset.priceScope === d.stock.priceScope) applyQuote(d.asset, d.stock);
    if (data.value?.trades?.length) {
      const merged = new Map((d.trades ?? []).map(t => [t.id, t]));
      for (const t of data.value.trades) if (!merged.has(t.id)) merged.set(t.id, t);
      d.trades = [...merged.values()].sort((a,b) => b.t-a.t).slice(0,150);
    }
    data.value = d;
    detail.watch(props.chain, props.address.toLowerCase());
    error.value = null;
    // A full snapshot may be older than the last SSE tick: the newer value wins.
    if (d.asset && !d.stock && lastSse) applyQuote(data.value.asset, lastSse);
    const priceText = money(d.asset?.price, d.asset?.priceCurrency);
    if (lastPriceText && priceText !== lastPriceText) flashPrice();
    lastPriceText = priceText;
    lastPriceNum = d.asset?.price ?? null;
    for (const t of d.trades ?? []) knownTradeIds.add(t.id);
    loadInsight();
  } catch (e) {
    error.value = tr('该资产尚未进入可用索引，或暂时读取失败。', 'This asset is not yet indexed or could not be loaded.');
  }
}

async function loadInsight() {
  const request = ++insightRequest;
  const language = lang.lang;
  try {
    const r = await getInsight(props.chain, props.address, language);
    if (disposed || request !== insightRequest || language !== lang.lang) return;
    insightStatus.value = r?.status ?? r?.reason ?? 'queued';
    insight.value = r?.text ?? null;
  } catch {
    if (request !== insightRequest || language !== lang.lang) return;
    insightStatus.value = 'upstream_failed';
  }
}

function copyAddress() {
  navigator.clipboard?.writeText(asset.value.token ?? '');
}

function onSsePrice(e) {
  const d = e.detail;
  if (data.value?.stock && asset.value.priceScope !== 'dex') return;
  if (!asset.value?.token || asset.value.token !== d.token || String(d.chainId ?? '196') !== props.chain) return;
  const knownAt = Math.max(lastSse?.at ?? 0, asset.value.fieldTimes?.price ?? asset.value.quoteAt ?? 0);
  if (knownAt > (d.at ?? 0)) return;
  const previousPrice = asset.value.price;
  if (!applyQuote(asset.value, d)) return;
  if (data.value?.stock) applyQuote(data.value.stock, d);
  lastSse = { ...d };
  if (d.price !== lastPriceNum || previousPrice !== asset.value.price) {
    lastPriceNum = d.price;
    const text = money(d.price, asset.value.priceCurrency);
    if (text !== lastPriceText) {
      lastPriceText = text;
      flashPrice();
    }
  }
}

function onSseStockQuote(e) {
  const d = e.detail;
  if (!data.value?.stock) return;
  const addr = data.value.stock.tokenContractAddress?.toLowerCase();
  if (addr !== d.token || String(d.chainId ?? '196') !== props.chain) return;
  if ((lastStockSse?.marketAt ?? data.value.stock.quoteAt ?? 0) > d.marketAt) return;
  lastStockSse = d;
  if (d.price != null) {
    if (asset.value.price !== d.price) flashPrice();
    asset.value.price = d.price;
    asset.value.fieldTimes = {...asset.value.fieldTimes, price: d.marketAt};
    data.value.stock.price = d.price;
    data.value.stock.quoteAt = d.marketAt;
  }
  if (d.change24h != null) data.value.stock.change24h = d.change24h;
  if (d.volume24h != null) data.value.stock.volume24h = d.volume24h;
  if (d.change24h != null) asset.value.change24h = d.change24h;
  if (d.volume24h != null) asset.value.volume24h = d.volume24h;
  applyQuote(asset.value, d);
  applyQuote(data.value.stock, d);
}

function onSseTrades(e) {
  const d = e.detail;
  if (!asset.value?.token || asset.value.token !== d.token || String(d.chainId ?? '196') !== props.chain) return;
  for (const t of d.fresh ?? []) {
    if (knownTradeIds.has(t.id)) continue;
    knownTradeIds.add(t.id);
    data.value.trades.unshift(t);
    newIds.add(t.id);
    setTimeout(() => newIds.delete(t.id), 2000);
  }
  data.value.trades.sort((a,b) => b.t-a.t);
  if (data.value.trades.length > 150) data.value.trades.length = 150;
}

let pollTimer;
let insightTimer;
let marketClockTimer;
let releaseActive;
let lastDetailLoad=0;
function onResourceChange(event){
  const r=event.detail??{};
  if(r.chainId&&String(r.chainId)!==props.chain||r.token&&String(r.token).toLowerCase()!==props.address.toLowerCase())return;
  if(r.kind==='insight'||r.kind==='all')loadInsight();
}
watch(() => asset.value.price, (value,old)=>{if(value!=null&&old!=null&&value!==old)flashPrice();});
onMounted(() => {
  releaseActive=detail.activate(props.chain,props.address);
  window.addEventListener('resource-change',onResourceChange);
  load();
  pollTimer = setInterval(() => { if (!document.hidden && (!dash.hasProjectionStream || Date.now()-lastDetailLoad>60000)) {lastDetailLoad=Date.now();load(true);} }, 20000);
  insightTimer = setInterval(() => {if(!dash.hasProjectionStream&&!document.hidden)loadInsight();}, 15000);
  marketClockTimer = setInterval(() => (marketClock.value = Date.now()), 30000);
  window.addEventListener('manual-refresh', onManualRefresh);
  window.addEventListener('sse-price', onSsePrice);
  window.addEventListener('sse-stock-quote', onSseStockQuote);
  window.addEventListener('sse-trades', onSseTrades);
});
onBeforeUnmount(() => {
  disposed = true;
  releaseActive?.();
  window.removeEventListener('resource-change',onResourceChange);
  clearInterval(pollTimer);
  clearInterval(insightTimer);
  clearInterval(marketClockTimer);
  for (const timer of marketFlashTimers) clearTimeout(timer);
  detail.unwatch();
  window.removeEventListener('sse-price', onSsePrice);
  window.removeEventListener('sse-stock-quote', onSseStockQuote);
  window.removeEventListener('sse-trades', onSseTrades);
  window.removeEventListener('manual-refresh', onManualRefresh);
});
function onManualRefresh() { load(true); loadInsight(); }
watch(() => lang.lang, () => {
  insight.value = null;
  loadInsight();
});
</script>
