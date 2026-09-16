<template>
  <div>
    <div class="page-heading">
      <h2>{{ tr('股票雷达', 'Stock radar') }}</h2>
      <p>{{ tr('按股票代码聚合发行方与合约。股票现货价缺失时保留原始空值。', 'Grouped by ticker across issuers and contracts. Missing spot prices remain unknown.') }}</p>
    </div>
    <div class="v2-stock-toolbar">
      <input id="xStockSearch" v-model="qInput" :placeholder="tr('搜索股票代码、名称、CA', 'Search ticker, name or CA')" @input="onSearch">
    </div>
    <div id="xStockRows">
      <div v-if="!rows.length" class="x-empty">{{ tr('暂无匹配股票。', 'No matching stocks.') }}</div>
      <details v-for="[ticker, list] in rows" :key="ticker" class="x-stock" :open="autoOpen(ticker)">
        <summary>
          <strong>{{ companyTitle(ticker, list[0]) }}</strong>
          <span>{{ list.length }} {{ tr('个股票资产', 'stock assets') }}</span>
          <span>{{ relatedCount(list) }} {{ tr('个关联资产', 'related assets') }}</span>
        </summary>
        <h3>{{ tr('关联 Meme', 'Related memes') }}</h3>
        <div v-if="relLinks(list).length">
          <RouterLink v-for="r in relLinks(list)" :key="r.id" class="x-signal" :to="pairLink(r)">
            <strong>{{ assetSymbol(r.token) }}</strong>
            <span>{{ chainName(r) }} · {{ r.status === 'verified' ? tr('已核验', 'Verified') : '?' }}</span>
            <span>{{ usd(r.liquidityUsd) }}</span>
          </RouterLink>
        </div>
        <div v-else class="x-empty">{{ tr('尚未发现已核验配对。', 'No verified pairs discovered yet.') }}</div>
        <details class="v2-versions">
          <summary>{{ tr('代币版本', 'Token versions') }}</summary>
          <div class="scroll">
            <table class="tbl">
              <thead><tr><th>{{ tr('股票代币 / 网络', 'Stock token / network') }}</th><th>{{ tr('发行方', 'Issuer') }}</th><th>{{ tr('代币价格', 'Token price') }}</th><th>{{ tr('参考股价', 'Stock reference') }}</th><th>{{ tr('链上价 vs 股价', 'on-chain vs stock') }}</th><th>{{ tr('24h 成交额 / 市场', '24h volume / market') }}</th><th>{{ tr('操作', 'Actions') }}</th></tr></thead>
              <tbody>
                <tr v-for="s in list" :key="s.assetId ?? s.tokenSymbol" :data-asset="s.assetId ?? s.tokenContractAddress ?? s.instrumentId">
                  <td>{{ s.tokenSymbol }}<small>{{ chainName(s) }} · {{ short(s.tokenContractAddress ?? s.instrumentId ?? '') }}</small></td>
                  <td>{{ s.issuer }}<small>{{ (s.providers ?? [s.provider]).join(' / ') }}</small></td>
                  <td data-field="price">{{ usd(s.price) }}</td>
                  <td data-field="stockPrice">{{ usd(s.stockPrice) }}</td>
                  <td data-field="premium">{{ pct(s.premium?.value) }}</td>
                  <td data-field="volume24h">{{ usd(s.volume24h) }}</td>
                  <td><RouterLink :to="pairLinkFor(s)">{{ tr('交易池分析', 'Pool analysis') }} →</RouterLink></td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
      </details>
    </div>
    <div class="x-pager">
      <button :disabled="safePage <= 0" @click="setPage(Math.max(0, safePage - 1))">{{ tr('上一页', 'Previous') }}</button>
      <span>{{ safePage + 1 }} / {{ pages }} · {{ allRows.length }} {{ tr('个股票标的', 'stock underlyings') }}</span>
      <button :disabled="(safePage + 1) >= pages" @click="setPage(safePage + 1)">{{ tr('下一页', 'Next') }}</button>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useDashboardStore } from '../stores/dashboard';
import { tr } from '../i18n';
import { chainName, pct, pairLink, short, usd } from '../utils/format';

const route = useRoute();
const router = useRouter();
const store = useDashboardStore();
const qInput = ref(route.query.q ?? '');

const COMPANY = { AAPL: '苹果', AMD: '超威半导体', AMZN: '亚马逊', BABA: '阿里巴巴', COIN: 'Coinbase', GME: '游戏驿站', GOOGL: '谷歌', HOOD: 'Robinhood', INTC: '英特尔', META: 'Meta', MSFT: '微软', NVDA: '英伟达', NFLX: '奈飞', QQQ: '纳斯达克100指数ETF', SPY: '标普500指数ETF', TSLA: '特斯拉', TSM: '台积电', MU: '美光科技', PLTR: '帕兰提尔', PYPL: '贝宝', QCOM: '高通', CRM: '赛富时', ORCL: '甲骨文', SNDK: '闪迪', SOXL: '半导体三倍做多ETF' };

const search = computed(() => String(route.query.q ?? '').toLowerCase());

const PAGE = 20;
const page = computed(() => Math.max(0, Number(route.query.page) || 0));
const allRows = computed(() => {
  const grouped = new Map();
  for (const s of store.stockTokens) {
    const id = s.stockIdentity?.id || s.stockCode || s.assetId || s.instrumentId;
    if (search.value && ![s.stockCode, s.tokenSymbol, s.tokenName, s.tokenContractAddress, s.stockIdentity?.nameZh, s.stockIdentity?.nameEn, s.stockIdentity?.code].filter(Boolean).join(' ').toLowerCase().includes(search.value)) continue;
    if (!grouped.has(id)) grouped.set(id, []);
    grouped.get(id).push(s);
  }
  return [...grouped.entries()].sort((a, b) => relatedCount(b[1]) - relatedCount(a[1]) || String(a[0] ?? "").localeCompare(String(b[0] ?? "")));
});
const pages = computed(() => Math.max(1, Math.ceil(allRows.value.length / PAGE)));
const safePage = computed(() => Math.min(page.value, pages.value - 1));
const rows = computed(() => allRows.value.slice(safePage.value * PAGE, safePage.value * PAGE + PAGE));

function relatedCount(list) {
  return new Set(
    store.relations
      .filter((r) => r.status === 'verified' && list.some((s) => s.tokenContractAddress?.toLowerCase() === r.stock && String(s.chainId ?? s.chainIndex ?? '196') === String(r.chainId ?? '196')))
      .map((r) => r.token),
  ).size;
}

function relLinks(list) {
  return store.relations.filter((r) => list.some((s) => s.tokenContractAddress?.toLowerCase() === r.stock));
}

function assetSymbol(token) {
  return store.assets.find((a) => a.token === token)?.symbol ?? short(token);
}

function companyTitle(ticker, first) {
  const id = first?.stockIdentity;
  if (id?.status === 'identified' && id.nameZh) return `${id.nameZh} · ${id.code}`;
  if (/^\d+$/.test(ticker)) return `${first?.tokenName ?? first?.tokenSymbol} · ${tr('名称待核实', 'Name unresolved')}`;
  const zh = COMPANY[ticker];
  return zh ? `${ticker} · ${zh}` : ticker;
}

function autoOpen(ticker) {
  const key = String(ticker ?? '');
  return (search.value && allRows.value.length === 1) || search.value === key.toLowerCase();
}

function setPage(p) {
  router.push({ query: { ...route.query, page: p || undefined } });
}

function pairLinkFor(s) {
  if (s.tokenContractAddress) return { path: `/pair/${String(s.chainId ?? '196')}/${s.tokenContractAddress}`, query: {} };
  return { path: '/pair/196/0x0', query: {} };
}

let searchTimer;
function onSearch(e) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => router.push({ query: { ...route.query, q: e.target.value || undefined } }), 350);
}
</script>
