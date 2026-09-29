<template>
  <div>
    <section v-if="!stock" class="panel">
      <h2>{{ tr('股票配对池', 'Stock pair pools') }}</h2>
      <form id="v2Search" @submit.prevent="goSearch">
        <input v-model="q" :placeholder="tr('输入股票或 CA', 'Enter ticker or CA')" required>
        <button>{{ tr('搜索', 'Search') }}</button>
      </form>
      <p v-if="!relationsReady" role="status" :class="verifiedRelations.length ? 'hint' : 'x-empty'">{{ store.error ? tr('完整配对列表读取失败，已显示可用数据。', 'The full pair list failed to load; available data remains visible.') : tr('完整配对列表正在同步，已显示可用数据。', 'The full pair list is syncing; available data is shown.') }} <button v-if="store.error" type="button" @click="store.poll()">{{ tr('重试', 'Retry') }}</button></p>
      <div v-if="verifiedRelations.length">
        <div v-for="r in verifiedRelations.slice(0, 30)" :key="r.id" class="x-signal">
          <RouterLink class="v2-pair-row-link" :to="pairLink(r)"><span>{{ r.ticker }} · {{ chainName(r) }} · {{ short(r.token) }}</span><span>{{ usd(r.liquidityUsd) }}</span></RouterLink>
          <RelationBadge :relation="r" />
        </div>
      </div>
      <div v-else-if="relationsReady" class="x-empty">{{ tr('当前没有已核验配对。', 'No verified pairs are available.') }}</div>
    </section>
    <section v-else class="panel">
      <h2>{{ tr('池子', 'Pools') }}</h2>
      <form id="v2Search" @submit.prevent="goSearch">
        <input v-model="q" :placeholder="tr('输入股票或 CA', 'Enter ticker or CA')" required>
        <button>{{ tr('搜索', 'Search') }}</button>
      </form>
      <p v-if="!relationsReady" role="status" class="x-empty">{{ store.error ? tr('配对数据读取失败，请重试。', 'Failed to load pairs. Please retry.') : tr('正在读取配对证据…', 'Loading pair evidence…') }} <button v-if="store.error" type="button" @click="store.poll()">{{ tr('重试', 'Retry') }}</button></p>
      <p v-if="stockError" role="alert">{{ tr('资产详情暂时无法读取，配对证据仍可查看。', 'Asset details unavailable; pair evidence remains available.') }} <button @click="loadStock">{{ tr('重试', 'Retry') }}</button></p>
      <p v-if="memeError" role="alert">{{ tr('流动性池分布读取失败，请重试。', 'Pool distribution unavailable. Please retry.') }} <button @click="loadMeme">{{ tr('重试', 'Retry') }}</button></p>
      <p v-if="stockLoading" role="status">{{ tr('正在读取资产详情…', 'Loading asset details…') }}</p>
      <div v-if="stockDetail" class="kpis">
        <div class="kpi"><span class="kpi-label">{{ tr('股票代币', 'Stock token') }}</span><strong class="kpi-value mono">{{ stockAsset.symbol ?? stockAsset.tokenSymbol }}</strong><span class="kpi-note">{{ short(stockAsset.token) }}</span></div>
        <div class="kpi"><span class="kpi-label">{{ tr('代币价格', 'Token price') }}</span><strong class="kpi-value mono">{{ money(stockAsset.price, stockAsset.priceCurrency) }}</strong><QuoteStatus :row="stockAsset" /></div>
        <div class="kpi"><span class="kpi-label">{{ tr('官方股票代币池配对', 'Approved stock-token pairs') }}</span><strong class="kpi-value mono">{{ num(officialRelationCount) }}</strong></div>
      </div>
      <section v-if="stockDetail" class="panel">
        <h2>{{ tr('股票代币行情', 'Stock-token market') }}</h2>
        <CandleChart :asset="stockAsset" :samples="stockDetail.samples ?? []" />
      </section>
      <SpreadPanel :chain="props.chain" :token="props.stock" :pool="selected?.pool" />
      <div v-if="verifiedRelations.length" class="v2-pair-layout">
        <aside>
          <div v-for="r in verifiedRelations.slice(0, 30)" :key="r.id" class="x-signal" :class="{ selected: selected?.id === r.id }">
            <RouterLink class="v2-pair-row-link" :to="pairLink(r)">
              <span><strong>{{ memeSymbol(r) }} ↔ {{ r.ticker }}</strong><small>{{ chainName(r) }} · {{ r.protocol }}</small></span>
              <span>{{ usd(r.liquidityUsd) }}<small>{{ poolStateLabel(r) }}</small></span>
            </RouterLink>
            <RelationBadge :relation="r" />
          </div>
        </aside>
        <div>
          <section v-if="selected" class="panel v3-evidence-panel">
            <div class="panel-head">
              <div>
                <h2>{{ tr('关系证据路径', 'Relationship evidence path') }}</h2>
                <p class="hint">{{ tr('这条路径只说明链上配对关系已经核验，不代表公司授权、价格因果或资金流向。', 'This path confirms an on-chain pair relationship only; it does not imply company authorization, price causality or fund flow.') }}</p>
              </div>
              <span class="v3-state" :class="selected.level === 'A' ? 'is-fresh' : 'is-historical'">{{ selected.level === 'A' ? tr('池配对', 'Paired') : tr('身份待核验', 'Identity unverified') }}</span>
            </div>
            <div class="v3-evidence-flow">
              <div class="v3-evidence-node">
                <small>{{ tr('股票主题', 'Stock theme') }}</small>
                <strong>{{ selected.ticker }}</strong>
                <code>{{ short(selected.stock) }}</code>
              </div>
              <div v-if="selected.wrapper" class="v3-evidence-link"><span>→</span><small>{{ tr('包装映射', 'wrapper map') }}</small></div>
              <div v-if="selected.wrapper" class="v3-evidence-node">
                <small>{{ tr('股票侧包装合约', 'Stock-side wrapper') }}</small>
                <strong>{{ stockSideSymbol(selected) }}</strong>
                <code>{{ short(selected.stockSide) }}</code>
              </div>
              <div class="v3-evidence-link"><span>→</span><small>{{ tr('同池核验', 'same-pool proof') }}</small></div>
              <div class="v3-evidence-node is-pool">
                <small>{{ selected.protocol }}</small>
                <strong>{{ tr('配对池', 'Pair pool') }}</strong>
                <code>{{ short(selected.pool) }}</code>
              </div>
              <div class="v3-evidence-link"><span>→</span><small>{{ tr('池的另一侧', 'other pool side') }}</small></div>
              <div class="v3-evidence-node">
                <small>{{ chainName(selected) }}</small>
                <strong>{{ memeSymbol(selected) }}</strong>
                <code>{{ short(selected.token) }}</code>
              </div>
            </div>
            <div class="v3-proof-meta">
              <span>{{ tr('核验时间', 'Checked') }} {{ date(selected.checkedAt) }}</span>
              <span>{{ tr('核验区块', 'Block') }} {{ num(selected.block) }}</span>
              <a :href="explorer(selected.pool, 'address', selected.chainId ?? props.chain)" target="_blank" rel="noopener">{{ tr('查看链上池地址', 'Open pool on explorer') }} ↗</a>
            </div>
          </section>
          <section v-if="selected" class="panel">
            <div class="panel-head">
              <h2>{{ tr('核心事实', 'Key facts') }}</h2>
            </div>
            <div class="x-coverage">
              <span>{{ tr('池流动性', 'Pool liquidity') }} {{ usd(selected.liquidityUsd) }}</span>
              <span>{{ selected.protocol }}</span>
              <span>{{ tr('流动性采样时间', 'Liquidity sampled at') }} {{ date(selected.liquidityAt) }}</span>
              <span v-if="!selected.liquidityAt || now - selected.liquidityAt > 900000">{{ tr('历史快照，非实时流动性', 'Historical snapshot, not live liquidity') }}</span>
              <span v-if="selected.feePct != null">{{ selected.feePct }}%</span>
            </div>
          </section>
          <section v-if="selected?.token" class="panel">
            <div class="panel-head">
              <h2>{{ tr('关联资产行情', 'Related asset market') }}</h2>
              <QuoteStatus :row="memeAsset" />
            </div>
            <p class="hint">{{ tr('图表为该代币的同市场聚合行情；池地址用于关系与流动性证据。', 'The chart is the token’s same-venue aggregate market; the pool address is used for relationship and liquidity evidence.') }}</p>
            <CandleChart :asset="memeAsset" :samples="memeDetail?.samples ?? []" :pool="selected.pool" />
          </section>
          <section v-if="memeDetail && (memeDetail.pools ?? []).length" class="panel">
            <div class="panel-head">
              <h2>{{ tr('流动性池分布', 'Liquidity pool distribution') }}</h2>
            </div>
            <div v-for="p in memeDetail.pools" :key="p.pool" class="v2-pool">
              <p><a :href="explorer(p.pool, 'address', String(selected?.chainId ?? props.chain))" target="_blank" rel="noopener">{{ short(p.pool) }}</a>
                · {{ p.protocol }} · {{ usd(p.liquidityUsd) }} <span class="v3-state" :class="`is-${poolState(p)}`">{{ poolStateLabel(p) }}</span></p>
              <div class="v3-pool-bar"><i :style="{ width: poolWidth(p) + '%' }"></i></div>
              <small>{{ tr('估值时间', 'Valued at') }} {{ date(p.liquidityAt ?? p.valuationAt ?? p.checkedAt) }}</small>
            </div>
          </section>
          <article v-if="selected" class="x-proof v3-raw-proof">
            <div class="panel-head">
              <h3>{{ tr('原始核验信息', 'Raw verification details') }}</h3>
              <RegistryBadge :relation="selected" />
            </div>
            <p>{{ chainName(selected) }} · {{ selected.protocol }} · {{ tr('核验区块', 'Verified block') }} {{ num(selected.block) }} · {{ date(selected.checkedAt) }}</p>
            <p><a :href="explorer(selected.pool, 'address', selected.chainId ?? '196')" target="_blank" rel="noopener">{{ tr('池地址', 'Pool') }} {{ selected.pool }} ↗</a></p>
            <p>{{ tr('池流动性', 'Pool liquidity') }} {{ usd(selected.liquidityUsd) }} · {{ age(selected.liquidityAt) }}</p>
            <details>
              <summary>{{ tr('查看交易池双方合约', 'View both pool contracts') }}</summary>
              <p>token0: <code>{{ selected.token0 }}</code></p>
              <p>token1: <code>{{ selected.token1 }}</code></p>
              <p>{{ tr('股票侧', 'Stock side') }}: <code>{{ selected.stockSide }}</code> → <code>{{ selected.stock }}</code></p>
            </details>
          </article>
        </div>

      </div>
      <div v-else-if="relationsReady" class="x-empty">
        {{ tr('当前资产尚无已核验配对，已有行情仍可查看。', 'This asset has no verified pairs yet. Available market history is shown above.') }}
        <RouterLink to="/stock">{{ tr('返回股票雷达', 'Back to Stock Radar') }}</RouterLink>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import CandleChart from '../components/CandleChart.vue';
import SpreadPanel from '../components/SpreadPanel.vue';
import QuoteStatus from '../components/QuoteStatus.vue';
import RegistryBadge from '../components/RegistryBadge.vue';
import RelationBadge from '../components/RelationBadge.vue';
import { useDashboardStore } from '../stores/dashboard';
import { useDetailStore } from '../stores/detail';
import { tr } from '../i18n';
import { age, chainName, date, explorer, num, pairLink, short, usd, money } from '../utils/format';
import { assetKey } from '../utils/realtime';
import { observationState } from '../utils/theme-presentation';
import { useMinuteClock } from '../composables/useMinuteClock';

const props = defineProps({
  chain: { type: String, default: '196' },
  stock: { type: String, default: '' },
});

const route = useRoute();
const router = useRouter();
const store = useDashboardStore();
const detail = useDetailStore();
const relationsReady = computed(() => !!store.snapshot && store.snapshot.unified?.snapshotScope !== 'overview');
const q = ref('');
const stockDetail = ref(null);
const now = useMinuteClock();

const verifiedRelations = computed(() =>
  props.stock
    ? store.relations.filter((r) => r.status === 'verified' && String(r.chainId ?? '196') === props.chain && r.stock?.toLowerCase() === props.stock.toLowerCase())
    : store.relations.filter((r) => r.level === 'A'),
);
const officialRelationCount = computed(() => verifiedRelations.value.filter(r => r.level === 'A').length);
const selected = computed(() => props.stock
  ? verifiedRelations.value.find((r) => r.pool?.toLowerCase() === String(route.query.pool ?? '').toLowerCase()) ?? verifiedRelations.value[0]
  : null);
const stockAsset = computed(() => {
  const base = stockDetail.value?.asset ?? {};
  const key = assetKey(base.chainId ?? props.chain, base.token ?? props.stock);
  const live = store.stockTokens.find((row) => assetKey(row.chainId, row.tokenContractAddress) === key);
  return live ? { ...base, ...live, token: base.token ?? live.tokenContractAddress } : base;
});

const memeDetail = ref(null);
const stockError = ref(false);
const memeError = ref(false);
const stockLoading = ref(false);
const memeAsset = computed(() => {
  const base = memeDetail.value?.asset ?? {};
  const relation = selected.value;
  const token = relation?.token ?? base.token;
  const chainId = String(relation?.chainId ?? base.chainId ?? props.chain);
  const key = assetKey(chainId, token);
  const live = store.assets.find((row) => assetKey(row.chainId, row.token) === key);
  return {
    ...base,
    ...(live ?? {}),
    token: live?.token ?? base.token ?? token,
    chainId: live?.chainId ?? base.chainId ?? chainId,
  };
});
function memeSymbol(relation) {
  const key = assetKey(relation?.chainId ?? props.chain, relation?.token);
  return store.assets.find((row) => assetKey(row.chainId, row.token) === key)?.symbol ?? short(relation?.token);
}
function stockSideSymbol(relation) {
  const side = String(relation?.stockSide ?? '').toLowerCase();
  return relation?.amounts?.find((amount) => String(amount.tokenContractAddress ?? '').toLowerCase() === side)?.tokenSymbol
    ?? tr('包装股票代币', 'Wrapped stock token');
}
function poolState(pool) {
  return observationState(pool?.liquidityUsd, pool?.liquidityAt ?? pool?.valuationAt ?? pool?.checkedAt, now.value);
}
function poolStateLabel(pool) {
  const state = poolState(pool);
  if (state === 'fresh') return tr('15 分钟内', 'Within 15m');
  if (state === 'historical') return tr('历史估值', 'Historical');
  return tr('规模待采集', 'Size pending');
}
function poolWidth(pool) {
  const rows = memeDetail.value?.pools ?? [];
  const max = Math.max(...rows.map((row) => Number(row.liquidityUsd) || 0), 0);
  return max > 0 ? Math.max(1, Math.min(100, (Number(pool?.liquidityUsd) || 0) / max * 100)) : 0;
}
let stockRequest = 0, memeRequest = 0, timer, releaseStock, releaseMeme, lastRefreshAt=0;
async function loadStock() {
  const request = ++stockRequest;
  if (!props.stock) return;
  stockLoading.value = !stockDetail.value;
  try {
    const data = await detail.fetch(props.chain, props.stock.toLowerCase(), { force: !!stockDetail.value });
    if (request === stockRequest) { stockDetail.value = data; stockError.value = false; }
  } catch {
    if (request === stockRequest) stockError.value = true;
  } finally {
    if (request === stockRequest) stockLoading.value = false;
  }
}
async function loadMeme() {
  const request = ++memeRequest;
  const sel = selected.value;
  if (!sel) { memeDetail.value = null; return; }
  try {
    const data = await detail.fetch(String(sel.chainId ?? props.chain), sel.token, { force: !!memeDetail.value });
    if (request === memeRequest) { memeDetail.value = data; memeError.value = false; }
  } catch {
    if (request === memeRequest) memeError.value = true;
  }
}
watch(() => [props.chain, props.stock], () => {
  releaseStock?.(); releaseStock=props.stock ? detail.activate(props.chain,props.stock) : null;
  stockDetail.value = null; stockError.value = false;
  loadStock();
}, { immediate: true });
watch(() => [selected.value?.chainId, selected.value?.token, selected.value?.pool], () => {
  releaseMeme?.(); releaseMeme=selected.value ? detail.activate(String(selected.value.chainId??props.chain),selected.value.token) : null;
  memeDetail.value = null; memeError.value = false;
  loadMeme();
}, { immediate: true });
function onManualRefresh() { loadStock(); loadMeme(); }
onMounted(() => { window.addEventListener('manual-refresh', onManualRefresh); timer = setInterval(() => {
  if (!document.hidden && (!store.hasProjectionStream || Date.now()-lastRefreshAt>60000)) { lastRefreshAt=Date.now();loadStock(); loadMeme(); }
}, 20000); });
onUnmounted(() => { window.removeEventListener('manual-refresh', onManualRefresh); clearInterval(timer); releaseStock?.();releaseMeme?.();stockRequest++; memeRequest++; });

function goSearch() {
  const value = q.value.trim();
  if (!value) return;
  const stocks = store.stockTokens.filter((s) => [s.stockCode, s.tokenContractAddress, s.tokenSymbol].filter(Boolean).some((v) => v.toLowerCase() === value.toLowerCase()));
  router.push(stocks.length ? { path: '/stock', query: { q: value } } : { path: '/meme', query: { q: value, filter: 'all' } });
}


</script>

<style scoped>
.v2-pair-row-link { display: flex; align-items: center; justify-content: space-between; flex: 1; min-width: 0; gap: 12px; color: inherit; text-decoration: none; }
.v2-pair-row-link:hover, .v2-pair-row-link:focus-visible { color: var(--accent); }
.v2-pair-row-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.v2-pair-row-link > span:last-child { text-align: right; font-variant-numeric: tabular-nums; }
</style>
