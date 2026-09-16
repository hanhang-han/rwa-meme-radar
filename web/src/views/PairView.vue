<template>
  <div>
    <section v-if="!stock" class="panel">
      <h2>{{ tr('选择资产或配对', 'Choose an asset or pair') }}</h2>
      <form id="v2Search" @submit.prevent="goSearch">
        <input v-model="q" :placeholder="tr('输入股票或 CA', 'Enter ticker or CA')" required>
        <button>{{ tr('搜索', 'Search') }}</button>
      </form>
      <div v-if="verifiedRelations.length">
        <RouterLink v-for="r in verifiedRelations.slice(0, 30)" :key="r.id" class="x-signal" :to="pairLink(r)">
          {{ r.ticker }} · {{ chainName(r) }} · {{ short(r.token) }} <span>{{ usd(r.liquidityUsd) }}</span>
        </RouterLink>
      </div>
      <div v-else class="x-empty">{{ tr('等待已核验配对。', 'Waiting for verified pairs.') }}</div>
    </section>
    <section v-else class="panel">
      <h2>{{ tr('交易池分析', 'Pool analysis') }}</h2>
      <form id="v2Search" @submit.prevent="goSearch">
        <input v-model="q" :placeholder="tr('输入股票或 CA', 'Enter ticker or CA')" required>
        <button>{{ tr('搜索', 'Search') }}</button>
      </form>
      <div v-if="verifiedRelations.length" class="v2-pair-layout">
        <aside>
          <a v-for="r in verifiedRelations.slice(0, 30)" :key="r.id" class="x-signal" :class="{ selected: selected?.id === r.id }"
             :href="pairLink(r)">
            {{ r.ticker }} · {{ chainName(r) }} · {{ short(r.token) }} <span>{{ usd(r.liquidityUsd) }}</span>
          </a>
        </aside>
        <div v-if="stockDetail">
          <div class="kpis">
            <div class="kpi"><span class="kpi-label">{{ tr('股票代币', 'Stock token') }}</span><strong class="kpi-value mono">{{ stockAsset.symbol }}</strong><span class="kpi-note">{{ short(stockAsset.token) }}</span></div>
            <div class="kpi"><span class="kpi-label">{{ tr('代币价格', 'Token price') }}</span><strong class="kpi-value mono">{{ usd(stockAsset.price) }}</strong><span class="kpi-note">{{ age(stockAsset.updatedAt) }}</span></div>
            <div class="kpi"><span class="kpi-label">{{ tr('已核验配对', 'Verified pairs') }}</span><strong class="kpi-value mono">{{ num(verifiedRelations.length) }}</strong><span class="kpi-note">{{ tr('含流动性证据', 'with liquidity evidence') }}</span></div>
          </div>
          <article v-for="r in verifiedRelations" :key="r.id" class="x-proof">
            <div class="panel-head">
              <h3>{{ r.ticker }} · {{ r.wrapper ? tr('包装股票配对', 'Wrapped stock pair') : tr('直接股票配对', 'Direct pair') }}</h3>
              <RouterLink :to="pairLink(r)">{{ tr('交易池分析', 'Pool analysis') }} →</RouterLink>
            </div>
            <p>{{ chainName(r) }} · {{ r.protocol }} · {{ tr('核验区块', 'Verified block') }} {{ num(r.block) }} · {{ date(r.checkedAt) }}</p>
            <p><a :href="explorer(r.pool, 'address', r.chainId ?? '196')" target="_blank" rel="noopener">{{ tr('池地址', 'Pool') }} {{ r.pool }} ↗</a></p>
            <p>{{ tr('池流动性', 'Pool liquidity') }} {{ usd(r.liquidityUsd) }} · {{ age(r.liquidityAt) }}</p>
            <details>
              <summary>{{ tr('查看交易池双方合约', 'View both pool contracts') }}</summary>
              <p>token0: <code>{{ r.token0 }}</code></p>
              <p>token1: <code>{{ r.token1 }}</code></p>
              <p>{{ tr('股票侧', 'Stock side') }}: <code>{{ r.stockSide }}</code> → <code>{{ r.stock }}</code></p>
            </details>
            <RegistryBadge :relation="r" />
          </article>
        </div>
        <div v-else class="x-empty">{{ tr('正在读取股票资产与配对证据…', 'Loading stock asset and pair evidence…') }}</div>
      </div>
      <div v-else class="x-empty">
        {{ tr('当前范围暂无已核验配对。从股票雷达选择资产查看证据。', 'No verified pairs in the current scope. Pick a stock from the radar to view evidence.') }}
        <RouterLink to="/stock">{{ tr('返回股票雷达', 'Back to Stock Radar') }}</RouterLink>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import RegistryBadge from '../components/RegistryBadge.vue';
import { useDashboardStore } from '../stores/dashboard';
import { useDetailStore } from '../stores/detail';
import { tr } from '../i18n';
import { age, chainName, date, explorer, num, pairLink, short, usd } from '../utils/format';

const props = defineProps({
  chain: { type: String, required: true },
  stock: { type: String, required: true },
});

const route = useRoute();
const router = useRouter();
const store = useDashboardStore();
const detail = useDetailStore();
const q = ref('');
const stockDetail = ref(null);

const verifiedRelations = computed(() =>
  props.stock
    ? store.relations.filter((r) => r.status === 'verified' && String(r.chainId ?? '196') === props.chain && r.stock === props.stock.toLowerCase())
    : store.relations.filter((r) => r.status === 'verified'),
);
const selected = computed(() => verifiedRelations.value.find((r) => r.pool === route.query.pool) ?? verifiedRelations.value[0]);
const stockAsset = computed(() => stockDetail.value?.asset ?? {});

async function load() {
  if (!props.stock) return;
  try {
    stockDetail.value = await detail.fetch(props.chain, props.stock.toLowerCase());
  } catch {
    stockDetail.value = null;
  }
}

function goSearch() {
  const value = q.value.trim();
  if (!value) return;
  const stocks = store.stockTokens.filter((s) => [s.stockCode, s.tokenContractAddress, s.tokenSymbol].filter(Boolean).some((v) => v.toLowerCase() === value.toLowerCase()));
  router.push(stocks.length ? { path: '/stock', query: { q: value } } : { path: '/meme', query: { q: value, filter: 'all' } });
}

onMounted(load);
watch(() => props.stock, load);
</script>
