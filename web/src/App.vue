<template>
  <header>
    <div class="brand">
      <span class="dot"></span>
      <h1>STOCKSMEME RADAR</h1>
      <span class="sub">{{ tr('股票相关 Meme 关系雷达', 'Meme-stock relationship radar') }}</span>
    </div>
    <div class="header-right">
      <div class="metric"><span class="label">{{ tr('页面同步', 'Synced') }}</span><span class="value" id="updatedAt">{{ age(syncedAt) }}</span></div>
      <button id="refresh" :title="tr('立即刷新', 'Refresh now')" @click="refreshNow">↻</button>
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
      <RouterLink v-for="item in nav" :key="item.to" :to="item.to" :class="{ active: item.match.has(route.name) }">
        {{ tr(item.zh, item.en) }}
      </RouterLink>
    </nav>
    <div class="view">
      <RouterView />
    </div>
  </main>
  <footer>{{ tr('关系有证据，数字有来源。历史持续保存，缺失不等于零。', 'Evidence for relationships. Sources for numbers. History persists; missing is not zero.') }}</footer>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { tr, setLang, useI18n } from './i18n';
import { age } from './utils/format';
import { useDashboardStore } from './stores/dashboard';

const route = useRoute();
const router = useRouter();
const lang = useI18n().lang;
const store = useDashboardStore();
const langFeedback = ref('');

const syncedAt = computed(() => store.updatedAt);
const nav = [
  { to: '/live', zh: '首页', en: 'Home', match: new Set(['live']) },
  { to: '/meme', zh: 'MEME', en: 'MEME', match: new Set(['meme', 'detail']) },
  { to: '/stock', zh: '股票', en: 'Stocks', match: new Set(['stock']) },
  { to: '/pair', zh: '交易池分析', en: 'Pool Analysis', match: new Set(['pair']) },
];

function setLangWithFeedback(l) {
  setLang(l);
  langFeedback.value = l === 'en' ? 'Switched to English' : '已切换为中文';
  setTimeout(() => (langFeedback.value = ''), 1200);
}

function refreshNow() {
  store.poll();
}

// Expose the language setter for tests and keep the template handler named.
defineExpose({ setLang: setLangWithFeedback });
</script>
