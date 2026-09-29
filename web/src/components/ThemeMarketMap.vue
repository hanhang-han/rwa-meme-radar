<template>
  <section class="panel theme-market" aria-labelledby="theme-map-title">
    <div class="theme-market-heading">
      <div>
        <span class="eyebrow">{{ tr('股票主题 / 链上资产', 'Stock themes / onchain assets') }}</span>
        <h2 id="theme-map-title">{{ layer==='verified' ? tr('主题行情地图', 'Theme market map') : tr('名称线索', 'Name clues') }}</h2>
        <p>{{ layer==='verified'
          ? tr('从已核验的股票代币配对池，发现关联 Meme 的行情变化。', 'Explore related meme markets through verified stock-token pools.')
          : tr('仅按名称或代号的精确规则匹配，尚无已核验股票配对证据。', 'Exact name or symbol matches only; these assets do not have verified stock-pair evidence.') }}</p>
      </div>
      <span v-if="layer==='verified' && themeMap?.asOf" class="theme-market-asof">{{ tr('最近采样', 'Latest observation') }} {{ date(themeMap.asOf) }}</span>
    </div>
    <div class="theme-layer-switch" role="group" :aria-label="tr('关系证据层级', 'Relationship evidence level')">
      <button type="button" :class="{active:layer==='verified'}" :aria-pressed="layer==='verified'" @click="layer='verified'">{{ tr('已核验配对', 'Verified pairs') }} <b>{{ themeMap?.totalAssets ?? model.totalAssets }}</b></button>
      <button type="button" :class="{active:layer==='clues'}" :aria-pressed="layer==='clues'" @click="layer='clues'">{{ tr('名称线索 · 未核验', 'Name clues · unverified') }} <b>{{ nameCluesLoading ? '…' : nameClues.length }}</b></button>
    </div>

    <div v-if="loading" class="theme-market-empty" role="status">
      {{ tr('正在加载完整的主题关系和行情…', 'Loading complete theme relationships and market data…') }}
    </div>
    <div v-else-if="layer==='clues' && nameCluesLoading" class="theme-market-empty" role="status">
      {{ tr('正在读取完整的名称线索…', 'Loading the complete name clues…') }}
    </div>
    <div v-else-if="layer==='clues'" class="theme-clue-layer">
      <p class="theme-clue-note">{{ tr('这里的关联只是币名或代号命中规则，不能推断池配对、发行方授权或价格联动；不计入已核验主题统计。', 'These are name or symbol rule matches only. They do not establish a paired pool, issuer endorsement or price linkage, and are excluded from verified theme totals.') }}</p>
      <div v-if="!nameClues.length" class="theme-market-empty" role="status">{{ tr('当前范围没有名称线索。', 'No name clues in this scope.') }}</div>
      <div v-else class="theme-clue-grid">
        <article v-for="clue in nameClues" :key="clue.key" class="theme-clue-card">
          <div class="theme-clue-top"><span>{{ clue.ticker }} {{ tr('名称线索', 'name clue') }}</span><span class="theme-clue-badge">B · {{ tr('未核验', 'unverified') }}</span></div>
          <RouterLink :to="detailPath(clue)" class="theme-clue-name">{{ clue.name || clue.symbol || short(clue.token) }}</RouterLink>
          <p class="theme-clue-identity">{{ chainName(clue) }} · {{ short(clue.token) }}</p>
          <p class="theme-clue-match">{{ clue.matchType==='symbol' ? tr('代号匹配', 'Symbol match') : tr('名称匹配', 'Name match') }}
            <strong>{{ clue.keyword ? `“${clue.keyword}”` : '—' }}</strong></p>
          <div class="theme-clue-actions">
            <span>{{ tr('本站发现', 'Site discovered') }} {{ date(clue.firstSeen) }}</span>
            <button type="button" @click="toggleWatch(clue)">{{ watched.has(clue.key) ? tr('★ 已关注','★ Following') : tr('☆ 关注','☆ Follow') }}</button>
          </div>
        </article>
      </div>
    </div>
    <div v-else-if="!model.rows.length" class="theme-market-empty" role="status">
      {{ tr('当前范围暂无可展示的已核验主题资产。', 'No verified theme assets are available in this scope.') }}
    </div>
    <template v-else>
      <p v-if="themeMap?.version==='compat-full-snapshot'" class="theme-market-compat">
        {{ tr('当前使用现有完整快照构建地图；主题关系已核验，成交额仅在资产汇总口径与独立采样时间齐全时着色。', 'This map uses the existing full snapshot. Relationships are verified; volume is sized only when aggregate scope and field observation time are available.') }}
      </p>
      <div class="theme-market-tools">
        <div class="theme-market-switch" role="group" :aria-label="tr('展示方式', 'Display mode')">
          <button type="button" :class="{active:mode==='map'}" :aria-pressed="mode==='map'" @click="mode='map'">{{ tr('主题地图', 'Theme map') }}</button>
          <button type="button" :class="{active:mode==='table'}" :aria-pressed="mode==='table'" @click="mode='table'">{{ tr('比较表格', 'Comparison table') }}</button>
        </div>
        <span>{{ model.totalThemes }} {{ tr('个主题', 'themes') }} · {{ model.rows.length }} {{ tr('个地图资产', 'assets on map') }} · {{ tr('全来源统一展示', 'All sources together') }}</span>
      </div>
      <div class="theme-market-layout">
        <div class="theme-market-content">
          <div v-if="mode==='map'" class="theme-market-grid">
            <article v-for="theme in model.themes" :key="theme.ticker" class="theme-cluster">
              <div class="theme-cluster-heading">
                <div><strong>{{ theme.ticker }}</strong><span>{{ themeName(theme) }}</span></div>
                <RouterLink :to="{path:'/stock',query:{chain:scope,q:theme.ticker}}" :aria-label="tr('查看','View')+' '+theme.ticker">↗</RouterLink>
              </div>
              <p class="theme-cluster-breadth">
                {{ tr('上涨', 'Up') }} {{ theme.breadth?.rising ?? '—' }} ·
                {{ tr('下跌', 'Down') }} {{ theme.breadth?.falling ?? '—' }} ·
                {{ tr('有效', 'Valid') }} {{ theme.breadth?.valid ?? '—' }}/{{ theme.breadth?.total ?? '—' }}
              </p>
              <div class="theme-bubbles">
                <button
                  v-for="bubble in theme.rows" :key="bubble.key" type="button"
                  class="theme-bubble" :class="['tone-'+tone(bubble),{'is-selected':selected?.key===bubble.key}]"
                  :style="{width:bubbleSize(bubble)+'px',height:bubbleSize(bubble)+'px'}"
                  :aria-pressed="selected?.key===bubble.key"
                  :aria-label="bubbleLabel(bubble)"
                  :title="bubbleLabel(bubble)"
                  @click="selectedKey=bubble.key">
                  <span class="theme-bubble-symbol">{{ bubble.symbol || short(bubble.token) }}</span>
                  <span class="theme-bubble-change">{{ changeText(bubble) }}</span>
                </button>
              </div>
              <p v-if="theme.totalRows>theme.rows.length" class="theme-cluster-more">
                {{ tr('另有','Plus') }} {{ theme.totalRows-theme.rows.length }} {{ tr('个关联资产未放入地图', 'related assets outside this map') }}
              </p>
            </article>
          </div>
          <div v-else class="theme-market-table-wrap">
            <table class="theme-market-table">
              <thead><tr>
                <th>{{ tr('资产', 'Asset') }}</th><th>{{ tr('主题', 'Theme') }}</th><th>{{ tr('链', 'Chain') }}</th>
                <th>{{ tr('24h 涨跌', '24h change') }}</th><th>{{ tr('24h 成交额', '24h volume') }}</th><th>{{ tr('数据来源', 'Data source') }}</th>
              </tr></thead>
              <tbody>
                <tr v-for="bubble in tableModel.rows" :key="bubble.key" :class="{'is-selected':selected?.key===bubble.key}">
                  <td><button type="button" @click="selectedKey=bubble.key">{{ bubble.name || bubble.symbol || short(bubble.token) }} <small>{{ short(bubble.token) }}</small></button></td>
                  <td>{{ bubble.primaryTicker }}</td>
                  <td>{{ chainName(bubble) }}</td>
                  <td :class="'tone-'+tone(bubble)">{{ changeText(bubble) }}</td>
                  <td>{{ volumeText(bubble) }}</td>
                  <td>{{ bubble.volume24h?.source || bubble.change24h?.source || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="theme-market-legend">
            <span>{{ tr('圆面积随 24h USD 成交额变化；最小和最大显示尺寸有限制。', 'Circle area follows 24h USD volume, with a minimum and maximum display size.') }}</span>
            <span><i class="legend-up"></i>{{ tr('上涨', 'Up') }} <i class="legend-down"></i>{{ tr('下跌', 'Down') }} <i class="legend-unknown"></i>{{ tr('未知或过期', 'Unknown or stale') }}</span>
          </div>
        </div>

        <aside v-if="selected" class="theme-market-detail" :aria-label="tr('所选资产', 'Selected asset')">
          <span class="eyebrow">{{ selected.primaryTicker }} {{ tr('主题', 'theme') }} · {{ chainName(selected) }}</span>
          <h3>{{ selected.name || selected.symbol || short(selected.token) }}</h3>
          <p class="theme-market-address">{{ selected.symbol }} · {{ short(selected.token) }}<template v-if="selected.otherTickers?.length"> · {{ tr('另关联', 'Also linked to') }} {{ selected.otherTickers.join('、') }}</template></p>
          <span class="verified-pill">{{ tr('已核验股票代币配对池', 'Verified stock-token pair') }}</span>
          <div class="theme-market-main-value" :class="'tone-'+tone(selected)">{{ changeText(selected) }}</div>
          <span class="theme-market-metric-label">{{ tr('关联 Meme 的 24h 涨跌', 'Related meme 24h change') }}</span>
          <div class="theme-market-metrics">
            <div><span>{{ tr('关联 Meme 价格', 'Related meme price') }}</span><strong>{{ priceText(selected) }}</strong></div>
            <div><span>{{ tr('资产 24h 成交额', 'Asset 24h volume') }}</span><strong>{{ volumeText(selected) }}</strong></div>
            <div><span>{{ tr('配对池流动性', 'Pair pool liquidity') }}</span><strong>{{ selected.relation?.liquidityUsd != null ? usd(selected.relation.liquidityUsd) : '—' }}<small v-if="selected.relation?.liquidityUsd != null && !recent(selected.relation?.liquidityAt)"> · {{ tr('历史值','Historical') }}</small></strong></div>
          </div>
          <p class="theme-market-data-note">
            {{ tr('成交额来源', 'Volume source') }} {{ selected.volume24h?.source || '—' }} ·
            {{ metricStateLabel(selected.volume24h,lang.lang,now) }}<br>
            {{ tr('采样时间', 'Observed') }} {{ date(selected.volume24h?.observedAt) }}
          </p>
          <div class="theme-market-actions">
            <RouterLink :to="detailPath(selected)" class="theme-market-primary">{{ tr('查看资产详情', 'Open asset details') }} →</RouterLink>
            <button type="button" @click="toggleWatch(selected)">{{ watched.has(selected.key) ? tr('★ 已在本机关注','★ Saved on this device') : tr('☆ 本机关注','☆ Save on this device') }}</button>
          </div>
          <details class="theme-market-proof">
            <summary>{{ tr('为什么关联这个股票？', 'Why is this linked to the stock?') }}</summary>
            <p>{{ tr('股票代币与该 Meme 曾在同一链上池配对；不代表上市公司授权或价格因果。', 'A stock token and this meme were paired in an onchain pool. This does not imply issuer authorization or price causality.') }}</p>
            <dl>
              <dt>{{ tr('股票侧合约', 'Stock-side contract') }}</dt><dd>{{ short(selected.relation?.stock) }}</dd>
              <dt>{{ tr('池地址', 'Pool address') }}</dt><dd>{{ short(selected.relation?.pool) }}</dd>
              <dt>{{ tr('最近核验', 'Last verified') }}</dt><dd>{{ date(selected.relation?.checkedAt) }}</dd>
              <dt>{{ tr('池流动性采样', 'Pool liquidity observed') }}</dt><dd>{{ date(selected.relation?.liquidityAt) }}</dd>
            </dl>
            <RouterLink v-if="selected.relation?.stock && selected.relation?.pool" :to="pairPath(selected)">
              {{ tr('查看完整核验证据', 'Open complete evidence') }} →
            </RouterLink>
            <a v-if="selected.relation?.pool" :href="explorer(selected.relation.pool,'address',String(selected.chainId))" target="_blank" rel="noopener">
              {{ tr('在区块浏览器查看池', 'Open pool on explorer') }} ↗
            </a>
          </details>
        </aside>
      </div>
      <p class="theme-market-footnote">{{ tr('颜色仅表示关联 Meme 的涨跌，不表示原股涨跌；灰色表示该指标缺失、过期或时间不可确认。', 'Colors show related meme changes, not stock returns. Gray means the metric is missing, stale or has unknown observation time.') }}</p>
    </template>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { tr, useI18n } from '../i18n';
import { chainName, date, explorer, pct, price, short, usd } from '../utils/format';
import { useMinuteClock } from '../composables/useMinuteClock';
import { buildThemeMap, bubbleDiameter, bubbleTone, currentMetric, currentUsdVolume, metricStateLabel, readThemeWatches, writeThemeWatches } from '../utils/theme-map-model';

const props = defineProps({
  themeMap: { type: Object, default: null },
  nameClues: { type: Array, default: () => [] },
  nameCluesLoading: Boolean,
  stockTokens: { type: Array, default: () => [] },
  scope: { type: String, default: 'all' },
  loading: Boolean,
});
const { lang } = useI18n();
const now = useMinuteClock();
const mode = ref('map');
const layer = ref('verified');
const selectedKey = ref(null);
const compact = ref(typeof window !== 'undefined' && window.innerWidth <= 600);
const model = computed(() => buildThemeMap(props.themeMap, props.scope,
  compact.value ? {maxThemes:4,maxPerTheme:4} : {}, now.value));
const tableModel = computed(() => buildThemeMap(props.themeMap, props.scope, {maxThemes:50,maxPerTheme:50}, now.value));
const selected = computed(() => {
  const rows = mode.value === 'table' ? tableModel.value.rows : model.value.rows;
  return rows.find(row => row.key === selectedKey.value) ?? rows[0] ?? null;
});
watch(() => selected.value?.key, key => { if (key && key !== selectedKey.value) selectedKey.value = key; }, { immediate: true });

const watched = ref(readThemeWatches());
function syncWatch() { watched.value = readThemeWatches(); }
function syncWidth() { compact.value = window.innerWidth <= 600; }
onMounted(() => {
  window.addEventListener('theme-watch-change',syncWatch);
  window.addEventListener('storage',syncWatch);
  window.addEventListener('resize',syncWidth);
});
onUnmounted(() => {
  window.removeEventListener('theme-watch-change',syncWatch);
  window.removeEventListener('storage',syncWatch);
  window.removeEventListener('resize',syncWidth);
});
function toggleWatch(bubble) {
  const next = new Set(watched.value);
  if (next.has(bubble.key)) next.delete(bubble.key);
  else next.add(bubble.key);
  watched.value = next;
  writeThemeWatches(next);
}
const KNOWN_THEME_NAMES_ZH = { QQQ:'纳斯达克100指数ETF', 9992:'泡泡玛特', 1810:'小米' };
function themeName(theme) {
  const official = props.stockTokens.find(stock => String(stock.stockCode ?? stock.stockIdentity?.code ?? '').toUpperCase() === theme.ticker
    && stock.issuerIdentity?.verificationStatus === 'official');
  const identity = official?.stockIdentity;
  return lang.lang === 'en' ? identity?.nameEn || theme.nameEn || theme.ticker
    : identity?.nameZh || theme.nameZh || KNOWN_THEME_NAMES_ZH[theme.ticker] || identity?.nameEn || theme.nameEn || theme.ticker;
}
function recent(value) {
  const at = Number(value);
  return at > 0 && at <= now.value + 60_000 && now.value - at <= 900_000;
}
function changeText(bubble) {
  const value = currentMetric(bubble?.change24h, now.value);
  return value == null ? metricStateLabel(bubble?.change24h, lang.lang, now.value) : pct(value);
}
function volumeText(bubble) {
  const value = currentUsdVolume(bubble, now.value);
  return value == null ? metricStateLabel(bubble?.volume24h, lang.lang, now.value) : usd(value);
}
function priceText(bubble) {
  const value = currentMetric(bubble?.price, now.value);
  return value == null ? metricStateLabel(bubble?.price, lang.lang, now.value) : price(value,bubble.price?.currency);
}
function tone(bubble) { return bubbleTone(bubble, now.value); }
function bubbleSize(bubble) { return bubbleDiameter(bubble, model.value.maxVolume, now.value); }
function bubbleLabel(bubble) {
  return [bubble.name || bubble.symbol || short(bubble.token), bubble.primaryTicker,
    chainName(bubble), tr('24h 涨跌', '24h change')+' '+changeText(bubble),
    tr('24h 成交额', '24h volume')+' '+volumeText(bubble)].join(' · ');
}
function detailPath(bubble) {
  return { path:`/detail/${encodeURIComponent(bubble.chainId)}/${encodeURIComponent(bubble.token)}`, query:{chain:props.scope} };
}
function pairPath(bubble) {
  return { path:`/pair/${encodeURIComponent(bubble.chainId)}/${encodeURIComponent(bubble.relation.stock)}`,
    query:{chain:props.scope,pool:bubble.relation.pool} };
}
</script>

<style scoped>
.theme-market{padding:22px 24px 18px;background:radial-gradient(ellipse at 20% 0%,#1b302c 0%,transparent 37%),linear-gradient(135deg,#111d26,#0d151d 75%);border-color:#2c3c43}
.theme-market-heading{display:flex;align-items:end;justify-content:space-between;gap:20px;margin-bottom:20px}
.theme-market-heading h2{font-size:clamp(24px,2.3vw,31px);letter-spacing:-.6px;color:#e9f1f4;margin:5px 0}
.theme-market-heading p{color:#9cabb5;font-size:13px}
.theme-market-asof{font-size:11px;color:#91a5af;white-space:nowrap}
.theme-layer-switch{display:inline-flex;flex-wrap:wrap;gap:5px;padding:3px;margin:0 0 15px;border:1px solid #33434b;background:#101a20;border-radius:9px}
.theme-layer-switch button{display:flex;align-items:center;gap:8px;border:0;border-radius:6px;padding:8px 11px;background:transparent;color:#a8b9c1;font:inherit;font-size:11px;cursor:pointer}
.theme-layer-switch button.active{background:#30433d;color:#e8f9ec}.theme-layer-switch b{font-size:10px;font-weight:700;color:#b9fa6a}
.theme-clue-note{padding:10px 12px;border:1px solid #685a3d;border-radius:8px;background:#4c40252b;color:#dccda7;font-size:11px;line-height:1.55;margin:0 0 12px}
.theme-clue-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.theme-clue-card{min-width:0;padding:14px;border:1px solid #3c454a;border-radius:11px;background:linear-gradient(150deg,#20282e,#171f26)}
.theme-clue-top{display:flex;align-items:center;justify-content:space-between;gap:6px;color:#e1d29c;font-size:10px;font-weight:700}.theme-clue-badge{border:1px solid #7e714b;border-radius:99px;padding:3px 7px;color:#d8c8a0;font-weight:500;white-space:nowrap}
.theme-clue-name{display:block;margin:9px 0 2px;color:#eef1ee;font-size:15px;font-weight:700;text-decoration:none;overflow-wrap:anywhere}.theme-clue-identity{font-size:10px;color:#93a7b1}.theme-clue-match{font-size:11px;color:#afbec1;margin:12px 0}.theme-clue-match strong{color:#e6d9af;font-weight:600}
.theme-clue-actions{display:flex;justify-content:space-between;align-items:center;gap:8px;border-top:1px solid #35434a;padding-top:9px;color:#8da0aa;font-size:10px}.theme-clue-actions button{border:0;background:transparent;color:#c9dfb8;font:inherit;font-size:11px;cursor:pointer}
.eyebrow{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#b9fa6a}
.theme-market-empty{padding:40px 12px;color:#aab9c6;text-align:center}
.theme-market-compat{padding:9px 12px;margin-bottom:11px;border:1px solid #6c6b3d;border-radius:8px;background:#574c2229;color:#d9cfaa;font-size:11px;line-height:1.5}
.theme-market-tools{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;color:#93a5b2;font-size:11px}
.theme-market-switch{display:inline-flex;gap:3px;padding:3px;border:1px solid #33434b;background:#101a20;border-radius:9px}
.theme-market-switch button{border:0;border-radius:6px;padding:7px 12px;background:transparent;color:#a6b6c0;cursor:pointer}
.theme-market-switch button.active{background:#30433d;color:#e8f9ec}
.theme-market-layout{display:grid;grid-template-columns:minmax(0,1fr) 285px;gap:14px;align-items:start}
.theme-market-content{min-width:0}
.theme-market-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.theme-cluster{display:flex;flex-direction:column;min-width:0;min-height:230px;padding:13px 13px 10px;border:1px solid #293840;border-radius:14px;background:linear-gradient(150deg,#15242b,#101922);overflow:hidden}
.theme-cluster-heading{display:flex;align-items:start;justify-content:space-between;gap:8px}
.theme-cluster-heading>div{min-width:0;display:flex;flex-wrap:wrap;align-items:baseline;gap:3px 6px}
.theme-cluster-heading strong{font-size:17px;color:#edf4f4}
.theme-cluster-heading span{font-size:10px;color:#95a9b5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.theme-cluster-heading a{color:#b9fa6a;text-decoration:none}
.theme-cluster-breadth{font-size:10px;color:#98aab5;margin:2px 0 9px}
.theme-bubbles{flex:1;display:flex;flex-wrap:wrap;align-items:center;align-content:center;justify-content:center;gap:4px 5px;min-height:150px}
.theme-bubble{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:none;border-radius:50%;padding:3px;border:1px solid #8b9a9f;background:radial-gradient(circle at 32% 22%,#ffffff18,#ffffff03 58%),#34404944;box-shadow:inset 0 0 13px #ffffff0b,0 0 17px #0005;color:#d4dedf;cursor:pointer;transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease,background .18s ease}
.theme-bubble.tone-up{border-color:#61d7ae;background:radial-gradient(circle at 30% 25%,#bbffe13a,#48bd9026 53%,#163a3044);box-shadow:inset 0 0 15px #9dffd522,0 0 15px #4bc99918}
.theme-bubble.tone-down{border-color:#fa8798;background:radial-gradient(circle at 30% 25%,#ffcad539,#e66e8129 53%,#47222a44);box-shadow:inset 0 0 15px #ffbdca1b,0 0 15px #ec758b18}
.theme-bubble.tone-flat{border-color:#86a7b2;background:#47828a20}
.theme-bubble:hover,.theme-bubble.is-selected{transform:translateY(-2px) scale(1.035);box-shadow:0 0 0 2px #dceee0,0 0 22px #a6ebc943}
.theme-bubble-symbol{max-width:100%;font-size:11px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.theme-bubble-change{font-size:10px;font-weight:650;line-height:1.2}
.theme-bubble.tone-up .theme-bubble-change,.tone-up{color:#76e1b9}
.theme-bubble.tone-down .theme-bubble-change,.tone-down{color:#ffa0ad}
.tone-unknown,.tone-flat{color:#a5b6bf}
.theme-cluster-more{margin-top:4px;font-size:10px;color:#8296a3}
.theme-market-legend{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;padding:10px 2px 0;color:#91a5af;font-size:10px}
.theme-market-legend i{display:inline-block;width:7px;height:7px;border-radius:50%;margin:0 4px 0 8px}.legend-up{background:#76e1b9}.legend-down{background:#ffa0ad}.legend-unknown{background:#a5b6bf}
.theme-market-detail{padding:18px;border:1px solid #3b5150;border-radius:14px;background:linear-gradient(160deg,#1b302f,#122128 58%,#121c24);min-height:430px}
.theme-market-detail h3{margin:5px 0 0;font-size:25px;line-height:1.15;color:#f3f8f5;overflow-wrap:anywhere}
.theme-market-address{font-size:10px;color:#9cafb7;margin:7px 0 11px;overflow-wrap:anywhere}
.verified-pill{display:inline-block;border:1px solid #7abf9b80;border-radius:99px;padding:3px 9px;color:#b2ecc9;font-size:10px;background:#5baf8040}
.theme-market-main-value{font-size:30px;font-weight:700;letter-spacing:-1px;margin-top:18px}
.theme-market-metric-label{font-size:11px;color:#a0b2ba}
.theme-market-metrics{display:grid;gap:9px;margin-top:16px;padding-top:13px;border-top:1px solid #3a4c4f}
.theme-market-metrics>div{display:flex;justify-content:space-between;gap:8px;font-size:11px}
.theme-market-metrics span{color:#9db0b8}.theme-market-metrics strong{text-align:right;color:#e5f0ed}
.theme-market-metrics small{font-size:9px;color:#a7b8bc}
.theme-market-data-note{color:#96a9b0;font-size:10px;line-height:1.6;margin:13px 0}
.theme-market-actions{display:grid;grid-template-columns:1fr;gap:7px}.theme-market-actions a,.theme-market-actions button{display:block;padding:9px 12px;border-radius:7px;font:inherit;font-size:11px;text-align:center;text-decoration:none;cursor:pointer}
.theme-market-primary{background:#b9fa6a;color:#182720;font-weight:700!important}.theme-market-actions button{border:1px solid #59766d;color:#cbe7d7;background:transparent}
.theme-market-proof{border-top:1px solid #3a4c4f;margin-top:17px;padding-top:12px;font-size:11px;color:#aebfc3}.theme-market-proof summary{cursor:pointer;color:#d3e7dd}.theme-market-proof p{margin:9px 0}.theme-market-proof dl{display:grid;grid-template-columns:1fr auto;gap:6px;margin:10px 0}.theme-market-proof dt{color:#91a5af}.theme-market-proof dd{text-align:right;max-width:135px;overflow-wrap:anywhere}.theme-market-proof a{display:block;margin:8px 0;color:#b9fa6a}
.theme-market-footnote{font-size:10px;color:#8fa2aa;line-height:1.5;margin-top:13px}
.theme-market-table-wrap{overflow:auto;border:1px solid #2c3d44;border-radius:10px;max-height:460px}
.theme-market-table{width:100%;min-width:680px;border-collapse:collapse;text-align:left;font-size:11px}.theme-market-table th,.theme-market-table td{padding:10px;border-bottom:1px solid #253740}.theme-market-table th{position:sticky;top:0;background:#1b2a31;color:#b3c1c6;font-weight:600}.theme-market-table tr.is-selected{background:#b9fa6a12}.theme-market-table td button{border:0;background:none;color:#e4f2ec;text-align:left;font:inherit;font-weight:650;cursor:pointer}.theme-market-table td small{display:block;color:#8399a4;font-size:10px}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid #b9fa6a;outline-offset:3px}
@media(max-width:1200px){.theme-market-grid,.theme-clue-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:880px){.theme-market-layout{grid-template-columns:1fr}.theme-market-detail{min-height:0}}
@media(max-width:600px){.theme-market{padding:16px}.theme-market-heading{align-items:start;flex-direction:column;gap:5px}.theme-market-tools{align-items:start;flex-direction:column}.theme-market-grid,.theme-clue-grid{grid-template-columns:1fr}.theme-cluster{min-height:180px}.theme-bubbles{min-height:115px}.theme-market-detail{padding:15px}.theme-layer-switch{display:flex}.theme-layer-switch button{flex:1;justify-content:center;padding:8px 5px}.theme-clue-actions{flex-wrap:wrap}}
@media(prefers-reduced-motion:reduce){.theme-bubble{transition:none}}
</style>
