<template>
  <section class="pool-list">
    <div class="pool-toolbar">
      <label>{{ tr('排序', 'Sort') }} <select :value="sort" @change="setQuery({sort:$event.target.value})">
        <option value="liquidity">{{ tr('池流动性', 'Pool liquidity') }}</option>
        <option value="volume24h">{{ tr('池 24h 成交', 'Pool 24h volume') }}</option>
        <option value="createdAt">{{ tr('创建时间', 'Created') }}</option>
      </select></label>
      <label><input type="checkbox" :checked="qualified" @change="setQuery({qualified:$event.target.checked?'1':undefined})"> {{ tr('流动性 ≥ $1K · 15 分钟内', 'Liquidity ≥ $1K · within 15m') }}</label>
      <span v-if="!loading" class="pool-count">{{ tr('显示', 'Showing') }} {{ num(pageRows.length) }} / {{ tr('共', 'of') }} {{ num(rows.length) }}</span>
    </div>
    <p v-if="search" class="pool-note">{{ tr('搜索', 'Search') }}：{{ search }} <button @click="setQuery({q:undefined})">{{ tr('清除', 'Clear') }}</button></p>
    <div v-if="loading" class="pool-skeleton" role="status" :aria-label="tr('加载交易池', 'Loading pools')"><p class="pool-note">{{ store.error?tr('池子列表暂时无法加载。','The pool list could not be loaded.'):tr('正在加载池子列表…','Loading the pool list…') }} <button type="button" @click="store.poll({view:'market'})">{{ tr('重试','Retry') }}</button></p><div v-for="n in 6" :key="n" class="skeleton-line"></div></div>
    <div v-else-if="!pageRows.length" class="x-empty">{{ tr('当前条件下暂无交易池。', 'No pools match these filters.') }} <button @click="setQuery({qualified:undefined,q:undefined})">{{ tr('清除筛选', 'Clear filters') }}</button></div>
    <template v-else>
      <div class="pool-desktop scroll">
        <table class="tbl pool-table">
          <thead><tr><th>Meme</th><th>{{ tr('股票', 'Stock') }}</th><th>{{ tr('链', 'Chain') }}</th><th>DEX</th><th class="numeric">{{ tr('池流动性', 'Pool liquidity') }}</th><th>{{ tr('创建时间', 'Created') }}</th><th class="numeric">{{ tr('池 24h 成交', 'Pool 24h volume') }}</th><th>{{ tr('池地址', 'Pool') }}</th></tr></thead>
          <tbody><tr v-for="row in pageRows" :key="row.key">
            <td><RouterLink :to="assetLink(row)"><strong>{{ row.asset?.symbol || short(row.relation.token) }}</strong></RouterLink><small>{{ row.asset?.name || short(row.relation.token) }}</small></td>
            <td><RouterLink :to="stockLink(row)">{{ row.relation.ticker || '—' }}</RouterLink></td>
            <td>{{ chainLabel(row.chainId) }}</td>
            <td>{{ row.relation.protocol || tr('未能识别', 'Unknown') }}</td>
            <td class="numeric" :title="liquidityTitle(row)"><LiveNumber :value="row.liquidity" /><small v-if="row.liquidity != null && !row.liquidityCurrent" class="historical">{{ tr('历史值', 'Historical') }}</small></td>
            <td :title="date(row.createdAt)">{{ row.createdAt ? age(row.createdAt) : tr('暂无数据', 'Unavailable') }}</td>
            <td class="numeric" :title="volumeTitle(row)"><LiveNumber :value="row.volume24h" /><small v-if="row.volume24h != null && !row.volumeCurrent" class="historical">{{ tr('历史值', 'Historical') }}</small></td>
            <td><RouterLink :to="assetLink(row, true)">{{ short(row.relation.pool) }}</RouterLink></td>
          </tr></tbody>
        </table>
      </div>
      <div class="pool-mobile">
        <article v-for="row in pageRows" :key="row.key" class="pool-card">
          <div><RouterLink :to="assetLink(row)"><strong>{{ row.asset?.symbol || short(row.relation.token) }}</strong></RouterLink><span>{{ chainLabel(row.chainId) }}</span></div>
          <p><RouterLink :to="stockLink(row)">{{ row.relation.ticker }}</RouterLink> · {{ row.relation.protocol || tr('未能识别', 'Unknown') }}</p>
          <dl><div><dt>{{ tr('池流动性', 'Pool liquidity') }}</dt><dd :title="liquidityTitle(row)">{{ usd(row.liquidity) }}<small v-if="row.liquidity != null && !row.liquidityCurrent">{{ tr('历史值', 'Historical') }}</small></dd></div><div><dt>{{ tr('池 24h 成交', 'Pool 24h volume') }}</dt><dd :title="volumeTitle(row)">{{ usd(row.volume24h) }}<small v-if="row.volume24h != null && !row.volumeCurrent">{{ tr('历史值', 'Historical') }}</small></dd></div><div><dt>{{ tr('创建时间', 'Created') }}</dt><dd>{{ row.createdAt ? age(row.createdAt) : tr('暂无数据', 'Unavailable') }}</dd></div></dl>
          <RouterLink :to="assetLink(row, true)" class="pool-evidence">{{ short(row.relation.pool) }} · {{ tr('关系证据', 'Relationship') }} →</RouterLink>
        </article>
      </div>
      <div class="x-pager"><button :disabled="safePage===0" @click="setQuery({page:safePage-1})">{{ tr('上一页', 'Previous') }}</button><span>{{ safePage+1 }} / {{ pages }}</span><button :disabled="safePage+1>=pages" @click="setQuery({page:safePage+1})">{{ tr('下一页', 'Next') }}</button></div>
    </template>
    <p class="pool-note">{{ tr('每个池子单独统计；创建时间未知时显示“暂无数据”。', 'Each pool is counted separately. Unknown creation times are unavailable.') }}</p>
  </section>
</template>

<script setup>
import { computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useDashboardStore } from '../stores/dashboard';
import { useMinuteClock } from '../composables/useMinuteClock';
import { tr } from '../i18n';
import { age, date, num, short, usd } from '../utils/format';
import { marketCatalogReady } from '../utils/meme-filter-model';
import { buildPoolRows, normalizeTicker } from '../utils/stock-theme-model';
import LiveNumber from '../components/LiveNumber.vue';

const props = defineProps({ scope:{ type:String, default:'all' }, qualified:Boolean });
const route = useRoute(), router = useRouter(), store = useDashboardStore(), now = useMinuteClock();
const search = computed(() => String(route.query.q ?? ''));
const sort = computed(() => ['liquidity','volume24h','createdAt'].includes(route.query.sort) ? route.query.sort : 'liquidity');
const loading = computed(() => !marketCatalogReady(store.snapshot));
const rows = computed(() => buildPoolRows(store.relations, store.assets, { scope:props.scope, qualified:props.qualified, q:search.value, now:now.value }).sort((a, b) => {
  const current = row => sort.value === 'liquidity' ? row.liquidityCurrent : sort.value === 'volume24h' ? row.volumeCurrent : true;
  return Number(current(b)) - Number(current(a)) || Number(b[sort.value] != null) - Number(a[sort.value] != null)
    || (b[sort.value] ?? 0) - (a[sort.value] ?? 0) || a.key.localeCompare(b.key);
}));
const PAGE_SIZE = 30;
const pages = computed(() => Math.max(1, Math.ceil(rows.value.length / PAGE_SIZE)));
const safePage = computed(() => Math.min(Math.max(0, Number(route.query.page) || 0), pages.value-1));
const pageRows = computed(() => rows.value.slice(safePage.value*PAGE_SIZE, (safePage.value+1)*PAGE_SIZE));
function setQuery(patch) { router.push({ query:{ ...route.query, ...patch, page:patch.page || undefined } }); }
function assetLink(row, relation = false) { return { path:`/asset/${row.chainId}/${row.relation.token}`, query:{ chain:props.scope, from:'meme', ...(relation ? { tab:'relation', pool:row.relation.pool } : {}) } }; }
function stockLink(row) { return { path:`/stock/${encodeURIComponent(normalizeTicker(row.relation.ticker))}`, query:{ chain:props.scope } }; }
function chainLabel(chain) { return { '56':'BNB Chain', '196':'X Layer', '4663':'Robinhood Chain' }[chain] ?? chain; }
function liquidityTitle(row) { return `${row.relation.liquiditySource ?? row.relation.provider ?? '—'} · ${date(row.relation.liquidityAt)}`; }
function volumeTitle(row) { return `${row.relation.poolMarket?.provider ?? row.relation.poolMarket?.source ?? '—'} · ${date(row.volumeAt)}`; }
</script>

<style scoped>
.pool-toolbar { display:flex; align-items:center; flex-wrap:wrap; gap:14px; margin-bottom:16px; }
.pool-toolbar label { display:flex; align-items:center; gap:7px; font-size:12px; color:var(--muted); }
.pool-toolbar select { min-height:34px; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:5px 8px; }
.pool-count { margin-left:auto; color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
.pool-table td { vertical-align:middle; padding-top:13px; padding-bottom:13px; }
.pool-table th { font-size:12px; }
.pool-table td small { display:block; color:var(--muted); font-size:12px; }
.pool-table td.numeric, .pool-table th.numeric { text-align:right; font-family:ui-monospace,monospace; font-variant-numeric:tabular-nums; }
.pool-table .historical { color:var(--warning); }
.pool-table a { color:var(--text); text-decoration:underline; text-underline-offset:3px; }
.pool-note { color:var(--muted); font-size:12px; margin:10px 0; }
.pool-mobile { display:none; }
@media(max-width:760px) {
  .pool-desktop { display:none; }
  .pool-mobile { display:grid; gap:10px; }
  .pool-card { padding:14px; border:1px solid var(--border); border-radius:8px; background:var(--bg); }
  .pool-card > div { display:flex; justify-content:space-between; align-items:center; gap:12px; }
  .pool-card > div span, .pool-card p, .pool-card dt { color:var(--muted); font-size:12px; }
  .pool-card p { margin:4px 0 12px; }
  .pool-card a { color:var(--text); }
  .pool-card dl { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .pool-card dd { font-family:ui-monospace,monospace; margin-top:3px; }
  .pool-card dd small { display:block; color:var(--warning); font-size:12px; }
  .pool-evidence { display:block; border-top:1px solid var(--border); padding-top:10px; margin-top:12px; font-size:12px; }
  .pool-count { width:100%; margin-left:0; }
}
</style>
