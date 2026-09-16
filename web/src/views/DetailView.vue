<template>
  <div v-if="data">
    <div class="page-heading">
      <RouterLink :to="backLink">← {{ backLabel }}</RouterLink>
      <h2>{{ asset.symbol }} <span class="x-badge">{{ tr('持续追踪', 'Tracked') }}</span></h2>
      <p>{{ asset.name }} · {{ chainName(asset) }} · {{ tr('首次发现', 'First seen') }} {{ date(asset.firstSeen) }}</p>
    </div>

    <section class="panel">
      <div class="address-row">
        <code>{{ asset.token }}</code>
        <button :data-copy="asset.token" @click="copyAddress">{{ tr('复制合约地址', 'Copy contract') }}</button>
        <a :href="explorer(asset.token, 'address', chain(asset))" target="_blank" rel="noopener">{{ tr('区块浏览器', 'Block explorer') }} ↗</a>
        <a :href="okxUrl" target="_blank" rel="noopener">{{ tr('在 OKX 查看资产', 'View asset on OKX') }} ↗</a>
      </div>
      <p>{{ tr('来源', 'Source') }} {{ asset.provider ?? 'OKX' }} · {{ age(asset.updatedAt) }}{{ asset.error ? ' · ' + tr('部分采集失败，历史保留', 'Partial collection failure; history retained') : '' }}</p>
    </section>

    <section class="panel x-verdict">
      <h2>{{ verdictTitle }}</h2>
      <p>{{ tr('依据同链双方合约与股票目录核验。配对不代表官方关系或安全认证。', 'Verified against both contracts and a same-chain stock catalogue. Pairing does not certify affiliation or safety.') }}</p>
      <div v-if="relations.length">
        <article v-for="r in relations" :key="r.id" class="x-proof">
          <div class="panel-head">
            <h3>{{ r.ticker }} · {{ r.wrapper ? tr('包装股票配对', 'Wrapped stock pair') : tr('直接股票配对', 'Direct pair') }}</h3>
            <span class="x-badge" :class="{ verified: r.status === 'verified' }">{{ r.status === 'verified' ? tr('地址已核验', 'Address verified') : tr('证据变化', 'Evidence changed') }} · {{ age(r.checkedAt) }}</span>
          </div>
          <p>
            {{ tr('代币', 'Token') }} <RouterLink :to="detailLink({ token: r.token, chainId: r.chainId ?? '196' })">{{ short(r.token) }}</RouterLink>
            → {{ tr('池', 'Pool') }} <a :href="explorer(r.pool, 'address', r.chainId ?? '196')" target="_blank" rel="noopener">{{ short(r.pool) }}</a>
            → {{ tr('股票侧', 'Stock side') }} <a :href="explorer(r.stockSide, 'address', r.chainId ?? '196')" target="_blank" rel="noopener">{{ short(r.stockSide) }}</a>
          </p>
          <div class="x-coverage">
            <span>{{ r.protocol }}</span>
            <span>{{ tr('池总流动性', 'Total pool liquidity') }} {{ usd(r.liquidityUsd) }}</span>
            <span>{{ tr('核验区块', 'Verified block') }} {{ num(r.block) }}</span>
          </div>
          <RegistryBadge :relation="r" />
        </article>
      </div>
      <div v-else class="x-empty">{{ tr('尚未完成配对池核验。', 'Pair-pool verification is not complete yet.') }}</div>
    </section>

    <div class="kpis">
      <div class="kpi" data-kpi-key="price">
        <span class="kpi-label">{{ tr('最新价格', 'Latest price') }}</span>
        <strong class="kpi-value mono" :class="{ 'kpi-flash': priceFlash }">{{ usd(asset.price) }}</strong>
        <span class="kpi-note">{{ pct(asset.change24h) }} · 24h</span>
      </div>
      <div class="kpi"><span class="kpi-label">{{ tr('24h 成交额', '24h volume') }}</span><strong class="kpi-value mono">{{ usd(asset.volume24h) }}</strong><span class="kpi-note">DEX</span></div>
      <div class="kpi"><span class="kpi-label">{{ tr('24h 交易', '24h trades') }}</span><strong class="kpi-value mono">{{ num(asset.txs24h) }}</strong><span class="kpi-note">{{ num(asset.buys24h) }} / {{ num(asset.sells24h) }}</span></div>
      <div class="kpi"><span class="kpi-label">{{ tr('持币地址数', 'Holder addresses') }}</span><strong class="kpi-value mono">{{ num(asset.holders) }}</strong><span class="kpi-note">{{ age(asset.fieldTimes?.holders) }}</span></div>
    </div>

    <div class="x-grid">
      <section class="panel">
        <div class="panel-head">
          <h2>{{ tr('价格走势', 'Price history') }}</h2>
        </div>
        <CandleChart :asset="asset" :samples="data.samples ?? []" />
      </section>
      <section class="panel">
        <h2>{{ tr('前10大地址持仓占比', 'Top-10 address share') }}</h2>
        <template v-if="asset.risk?.top10 != null">
          <strong class="kpi-value">{{ num(asset.risk.top10) }}%</strong>
          <progress max="100" :value="Math.max(0, Math.min(100, asset.risk.top10))"></progress>
          <p>{{ date(asset.risk.checkedAt) }} · OKX</p>
        </template>
        <div v-else class="x-empty">{{ tr('上游尚未提供集中度数据。', 'Holder concentration is not available from the source yet.') }}</div>
      </section>
    </div>

    <section class="panel">
      <div class="panel-head">
        <h2>{{ tr('最近成交', 'Recent activity') }}</h2>
        <span class="hint">{{ tr('保存并按成交 ID 去重', 'Persisted and deduplicated by trade ID') }}</span>
      </div>
      <p class="hint">{{ statText }}</p>
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

    <section class="panel x-ai">
      <h2>{{ tr('数据解读', 'Data readout') }}</h2>
      <p class="hint">{{ tr('由 DeepSeek 基于本站已采集字段生成，只解读已有数据，不预测价格。', 'Generated by DeepSeek from collected fields only; no price forecasts.') }}</p>
      <div class="v2-ai-text">{{ insight ?? tr('解读生成中…', 'Readout is being generated…') }}</div>
    </section>

    <div class="x-grid">
      <section class="panel">
        <h2>{{ tr('风险信息', 'Risk information') }}</h2>
        <p v-if="asset.risk">{{ tr('风险等级', 'Risk level') }}: {{ asset.risk.level }} / 5</p>
        <p v-else>{{ tr('风险数据尚未返回。', 'Risk data has not returned yet.') }}</p>
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
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import CandleChart from '../components/CandleChart.vue';
import RegistryBadge from '../components/RegistryBadge.vue';
import EventRow from '../components/EventRow.vue';
import { useDetailStore } from '../stores/detail';
import { getInsight } from '../api/client';
import { tr } from '../i18n';
import { age, chain, chainName, date, detailLink, explorer, num, pct, short, usd } from '../utils/format';

const props = defineProps({
  chain: { type: String, required: true },
  address: { type: String, required: true },
});

const route = useRoute();
const router = useRouter();
const detail = useDetailStore();
const data = ref(null);
const error = ref(null);
const insight = ref(null);
const newIds = reactive(new Set());
const priceFlash = ref(false);
const knownTradeIds = new Set();

const asset = computed(() => data.value?.asset ?? {});
const relations = computed(() => data.value?.relations ?? []);
const trades = computed(() => data.value?.trades ?? []);
const events = computed(() => data.value?.events ?? []);
const backLink = computed(() => (route.query.from === 'stock' ? '/stock' : '/meme'));
const backLabel = computed(() => (route.query.from === 'stock' ? tr('股票', 'Stock') : tr('Meme', 'Meme')) + ' Radar');
const verdictTitle = computed(() =>
  relations.value.some((r) => r.status === 'verified')
    ? tr('已确认的股票配对', 'Confirmed stock pair')
    : asset.value.kind === 'stock'
      ? tr('已收录股票代币', 'Stock token catalogued')
      : tr('关系待核实', 'Relationship pending verification'),
);
const statText = computed(() =>
  asset.value.tradeAt
    ? tr('已保存范围内的成交，可能存在分页缺口，不代表完整历史。', 'Trades in the saved coverage may have pagination gaps; this is not complete history.')
    : tr('成交采集排队中，缺失不表示零成交。', 'Trade collection is queued; missing data does not mean zero trades.'),
);

let lastPriceText = '';
const okxUrl = computed(() => {
  const cid = chain(asset.value);
  const host = { 196: 'xlayer', 56: 'bsc', 4663: 'robinhood' }[cid] ?? 'xlayer';
  return `https://www.okx.com/web3/detail?chain=${host}&address=${encodeURIComponent(asset.value.token ?? '')}`;
});
async function load() {
  try {
    const d = await detail.fetch(props.chain, props.address.toLowerCase());
    data.value = d;
    detail.watch(props.chain, props.address.toLowerCase());
    error.value = null;
    const priceText = usd(d.asset?.price);
    if (lastPriceText && priceText !== lastPriceText) {
      priceFlash.value = false;
      requestAnimationFrame(() => (priceFlash.value = true));
      setTimeout(() => (priceFlash.value = false), 1200);
    }
    lastPriceText = priceText;
    for (const t of d.trades ?? []) knownTradeIds.add(t.id);
    getInsight(props.chain, props.address, 'zh').then((r) => {
      if (r?.text) insight.value = r.text;
    });
  } catch (e) {
    error.value = tr('该资产尚未进入可用索引，或暂时读取失败。', 'This asset is not yet indexed or could not be loaded.');
  }
}

function copyAddress() {
  navigator.clipboard?.writeText(asset.value.token ?? '');
}

function onSsePrice(e) {
  const d = e.detail;
  if (asset.value?.token !== d.token) return;
  const text = usd(d.price);
  if (text !== lastPriceText) {
    asset.value.price = d.price;
    lastPriceText = text;
    priceFlash.value = false;
    requestAnimationFrame(() => (priceFlash.value = true));
    setTimeout(() => (priceFlash.value = false), 1200);
  }
}

function onSseTrades(e) {
  const d = e.detail;
  if (asset.value?.token !== d.token) return;
  for (const t of d.fresh ?? []) {
    if (knownTradeIds.has(t.id)) continue;
    knownTradeIds.add(t.id);
    data.value.trades.unshift(t);
    newIds.add(t.id);
    setTimeout(() => newIds.delete(t.id), 2000);
  }
  if (data.value.trades.length > 150) data.value.trades.length = 150;
}

let pollTimer;
onMounted(() => {
  load();
  pollTimer = setInterval(load, 20000);
  window.addEventListener('sse-price', onSsePrice);
  window.addEventListener('sse-trades', onSseTrades);
});
onBeforeUnmount(() => {
  clearInterval(pollTimer);
  detail.unwatch();
  window.removeEventListener('sse-price', onSsePrice);
  window.removeEventListener('sse-trades', onSseTrades);
});
</script>
