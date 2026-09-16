<template>
  <section class="panel">
    <div class="page-heading">
      <h2>{{ tr('Meme 雷达', 'Meme Radar') }}</h2>
      <p>{{ tr('已核验配对与同名合约分组；缺失与历史值分别标记。', 'Verified pairs and same-name grouping; missing and historical values are marked separately.') }}</p>
    </div>
    <div class="tab-group">
      <button v-for="[id, label] in filters" :key="id" class="tab" :class="{ active: filter === id }" @click="setQuery({ filter: id, page: null })">{{ label }}</button>
      <HelpTip :title="tr('Meme 雷达', 'Meme Radar')" :body="tr('按已核验配对与流动性筛选；同名折叠，每个 CA 独立保存。', 'Filter by verified pairs and liquidity; same-name contracts are grouped, every CA is kept.')" />
    </div>
    <div class="x-toolbar">
      <input id="xMemeSearch" v-model="qInput" :placeholder="tr('名称、股票或 CA', 'Name, stock or CA')" @input="onSearch">
      <select id="v2Sort" v-model="sortInput" @change="setQuery({ sort: $event.target.value, page: null })" :aria-label="tr('排序', 'Sort')">
        <option value="volume24h">{{ tr('24h 成交额', '24h volume') }}</option>
        <option value="liquidity">{{ tr('资产总流动性', 'Asset liquidity') }}</option>
        <option value="marketCap">{{ tr('市值', 'Market cap') }}</option>
        <option value="holders">{{ tr('持币地址数', 'Holder addresses') }}</option>
        <option value="change24h">{{ tr('24h 涨跌', '24h change') }}</option>
        <option value="firstSeen">{{ tr('首次收录', 'First listed') }}</option>
      </select>
      <details class="v2-filters">
        <summary>{{ tr('更多筛选', 'More filters') }}</summary>
        <div class="v2-filter-fields">
          <select id="v2Chain" :value="route.query.chain ?? ''" @change="setQuery({ chain: $event.target.value || undefined, page: null })" :aria-label="tr('网络', 'Network')">
            <option value="">{{ tr('全部网络', 'All networks') }}</option>
            <option value="196">X Layer</option>
            <option value="56">BNB Chain</option>
            <option value="4663">Robinhood Chain</option>
          </select>
          <select id="v2Min" :value="route.query.minLiquidity ?? ''" @change="setQuery({ minLiquidity: $event.target.value || undefined, page: null })" :aria-label="tr('最低流动性', 'Min liquidity')">
            <option value="">{{ tr('最低流动性', 'Min liquidity') }}</option>
            <option value="1000">≥ $1,000</option>
            <option value="10000">≥ $10,000</option>
            <option value="100000">≥ $100,000</option>
          </select>
        </div>
      </details>
    </div>
    <p class="hint">{{ tr('重点关系：已核验且配对池流动性 ≥ $1,000。同名折叠，每个 CA 独立保存；交易活跃不代表安全。', 'Priority: verified pairs with pool liquidity ≥ $1,000. Same-name contracts are grouped, but every CA is retained; activity does not prove safety.') }}</p>
    <div class="scroll v2-table-scroll" id="xMemeRows">
      <div v-if="!pageGroups.length" class="x-empty">{{ tr('当前筛选暂无资产。可切换“全部资产”查看热门榜和已发现的股票配对候选。', 'No assets match this filter. Switch to All assets for the hot list and discovered stock-pair candidates.') }}</div>
      <table v-else class="tbl v2-meme-table">
        <thead>
          <tr>
            <th>{{ tr('资产', 'Asset') }}</th>
            <th>{{ tr('关联股票', 'Related stock') }}</th>
            <th>{{ tr('资产总流动性', 'Asset liquidity') }}</th>
            <th>{{ tr('资产 24h 成交额', 'Asset 24h volume') }}</th>
            <th>{{ tr('24h 成交次数', '24h trades') }}</th>
            <th>{{ tr('Meme 价格', 'Meme price') }}</th>
            <th>{{ tr('持币地址数', 'Holder addresses') }}</th>
            <th>{{ tr('行情更新时间', 'Quote time') }}</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="g in pageGroups" :key="g.symbol">
            <MemeRow :a="g.members[0]" :relations="relations" :store="store" :extra="g.members.length > 1 ? g.members.length - 1 : 0" @toggle-group="toggleGroup(g.symbol)" />
            <template v-if="openGroups.has(g.symbol)">
              <MemeRow v-for="m in g.members.slice(1)" :key="m.token" :a="m" :relations="relations" :store="store" :child="true" />
            </template>
          </template>
        </tbody>
      </table>
    </div>
    <div class="x-pager">
      <button :disabled="safePage <= 0" @click="setQuery({ page: Math.max(0, safePage - 1) })">{{ tr('上一页', 'Previous') }}</button>
      <span>{{ safePage + 1 }} / {{ pages }} · {{ groups.length }} {{ tr('组', 'groups') }}</span>
      <button :disabled="(safePage + 1) >= pages" @click="setQuery({ page: safePage + 1 })">{{ tr('下一页', 'Next') }}</button>
    </div>
  </section>
</template>

<script setup>
import { computed, reactive, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import HelpTip from '../components/HelpTip.vue';
import MemeRow from '../components/MemeRow.vue';
import { useDashboardStore } from '../stores/dashboard';
import { tr } from '../i18n';
import { fresh, chain } from '../utils/format';

const route = useRoute();
const router = useRouter();
const store = useDashboardStore();
const qInput = ref(route.query.q ?? '');
const sortInput = ref(route.query.sort ?? 'volume24h');
const openGroups = reactive(new Set());

const filter = computed(() => route.query.filter ?? 'related');
const search = computed(() => String(route.query.q ?? '').toLowerCase());
const sort = computed(() => route.query.sort ?? 'volume24h');
const page = computed(() => Math.max(0, Number(route.query.page) || 0));

const filters = computed(() => [
  ['related', tr('配对池≥$1,000', 'Pools at least $1,000')],
  ['verified', tr('已确认配对', 'Confirmed pairs')],
  ['name', tr('仅名称匹配', 'Name match only')],
  ['all', tr('全部资产', 'All assets')],
  ['history', tr('行情较旧', 'Stale quotes')],
]);

function relationsOf(a) {
  return store.relations.filter((r) => r.token === a.token);
}
function verifiedOf(a) {
  return relationsOf(a).filter((r) => r.status === 'verified' && Date.now() - r.checkedAt < 3600000);
}

const groups = computed(() => {
  const bySymbol = new Map();
  for (const a of store.assets) {
    const rs = verifiedOf(a);
    if (route.query.chain && chain(a) !== route.query.chain) continue;
    const term = [a.name, a.symbol, a.token, ...relationsOf(a).map((r) => r.ticker)].join(' ').toLowerCase();
    if (search.value && !term.includes(search.value)) continue;
    if (Number(route.query.minLiquidity ?? 0) > (a.liquidity ?? -1)) continue;
    const f = filter.value;
    if (f === 'related' && !rs.some((r) => (r.liquidityUsd ?? 0) >= 1000 && fresh(r.liquidityAt ?? r.checkedAt))) continue;
    if (f === 'verified' && !rs.length) continue;
    if (f === 'name' && !(a.match && !rs.length)) continue;
    if (f === 'history' && fresh(a.fieldTimes?.price ?? a.updatedAt)) continue;
    const key = String(a.symbol ?? '?').toUpperCase();
    if (!bySymbol.has(key)) bySymbol.set(key, []);
    bySymbol.get(key).push(a);
  }
  const out = [];
  for (const [symbol, members] of bySymbol) {
    const poolActivity = (a) => Math.max(0, ...relationsOf(a).filter((r) => r.status === 'verified').map((r) => Number(r.liquidityUsd ?? 0) || 0));
    const relSort = (filter.value === 'related' || filter.value === 'verified') && sort.value === 'volume24h';
    members.sort((a, b) => {
      const av = relSort ? poolActivity(a) : a[sort.value];
      const bv = relSort ? poolActivity(b) : b[sort.value];
      return (bv == null ? -Infinity : Number(bv)) - (av == null ? -Infinity : Number(av)) || a.token.localeCompare(b.token);
    });
    out.push({ symbol, members });
  }
  out.sort((a, b) => {
    const poolActivity = (m) => Math.max(0, ...relationsOf(m).filter((r) => r.status === 'verified').map((r) => Number(r.liquidityUsd ?? 0) || 0));
    const relSort = (filter.value === 'related' || filter.value === 'verified') && sort.value === 'volume24h';
    const av = relSort ? poolActivity(a.members[0]) : a.members[0][sort.value];
    const bv = relSort ? poolActivity(b.members[0]) : b.members[0][sort.value];
    return (bv == null ? -Infinity : Number(bv)) - (av == null ? -Infinity : Number(av)) || a.symbol.localeCompare(b.symbol);
  });
  return out;
});

const PAGE_SIZE = 20;
const pages = computed(() => Math.max(1, Math.ceil(groups.value.length / PAGE_SIZE)));
const safePage = computed(() => Math.min(page.value, pages.value - 1));
const pageGroups = computed(() => groups.value.slice(safePage.value * PAGE_SIZE, safePage.value * PAGE_SIZE + PAGE_SIZE));

function setQuery(patch) {
  router.push({ query: { ...route.query, ...patch, page: patch.page ?? undefined } });
}

function toggleGroup(symbol) {
  openGroups.has(symbol) ? openGroups.delete(symbol) : openGroups.add(symbol);
}

let searchTimer;
function onSearch(e) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => setQuery({ q: e.target.value || undefined, page: null }), 350);
}
</script>
