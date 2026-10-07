<template>
  <section class="panel theme-market" aria-labelledby="theme-map-title">
    <div class="theme-market-heading">
      <div>
        <h2 id="theme-map-title">{{ layer==='verified' ? tr('主题地图', 'Theme map') : tr('名称匹配', 'Name matches') }}</h2>
        <p v-if="layer==='clues'">{{ tr('仅名称相似，非交易关系。', 'Similar names only; no trading link.') }}</p>
      </div>
      <span v-if="layer==='verified' && themeMap?.asOf" class="theme-market-asof">{{ tr('截至', 'As of') }} {{ date(themeMap.asOf) }}</span>
    </div>
    <div class="theme-layer-switch" role="group" :aria-label="tr('关系证据层级', 'Relationship evidence level')">
      <button type="button" :class="{active:layer==='verified'}" :aria-pressed="layer==='verified'" @click="layer='verified'">{{ tr('同池', 'Pool pairs') }}</button>
      <button type="button" :class="{active:layer==='clues'}" :aria-pressed="layer==='clues'" @click="showClues">{{ tr('名称匹配', 'Name matches') }}</button>
    </div>

    <div v-if="loading" class="theme-market-empty" role="status">
      <div v-for="n in 5" :key="n" class="skeleton-line"></div>
    </div>
    <div v-else-if="layer==='clues' && nameCluesLoading" class="theme-market-empty" role="status">
      <div v-for="n in 4" :key="n" class="skeleton-line"></div>
    </div>
    <div v-else-if="layer==='clues'" class="theme-clue-layer">
      <div v-if="!nameClues.length" class="theme-market-empty" role="status">{{ tr('当前范围没有名称匹配。', 'No name matches in this scope.') }}</div>
      <div v-else class="theme-clue-grid">
        <article v-for="clue in nameClues" :key="clue.key" class="theme-clue-card">
          <div class="theme-clue-top"><span>{{ clue.ticker }}</span><span class="theme-clue-badge">{{ tr('名称匹配', 'Name match') }}</span></div>
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
      {{ tr('当前范围暂无池子配对。', 'No pool pairs in this scope.') }}
    </div>
    <template v-else>
      <div class="theme-market-tools">
        <div class="theme-market-switch" role="group" :aria-label="tr('展示方式', 'Display mode')">
          <button type="button" :class="{active:mode==='map'}" :aria-pressed="mode==='map'" @click="mode='map'">{{ tr('地图', 'Map') }}</button>
          <button type="button" :class="{active:mode==='table'}" :aria-pressed="mode==='table'" @click="mode='table'">{{ tr('列表', 'Table') }}</button>
        </div>
        <span>{{ mode==='map' ? model.themes.length : tableModel.themes.length }}<template v-if="totalThemes!=null">/{{ totalThemes }}</template> {{ tr('主题', 'themes') }} · {{ mode==='map' ? model.rows.length : tableModel.rows.length }}<template v-if="totalAssets!=null">/{{ totalAssets }}</template> Meme</span>
      </div>
      <div class="theme-market-layout">
        <div class="theme-market-content">
          <div v-if="mode==='map'" class="theme-market-grid">
            <article v-for="theme in model.themes" :key="theme.ticker" class="theme-cluster" :class="{'is-selected':selectedTicker===theme.ticker}" :style="themeCardStyle(theme)">
              <div class="theme-cluster-heading"><button type="button" class="theme-select" :aria-pressed="selectedTicker===theme.ticker" @click="selectTheme(theme)"><strong>{{ themeName(theme) }}</strong><span>{{ themeCodeLabel(theme) }}</span></button><RouterLink :to="themeNavigationLink(theme.ticker,{route,scope})" :aria-label="tr('查看主题','View theme')+' '+theme.ticker">↗</RouterLink></div>
              <div class="theme-cluster-volume"><strong>{{ themeVolumeText(theme) }}</strong><span>{{ tr('同池 Meme · 24h 成交额 USD','Paired memes · 24h volume USD') }}</span><small>{{ tr('覆盖','Coverage') }} {{ theme.volumeKnownCount }}/{{ theme.totalRows }}</small></div>
              <div v-if="stockForTheme(theme)" class="theme-stock-baseline"><span>{{ tr('股票代币基准','Stock-token reference') }}</span><strong>{{ stockForTheme(theme).tokenSymbol || theme.ticker }}</strong><span>{{ stockChangeText(theme) }}</span></div>
              <div class="theme-breadth-bar" role="img" :aria-label="breadthLabel(theme.mapBreadth)"><i v-for="tone in ['up','down','flat','unknown']" :key="tone" :class="'breadth-'+tone" :style="{flexGrow:theme.mapBreadth[tone]}" :hidden="!theme.mapBreadth[tone]"></i></div>
              <p class="theme-cluster-breadth"><span class="tone-up">↑ {{ theme.mapBreadth.up }}</span><span class="tone-down">↓ {{ theme.mapBreadth.down }}</span><span v-if="theme.mapBreadth.flat">{{ tr('平盘','Flat') }} {{ theme.mapBreadth.flat }}</span><span v-if="theme.mapBreadth.unknown">{{ tr('无行情','Unavailable') }} {{ theme.mapBreadth.unknown }}</span></p>
              <div class="theme-bubbles"><button v-for="bubble in theme.rows" :key="bubble.key" type="button" class="theme-bubble" :class="['tone-'+tone(bubble),{'is-selected':selected?.key===bubble.key}]" :aria-pressed="selected?.key===bubble.key" :aria-label="bubbleLabel(bubble)" :title="bubbleLabel(bubble)" @click="selectAsset(bubble)"><span class="theme-bubble-symbol">{{ bubble.symbol || short(bubble.token) }}</span><span class="theme-bubble-change">{{ changeText(bubble) }}</span><small>{{ volumeText(bubble) }}</small></button></div>
              <p v-if="theme.totalRows>theme.rows.length" class="theme-cluster-more"><RouterLink :to="themeNavigationLink(theme.ticker,{route,scope})">+{{ theme.totalRows-theme.rows.length }} {{ tr('个资产','more assets') }} →</RouterLink></p>
            </article>
          </div>
          <div class="theme-table-heading"><strong>{{ tr('关联 Meme 行情','Related Meme markets') }} <small>{{ visibleTableRows.length }}</small></strong><button v-if="selectedTicker" type="button" @click="clearTheme">{{ selectedTicker }} ×</button><span v-else>{{ tr('全部主题','All themes') }}</span></div>
          <div class="theme-market-table-wrap"><table class="theme-market-table"><thead><tr><th>{{ tr('资产','Asset') }}</th><th>{{ tr('主题','Theme') }}</th><th>{{ tr('链','Chain') }}</th><th>{{ tr('24h 涨跌','24h change') }}</th><th>{{ tr('24h 成交额（USD）','24h volume (USD)') }}</th><th>{{ tr('来源','Source') }}</th></tr></thead><tbody><tr v-for="bubble in visibleTableRows" :key="bubble.key" :class="{'is-selected':selected?.key===bubble.key}"><td><button type="button" @click="selectAsset(bubble)">{{ bubble.name || bubble.symbol || short(bubble.token) }} <small>{{ short(bubble.token) }}</small></button></td><td>{{ bubble.primaryTicker }}</td><td>{{ chainName(bubble) }}</td><td :title="tr('资产24h价格涨跌（%）；无可用观测时不填零','Asset price change over 24h (%); missing observations are not filled with zero')" :class="'tone-'+tone(bubble)">{{ changeText(bubble) }}</td><td :title="bubbleLabel(bubble)">{{ volumeText(bubble) }}</td><td>{{ bubble.volume24h?.source || bubble.change24h?.source || '—' }}</td></tr></tbody></table></div>
          <div class="theme-market-legend">
            <span>{{ tr('主题分组 · 成交额仅汇总当前可用 USD 数据','Theme groups · volume sums current available USD observations') }}</span>
            <span>{{ tr('颜色：Meme 24h 涨跌', 'Color: meme 24h change') }} <i class="legend-up"></i>{{ tr('涨', 'Up') }} <i class="legend-down"></i>{{ tr('跌', 'Down') }} <i class="legend-unknown"></i>{{ tr('无数据', 'Unavailable') }}</span>
          </div>
        </div>

        <aside v-if="selected" class="theme-market-detail" :aria-label="tr('所选资产', 'Selected asset')">
          <span class="eyebrow">{{ selected.primaryTicker }} {{ tr('主题', 'theme') }} · {{ chainName(selected) }}</span>
          <h3>{{ selected.name || selected.symbol || short(selected.token) }}</h3>
          <p class="theme-market-address">{{ selected.symbol }} · {{ short(selected.token) }}<template v-if="selected.otherTickers?.length"> · {{ tr('另关联', 'Also linked to') }} {{ selected.otherTickers.join('、') }}</template></p>
          <span class="verified-pill">{{ tr('同池', 'Shared pool') }}</span>
          <div class="theme-market-main-value" :class="'tone-'+tone(selected)">{{ changeText(selected) }}</div>
          <span class="theme-market-metric-label">{{ tr('Meme · 24h 涨跌', 'Meme · 24h change') }}</span>
          <div class="theme-market-metrics">
            <div><span>{{ tr('资产报价', 'Asset quote') }}</span><strong :title="tr('资产报价，币种见数值；观测时间','Asset quote in the displayed currency; observed at')+' '+date(selected.price?.observedAt)">{{ priceText(selected) }}</strong></div>
            <div><span>{{ tr('24h 成交（USD）', '24h volume (USD)') }}</span><strong :title="tr('资产24h美元成交；来源','Asset volume 24h (USD); source')+' '+(selected.volume24h?.source||'—')+' · '+date(selected.volume24h?.observedAt)">{{ volumeText(selected) }}</strong></div>
            <div><span>{{ tr('池流动性', 'Pool liquidity') }}</span><strong :title="tr('此配对池的双边流动性（USD），历史值按观测时间标注','Two-sided liquidity of this pair pool (USD), with historical values labeled by observation time')+' · '+date(selected.relation?.liquidityAt)">{{ selected.relation?.liquidityUsd != null ? usd(selected.relation.liquidityUsd) : '—' }}<small v-if="selected.relation?.liquidityUsd != null && !recent(selected.relation?.liquidityAt)"> · {{ tr('历史值','Historical') }}</small></strong></div>
          </div>
          <details class="theme-market-data-note">
            <summary>{{ tr('24h 成交来源与时间', '24h volume source and time') }}</summary>
            <p>{{ selected.volume24h?.source || '—' }} · {{ metricStateLabel(selected.volume24h,lang.lang,now) }}<br>{{ date(selected.volume24h?.observedAt) }}</p>
          </details>
          <div class="theme-market-actions">
            <RouterLink :to="detailPath(selected)" class="theme-market-primary">{{ tr('资产详情', 'Asset details') }} →</RouterLink>
            <button type="button" :aria-pressed="watched.has(selected.key)" @click="toggleWatch(selected)">{{ watched.has(selected.key) ? tr('★ 已关注','★ Following') : tr('☆ 关注','☆ Follow') }}</button>
          </div>
          <details class="theme-market-proof">
            <summary>{{ tr('配对依据', 'Pair evidence') }}</summary>
            <p>{{ tr('Meme 与股票代币在同一个链上池中交易。', 'The meme and stock token trade in the same on-chain pool.') }}</p>
            <dl>
              <dt>{{ tr('股票侧合约', 'Stock-side contract') }}</dt><dd>{{ short(selected.relation?.stock) }}</dd>
              <dt>{{ tr('池地址', 'Pool address') }}</dt><dd>{{ short(selected.relation?.pool) }}</dd>
              <dt>{{ tr('最近记录', 'Last recorded') }}</dt><dd>{{ date(selected.relation?.checkedAt) }}</dd>
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
      <p class="theme-market-footnote">{{ tr('缺失或过期行情单独标记，不计作零成交。','Missing or stale observations are labeled separately, never counted as zero volume.') }}</p>
    </template>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { assetNavigationLink, themeNavigationLink } from '../utils/navigation-context';
import { readPageState, writePageState } from '../utils/page-navigation-state';
import { tr, useI18n } from '../i18n';
import { chainName, date, explorer, pct, price, short, usd } from '../utils/format';
import { useMinuteClock } from '../composables/useMinuteClock';
import { normalizeTicker, stockThemeCodeLabel, stockThemeName } from '../utils/stock-theme-model';
import { buildThemeMap, changeBreadth, bubbleTone, currentMetric, currentUsdVolume, metricStateLabel, readThemeWatches, writeThemeWatches } from '../utils/theme-map-model';

const props = defineProps({
  themeMap: { type: Object, default: null },
  nameClues: { type: Array, default: () => [] },
  nameCluesLoading: Boolean,
  stockTokens: { type: Array, default: () => [] },
  scope: { type: String, default: 'all' },
  modelValue: { type: String, default: undefined },
  loading: Boolean,
});
const emit=defineEmits(['load-clues','update:modelValue']);
const route=useRoute();
const stateKey=()=>`home-theme-map:${props.scope}`;
const restored=readPageState(stateKey())??{};
function showClues(){layer.value='clues';if(props.nameCluesLoading)emit('load-clues');}
const { lang } = useI18n();
const now = useMinuteClock();
const mode = ref(restored.mode==='table'?'table':'map');
const layer = ref(restored.layer==='clues'?'clues':'verified');
const selectedKey = ref(typeof restored.selectedKey==='string'?restored.selectedKey:null);
const selectedTicker = ref(null);
watch([mode,layer,selectedKey],()=>writePageState(stateKey(),{mode:mode.value,layer:layer.value,selectedKey:selectedKey.value}));
watch(()=>props.scope,()=>{const value=readPageState(stateKey())??{};mode.value=value.mode==='table'?'table':'map';layer.value=value.layer==='clues'?'clues':'verified';selectedKey.value=typeof value.selectedKey==='string'?value.selectedKey:null;if(layer.value==='clues'&&props.nameCluesLoading)emit('load-clues');});
watch(()=>props.modelValue,value=>{if(value!==undefined)selectedTicker.value=normalizeTicker(value)||null;},{immediate:true});
const compact = ref(typeof window !== 'undefined' && window.innerWidth <= 600);
const model = computed(() => {
  const result=buildThemeMap(props.themeMap,props.scope,compact.value?{maxThemes:4,maxPerTheme:4}:{},now.value);
  return {...result,themes:result.themes.map(theme=>({...theme,mapBreadth:changeBreadth(theme.rows,now.value)}))};
});
const tableModel = computed(() => buildThemeMap(props.themeMap, props.scope, {maxThemes:50,maxPerTheme:50}, now.value));
const totalThemes=computed(()=>props.scope==='all'?(props.themeMap?.totalThemes??model.value.totalThemes):null);
const totalAssets=computed(()=>props.scope==='all'?(props.themeMap?.totalAssets??model.value.totalAssets):null);
const visibleTableRows = computed(() => selectedTicker.value ? tableModel.value.rows.filter(row=>row.primaryTicker===selectedTicker.value) : tableModel.value.rows);
const selected = computed(() => {
  const rows = visibleTableRows.value;
  return rows.find(row => row.key === selectedKey.value) ?? rows[0] ?? null;
});
watch(() => selected.value?.key, key => { if (key && key !== selectedKey.value) selectedKey.value = key; }, { immediate: true });

watch(tableModel, result=>{if(props.modelValue===undefined&&selectedTicker.value&&!result.themes.some(theme=>theme.ticker===selectedTicker.value))selectedTicker.value=null;});
function clearTheme(){selectedTicker.value=null;emit('update:modelValue','');}
function selectTheme(theme){selectedTicker.value=theme.ticker;selectedKey.value=theme.rows[0]?.key??null;emit('update:modelValue',theme.ticker);}
function selectAsset(asset){selectedKey.value=asset.key;if(selectedTicker.value||props.modelValue!==undefined){selectedTicker.value=asset.primaryTicker;emit('update:modelValue',asset.primaryTicker);}}
function themeVolumeText(theme){return theme.volumeKnownCount>0?usd(theme.knownVolume):'—';}
function themeCardStyle(theme){const max=Math.max(...model.value.themes.map(item=>item.knownVolume),0);const strength=theme.volumeKnownCount&&max>0?5+Math.round(theme.knownVolume/max*7):3;return {background:`color-mix(in srgb,var(--accent) ${strength}%,var(--panel))`};}
function stockForTheme(theme){return props.stockTokens.find(stock=>normalizeTicker(stock.stockCode??stock.stockIdentity?.code)===normalizeTicker(theme.ticker)&&stock.issuerIdentity?.verificationStatus==='official');}
function stockChangeText(theme){const stock=stockForTheme(theme),raw=stock?.change24h,at=Number(stock?.fieldTimes?.change24h);return raw!=null&&raw!==''&&Number.isFinite(Number(raw))&&at>0&&at<=now.value+60000&&now.value-at<=900000?pct(raw):'—';}
const watched = ref(readThemeWatches());
function syncWatch() { watched.value = readThemeWatches(); }
function syncWidth() { compact.value = window.innerWidth <= 600; }
onMounted(() => {
  if(layer.value==='clues'&&props.nameCluesLoading)emit('load-clues');
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
function themeName(theme) {
  const official=stockForTheme(theme);
  return stockThemeName(official??{stockIdentity:{nameZh:theme.nameZh,nameEn:theme.nameEn}},theme.ticker,lang.lang);
}
function themeCodeLabel(theme){return stockThemeCodeLabel(stockForTheme(theme),theme.ticker,lang.lang);}
function breadthLabel(breadth) {
  return tr(`上涨 ${breadth.up} · 下跌 ${breadth.down} · 平盘 ${breadth.flat} · 缺失或过期 ${breadth.unknown}`,
    `Up ${breadth.up} · Down ${breadth.down} · Flat ${breadth.flat} · Missing or stale ${breadth.unknown}`);
}
function recent(value) {
  const at = Number(value);
  return at > 0 && at <= now.value + 60_000 && now.value - at <= 900_000;
}
function missingReason(metric){if(['unsupported-currency','unsupported-scope'].includes(metric?.status))return tr('计价单位不同，暂不比较','Different units; not compared');return metricStateLabel(metric,lang.lang,now.value);}
function changeText(bubble) {
  const value = currentMetric(bubble?.change24h, now.value);
  return value == null ? missingReason(bubble?.change24h) : pct(value);
}
function volumeText(bubble) {
  const value = currentUsdVolume(bubble, now.value);
  return value == null ? missingReason(bubble?.volume24h) : usd(value);
}
function priceText(bubble) {
  const value = currentMetric(bubble?.price, now.value);
  return value == null ? missingReason(bubble?.price) : price(value,bubble.price?.currency);
}
function tone(bubble) { return bubbleTone(bubble, now.value); }
function bubbleLabel(bubble) {
  return [bubble.name || bubble.symbol || short(bubble.token), bubble.primaryTicker,
    chainName(bubble), tr('24h 涨跌', '24h change')+' '+changeText(bubble),
    tr('24h 成交额', '24h volume')+' '+volumeText(bubble)].join(' · ');
}
function detailPath(bubble) {
  return assetNavigationLink(bubble,{route,scope:props.scope});
}
function pairPath(bubble) {
  return assetNavigationLink(bubble,{route,scope:props.scope,tab:'relation',pool:bubble.relation?.pool});
}
</script>

<style scoped>
.theme-market{padding:20px 24px;background:var(--panel);border-color:var(--border)}
.theme-market-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:16px}
.theme-market-heading h2{font-size:18px;font-weight:600;letter-spacing:0;color:var(--text);margin:0 0 4px}
.theme-market-heading p{color:var(--muted);font-size:12px}.theme-market-asof{font-size:12px;color:var(--muted);white-space:nowrap}
.theme-layer-switch{display:flex;gap:20px;margin-bottom:16px;border-bottom:1px solid var(--border)}
.theme-layer-switch button{border:0;border-bottom:2px solid transparent;padding:8px 0 12px;background:transparent;color:var(--muted);font:inherit;font-size:12px;cursor:pointer}
.theme-layer-switch button.active{border-color:var(--accent);color:var(--text)}
.theme-clue-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.theme-clue-card{min-width:0;padding:14px;border:1px solid var(--border);border-radius:6px;background:var(--bg)}
.theme-clue-top{display:flex;align-items:center;justify-content:space-between;gap:6px;color:var(--muted);font-size:12px}.theme-clue-badge{border-radius:4px;padding:2px 6px;color:var(--warning);background:var(--surface-raised);font-size:12px;white-space:nowrap}
.theme-clue-name{display:block;margin:10px 0 3px;color:var(--text);font-size:14px;font-weight:600;text-decoration:none;overflow-wrap:anywhere}.theme-clue-identity{font-size:12px;color:var(--muted)}.theme-clue-match{font-size:12px;color:var(--muted);margin:12px 0}.theme-clue-match strong{color:var(--text);font-weight:500}
.theme-clue-actions{display:flex;justify-content:space-between;align-items:center;gap:8px;border-top:1px solid var(--border);padding-top:9px;color:var(--muted);font-size:12px}.theme-clue-actions button{border:0;background:transparent;color:var(--accent);font:inherit;font-size:12px;cursor:pointer}
.eyebrow{font-size:12px;letter-spacing:0;color:var(--muted)}.theme-market-empty{padding:36px 12px;color:var(--muted);text-align:center;font-size:13px}
.theme-market-tools{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;color:var(--muted);font-size:12px}
.theme-market-switch{display:inline-flex;gap:3px;padding:3px;border:1px solid var(--border);background:var(--bg);border-radius:6px}
.theme-market-switch button{border:0;border-radius:4px;padding:5px 12px;background:transparent;color:var(--muted);font:inherit;font-size:12px;cursor:pointer}.theme-market-switch button.active{background:var(--surface-raised);color:var(--text)}
.theme-market-layout{display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:20px;align-items:start}.theme-market-content{min-width:0}
.theme-market-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.theme-cluster{display:flex;flex-direction:column;min-width:0;min-height:226px;padding:13px;border:1px solid var(--border);border-radius:6px;background:var(--bg);overflow:hidden}
.theme-cluster-heading{display:flex;align-items:start;justify-content:space-between;gap:8px}.theme-cluster-heading>div{min-width:0;display:flex;flex-wrap:wrap;align-items:baseline;gap:3px 7px}
.theme-cluster-heading strong{font-size:16px;color:var(--text);font-variant-numeric:tabular-nums}.theme-cluster-heading span{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.theme-cluster-heading a{color:var(--muted);text-decoration:none}.theme-cluster-heading a:hover{color:var(--accent)}
.theme-breadth-bar{display:flex;gap:2px;height:3px;margin-top:12px;overflow:hidden;border-radius:2px}.theme-breadth-bar i{min-width:0;flex-basis:0;background:var(--border)}.theme-breadth-bar .breadth-up{background:var(--up)}.theme-breadth-bar .breadth-down{background:var(--down)}.theme-breadth-bar .breadth-flat{background:var(--muted)}
.theme-cluster-breadth{display:flex;flex-wrap:wrap;gap:8px;font-size:12px;color:var(--muted);margin:6px 0 9px;font-variant-numeric:tabular-nums}
.theme-bubbles{flex:1;display:flex;flex-wrap:wrap;align-items:center;align-content:center;justify-content:center;gap:7px;min-height:145px}
.theme-bubble{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;flex:none;border-radius:50%;padding:5px;border:1px solid var(--border);background:var(--surface-raised);color:var(--text);cursor:pointer;transition:transform .15s ease,border-color .15s ease}
.theme-bubble.tone-up{border-color:color-mix(in srgb,var(--up) 60%,transparent);background:color-mix(in srgb,var(--up) 12%,var(--bg))}.theme-bubble.tone-down{border-color:color-mix(in srgb,var(--down) 60%,transparent);background:color-mix(in srgb,var(--down) 12%,var(--bg))}.theme-bubble.tone-flat{border-color:var(--muted)}
.theme-bubble:hover{transform:translateY(-2px)}.theme-bubble.is-selected{outline:2px solid var(--accent);outline-offset:3px}
.theme-bubble-symbol{max-width:100%;font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text)}.theme-bubble-change{font-size:12px;font-weight:500;line-height:1.2;font-variant-numeric:tabular-nums}
.tone-up{color:var(--up)}.tone-down{color:var(--down)}.tone-unknown,.tone-flat{color:var(--muted)}
.theme-cluster-more{margin-top:8px;font-size:12px}.theme-cluster-more a{color:var(--muted);text-decoration:none}.theme-cluster-more a:hover{color:var(--accent)}
.theme-market-legend{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;padding:12px 0 0;color:var(--muted);font-size:12px}.theme-market-legend i{display:inline-block;width:6px;height:6px;border-radius:50%;margin:0 4px 0 8px}.legend-up{background:var(--up)}.legend-down{background:var(--down)}.legend-unknown{background:var(--muted)}
.theme-market-detail{position:sticky;top:18px;padding:4px 0 4px 20px;border-left:1px solid var(--border);min-width:0}.theme-market-detail h3{margin:6px 0 0;font-size:22px;line-height:1.3;color:var(--text);overflow-wrap:anywhere}
.theme-market-address{font-size:12px;color:var(--muted);margin:6px 0 12px;overflow-wrap:anywhere}.verified-pill{display:inline-block;border-radius:4px;padding:3px 7px;color:var(--accent);font-size:12px;background:var(--accent-soft)}
.theme-market-main-value{font-size:28px;font-weight:600;letter-spacing:-.5px;margin-top:18px;font-variant-numeric:tabular-nums}.theme-market-metric-label{font-size:12px;color:var(--muted)}
.theme-market-metrics{display:grid;gap:12px;margin-top:18px;padding-top:14px;border-top:1px solid var(--border)}.theme-market-metrics>div{display:flex;justify-content:space-between;gap:8px;font-size:12px}.theme-market-metrics span{color:var(--muted)}.theme-market-metrics strong{text-align:right;color:var(--text);font-weight:500}.theme-market-metrics small{font-size:12px;color:var(--muted)}
.theme-market-data-note{color:var(--muted);font-size:12px;line-height:1.7;margin:14px 0}.theme-market-data-note summary{cursor:pointer}.theme-market-data-note p{margin:5px 0 0}.theme-market-actions{display:grid;grid-template-columns:1fr;gap:8px}
.theme-market-actions a,.theme-market-actions button{display:block;padding:9px 12px;border-radius:5px;font:inherit;font-size:12px;text-align:center;text-decoration:none;cursor:pointer}.theme-market-primary{background:var(--accent);color:var(--bg);font-weight:600!important}.theme-market-actions button{border:1px solid var(--border);color:var(--text);background:transparent}
.theme-market-proof{border-top:1px solid var(--border);margin-top:18px;padding-top:12px;font-size:12px;color:var(--muted)}.theme-market-proof summary{cursor:pointer;color:var(--text)}.theme-market-proof p{margin:9px 0;line-height:1.6}.theme-market-proof dl{display:grid;grid-template-columns:1fr auto;gap:7px;margin:12px 0}.theme-market-proof dt{color:var(--muted)}.theme-market-proof dd{text-align:right;max-width:135px;overflow-wrap:anywhere}.theme-market-proof a{display:block;margin:8px 0;color:var(--accent)}
.theme-market-footnote{font-size:12px;color:var(--muted);line-height:1.5;margin-top:10px}
.theme-market-table-wrap{overflow:auto;border:1px solid var(--border);border-radius:6px;max-height:480px}.theme-market-table{width:100%;min-width:680px;border-collapse:collapse;text-align:left;font-size:12px}.theme-market-table th,.theme-market-table td{padding:11px 10px;border-bottom:1px solid var(--border)}.theme-market-table th{position:sticky;top:0;background:var(--surface-raised);color:var(--muted);font-size:12px;font-weight:500}.theme-market-table tr.is-selected{background:var(--accent-soft)}.theme-market-table td button{border:0;background:none;color:var(--text);text-align:left;font:inherit;font-weight:500;cursor:pointer}.theme-market-table td small{display:block;color:var(--muted);font-size:12px}
button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(max-width:1200px){.theme-market-grid,.theme-clue-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:880px){.theme-market-layout{grid-template-columns:1fr}.theme-market-detail{position:static;border-left:0;border-top:1px solid var(--border);padding:20px 0 0}.theme-market-actions{grid-template-columns:1fr 1fr}}
@media(max-width:600px){.theme-market{padding:16px}.theme-market-heading{align-items:start;flex-direction:column;gap:6px}.theme-market-tools{align-items:start;flex-direction:column}.theme-market-grid,.theme-clue-grid{grid-template-columns:1fr}.theme-cluster{min-height:180px}.theme-bubbles{min-height:115px}.theme-clue-actions{flex-wrap:wrap}.theme-market-main-value{font-size:26px}}
@media(prefers-reduced-motion:reduce){.theme-bubble{transition:none}}

/* The atlas groups actual theme rows; exact values and coverage remain visible. */
.theme-market{border:1px solid var(--border);border-radius:12px;padding:22px;background:var(--panel)}
.theme-market-heading h2{font-size:16px;font-weight:700}.theme-market-heading p,.theme-market-asof{font-size:12px}.theme-layer-switch{gap:24px}.theme-layer-switch button{font-size:12px}.theme-layer-switch button.active{color:var(--accent)}
.theme-market-layout{grid-template-columns:minmax(0,1fr) 298px;gap:22px}.theme-market-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.theme-cluster{min-height:230px;border-radius:8px;border-color:color-mix(in srgb,var(--accent) 15%,var(--border));padding:17px}.theme-cluster.is-selected{border-color:var(--accent)}
.theme-select{border:0;background:none;padding:0;display:flex;flex-direction:column;gap:6px;text-align:left;cursor:pointer}.theme-select>span{font-size:12px;color:var(--muted)}.theme-select>strong{font-size:21px;font-weight:650;letter-spacing:-.5px;color:var(--text);line-height:1.4;overflow-wrap:anywhere}.theme-cluster-volume{display:flex;flex-wrap:wrap;gap:5px 9px;align-items:baseline;margin:17px 0 12px}.theme-cluster-volume>strong{font-size:18px;font-weight:650;color:var(--text);font-variant-numeric:tabular-nums}.theme-cluster-volume>span,.theme-cluster-volume>small{font-size:12px;color:var(--muted)}.theme-cluster-volume>small{margin-left:auto}.theme-stock-baseline{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);border-bottom:1px solid color-mix(in srgb,var(--accent) 12%,var(--border));padding-bottom:11px}.theme-stock-baseline strong{color:var(--text);font-weight:600}.theme-stock-baseline>span:last-child{border-left:1px solid var(--border);padding-left:8px}
.theme-bubbles{align-content:start;justify-content:start;align-items:start;min-height:0;gap:6px}.theme-bubble{border-radius:5px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 9px;width:100%;height:auto;min-height:44px;padding:8px 10px;background:color-mix(in srgb,var(--panel) 80%,transparent);border-color:color-mix(in srgb,var(--accent) 17%,var(--border));text-align:left}.theme-bubble.tone-up,.theme-bubble.tone-down,.theme-bubble.tone-flat{border-color:color-mix(in srgb,var(--accent) 17%,var(--border));background:color-mix(in srgb,var(--panel) 80%,transparent)}.theme-bubble small{grid-column:1/-1;font-size:12px;color:var(--muted)}.theme-bubble-symbol,.theme-bubble-change{font-size:12px}.theme-bubble.is-selected{outline:1px solid var(--accent);outline-offset:0}.theme-bubble:hover{transform:none;background:var(--panel)}.theme-cluster-breadth{font-size:12px;margin:7px 0 11px}.theme-cluster-more{font-size:12px}.theme-breadth-bar{margin-top:11px}.theme-table-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:20px 0 12px;font-size:12px}.theme-table-heading strong{font-size:12px;font-weight:650}.theme-table-heading small{font-size:12px;color:var(--muted);margin-left:7px}.theme-table-heading>span{font-size:12px;color:var(--muted)}.theme-table-heading>button{font-size:12px;background:var(--accent-soft);border:0;border-radius:4px;padding:5px 8px;color:var(--accent);cursor:pointer}.theme-market-table-wrap{max-height:350px;border-radius:7px}.theme-market-table th{background:var(--bg);font-size:12px}.theme-market-table td{font-size:12px;padding:10px}.theme-market-table td small{font-size:12px;margin-top:4px}.theme-market-table tr:hover{background:var(--bg)}.theme-market-table tr.is-selected{background:var(--accent-soft)}.theme-market-detail{border:1px solid var(--border);border-radius:9px;padding:18px;background:var(--panel);top:20px}.theme-market-detail h3{font-size:20px;font-weight:650}.theme-market-main-value{font-size:29px}.theme-market-address,.theme-market-metric-label,.theme-market-metrics>div,.theme-market-data-note,.theme-market-proof{font-size:12px}.theme-market-actions a,.theme-market-actions button{font-size:12px}.theme-market-primary{color:var(--panel)}.theme-market-legend,.theme-market-footnote{font-size:12px;line-height:1.7}.theme-clue-card{border-radius:8px;background:var(--bg)}
@media(max-width:1150px){.theme-market-layout{grid-template-columns:minmax(0,1fr) 268px}.theme-cluster{padding:14px}.theme-market-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:900px){.theme-market-layout{grid-template-columns:1fr}.theme-market-detail{position:static;padding:18px;border:1px solid var(--border)}.theme-market-metrics{grid-template-columns:repeat(3,1fr);gap:12px}.theme-market-metrics>div{display:flex;flex-direction:column;gap:5px}.theme-market-metrics strong{text-align:left}.theme-market-actions{grid-template-columns:1fr 1fr}.theme-market-proof dl{grid-template-columns:1fr auto}}
@media(max-width:600px){.theme-market{padding:16px}.theme-market-grid{grid-template-columns:1fr}.theme-cluster{min-height:0;padding:15px}.theme-bubbles{min-height:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.theme-bubble{grid-template-columns:1fr;gap:4px}.theme-bubble small{grid-column:1}.theme-bubble-change{font-size:12px}.theme-market-tools{flex-direction:row;align-items:center}.theme-market-tools>span{font-size:12px;text-align:right}.theme-market-legend{display:block}.theme-market-legend>span{display:block}.theme-market-detail h3{font-size:18px}.theme-market-metrics{grid-template-columns:1fr}.theme-market-metrics>div{flex-direction:row}.theme-market-metrics strong{text-align:right}.theme-cluster-heading strong{font-size:19px}}


@media(max-width:600px){.theme-market-tools{flex-wrap:wrap;gap:9px}.theme-market-tools>span{text-align:left;line-height:1.6}.theme-cluster-volume{line-height:1.6}.theme-stock-baseline{flex-wrap:wrap;line-height:1.7}.theme-bubble{line-height:1.6}.theme-bubble-symbol,.theme-bubble-change,.theme-bubble small{overflow-wrap:anywhere;white-space:normal}.theme-market-metrics>div{line-height:1.7}.theme-market-metrics strong{max-width:58%;overflow-wrap:anywhere}.theme-table-heading{flex-wrap:wrap}}
</style>
