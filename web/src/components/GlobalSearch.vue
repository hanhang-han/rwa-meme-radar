<template>
  <div class="global-search" @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)" @keydown.esc="close">
    <form @submit.prevent="submit">
      <input ref="input" v-model="term" type="search" :aria-label="tr('全局搜索', 'Global search')" :placeholder="tr('搜索股票、Meme 或合约地址', 'Search stock, meme or address')" @focus="open = true" @input="open = true">
      <button type="submit" :aria-label="tr('搜索', 'Search')">⌕</button>
    </form>
    <div v-if="open && term.trim()" class="search-results" role="listbox">
      <template v-if="results.length">
        <RouterLink v-for="(row, index) in results" :key="row.key" :to="row.to" role="option" :aria-selected="index === selected" :class="{ selected: index === selected }" @click="close">
          <span class="search-kind">{{ row.kind === 'stock' ? tr('股票', 'Stock') : 'Meme' }}</span>
          <strong>{{ row.name }}</strong><small>{{ row.chain }} · {{ row.address }}</small>
          <span class="search-liquidity">{{ row.liquidity }}</span>
        </RouterLink>
      </template>
      <p v-else class="x-empty">{{ tr('没有匹配结果，可按完整地址搜索。', 'No matches. Try a full address.') }}</p>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useDashboardStore } from '../stores/dashboard';
import { chainScope, inChainScope } from '../utils/chain-scope';
import { chainName, short, usd } from '../utils/format';
import { tr } from '../i18n';

const store = useDashboardStore();
const route = useRoute();
const router = useRouter();
const input = ref(null);
const term = ref('');
const open = ref(false);
const selected = ref(0);
const scope = computed(() => chainScope(route.query));
const results = computed(() => {
  const q = term.value.trim().toLowerCase();
  if (!q) return [];
  const stocks = store.stockTokens.filter(s => inChainScope(s, scope.value) && [s.stockCode, s.tokenName, s.tokenSymbol, s.tokenContractAddress, s.stockIdentity?.nameZh, s.stockIdentity?.nameEn].some(v => String(v ?? '').toLowerCase().includes(q)))
    .sort((a,b) => Number(b.issuerIdentity?.verificationStatus === 'official') - Number(a.issuerIdentity?.verificationStatus === 'official') || Number(b.totalLiquidityUsd ?? 0) - Number(a.totalLiquidityUsd ?? 0))
    .slice(0,4).map(s => ({ key:`stock:${s.chainId}:${s.tokenContractAddress}`, kind:'stock', name:`${s.stockCode ?? s.tokenSymbol} ${s.stockIdentity?.nameZh ?? s.stockIdentity?.nameEn ?? ''}`.trim(), chain:chainName(s), address:short(s.tokenContractAddress), rawAddress:s.tokenContractAddress, liquidity:usd(s.totalLiquidityUsd), to:{path:'/stock',query:{q:s.stockCode ?? s.tokenSymbol,chain:scope.value}} }));
  const memes = store.assets.filter(a => a.kind === 'candidate' && inChainScope(a,scope.value) && [a.name,a.symbol,a.token].some(v => String(v ?? '').toLowerCase().includes(q)))
    .sort((a,b) => Number(b.totalLiquidityUsd ?? 0) - Number(a.totalLiquidityUsd ?? 0))
    .slice(0,6).map(a => ({ key:`meme:${a.chainId}:${a.token}`, kind:'meme', name:a.name || a.symbol || short(a.token), chain:chainName(a), address:short(a.token), rawAddress:a.token, liquidity:usd(a.totalLiquidityUsd), to:{path:`/detail/${a.chainId}/${a.token}`,query:{chain:scope.value}} }));
  return [...stocks,...memes].slice(0,8);
});
function close() { open.value = false; selected.value = 0; }
function move(delta) { if (!open.value) open.value = true; selected.value = Math.max(0,Math.min(results.value.length-1,selected.value+delta)); }
function submit() {
  const exact = results.value.find(r => String(r.rawAddress).toLowerCase() === term.value.trim().toLowerCase());
  if (exact || results.value[selected.value]) router.push((exact ?? results.value[selected.value]).to);
  else router.push({path:'/meme',query:{q:term.value.trim(),chain:scope.value,rel:'all',fresh:'0'}});
  close(); input.value?.blur();
}
function onKey(event) { if (event.key === '/' && !/^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName ?? '')) { event.preventDefault(); input.value?.focus(); } }
onMounted(() => document.addEventListener('keydown',onKey));
onUnmounted(() => document.removeEventListener('keydown',onKey));
</script>
