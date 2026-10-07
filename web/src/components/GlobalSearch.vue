<template>
  <div ref="root" class="global-search" @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)" @keydown.esc="close">
    <form @submit.prevent="submit">
      <input ref="input" v-model="term" type="search" maxlength="80" role="combobox" aria-autocomplete="list" :aria-expanded="open && !!term.trim()" aria-controls="global-search-listbox" :aria-activedescendant="open && results.length ? `global-search-option-${selected}` : undefined" :aria-label="tr('全局搜索', 'Global search')" :placeholder="tr('搜索股票主题、Meme 或合约地址', 'Search stock theme, meme or address')" @focus="open = true" @input="open = true">
      <button type="submit" :aria-label="tr('搜索', 'Search')">⌕</button>
    </form>
    <div v-if="open && term.trim()" id="global-search-listbox" class="search-results" role="listbox" :aria-label="tr('搜索结果', 'Search results')">
      <template v-if="results.length">
        <RouterLink v-for="(row, index) in results" :id="`global-search-option-${index}`" :key="row.key" :to="row.to" role="option" :aria-selected="index === selected" :class="{ selected: index === selected }" @click="close">
          <span class="search-kind">{{ row.kind === 'stock' ? row.exact?tr('股票代币','Stock token'):tr('股票主题', 'Stock theme') : 'Meme' }}</span>
          <strong>{{ row.name }}</strong><small>{{ row.kind === 'stock' ? row.networks > 1 ? `${row.networks} ${tr('个网络', 'networks')}` : row.chain : `${row.chain} · ${row.address}` }}</small>
        </RouterLink>
      </template>
      <p v-else-if="searchLoading" class="x-empty" role="status">{{ tr('搜索中…', 'Searching…') }}</p>
      <p v-else-if="searchError" class="x-empty" role="status">{{ tr('搜索暂时无法读取。','Search is unavailable.') }} <button type="button" @click="loadSearch">{{ tr('重试','Retry') }}</button></p>
      <p v-else class="x-empty">{{ tr('没有匹配结果，可按完整地址搜索。', 'No matches. Try a full address.') }}</p>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useDashboardStore } from '../stores/dashboard';
import { chainScope, inChainScope } from '../utils/chain-scope';
import { chainName, short } from '../utils/format';
import { tr, useI18n } from '../i18n';
import { getProductSearch } from '../api/product';
import { stockThemeName, stockTicker } from '../utils/stock-theme-model';
import { assetNavigationLink, themeNavigationLink } from '../utils/navigation-context';

const store = useDashboardStore();
const route = useRoute();
const router = useRouter();
const { lang } = useI18n();
const root = ref(null);
const input = ref(null);
const term = ref(String(route.query.q??''));
watch(()=>route.query.q,value=>{if(typeof value==='string')term.value=value;});
const open = ref(false);
const selected = ref(0);
const scope = computed(() => chainScope(route.query));
const searchPayload=ref(null),searchLoading=ref(false),searchError=ref(false);
let searchRequest=0,searchTimer;
const searchStocks=computed(()=>searchPayload.value?.unified?.stockTokens??store.stockTokens);
const searchAssets=computed(()=>searchPayload.value?.unified?.assets??store.assets);
async function loadSearch(){
  const q=term.value.trim(),request=++searchRequest;
  if(!q){searchPayload.value=null;searchLoading.value=false;return;}
  searchLoading.value=true;searchError.value=false;
  try{const result=await getProductSearch(q,scope.value);if(request===searchRequest)searchPayload.value=result;}
  catch{if(request===searchRequest)searchError.value=true;}
  finally{if(request===searchRequest)searchLoading.value=false;}
}
const results = computed(() => {
  const q = term.value.trim().toLowerCase();
  if (!q) return [];
  const matchedStocks = searchStocks.value.filter(s => inChainScope(s, scope.value) && (searchPayload.value || [s.stockCode, s.tokenName, s.tokenSymbol, s.tokenContractAddress, s.stockIdentity?.nameZh, s.stockIdentity?.nameEn].some(v => String(v ?? '').toLowerCase().includes(q))))
    .sort((a,b) => Number(b.issuerIdentity?.verificationStatus === 'official') - Number(a.issuerIdentity?.verificationStatus === 'official') || Number(b.totalLiquidityUsd ?? 0) - Number(a.totalLiquidityUsd ?? 0));
  const stockGroups = new Map();
  for (const stock of matchedStocks) {
    const ticker = stockTicker(stock) || stock.tokenSymbol;
    if (!ticker) continue;
    if (!stockGroups.has(ticker)) stockGroups.set(ticker, { stock, chains:new Set() });
    stockGroups.get(ticker).chains.add(String(stock.chainId));
  }
  const stocks = [...stockGroups.entries()].slice(0,4).map(([ticker,group]) => {
    const company = stockThemeName(group.stock,ticker,lang.lang);
    const assetTo=assetNavigationLink(group.stock,{route,scope:scope.value}),exact=q===String(group.stock.tokenContractAddress).toLowerCase();
    return { key:`stock:${ticker}`, kind:'stock', exact, name:company === ticker ? ticker : `${ticker} ${company}`, chain:chainName(group.stock), networks:group.chains.size, rawAddress:group.stock.tokenContractAddress, assetTo, to:exact?assetTo:themeNavigationLink(ticker,{route,scope:scope.value}) };
  });
  const memes = searchAssets.value.filter(a => a.kind === 'candidate' && inChainScope(a,scope.value) && (searchPayload.value || [a.name,a.symbol,a.token].some(v => String(v ?? '').toLowerCase().includes(q))))
    .sort((a,b) => Number(b.totalLiquidityUsd ?? 0) - Number(a.totalLiquidityUsd ?? 0))
    .slice(0,6).map(a => ({ key:`meme:${a.chainId}:${a.token}`, kind:'meme', name:a.name || a.symbol || short(a.token), chain:chainName(a), address:short(a.token), rawAddress:a.token, to:assetNavigationLink(a,{route,scope:scope.value}) }));
  return [...stocks,...memes].slice(0,8);
});
function close() { open.value = false; selected.value = 0; }
watch(()=>route.fullPath,close);
function move(delta) { if (!open.value) open.value = true; if (results.value.length) selected.value = Math.max(0,Math.min(results.value.length-1,selected.value+delta)); }
async function submit() {
  clearTimeout(searchTimer);
  if(searchLoading.value)await loadSearch();
  const exact = results.value.find(r => String(r.rawAddress).toLowerCase() === term.value.trim().toLowerCase());
  if (exact || results.value[selected.value]) router.push(exact ? exact.assetTo??exact.to : results.value[selected.value].to);
  else if(/^0x[0-9a-fA-F]{40}$/.test(term.value.trim())&&scope.value!=='all')router.push(assetNavigationLink({chainId:scope.value,token:term.value.trim().toLowerCase()},{route,scope:scope.value}));
  else router.push({path:'/meme',query:{q:term.value.trim(),chain:scope.value,rel:'all',fresh:'0'}});
  close(); input.value?.blur();
}
function onKey(event) { if (event.key === '/' && !/^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName ?? '')) { event.preventDefault(); input.value?.focus(); } }
function onOutside(event) { if (root.value && !root.value.contains(event.target)) close(); }
watch([term,scope],()=>{selected.value=0;searchPayload.value=null;searchRequest++;clearTimeout(searchTimer);if(term.value.trim()){searchLoading.value=true;searchTimer=setTimeout(loadSearch,180);}else searchLoading.value=false;},{immediate:true});
onMounted(() => {document.addEventListener('keydown',onKey);document.addEventListener('pointerdown',onOutside);});
onUnmounted(() => {searchRequest++;clearTimeout(searchTimer);document.removeEventListener('keydown',onKey);document.removeEventListener('pointerdown',onOutside);});
</script>
