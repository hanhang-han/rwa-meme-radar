<template>
  <section class="panel spread-panel" aria-live="off">
    <div class="panel-head">
      <div><h2>{{ tr('价格比较', 'Price comparison') }}</h2><p class="hint">{{ tr('比较同一时间、同一单位的真实报价；价差不等于扣费后的交易收益。', 'Compare observed prices with aligned times and units. Spreads are not trading returns after costs.') }}</p></div>
      <span v-if="error" role="status">{{ tr('更新失败，保留历史', 'Update failed; history retained') }} <button @click="load">{{ tr('重试', 'Retry') }}</button></span>
    </div>
    <div class="spread-cards">
      <article><small>{{ tr('股票代币溢价', 'Stock-token premium') }}</small><ComparisonMetric :value="current?.premium" /><p>{{ tr('代币市场价相对每枚对应股票价值的偏离。', 'Token market price versus the stock value represented by one token.') }}</p></article>
      <article v-if="pool"><small>{{ tr('配对池价差', 'Pair-pool spread') }}</small><ComparisonMetric :value="pair?.spread" /><p>{{ tr('池内兑换报价相对独立市场报价的偏离。', 'Pool-implied quote versus independent market price.') }}</p></article>
      <article v-if="pool"><small>{{ tr('Meme 相对涨幅', 'Meme relative performance') }} · {{ windowLabel }}</small><ComparisonMetric :value="pair?.relative?.[windowSize]" unit="pp" /><p>{{ tr('Meme 涨幅减股票涨幅；不代表应当回归。', 'Meme return minus stock return; no convergence is implied.') }}</p></article>
    </div>
    <div class="spread-controls">
      <label>{{ tr('比较窗口', 'Comparison window') }} <select v-model="windowSize"><option value="1h">{{ tr('1 小时', '1 hour') }}</option><option value="24h">{{ tr('24 小时', '24 hours') }}</option></select></label>
      <p v-if="pair?.poolQuote">{{ tr('池内边际兑换比例', 'Marginal pool exchange ratio') }}: 1 {{ tr('枚股票侧代币', 'stock-side token') }} ≈ {{ ratio }} Meme · {{ date(pair.poolQuote.at) }}<br><small>{{ tr('包装代币按实际合约单位比较，非一股股票；不含手续费和滑点。', 'Wrapper units refer to the actual contract, not one stock share. Excludes fees and slippage.') }}</small></p>
    </div>
    <details class="spread-evidence"><summary>{{ tr('查看计算依据与报价来源', 'Calculation inputs and sources') }}</summary>
      <p>{{ tr('代币市场价', 'Token market price') }}: {{ amount(current?.premium?.inputs?.price, current?.premium?.inputs?.priceCurrency) }} · {{ current?.premium?.inputs?.provider || '—' }} · {{ date(current?.premium?.inputs?.quoteAt) }}</p>
      <p>{{ tr('股票参考价', 'Stock reference') }}: {{ amount(current?.premium?.inputs?.stockPrice, current?.premium?.inputs?.referenceCurrency) }} · {{ current?.premium?.inputs?.referenceProvider || '—' }} · {{ date(current?.premium?.inputs?.referenceAt) }}</p>
      <p>{{ tr('每枚对应股数', 'Shares per token') }}: {{ current?.premium?.inputs?.tokenToAssetRatio ?? '—' }} · {{ current?.premium?.inputs?.ratioSource || tr('来源待核实', 'Source unverified') }}</p>
      <p>{{ tr('交易时段', 'Market session') }}: {{ sessionLabel }} · {{ tr('方法版本', 'Method') }} {{ current?.method || '—' }}</p>
      <p v-if="pool">{{ tr('Meme 对照报价（独立性通过后才参与计算）', 'Meme comparison quote (used only after independence checks)') }}: {{ amount(pair?.spread?.inputs?.meme?.price, pair?.spread?.inputs?.meme?.currency) }} · {{ pair?.spread?.inputs?.meme?.provider || '—' }} · {{ date(pair?.spread?.inputs?.meme?.at) }}</p>
      <p v-if="pool">{{ tr('股票侧对照报价（独立性通过后才参与计算）', 'Stock-side comparison quote (used only after independence checks)') }}: {{ amount(pair?.spread?.inputs?.stock?.price, pair?.spread?.inputs?.stock?.currency) }} · {{ pair?.spread?.inputs?.stock?.provider || '—' }} · {{ date(pair?.spread?.inputs?.stock?.at) }}</p>
    </details>
    <div class="spread-controls"><label>{{ tr('走势图', 'Chart') }} <select v-model="chart"><option value="spread">{{ pool ? tr('配对池价差历史', 'Pair spread history') : tr('股票代币溢价历史', 'Stock-token premium history') }}</option><option v-if="pool" value="relative">{{ tr('股票与 Meme 走势对比', 'Stock and meme comparison') }}</option></select></label></div>
    <p v-if="chart === 'relative'" class="hint">{{ tr('共同起点为 100，仅比较涨跌。来源、证券调整版本或计价单位变化后重新建立基准；不表示相关性。', 'Common baseline of 100 compares performance only. A source, corporate-action version or unit change starts a new baseline; this is not evidence of correlation.') }}</p>
    <p v-if="chart === 'relative' && relative.some(p => !p.adjustmentVersion)" class="hint">{{ tr('部分股票报价缺少拆股等公司行动版本，暂不将其连成收益曲线。', 'Some equity observations lack corporate-action versions and are excluded from return curves.') }}</p>
    <ComparisonChart :rows="chartRows" :series="series" :label="chart === 'relative' ? tr('股票与 Meme 走势对比', 'Stock and meme comparison') : tr('价差历史', 'Spread history')" />
    <details class="spread-evidence"><summary>{{ tr('价差观察记录', 'Spread observations') }} · {{ alerts.length }}</summary>
      <p class="hint">{{ tr('仅实时对齐报价触发：溢价 2%、池价差 3%，持续 30 秒且至少 3 次新报价；恢复阈值为触发阈值的 60%。池价差还需新鲜流动性至少 1 万美元。', 'Live aligned quotes only: 2% premium or 3% pool spread for 30 seconds and at least 3 new observations. Recovery uses 60% of the trigger threshold. Pool spreads also need fresh liquidity of at least $10,000.') }}</p>
      <p v-if="!alerts.length">{{ tr('尚无符合条件的观察记录。', 'No qualifying observations yet.') }}</p>
      <p v-for="event in alerts" :key="event.id">{{ date(event.t) }} · {{ event.pool ? tr('配对池价差', 'Pair-pool spread') : tr('股票代币溢价', 'Stock-token premium') }} · {{ event.kind === 'spread-expanded' ? tr('持续超出观察阈值', 'Persistently above observation threshold') : tr('回到观察阈值内', 'Returned within observation threshold') }} · {{ event.value.toFixed(2) }}%</p>
    </details>
  </section>
</template>
<script setup>
import { computed, onUnmounted, ref, watch } from 'vue';
import { tr } from '../i18n';
import { date } from '../utils/format';
import { normalizedHistory, chartSamples, windowHistory } from '../utils/comparisons';
import { getJSON } from '../api/client';
import { useDashboardStore } from '../stores/dashboard';
import { useComparisonStore } from '../stores/comparisons';
import ComparisonMetric from './ComparisonMetric.vue';
import { useMinuteClock } from '../composables/useMinuteClock';
import ComparisonChart from './ComparisonChart.vue';
const props = defineProps({ chain: String, token: String, pool: String });
const store = useComparisonStore();
const dash=useDashboardStore();
const now = useMinuteClock();
const error = ref(false), history = ref([]), relative = ref([]), alerts = ref([]), chart = ref('spread'), windowSize = ref('1h');
const current = computed(() => store.latest.get(`${props.chain}:${String(props.token).toLowerCase()}`));
const pair = computed(() => current.value?.pairs?.find(p => p.pool.toLowerCase() === props.pool?.toLowerCase()));
const ratio = computed(() => Number(pair.value?.poolQuote?.memePerStock).toLocaleString(undefined, { maximumSignificantDigits: 8 }));
const windowLabel = computed(() => windowSize.value === '1h' ? tr('1 小时', '1 hour') : tr('24 小时', '24 hours'));
const sessionLabel = computed(() => {
  const s = current.value?.premium?.inputs?.marketSession;
  return s === 'regular' ? tr('盘中', 'Regular') : s === 'extended' ? tr('盘前 / 盘后', 'Extended') : s === 'closed' ? tr('休市', 'Closed') : s === 'halted' ? tr('停牌', 'Halted') : tr('待核实，按快照处理', 'Unverified; treated as snapshot');
});
const amount = (v, currency) => {
  if (v == null) return '—';
  const unit = currency?.startsWith('asset:') ? `${tr('报价代币', 'quote token')} ${currency.split(':').at(-1).slice(0, 8)}…` : currency || tr('币种待核实', 'currency unverified');
  return `${Number(v).toLocaleString(undefined, { maximumSignificantDigits: 8 })} ${unit}`;
};
const chartRows = computed(() => {
  const duration = windowSize.value === '1h' ? 3600000 : 86400000;
  return chart.value === 'relative' ? chartSamples(normalizedHistory(windowHistory(relative.value, duration, now.value)), ['stock', 'meme']) : chartSamples(windowHistory(history.value, duration, now.value), ['value']);
});
const series = computed(() => chart.value === 'relative' ? [
  { key: 'stock', color: '#648cf4', label: tr('股票参考价', 'Stock reference') },
  { key: 'meme', color: '#20ba98', label: 'Meme' },
] : [{ key: 'value', color: '#648cf4', label: tr('价差（%）', 'Spread (%)') }]);
function mergeRows(old, rows) {
  return [...new Map([...old, ...rows].map(p => [p.at, p])).values()].sort((a, b) => a.at-b.at).filter(p => p.at >= Date.now()-86400000).slice(-12000);
}
function appendCurrent() {
  const metric = props.pool ? pair.value?.spread : current.value?.premium;
  if (metric?.value != null) history.value = mergeRows(history.value, [{ at: metric.at, value: metric.value, version: metric.inputs?.ratioVersion, method: metric.method }]);
  const p = pair.value?.relativePoint;
  if (p) relative.value = mergeRows(relative.value, [{ at: p.at, stock: p.stock, meme: p.meme, ...p.inputs }]);
}
let generation = 0, timer, loading = false;
async function load() {
  if (!props.token || loading) return;
  const request = generation;
  loading = true;
  try {
    const query = props.pool ? `?pool=${encodeURIComponent(props.pool)}` : '';
    const data = await getJSON(`comparisons/${encodeURIComponent(props.chain)}/${encodeURIComponent(props.token)}${query}`);
    if (request !== generation) return;
    store.receive(data.current); history.value = mergeRows(history.value, data.history ?? []); relative.value = mergeRows(relative.value, data.relativeHistory ?? []);
    alerts.value = data.alerts ?? []; appendCurrent(); error.value = false;
  } catch { if (request === generation) error.value = true; }
  finally { if (request === generation) loading = false; }
}
watch(() => [props.chain, props.token, props.pool], () => {
  generation++; loading = false; history.value = []; relative.value = []; alerts.value = []; error.value = false;
  clearInterval(timer); load(); timer = setInterval(() => { if (!document.hidden && !dash.hasProjectionStream) load(); }, 20000);
}, { immediate: true });
watch(current, appendCurrent);
function onResourceChange(event){const r=event.detail??{};if(!['comparison','alert','all'].includes(r.kind))return;if(r.chainId&&String(r.chainId)!==props.chain||r.token&&r.token.toLowerCase()!==props.token.toLowerCase())return;load();}
if(typeof window!=='undefined')window.addEventListener('resource-change',onResourceChange);
onUnmounted(() => { generation++; clearInterval(timer);window.removeEventListener('resource-change',onResourceChange); });
</script>
<style scoped>
.spread-panel { margin-block: 16px; }
.spread-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
.spread-cards article { padding: 16px; border: 1px solid var(--line, #293246); border-radius: 8px; display: flex; flex-direction: column; gap: 10px; }
.spread-cards article > small { font-size: 13px; }
.spread-cards p, .spread-evidence, .spread-controls { font-size: 12px; color: var(--muted, #8795a7); }
.spread-cards p { margin: 0; line-height: 1.6; }
.spread-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 16px; margin: 16px 0; }
.spread-controls select { padding: 6px; margin-left: 8px; }
.spread-evidence { margin-top: 16px; }
.spread-evidence summary { cursor: pointer; }
</style>
