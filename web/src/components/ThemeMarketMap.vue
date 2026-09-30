<template>
  <section class="panel theme-market" aria-labelledby="theme-map-title">
    <div class="theme-market-heading">
      <div>
        <h2 id="theme-map-title">{{ layer==='verified' ? tr('股票主题行情', 'Stock themes') : tr('名称线索', 'Name clues') }}</h2>
        <p>{{ layer==='verified'
          ? tr('关联 Meme · 24h 涨跌', 'Related memes · 24h change')
          : tr('按名称匹配，股票关联尚未核实。', 'Name matches; stock connections are unverified.') }}</p>
      </div>
      <span v-if="layer==='verified' && themeMap?.asOf" class="theme-market-asof">{{ tr('数据时间', 'Data as of') }} {{ date(themeMap.asOf) }}</span>
    </div>
    <div class="theme-layer-switch" role="group" :aria-label="tr('关系证据层级', 'Relationship evidence level')">
      <button type="button" :class="{active:layer==='verified'}" :aria-pressed="layer==='verified'" @click="layer='verified'">{{ tr('已核验配对', 'Verified pairs') }}</button>
      <button type="button" :class="{active:layer==='clues'}" :aria-pressed="layer==='clues'" @click="layer='clues'">{{ tr('名称线索', 'Name matches') }}</button>
    </div>

    <div v-if="loading" class="theme-market-empty" role="status">
      {{ tr('正在加载行情…', 'Loading markets…') }}
    </div>
    <div v-else-if="layer==='clues' && nameCluesLoading" class="theme-market-empty" role="status">
      {{ tr('正在加载名称线索…', 'Loading name matches…') }}
    </div>
    <div v-else-if="layer==='clues'" class="theme-clue-layer">
      <div v-if="!nameClues.length" class="theme-market-empty" role="status">{{ tr('当前范围没有名称线索。', 'No name clues in this scope.') }}</div>
      <div v-else class="theme-clue-grid">
        <article v-for="clue in nameClues" :key="clue.key" class="theme-clue-card">
          <div class="theme-clue-top"><span>{{ clue.ticker }} {{ tr('名称线索', 'name clue') }}</span><span class="theme-clue-badge">B · {{ tr('待核验', 'unverified') }}</span></div>
          <RouterLink :to="detailPath(clue)" class="theme-clue-name">{{ clue.name || clue.symbol || short(clue.token) }}</RouterLink>
          <p class="theme-clue-identity">{{ chainName(clue) }} · {{ short(clue.token) }}</p>
          <p class="theme-clue-match">{{ clue.matchType==='symbol' ? tr('代号匹配', 'Symbol match') : tr('名称匹配', 'Name match') }}
            <strong>{{ clue.keyword ? `“${clue.keyword}”` : '—' }}</strong></p>
          <div class="theme-clue-actions">
            <span>{{ tr('收录', 'Indexed') }} {{ date(clue.firstSeen) }}</span>
            <button type="button" @click="toggleWatch(clue)">{{ watched.has(clue.key) ? tr('★ 已关注','★ Following') : tr('☆ 关注','☆ Follow') }}</button>
          </div>
        </article>
      </div>
    </div>
    <div v-else-if="!model.rows.length" class="theme-market-empty" role="status">
      {{ tr('暂无已核验配对资产。', 'No verified paired assets in this scope.') }}
    </div>
    <template v-else>
      <div class="theme-market-tools">
        <div class="theme-market-switch" role="group" :aria-label="tr('展示方式', 'Display mode')">
          <button type="button" :class="{active:mode==='map'}" :aria-pressed="mode==='map'" @click="mode='map'">{{ tr('气泡图', 'Bubbles') }}</button>
          <button type="button" :class="{active:mode==='table'}" :aria-pressed="mode==='table'" @click="mode='table'">{{ tr('列表', 'Table') }}</button>
        </div>
        <span>{{ mode==='map' ? model.themes.length : tableModel.themes.length }} / {{ model.totalThemes }} {{ tr('个主题', 'themes') }} · {{ mode==='map' ? model.rows.length : tableModel.rows.length }} {{ tr('个资产', 'assets') }}</span>
      </div>
      <div class="theme-market-layout">
        <div class="theme-market-content">
          <div v-if="mode==='map'" class="theme-market-grid">
            <article v-for="theme in model.themes" :key="theme.ticker" class="theme-cluster">
              <div class="theme-cluster-heading">
                <div><strong>{{ theme.ticker }}</strong><span>{{ themeName(theme) }}</span></div>
                <RouterLink :to="{path:'/stock',query:{chain:scope,q:theme.ticker}}" :aria-label="tr('查看','View')+' '+theme.ticker">↗</RouterLink>
              </div>
              <div class="theme-breadth-bar" role="img" :aria-label="breadthLabel(theme.mapBreadth)">
                <i v-for="tone in ['up','down','flat','unknown']" :key="tone" :class="'breadth-'+tone" :style="{flexGrow:theme.mapBreadth[tone]}" :hidden="!theme.mapBreadth[tone]"></i>
              </div>
              <p class="theme-cluster-breadth"><span class="tone-up">↑ {{ theme.mapBreadth.up }}</span><span class="tone-down">↓ {{ theme.mapBreadth.down }}</span><span>{{ tr('平盘', 'Flat') }} {{ theme.mapBreadth.flat }}</span><span v-if="theme.mapBreadth.unknown">{{ tr('暂无行情', 'Unavailable') }} {{ theme.mapBreadth.unknown }}</span></p>
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
                  <span class="theme-bubble-change">{{ currentMetric(bubble.change24h,now)==null ? '—' : changeText(bubble) }}</span>
                </button>
              </div>
              <p v-if="theme.totalRows>theme.rows.length" class="theme-cluster-more">
                <RouterLink :to="{path:'/stock',query:{chain:scope,q:theme.ticker}}">+{{ theme.totalRows-theme.rows.length }} {{ tr('个资产', 'more assets') }} →</RouterLink>
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
            <span>{{ tr('气泡面积：24h 美元成交额（尺寸设有上下限）', 'Area: 24h USD volume (size capped)') }}</span>
            <span><i class="legend-up"></i>{{ tr('上涨', 'Up') }} <i class="legend-down"></i>{{ tr('下跌', 'Down') }} <i class="legend-unknown"></i>{{ tr('缺失 / 过期', 'Missing / stale') }}</span>
          </div>
        </div>

        <aside v-if="selected" class="theme-market-detail" :aria-label="tr('所选资产', 'Selected asset')">
          <span class="eyebrow">{{ selected.primaryTicker }} {{ tr('主题', 'theme') }} · {{ chainName(selected) }}</span>
          <h3>{{ selected.name || selected.symbol || short(selected.token) }}</h3>
          <p class="theme-market-address">{{ selected.symbol }} · {{ short(selected.token) }}<template v-if="selected.otherTickers?.length"> · {{ tr('另关联', 'Also linked to') }} {{ selected.otherTickers.join('、') }}</template></p>
          <span class="verified-pill">{{ tr('配对已核验', 'Verified pair') }}</span>
          <div class="theme-market-main-value" :class="'tone-'+tone(selected)">{{ changeText(selected) }}</div>
          <span class="theme-market-metric-label">{{ tr('Meme · 24h 涨跌', 'Meme · 24h change') }}</span>
          <div class="theme-market-metrics">
            <div><span>{{ tr('Meme 价格', 'Meme price') }}</span><strong>{{ priceText(selected) }}</strong></div>
            <div><span>{{ tr('资产 24h 成交额', 'Asset 24h volume') }}</span><strong>{{ volumeText(selected) }}</strong></div>
            <div><span>{{ tr('配对池流动性', 'Pair pool liquidity') }}</span><strong>{{ selected.relation?.liquidityUsd != null ? usd(selected.relation.liquidityUsd) : '—' }}<small v-if="selected.relation?.liquidityUsd != null && !recent(selected.relation?.liquidityAt)"> · {{ tr('历史值','Historical') }}</small></strong></div>
          </div>
          <p class="theme-market-data-note">
            {{ tr('成交额来源', 'Volume source') }} {{ selected.volume24h?.source || '—' }} ·
            {{ metricStateLabel(selected.volume24h,lang.lang,now) }}<br>
            {{ tr('采样时间', 'Observed') }} {{ date(selected.volume24h?.observedAt) }}
          </p>
          <div class="theme-market-actions">
            <RouterLink :to="detailPath(selected)" class="theme-market-primary">{{ tr('资产详情', 'Asset details') }} →</RouterLink>
            <button type="button" :title="tr('关注保存在当前浏览器','Saved in this browser')" :aria-pressed="watched.has(selected.key)" @click="toggleWatch(selected)">{{ watched.has(selected.key) ? tr('★ 已关注','★ Following') : tr('☆ 关注','☆ Follow') }}</button>
          </div>
          <details class="theme-market-proof">
            <summary>{{ tr('配对依据', 'Pair evidence') }}</summary>
            <p>{{ tr('来自股票代币与 Meme 的链上配对池；配对不代表发行方授权或价格联动。', 'Based on an onchain pool pairing a stock token and meme. Pairing does not imply issuer endorsement or linked prices.') }}</p>
            <dl>
              <dt>{{ tr('股票侧合约', 'Stock-side contract') }}</dt><dd>{{ short(selected.relation?.stock) }}</dd>
              <dt>{{ tr('池地址', 'Pool address') }}</dt><dd>{{ short(selected.relation?.pool) }}</dd>
              <dt>{{ tr('最近核验', 'Last verified') }}</dt><dd>{{ date(selected.relation?.checkedAt) }}</dd>
              <dt>{{ tr('池流动性采样', 'Pool liquidity observed') }}</dt><dd>{{ date(selected.relation?.liquidityAt) }}</dd>
            </dl>
            <RouterLink v-if="selected.relation?.stock && selected.relation?.pool" :to="pairPath(selected)">
              {{ tr('查看配对详情', 'View pair details') }} →
            </RouterLink>
            <a v-if="selected.relation?.pool" :href="explorer(selected.relation.pool,'address',String(selected.chainId))" target="_blank" rel="noopener">
              {{ tr('链上记录', 'View onchain') }} ↗
            </a>
          </details>
        </aside>
      </div>
      <p class="theme-market-footnote">{{ tr('颜色代表 Meme 涨跌，不代表股票涨跌。缺失或过期的成交额按最小尺寸显示。', 'Colors show meme returns, not stock returns. Missing or stale volume uses the minimum size.') }}</p>
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
function breadthLabel(breadth) {
  return tr(`上涨 ${breadth.up} · 下跌 ${breadth.down} · 平盘 ${breadth.flat} · 缺失或过期 ${breadth.unknown}`,
    `Up ${breadth.up} · Down ${breadth.down} · Flat ${breadth.flat} · Missing or stale ${breadth.unknown}`);
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
.theme-market{padding:20px 24px;background:var(--panel);border-color:var(--border)}
.theme-market-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:16px}
.theme-market-heading h2{font-size:18px;font-weight:600;letter-spacing:0;color:var(--text);margin:0 0 4px}
.theme-market-heading p{color:var(--muted);font-size:12px}.theme-market-asof{font-size:11px;color:var(--muted);white-space:nowrap}
.theme-layer-switch{display:flex;gap:20px;margin-bottom:16px;border-bottom:1px solid var(--border)}
.theme-layer-switch button{border:0;border-bottom:2px solid transparent;padding:8px 0 12px;background:transparent;color:var(--muted);font:inherit;font-size:12px;cursor:pointer}
.theme-layer-switch button.active{border-color:var(--accent);color:var(--text)}
.theme-clue-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.theme-clue-card{min-width:0;padding:14px;border:1px solid var(--border);border-radius:6px;background:var(--bg)}
.theme-clue-top{display:flex;align-items:center;justify-content:space-between;gap:6px;color:var(--muted);font-size:11px}.theme-clue-badge{border-radius:4px;padding:2px 6px;color:var(--warning);background:var(--surface-raised);font-size:10px;white-space:nowrap}
.theme-clue-name{display:block;margin:10px 0 3px;color:var(--text);font-size:14px;font-weight:600;text-decoration:none;overflow-wrap:anywhere}.theme-clue-identity{font-size:11px;color:var(--muted)}.theme-clue-match{font-size:12px;color:var(--muted);margin:12px 0}.theme-clue-match strong{color:var(--text);font-weight:500}
.theme-clue-actions{display:flex;justify-content:space-between;align-items:center;gap:8px;border-top:1px solid var(--border);padding-top:9px;color:var(--muted);font-size:10px}.theme-clue-actions button{border:0;background:transparent;color:var(--accent);font:inherit;font-size:11px;cursor:pointer}
.eyebrow{font-size:11px;letter-spacing:0;color:var(--muted)}.theme-market-empty{padding:36px 12px;color:var(--muted);text-align:center;font-size:13px}
.theme-market-tools{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;color:var(--muted);font-size:11px}
.theme-market-switch{display:inline-flex;gap:3px;padding:3px;border:1px solid var(--border);background:var(--bg);border-radius:6px}
.theme-market-switch button{border:0;border-radius:4px;padding:5px 12px;background:transparent;color:var(--muted);font:inherit;font-size:12px;cursor:pointer}.theme-market-switch button.active{background:var(--surface-raised);color:var(--text)}
.theme-market-layout{display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:20px;align-items:start}.theme-market-content{min-width:0}
.theme-market-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.theme-cluster{display:flex;flex-direction:column;min-width:0;min-height:226px;padding:13px;border:1px solid var(--border);border-radius:6px;background:var(--bg);overflow:hidden}
.theme-cluster-heading{display:flex;align-items:start;justify-content:space-between;gap:8px}.theme-cluster-heading>div{min-width:0;display:flex;flex-wrap:wrap;align-items:baseline;gap:3px 7px}
.theme-cluster-heading strong{font-size:16px;color:var(--text);font-variant-numeric:tabular-nums}.theme-cluster-heading span{font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.theme-cluster-heading a{color:var(--muted);text-decoration:none}.theme-cluster-heading a:hover{color:var(--accent)}
.theme-breadth-bar{display:flex;gap:2px;height:3px;margin-top:12px;overflow:hidden;border-radius:2px}.theme-breadth-bar i{min-width:0;flex-basis:0;background:var(--border)}.theme-breadth-bar .breadth-up{background:var(--up)}.theme-breadth-bar .breadth-down{background:var(--down)}.theme-breadth-bar .breadth-flat{background:var(--muted)}
.theme-cluster-breadth{display:flex;flex-wrap:wrap;gap:8px;font-size:10px;color:var(--muted);margin:6px 0 9px;font-variant-numeric:tabular-nums}
.theme-bubbles{flex:1;display:flex;flex-wrap:wrap;align-items:center;align-content:center;justify-content:center;gap:7px;min-height:145px}
.theme-bubble{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;flex:none;border-radius:50%;padding:5px;border:1px solid var(--border);background:var(--surface-raised);color:var(--text);cursor:pointer;transition:transform .15s ease,border-color .15s ease}
.theme-bubble.tone-up{border-color:color-mix(in srgb,var(--up) 60%,transparent);background:color-mix(in srgb,var(--up) 12%,var(--bg))}.theme-bubble.tone-down{border-color:color-mix(in srgb,var(--down) 60%,transparent);background:color-mix(in srgb,var(--down) 12%,var(--bg))}.theme-bubble.tone-flat{border-color:var(--muted)}
.theme-bubble:hover{transform:translateY(-2px)}.theme-bubble.is-selected{outline:2px solid var(--accent);outline-offset:3px}
.theme-bubble-symbol{max-width:100%;font-size:11px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text)}.theme-bubble-change{font-size:11px;font-weight:500;line-height:1.2;font-variant-numeric:tabular-nums}
.tone-up{color:var(--up)}.tone-down{color:var(--down)}.tone-unknown,.tone-flat{color:var(--muted)}
.theme-cluster-more{margin-top:8px;font-size:10px}.theme-cluster-more a{color:var(--muted);text-decoration:none}.theme-cluster-more a:hover{color:var(--accent)}
.theme-market-legend{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;padding:12px 0 0;color:var(--muted);font-size:10px}.theme-market-legend i{display:inline-block;width:6px;height:6px;border-radius:50%;margin:0 4px 0 8px}.legend-up{background:var(--up)}.legend-down{background:var(--down)}.legend-unknown{background:var(--muted)}
.theme-market-detail{padding:4px 0 4px 20px;border-left:1px solid var(--border);min-width:0}.theme-market-detail h3{margin:6px 0 0;font-size:22px;line-height:1.3;color:var(--text);overflow-wrap:anywhere}
.theme-market-address{font-size:11px;color:var(--muted);margin:6px 0 12px;overflow-wrap:anywhere}.verified-pill{display:inline-block;border-radius:4px;padding:3px 7px;color:var(--accent);font-size:10px;background:var(--accent-soft)}
.theme-market-main-value{font-size:28px;font-weight:600;letter-spacing:-.5px;margin-top:18px;font-variant-numeric:tabular-nums}.theme-market-metric-label{font-size:11px;color:var(--muted)}
.theme-market-metrics{display:grid;gap:12px;margin-top:18px;padding-top:14px;border-top:1px solid var(--border)}.theme-market-metrics>div{display:flex;justify-content:space-between;gap:8px;font-size:11px}.theme-market-metrics span{color:var(--muted)}.theme-market-metrics strong{text-align:right;color:var(--text);font-weight:500}.theme-market-metrics small{font-size:10px;color:var(--muted)}
.theme-market-data-note{color:var(--muted);font-size:10px;line-height:1.7;margin:14px 0}.theme-market-actions{display:grid;grid-template-columns:1fr;gap:8px}
.theme-market-actions a,.theme-market-actions button{display:block;padding:9px 12px;border-radius:5px;font:inherit;font-size:12px;text-align:center;text-decoration:none;cursor:pointer}.theme-market-primary{background:var(--accent);color:var(--bg);font-weight:600!important}.theme-market-actions button{border:1px solid var(--border);color:var(--text);background:transparent}
.theme-market-proof{border-top:1px solid var(--border);margin-top:18px;padding-top:12px;font-size:11px;color:var(--muted)}.theme-market-proof summary{cursor:pointer;color:var(--text)}.theme-market-proof p{margin:9px 0;line-height:1.6}.theme-market-proof dl{display:grid;grid-template-columns:1fr auto;gap:7px;margin:12px 0}.theme-market-proof dt{color:var(--muted)}.theme-market-proof dd{text-align:right;max-width:135px;overflow-wrap:anywhere}.theme-market-proof a{display:block;margin:8px 0;color:var(--accent)}
.theme-market-footnote{font-size:10px;color:var(--muted);line-height:1.5;margin-top:10px}
.theme-market-table-wrap{overflow:auto;border:1px solid var(--border);border-radius:6px;max-height:480px}.theme-market-table{width:100%;min-width:680px;border-collapse:collapse;text-align:left;font-size:12px}.theme-market-table th,.theme-market-table td{padding:11px 10px;border-bottom:1px solid var(--border)}.theme-market-table th{position:sticky;top:0;background:var(--surface-raised);color:var(--muted);font-size:11px;font-weight:500}.theme-market-table tr.is-selected{background:var(--accent-soft)}.theme-market-table td button{border:0;background:none;color:var(--text);text-align:left;font:inherit;font-weight:500;cursor:pointer}.theme-market-table td small{display:block;color:var(--muted);font-size:10px}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(max-width:1200px){.theme-market-grid,.theme-clue-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:880px){.theme-market-layout{grid-template-columns:1fr}.theme-market-detail{border-left:0;border-top:1px solid var(--border);padding:20px 0 0}.theme-market-actions{grid-template-columns:1fr 1fr}}
@media(max-width:600px){.theme-market{padding:16px}.theme-market-heading{align-items:start;flex-direction:column;gap:6px}.theme-market-tools{align-items:start;flex-direction:column}.theme-market-grid,.theme-clue-grid{grid-template-columns:1fr}.theme-cluster{min-height:180px}.theme-bubbles{min-height:115px}.theme-clue-actions{flex-wrap:wrap}.theme-market-main-value{font-size:26px}}
@media(prefers-reduced-motion:reduce){.theme-bubble{transition:none}}
</style>
