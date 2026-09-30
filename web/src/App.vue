<template>
  <header>
    <div class="brand">
      <span class="dot"></span>
      <h1>STOCKSMEME RADAR</h1>
    </div>
    <div class="header-right">
      <select v-if="isMarketPage" class="header-chain" :value="scope" :aria-label="tr('网络范围', 'Chain scope')" @change="changeChain($event.target.value)">
        <option value="all">{{ tr('全部链', 'All chains') }}</option>
        <option value="196">X Layer</option>
        <option value="56">BNB Chain</option>
        <option value="4663">Robinhood Chain</option>
      </select>
      <GlobalSearch v-if="isMarketPage" />
      <span v-if="isMarketPage" id="updatedAt" class="header-status" :class="statusClass" :title="streamLabel"><i></i><time>{{ statusTime }}</time></span>
      <RouterLink to="/developer" class="header-api">API</RouterLink>
      <div class="lang-control">
        <div class="lang-switch">
          <button class="lang-btn" :class="{ active: lang.lang === 'zh' }" @click="setLangWithFeedback('zh')">中</button>
          <button class="lang-btn" :class="{ active: lang.lang === 'en' }" @click="setLangWithFeedback('en')">EN</button>
        </div>
        <div v-if="langFeedback" class="lang-feedback">{{ langFeedback }}</div>
      </div>
    </div>
  </header>
  <main class="radar-workspace">
    <nav class="workspace-nav">
      <RouterLink v-for="item in nav" :key="item.to" :to="item.to" :class="{ active: item.match.has(activeNav) }">
        {{ tr(item.zh, item.en) }}
      </RouterLink>
    </nav>
    <div class="view">
      <p v-if="routeError" role="alert" class="x-empty">{{ tr('页面加载失败，请重新加载。', 'Page failed to load. Please reload.') }} <button @click="reloadPage">{{ tr('重新加载', 'Reload') }}</button></p>
      <section v-if="waitingForSnapshot" class="panel loading-skeleton" role="status" :aria-label="tr('加载中', 'Loading')">
        <template v-if="!loadTimedOut"><div v-for="n in 8" :key="n" class="skeleton-line"></div></template>
        <button v-else @click="retryLoad">{{ tr('加载失败 · 重试', 'Failed to load · Retry') }}</button>
      </section>
      <p v-else-if="listWaitingForData" class="panel market-load-status" role="status">
        {{ store.error
          ? tr('行情列表暂时加载失败，已显示可用数据。', 'Market list failed to load; available data remains visible.')
          : tr('行情列表正在同步，筛选和导航已可使用。', 'Market list is syncing; filters and navigation are available.') }}
        <button v-if="store.error" type="button" @click="retryLoad">{{ tr('重试加载', 'Retry loading') }}</button>
      </p>
      <p v-else-if="isMarketPage && store.error" class="hint" role="status">{{ tr('行情更新失败，暂时保留上一份数据；稍后自动重试。', 'Market update failed; the last snapshot is retained. Retrying automatically.') }}</p>
      <RouterView v-if="!waitingForSnapshot" :key="$route.path" />
    </div>
  </main>
  <footer class="site-footer"><p v-if="isMarketPage && syncedAt">{{ tr('数据时间','Data as of') }} {{ statusTime }}<span v-if="clock-syncedAt>180000" class="warn"> · {{ tr('已过期','Stale') }}</span></p>{{ tr('池子配对与名称匹配仅用于市场研究，不代表发行方授权或投资建议。','Pool pairings and name matches are research information, not issuer endorsement or investment advice.') }}</footer>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { tr, setLang, useI18n } from './i18n';
import { useDashboardStore } from './stores/dashboard';
import { chainScope } from './utils/chain-scope';
import GlobalSearch from './components/GlobalSearch.vue';

const route = useRoute();
const router = useRouter();
const lang = useI18n().lang;
const store = useDashboardStore();
const isMarketPage = computed(() => route.meta.marketData !== false);
const scope = computed(() => chainScope(route.query));
const waitingForSnapshot = computed(() => route.name === 'live' && !store.snapshot);
const listWaitingForData = computed(() => ['meme', 'stock', 'stockDetail'].includes(route.name) && (!store.snapshot || store.snapshot.unified?.snapshotScope === 'overview'));
const langFeedback = ref('');
const clock = ref(Date.now());
const loadTimedOut = ref(false);
let clockTimer;
const routeLoading = ref(!route.matched.length);
const routeError = ref(false);
const removeBefore = router.beforeEach(() => { routeLoading.value = true; routeError.value = false; });
const removeAfter = router.afterEach(() => { routeLoading.value = false; });
const removeError = router.onError(() => { routeLoading.value = false; routeError.value = true; });
router.isReady().then(() => { routeLoading.value = false; }).catch(() => { routeLoading.value = false; routeError.value = true; });
onMounted(() => { clockTimer = setInterval(() => (clock.value = Date.now()), 5000); });
onUnmounted(() => { removeBefore(); removeAfter(); removeError(); clearInterval(clockTimer); });
function reloadPage() { window.location.reload(); }
async function retryLoad() { loadTimedOut.value = false; await store.poll(); }
function changeChain(chain) { router.push({ path: route.path, query: { ...route.query, chain, page: undefined } }); }
watch(waitingForSnapshot, (waiting, _, onCleanup) => {
  if (!waiting) { loadTimedOut.value = false; return; }
  const timer = setTimeout(() => { loadTimedOut.value = true; }, 10000);
  onCleanup(() => clearTimeout(timer));
}, { immediate: true });


const syncedAt = computed(() => Number(store.snapshot?.now) || 0);
const statusTime = computed(() => { clock.value; return syncedAt.value ? new Date(syncedAt.value).toLocaleTimeString(lang.lang === 'en' ? 'en-US' : 'zh-CN', { hour:'2-digit', minute:'2-digit', hour12:false }) : '—'; });
const statusClass = computed(() => { clock.value; return !syncedAt.value || clock.value - syncedAt.value > 120000 ? 'is-stale' : clock.value-syncedAt.value>30000 || !store.stream.connected ? 'is-paused' : 'is-live'; });
const streamLabel = computed(() => {
  clock.value;
  if (store.stream.state === 'syncing') return tr('行情同步中', 'Market data syncing');
  if (store.stream.state === 'reconnecting') return tr('行情重连中', 'Market data reconnecting');
  if (store.stream.connected) return tr('推送已连接', 'Stream connected');
  return tr('轮询更新', 'Polling updates');
});
const nav = [
  { to: '/live', zh: '首页', en: 'Home', match: new Set(['live']) },
  { to: '/stock', zh: '股票', en: 'Stocks', match: new Set(['stock', 'stockDetail']) },
  { to: '/meme', zh: 'Meme', en: 'Memes', match: new Set(['meme', 'pair']) },
  { to: '/watch', zh: '追踪', en: 'Watchlist', match: new Set(['watch', 'events']) },
  { to: '/me', zh: '我的', en: 'My account', match: new Set(['account', 'developer','status']) },
];
const activeNav=computed(()=>route.name==='detail'?(route.query.from==='stock'?'stock':route.query.from==='watch'?'watch':'meme'):route.name);

function setLangWithFeedback(l) {
  setLang(l);
  langFeedback.value = l === 'en' ? 'Switched to English' : '已切换为中文';
  setTimeout(() => (langFeedback.value = ''), 1200);
}

// Expose the language setter for tests and keep the template handler named.
defineExpose({ setLang: setLangWithFeedback });
</script>
