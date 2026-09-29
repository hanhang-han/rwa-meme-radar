<template>
  <div>
    <div class="page-heading">
      <RouterLink v-if="detailMode" :to="{path:'/stock',query:{chain:scope}}">← {{ tr('返回股票', 'Back to stocks') }}</RouterLink>
      <h2>{{ detailMode ? tr('股票详情', 'Stock detail') : tr('股票', 'Stocks') }}</h2>
    </div>

    <div v-if="!detailMode" class="v2-stock-toolbar v3-theme-toolbar">
      <input id="xStockSearch" v-model="qInput" :placeholder="tr('搜索股票、名称或合约地址', 'Search ticker, name or contract')" @input="onSearch">
      <select v-model="sortBy" :aria-label="tr('关联资产排序', 'Sort related assets')">
        <option value="poolLiquidity">{{ tr('按配对池规模', 'Pair-pool size') }}</option>
        <option value="volume24h">{{ tr('按资产 24h 成交额', 'Asset 24h volume') }}</option>
        <option value="change24h">{{ tr('按资产 24h 涨跌', 'Asset 24h change') }}</option>
        <option value="firstSeen">{{ tr('按关系首次发现', 'First relationship detection') }}</option>
      </select>
      <label class="v3-check"><input v-model="includeHistorical" type="checkbox"> {{ tr('展示历史池估值', 'Include historical pool estimates') }}</label>
    </div>

    <div id="xStockRows" class="v3-theme-list">
      <div v-if="!cards.length" class="x-empty">{{ !store.snapshot ? tr('正在加载股票行情…', 'Loading stock quotes…') : store.snapshot.unified?.snapshotScope === 'overview' ? tr('股票列表仍在同步。', 'The stock list is still syncing.') : tr('暂无匹配股票主题。', 'No matching stock themes.') }}</div>
      <div v-else-if="!detailMode" class="stock-summary-head"><span>{{ tr('股票','Stock') }}</span><span>{{ tr('价格','Price') }}</span><span>24h</span><span>Meme</span><span>{{ tr('Meme 24h 成交','Meme volume') }}</span><span>{{ tr('配对池流动性','Pair liquidity') }}</span></div>
      <details v-for="card in cards" :key="card.ticker" class="x-stock v3-theme-card" :open="detailMode || autoOpen(card.ticker)">
        <summary class="stock-summary-row" :aria-label="detailMode ? undefined : tr('查看股票详情', 'View stock detail') + ' ' + card.ticker" @click="onStockSummary($event, card)">
          <strong>{{ companyTitle(card.ticker, card.list[0]) }}</strong>
          <span><small class="stock-mobile-label">{{ tr('价格','Price') }}</small><LiveNumber :value="officialStock(card)?.price" :currency="officialStock(card)?.priceCurrency ?? ''" format="price" /><small v-if="officialStock(card)?.price != null" class="stock-quote-source">{{ chainName(officialStock(card)) }} · {{ officialStock(card)?.provider ?? tr('来源待核验','Source unverified') }}</small></span>
          <span :class="Number(officialStock(card)?.change24h)>0?'up':Number(officialStock(card)?.change24h)<0?'down':''"><small class="stock-mobile-label">24h</small><LiveNumber :value="officialStock(card)?.change24h" format="percent" /></span>
          <span><small class="stock-mobile-label">Meme</small>{{ card.themeRows.length }} Meme</span><span><small class="stock-mobile-label">{{ tr('Meme 24h 成交','Meme volume') }}</small>{{ usd(cardMemeVolume(card)) }}</span><span><small class="stock-mobile-label">{{ tr('配对池流动性','Pair liquidity') }}</small>{{ usd(cardPoolLiquidity(card)) }}</span>
        </summary>

        <section class="v3-theme-body">
          <div class="panel-head v3-theme-head">
            <div>
              <h3>{{ tr('关联资产比较', 'Related asset comparison') }}</h3>
              <p class="hint">{{ tr('每个资产选择一个代表池：优先新鲜估值，再按规模选择。资产成交额不等于这个配对池的成交额。', 'One representative pool per asset: fresh estimates first, then size. Asset-wide volume is not this pair pool’s volume.') }}</p>
            </div>
            <div class="v3-theme-stats">
              <span>{{ card.themeRows.length }} {{ tr('资产', 'assets') }}</span>
              <span>{{ poolCount(card) }} {{ tr('已核验池', 'verified pools') }}</span>
            </div>
          </div>

          <div v-if="card.visibleRows.length" class="scroll v3-theme-scroll">
            <table class="tbl v3-theme-table">
              <thead>
                <tr>
                  <th>{{ tr('关联资产', 'Related asset') }}</th>
                  <th>{{ tr('关系证据', 'Relationship evidence') }}</th>
                  <th>{{ tr('股票配对池规模', 'Stock pair-pool size') }}</th>
                  <th>{{ tr('资产 24h 成交额', 'Asset 24h volume') }}</th>
                  <th>{{ tr('资产 24h 涨跌', 'Asset 24h change') }}</th>
                  <th>{{ tr('较股票涨幅 · 1h', 'Vs stock return · 1h') }}</th>
                  <th>{{ tr('操作', 'Action') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in card.visibleRows" :key="row.key">
                  <td>
                    <strong class="v3-token-name">{{ row.asset?.symbol ?? short(row.relation.token) }}</strong>
                    <small>{{ chainName(row.relation) }} · {{ short(row.relation.token) }}</small>
                  </td>
                  <td>
                    <span class="v3-proof-kind">{{ relationType(row.relation) }}</span>
                    <small>{{ row.relation.protocol || tr('协议待核实', 'Protocol pending') }} · {{ short(row.relation.pool) }}</small>
                  </td>
                  <td>
                    <strong><LiveNumber :value="row.relation.liquidityUsd" /></strong>
                    <span class="v3-state" :class="`is-${row.liquidityState}`">{{ poolStateLabel(row) }}</span>
                    <div class="v3-pool-bar"><i :style="{ width: poolBarWidth(card.poolBarMax, row) + '%' }"></i></div>
                    <small>{{ age(row.relation.liquidityAt) }}<template v-if="row.poolCount > 1"> · {{ row.poolCount }} {{ tr('个池，当前显示代表池', 'pools; representative shown') }}</template></small>
                  </td>
                  <td>
                    <strong><LiveNumber :value="row.asset?.volume24h" :currency="row.asset?.volumeCurrency ?? row.asset?.priceCurrency" /></strong>
                    <small>{{ row.asset?.provider ?? row.asset?.source ?? tr('来源待核实', 'Source pending') }} · {{ age(fieldObservedAt(row.asset, 'volume24h')) }}</small>
                  </td>
                  <td>
                    <strong :class="Number(row.asset?.change24h) > 0 ? 'up' : Number(row.asset?.change24h) < 0 ? 'down' : ''"><LiveNumber :value="row.asset?.change24h" format="percent" /></strong>
                    <small>{{ age(fieldObservedAt(row.asset, 'change24h')) }}</small>
                  </td>
                  <td><ComparisonMetric :value="relativeMetric(row.relation)" unit="pp" /></td>
                  <td><RouterLink class="v3-action" :to="pairLink(row.relation)">{{ tr('价格与配对分析', 'Price and pair analysis') }} →</RouterLink></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else-if="card.themeRows.length" class="x-empty">{{ tr('这个主题目前没有 15 分钟内的配对池估值。勾选“展示历史池估值”可以查看已有证据。', 'This theme has no pair-pool estimate from the last 15 minutes. Include historical estimates to view existing evidence.') }}</div>
          <div v-else class="x-empty">{{ tr('尚未发现已核验配对。', 'No verified pairs discovered yet.') }}</div>

          <details class="v2-versions v3-versions">
            <summary>{{ card.list.length }} {{ tr('个股票代币版本与参考行情', 'stock-token versions and reference quotes') }}</summary>
            <div class="scroll">
              <table class="tbl">
                <thead><tr><th>{{ tr('股票代币 / 网络', 'Stock token / network') }}</th><th>{{ tr('发行方', 'Issuer') }}</th><th>{{ tr('代币价格', 'Token price') }}</th><th>{{ tr('参考股价', 'Stock reference') }}</th><th>{{ tr('链上价 vs 股价', 'On-chain vs stock') }}</th><th>{{ tr('24h 成交额 / 市场', '24h volume / market') }}</th><th>{{ tr('操作', 'Actions') }}</th></tr></thead>
                <tbody>
                  <tr v-for="s in card.list" :key="s.assetId ?? s.tokenSymbol" :data-asset="s.assetId ?? s.tokenContractAddress ?? s.instrumentId">
                    <td>{{ s.tokenSymbol }}<small>{{ chainName(s) }} · {{ short(s.tokenContractAddress ?? s.instrumentId ?? '') }}</small></td>
                    <td>{{ s.issuer }}<small>{{ s.issuerIdentity?.verificationStatus === 'official' ? tr('身份已核验','Identity verified') : s.issuerIdentity?.verificationStatus === 'legacy' ? tr('旧版官方部署','Legacy official deployment') : tr('身份未核验','Unverified identity') }}</small></td>
                    <td data-field="price"><LiveNumber :value="s.price" :currency="s.priceCurrency" format="price" /><QuoteStatus :row="s" /></td>
                    <td data-field="stockPrice"><LiveNumber :value="s.stockPrice" :currency="s.referenceCurrency" format="price" /><QuoteStatus :row="s" reference /></td>
                    <td data-field="premium"><ComparisonMetric :value="comparisons.latest.get(`${s.chainId}:${s.tokenContractAddress?.toLowerCase()}`)?.premium ?? s.premium" /></td>
                    <td data-field="volume24h"><LiveNumber :value="s.volume24h" :currency="s.volumeCurrency ?? s.priceCurrency" /></td>
                    <td><RouterLink :to="pairLinkFor(s)">{{ tr('交易池分析', 'Pool analysis') }} →</RouterLink></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </details>
        </section>
      </details>
    </div>

    <div v-if="!detailMode" class="x-pager">
      <button :disabled="safePage <= 0" @click="setPage(Math.max(0, safePage - 1))">{{ tr('上一页', 'Previous') }}</button>
      <span>{{ safePage + 1 }} / {{ pages }} · {{ allRows.length }} {{ tr('个股票主题', 'stock themes') }}</span>
      <button :disabled="(safePage + 1) >= pages" @click="setPage(safePage + 1)">{{ tr('下一页', 'Next') }}</button>
    </div>
    <section v-if="!detailMode" class="panel stock-themes-index"><h2>{{ tr('主题指数','Theme indexes') }}</h2><div class="v2-baskets"><details v-for="sector in sectors" :key="sector.chainId+':'+sector.sector+':'+sector.basketVersion" class="x-basket"><summary class="stock-basket-summary"><strong>{{ themeLabel(sector.sector, lang.lang) }} <small>{{ sector.scopeLabel || chainName(sector) }}</small><small v-if="sector.dataStatus !== 'current'" class="basket-reason">{{ tr(sector.reason || '指数暂停，等待有效行情', 'Paused: qualifying assets or current quotes are insufficient') }}</small></strong><span :class="{ 'is-stale':sector.dataStatus !== 'current' }">{{ sector.dataStatus === 'current' && sector.value != null ? num(sector.value) : '—' }}</span><small>{{ tr('有效行情','Current quotes') }} {{ num(sector.quoteCoverage?.fresh) }} / {{ num(sector.quoteCoverage?.total) }}<br>{{ tr('最后有效','Last valid') }} {{ date(sector.dataStatus === 'current' ? sector.at : sector.lastAt) }}</small></summary><div class="stock-basket-meta">{{ tr('基期','Base') }} {{ date(sector.baseAt) }} · {{ num(sector.members) }} {{ tr('个成分','constituents') }} · {{ tr('篮子版本','Basket version') }} {{ sector.basketVersion ?? '—' }} · {{ tr('方法版本','Method version') }} {{ sector.methodVersion ?? '—' }}</div><p v-for="part in sector.components??[]" :key="part.token"><RouterLink :to="'/detail/'+sector.chainId+'/'+part.token">{{ part.symbol }}</RouterLink> {{ part.weight!=null?pct(part.weight*100):'' }}</p></details></div><p v-if="!sectors.length" class="x-empty">{{ tr('暂无可发布主题指数；成分证据或有效行情不足时不会显示指数值。','No publishable theme indexes; values remain unavailable without qualifying constituents and current quotes.') }}</p></section>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import LiveNumber from '../components/LiveNumber.vue';
import QuoteStatus from '../components/QuoteStatus.vue';
import ComparisonMetric from '../components/ComparisonMetric.vue';
import { useComparisonStore } from '../stores/comparisons';
import { useDashboardStore } from '../stores/dashboard';
import { tr, useI18n } from '../i18n';
import { age, chainName, date, num, pairLink, pct, short, usd } from '../utils/format';
import { chainScope, inChainScope } from '../utils/chain-scope';
import { assetKey } from '../utils/realtime';
import { buildThemeAssetMap, buildThemeRows, countThemeRows, fieldObservedAt, sortThemeRows } from '../utils/theme-presentation';
import { preferredOfficialStock } from '../utils/product-labels';
import { themeLabel } from '../utils/theme-labels';

const route = useRoute();
const router = useRouter();
const store = useDashboardStore();
const { lang } = useI18n();
const detailMode = computed(() => route.name === 'stockDetail');
const scope = computed(() => chainScope(route.query));
const sectors = computed(() => (store.snapshot?.unified?.sectors ?? []).filter(row => inChainScope(row,scope.value)));
const comparisons = useComparisonStore();
function relativeMetric(relation) {
  const packet = comparisons.latest.get(`${relation.chainId}:${relation.stock?.toLowerCase()}`);
  const pair = packet?.pairs?.find(p => p.pool.toLowerCase() === relation.pool.toLowerCase());
  return pair?.relative?.['1h'] ?? relation.priceComparison?.relative?.['1h'];
}
const qInput = ref(route.query.q ?? '');
const sortBy = ref('poolLiquidity');
const includeHistorical = ref(true);

const COMPANY = { AAPL: '苹果', AMD: '超威半导体', AMZN: '亚马逊', BABA: '阿里巴巴', COIN: 'Coinbase', GME: '游戏驿站', GOOGL: '谷歌', HOOD: 'Robinhood', INTC: '英特尔', META: 'Meta', MSFT: '微软', NVDA: '英伟达', NFLX: '奈飞', QQQ: '纳斯达克100指数ETF', SPY: '标普500指数ETF', TSLA: '特斯拉', TSM: '台积电', MU: '美光科技', PLTR: '帕兰提尔', PYPL: '贝宝', QCOM: '高通', CRM: '赛富时', ORCL: '甲骨文', SNDK: '闪迪', SOXL: '半导体三倍做多ETF' };

const search = computed(() => String(detailMode.value ? route.params.ticker : route.query.q ?? '').toLowerCase());
const PAGE = 20;
const page = computed(() => Math.max(0, Number(route.query.page) || 0));
const assetMap = computed(() => buildThemeAssetMap(store.assets));

const allRows = computed(() => {
  const grouped = new Map();
  const groupsByStock = new Map();
  for (const stock of store.stockTokens) {
    if (!inChainScope(stock,scope.value)) continue;
    const id = stock.stockIdentity?.id || stock.stockCode || stock.assetId || stock.instrumentId;
    const haystack = [stock.stockCode, stock.tokenSymbol, stock.tokenName, stock.tokenContractAddress, stock.stockIdentity?.nameZh, stock.stockIdentity?.nameEn, stock.stockIdentity?.code].filter(Boolean).join(' ').toLowerCase();
    if (search.value && !haystack.includes(search.value)) continue;
    if (!grouped.has(id)) grouped.set(id, { ticker: id, list: [], relations: [] });
    const group = grouped.get(id);
    group.list.push(stock);
    const key = assetKey(stock?.chainId ?? stock?.chain ?? stock?.chainIndex, stock?.tokenContractAddress ?? stock?.instrumentId);
    if (!groupsByStock.has(key)) groupsByStock.set(key, new Set());
    groupsByStock.get(key).add(group);
  }
  // Keep the relation's source order, including when several stock tokens share a ticker.
  for (const relation of store.relations) {
    const key = assetKey(relation?.chainId ?? relation?.chain ?? relation?.chainIndex, relation?.stock);
    for (const group of groupsByStock.get(key) ?? []) group.relations.push(relation);
  }
  return [...grouped.values()]
    .map((group) => ({ ...group, themeRowCount: countThemeRows(group.relations) }))
    .sort((a, b) => b.themeRowCount - a.themeRowCount || String(a.ticker ?? '').localeCompare(String(b.ticker ?? '')));
});
const pages = computed(() => Math.max(1, Math.ceil(allRows.value.length / PAGE)));
const safePage = computed(() => Math.min(page.value, pages.value - 1));
const cards = computed(() => allRows.value.slice(safePage.value * PAGE, safePage.value * PAGE + PAGE).map((group) => {
  const themeRows = buildThemeRows(group.relations, assetMap.value);
  const visibleRows = sortThemeRows(themeRows, sortBy.value, includeHistorical.value);
  const poolBarMax = Math.max(0, ...visibleRows.map((row) => Number(row.relation.liquidityUsd) || 0));
  return { ...group, themeRows, visibleRows, poolBarMax };
}));

function officialStock(card) { return preferredOfficialStock(card.list); }
function cardMemeVolume(card) { const assets=[...new Map(card.themeRows.map(row => [row.key,row.asset])).values()]; if(!assets.length || assets.some(asset => asset?.volume24h == null || asset?.volumeCurrency !== 'USD'))return null; return assets.reduce((sum,asset)=>sum+Number(asset.volume24h),0); }
function cardPoolLiquidity(card) { const pools = new Map(); for(const row of card.themeRows) for(const pool of row.pools ?? []) if(pool.pool) pools.set(String(pool.chainId)+':'+String(pool.pool).toLowerCase(),pool.liquidityUsd); if(!pools.size || [...pools.values()].some(value => value == null)) return null; return [...pools.values()].reduce((sum,value)=>sum+Number(value),0); }

function freshPoolCount(card) {
  return card.themeRows.filter((row) => row.liquidityState === 'fresh').length;
}

function poolCount(card) {
  return card.themeRows.reduce((total, row) => total + row.poolCount, 0);
}

function poolBarWidth(max, row) {
  return max > 0 ? Math.max(1, Math.min(100, (Number(row.relation.liquidityUsd) || 0) / max * 100)) : 0;
}

function poolStateLabel(row) {
  if (row.liquidityState === 'fresh') return tr('15 分钟内', 'Within 15m');
  if (row.liquidityState === 'historical') return tr('历史估值', 'Historical');
  return tr('规模待采集', 'Size pending');
}

function relationType(relation) {
  return relation.wrapper ? tr('包装股票配对已核验', 'Verified wrapped-stock pair') : tr('股票直接配对已核验', 'Verified direct-stock pair');
}

function companyTitle(ticker, first) {
  const identity = first?.stockIdentity;
  if (identity?.status === 'identified' && (identity.nameZh || identity.nameEn)) return `${tr(identity.nameZh || identity.nameEn, identity.nameEn || identity.nameZh)} · ${identity.code}`;
  if (/^\d+$/.test(ticker)) return first?.tokenName ? `${ticker} · ${first.tokenName}` : ticker;
  const zh = COMPANY[ticker];
  return zh ? tr(`${ticker} · ${zh}`, ticker) : ticker;
}

function autoOpen(ticker) {
  const key = String(ticker ?? '').toLowerCase();
  return (search.value && allRows.value.length === 1) || search.value === key;
}

function openStock(card) {
  router.push({ name: 'stockDetail', params: { ticker: card.list[0]?.stockCode ?? card.ticker }, query: { chain: scope.value } });
}

function onStockSummary(event, card) {
  if (detailMode.value) return;
  event.preventDefault();
  openStock(card);
}

function setPage(next) {
  router.push({ query: { ...route.query, page: next || undefined } });
}

function pairLinkFor(stock) {
  if (stock.tokenContractAddress) return { path: `/pair/${String(stock.chainId ?? stock.chainIndex ?? '196')}/${stock.tokenContractAddress}`, query: {} };
  return { path: '/pair' };
}

let searchTimer;
function onSearch(event) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => router.push({ query: { ...route.query, q: event.target.value || undefined, page: undefined } }), 250);
}
</script>
