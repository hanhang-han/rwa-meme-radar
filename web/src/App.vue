<template>
  <header class="site-header" :class="{ 'asset-header': route.name === 'detail' }">
    <RouterLink class="brand" :to="{path:'/live',query:{chain:scope}}" :aria-label="tr('CliperX 首页','CliperX home')">
      <svg class="brand-mark" viewBox="0 0 64 64" fill="none" aria-hidden="true" focusable="false">
        <path class="brand-mark__c" d="M46 13C42 10 37 8.5 31.5 8.5 18.5 8.5 9 18.7 9 32s9.5 23.5 22.5 23.5C37 55.5 42 54 46 51" stroke-width="7.5" stroke-linecap="round" />
        <path class="brand-mark__signal" d="m19 39 8-7 6 4 12-12" stroke-width="6.5" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      <h1>CliperX</h1>
    </RouterLink>
    <nav class="workspace-nav" :aria-label="tr('主导航','Main navigation')">
      <RouterLink v-for="item in nav" :key="item.to" :to="{path:item.to,query:{chain:scope}}" :class="{ active: item.match.has(activeNav) }" :aria-current="item.match.has(activeNav) ? 'page' : undefined">
        <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="item.icon" /></svg>
        <span>{{ tr(item.zh, item.en) }}</span>
      </RouterLink>
    </nav>
    <div class="header-right">
      <RouterLink :to="pageNavigationLink('/developer',{route,scope})" class="header-api">API</RouterLink>
      <div class="lang-control">
        <div class="lang-switch">
          <button class="lang-btn" :class="{ active: lang.lang === 'zh' }" :aria-pressed="lang.lang === 'zh'" @click="setLangWithFeedback('zh')">中</button>
          <button class="lang-btn" :class="{ active: lang.lang === 'en' }" :aria-pressed="lang.lang === 'en'" @click="setLangWithFeedback('en')">EN</button>
        </div>
        <div v-if="langFeedback" class="lang-feedback">{{ langFeedback }}</div>
      </div>
    </div>
  </header>
  <main class="radar-workspace">
    <div v-if="isMarketPage && route.name !== 'detail'" class="market-tools">
      <GlobalSearch />
      <div class="market-scope">
        <span v-if="route.name !== 'watch'" id="updatedAt" class="header-status" :class="statusClass" :title="streamLabel" role="status" :aria-label="snapshotStatus"><i aria-hidden="true"></i><span>{{ tr('更新', 'Updated') }} <time>{{ statusTime }}</time><small v-if="statusClass==='is-stale'"> · {{ tr('已延迟', 'Delayed') }}</small></span></span>
        <select class="header-chain" :value="scope" :aria-label="tr('网络范围', 'Chain scope')" @change="changeChain($event.target.value)">
          <option value="all">{{ tr('全部链', 'All chains') }}</option>
          <option value="196">X Layer</option>
          <option value="56">BNB Chain</option>
          <option value="4663">Robinhood Chain</option>
        </select>
      </div>
    </div>
    <div class="view">
      <PageNavigation />
      <p v-if="routeError" role="alert" class="x-empty">{{ tr('页面加载失败，请重新加载。', 'Page failed to load. Please reload.') }} <button @click="reloadPage">{{ tr('重新加载', 'Reload') }}</button></p>
      <section v-if="waitingForSnapshot" class="panel loading-skeleton" role="status" :aria-label="tr('加载中', 'Loading')">
        <template v-if="!loadTimedOut"><div v-for="n in 8" :key="n" class="skeleton-line"></div></template>
        <button v-else @click="retryLoad">{{ tr('加载失败 · 重试', 'Failed to load · Retry') }}</button>
      </section>
      <p v-else-if="isMarketPage && !['meme','watch'].includes(route.name) && store.error" class="hint" role="status">{{ tr('更新失败，显示上次数据 · 自动重试中', 'Update failed; showing previous data · retrying') }}</p>
      <RouterView v-if="!waitingForSnapshot" :key="$route.path" />
    </div>
  </main>
  <footer class="site-footer">{{ tr('Meme 为社区发行，与所标注的上市公司没有授权关系。数据来自公开市场，不构成投资建议。','Memes are community-issued and are not authorized by the named listed companies. Public market data is not investment advice.') }}</footer>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { tr, setLang, useI18n } from './i18n';
import { useDashboardStore } from './stores/dashboard';
import { useMemeDirectoryStore } from './stores/meme-directory';
import { chainScope } from './utils/chain-scope';
import GlobalSearch from './components/GlobalSearch.vue';
import PageNavigation from './components/PageNavigation.vue';
import { navigationSource, pageNavigationLink } from './utils/navigation-context.js';
import { clearPageState } from './utils/page-navigation-state.js';
import { useAccountStore } from './stores/account.js';
import { watchProductPreferences } from './utils/product-preferences';

const stopPreferences = watchProductPreferences();
onUnmounted(stopPreferences);

const route = useRoute();
const router = useRouter();
const lang = useI18n().lang;
const store = useDashboardStore();
const account = useAccountStore();
watch(() => account.session?.user?.id ?? 'guest', (id, previous) => {
  if (id !== previous) { clearPageState(`wallet-profile:${previous}`); clearPageState('wallet-profile:guest'); }
});
const memeDirectory = useMemeDirectoryStore();
const isMarketPage = computed(() => route.meta.marketData !== false);
const scope = computed(() => chainScope(route.query));
const waitingForSnapshot = computed(() => route.name === 'live' && !store.snapshot);
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


const activeStream = computed(() => route.name === 'meme' ? memeDirectory.stream : store.stream);
const syncedAt = computed(() => Number((route.name === 'meme' ? memeDirectory.snapshot : store.snapshot)?.now) || 0);
const statusTime = computed(() => { clock.value; return syncedAt.value ? new Date(syncedAt.value).toLocaleTimeString(lang.lang === 'en' ? 'en-US' : 'zh-CN', { hour:'2-digit', minute:'2-digit', hour12:false }) : '—'; });
const statusClass = computed(() => { clock.value; return !syncedAt.value || clock.value - syncedAt.value > 120000 ? 'is-stale' : clock.value-syncedAt.value>30000 || !activeStream.value.connected ? 'is-paused' : 'is-live'; });
const snapshotStatus = computed(() => syncedAt.value
  ? `${tr('快照更新于', 'Snapshot updated at')} ${statusTime.value}${statusClass.value==='is-stale'?`，${tr('已延迟', 'delayed')}`:''}`
  : tr('快照更新时间未知', 'Snapshot update time unknown'));
const streamLabel = computed(() => {
  clock.value;
  if (activeStream.value.state === 'syncing') return tr('行情同步中', 'Market data syncing');
  if (activeStream.value.state === 'reconnecting') return tr('行情重连中', 'Market data reconnecting');
  if (activeStream.value.connected) return tr('推送已连接', 'Stream connected');
  return tr('轮询更新', 'Polling updates');
});
const nav = [
  { to: '/live', zh: '首页', en: 'Home', icon:'M3 10.5 12 3l9 7.5M5 9v12h5v-7h4v7h5V9', match: new Set(['live']) },
  { to: '/stock', zh: '股票主题', en: 'Stock themes', icon:'M3 3h8v8H3zM15 3h6v6h-6zM3 15h6v6H3zM13 13h8v8h-8z', match: new Set(['stock', 'stockDetail']) },
  { to: '/meme', zh: 'Meme', en: 'Memes', icon:'M3 17l5-5 4 3 8-10M15 5h5v5M3 21h18', match: new Set(['meme', 'pair']) },
  { to: '/watch', zh: '关注', en: 'Watchlist', icon:'m12 3 2.8 5.7 6.3.9-4.5 4.4 1.1 6.2-5.7-3-5.7 3 1.1-6.2-4.5-4.4 6.3-.9z', match: new Set(['watch', 'events']) },
  { to: '/me', zh: '我的', en: 'My account', icon:'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-2a8 8 0 0 1 16 0v2', match: new Set(['me']) },
];
const activeNav=computed(()=>navigationSource(route));

function setLangWithFeedback(l) {
  setLang(l);
  langFeedback.value = l === 'en' ? 'Switched to English' : '已切换为中文';
  setTimeout(() => (langFeedback.value = ''), 1200);
}

// Expose the language setter for tests and keep the template handler named.
defineExpose({ setLang: setLangWithFeedback });
</script>
