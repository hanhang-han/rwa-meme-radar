<template>
  <header>
    <div class="brand">
      <span class="dot"></span>
      <h1>RWA Meme Radar</h1>
      <span class="sub">{{ tr('股票相关 Meme 关系雷达', 'Meme-stock relationship radar') }}</span>
    </div>
    <div class="header-right">
      <div class="lang-control">
        <div class="lang-switch">
          <button class="lang-btn" :class="{ active: lang.lang === 'zh' }" @click="setLang('zh')">中</button>
          <button class="lang-btn" :class="{ active: lang.lang === 'en' }" @click="setLang('en')">EN</button>
        </div>
      </div>
    </div>
  </header>
  <main class="radar-workspace">
    <nav class="workspace-nav">
      <RouterLink v-for="item in nav" :key="item.to" :to="item.to" :class="{ active: item.match.has(route.name) }">
        {{ tr(item.zh, item.en) }}
      </RouterLink>
    </nav>
    <RouterView />
  </main>
</template>

<script setup>
import { useRoute } from 'vue-router';
import { tr, setLang, useI18n } from './i18n';

const route = useRoute();
const lang = useI18n().lang;
const nav = [
  { to: '/live', zh: '首页', en: 'Home', match: new Set(['live']) },
  { to: '/meme', zh: 'MEME', en: 'MEME', match: new Set(['meme', 'detail']) },
  { to: '/stock', zh: '股票', en: 'Stocks', match: new Set(['stock']) },
  { to: '/pair/196/0x0', zh: '交易池分析', en: 'Pool Analysis', match: new Set(['pair']) },
  { to: '/events', zh: '事件', en: 'Events', match: new Set(['events']) },
];
</script>

<style scoped>
.workspace-nav { display: flex; gap: 8px; padding: 12px 0; border-top: 1px solid var(--border); }
.workspace-nav a { padding: 9px 14px; border-radius: 7px; color: var(--text); text-decoration: none; }
.workspace-nav a.active { color: var(--bg); background: var(--accent); }
</style>
