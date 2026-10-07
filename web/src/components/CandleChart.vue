<template>
  <div>
    <div class="tab-group" id="v2ChartTabs">
      <button class="tab" :class="{ active: mode === 'line' }" :title="tr('使用所选周期收盘价或已保存真实报价画线。','Plot selected-interval closes or saved actual quote observations.')" @click="setMode('line')">{{ tr('分时', 'Line') }}</button>
      <button class="tab" :class="{ active: mode === 'candle' }" :title="tr('每根 K 线表示所选市场在一个周期内的开、高、低、收。','Each candle shows open, high, low and close for one interval in the selected market.')" @click="setMode('candle')">{{ tr('K线', 'Candles') }}</button>
      <template v-if="mode === 'candle'">
        <button v-for="b in bars" :key="b" class="tab" :class="{ active: bar === b }" :title="tr('每根 K 线周期：','Candle interval: ')+b" @click="setBar(b)">{{ b }}</button>
      </template>
      <button class="tab" @click="goLatest">{{ tr('回到最新', 'Go live') }}</button>
    </div>
    <p v-if="mode === 'candle' && lastBar" class="chart-ohlc-label">{{ tr('最新 K 线','Latest candle') }} · {{ bar }} · {{ candleInfo?.priceCurrency || tr('计价单位待核实','quote unit unverified') }}</p>
    <div v-if="mode === 'candle' && lastBar" class="x-coverage" data-chart-ohlc>
      <span :title="tr('最新一根 K 线开盘价格，计价单位以图内说明为准。','Latest candle open price; quote units follow the chart notes.')">{{ tr('开盘','Open') }} {{ priceText(lastBar.o) }}</span><span :title="tr('最新一根 K 线周期内最高价格。','Highest price within the latest candle interval.')">{{ tr('最高','High') }} {{ priceText(lastBar.h) }}</span>
      <span :title="tr('最新一根 K 线周期内最低价格。','Lowest price within the latest candle interval.')">{{ tr('最低','Low') }} {{ priceText(lastBar.l) }}</span><span :title="tr('最新一根 K 线收盘价格；未结束周期会继续更新。','Latest candle close price; the open interval continues updating.')">{{ tr('收盘','Close') }} {{ priceText(lastBar.c) }}</span>
      <span :title="tr('最新一根 K 线周期内的实际成交；缺值显示 —。','Actual volume within the latest candle interval; missing data is shown as —.')">{{ volumeUnit }} {{ volumeText(lastBar) }}</span>
    </div>
    <div class="chart-stage" :data-chart-state="libraryState === 'ready' ? displayState : libraryState === 'unavailable' ? 'unavailable' : 'loading'">
      <div ref="el" class="chart x-candles"></div>
      <div v-if="libraryState !== 'ready' || displayState !== 'ready'" class="chart-empty" role="status">
        <span>{{ chartMessage }}</span>
        <button v-if="libraryState === 'unavailable'" type="button" @click="retryChartLibrary">{{ tr('重新加载图表', 'Retry chart') }}</button>
      </div>
    </div>
    <p v-if="hint" class="hint">{{ hint }}</p>
    <p v-if="candleInfo?.rows?.length && (mode === 'candle' || candleClosePoints(candleInfo.rows).length >= 2)" class="hint" :class="{ 'quote-stale': candleTail !== 'current' }" data-chart-status>
      {{ candleStatusLabel }}
    </p>
    <details class="chart-data-notes">
      <summary>{{ tr('图表数据说明', 'Chart data notes') }}</summary>
      <p v-if="mode === 'candle' && candleInfo?.rows?.length">{{ tr('K 线基于真实成交；横轴标注每根 K 线的开盘时间。', 'Candles use actual trades; the horizontal axis marks each candle open.') }}<template v-if="candleInfo.streamAt"> {{ tr('最近一根含逐笔更新。', 'The latest candle includes trade-by-trade updates.') }}</template></p>
      <p v-if="mode === 'line' && candleClosePoints(candleInfo?.rows).length >= 2">{{ tr('分时线取自所选周期 K 线的收盘价。', 'The line uses closes from the selected candle interval.') }}</p>
      <p v-if="candleInfo?.pool">{{ tr('池地址', 'Pool') }}：{{ candleInfo.pool }}</p>
      <p v-if="candleInfo?.liveMarket" data-chart-live-market>{{ tr('实时成交；未采集的历史区间暂不显示。','Live trades; history outside the observed range is not shown.') }}</p>
      <p v-if="candleInfo?.historicalCoverageStatus === 'backfilling' || candleInfo?.historyGap" data-chart-history-pending>{{ tr('较早的成交记录正在补充。', 'Earlier trade history is being collected.') }}</p>
      <p v-if="mode === 'line' && candleClosePoints(candleInfo?.rows).length < 2 && displayState === 'ready'">{{ tr('已保存观测的来源与计价以各条记录为准。', 'The source and unit of saved observations follow each record.') }}</p>
      <p v-if="mode === 'line' && candleClosePoints(candleInfo?.rows).length < 2 && samplesMissingProvenance">{{ tr('部分历史样本缺少来源证明，未拼接最新价。', 'Some historical observations lack provenance; the latest quote is not joined.') }}</p>
      <p data-chart-clock>{{ tr('图表时区', 'Chart timezone') }}：{{ chartTimeZone }} · {{ tr('当前时间', 'Now') }} {{ chartDateTime(now / 1000) }}</p>
      <p v-if="mode === 'candle' && candleInfo && lastCandleAt">{{ tr('最后一根开盘时间', 'Last candle opened') }}：{{ chartDateTime(lastCandleAt / 1000) }}</p>
      <p v-if="mode === 'candle' && candleInfo?.lastTradeAt">{{ tr('最后成交', 'Last trade') }}：{{ chartDateTime(candleInfo.lastTradeAt / 1000) }}</p>
      <p v-if="mode === 'candle' && candleInfo?.nextRefreshAt > now && candleInfo?.status !== 'current'">{{ tr('下次更新', 'Next update') }}：{{ chartDateTime(candleInfo.nextRefreshAt / 1000) }}</p>
      <p v-if="mode === 'candle' && candleInfo?.error === 'quota-exhausted'">{{ tr('OKX 当日额度已用尽', 'OKX daily budget exhausted') }}</p>
    </details>
    <div v-if="offerNativePool" class="chart-native-fallback" data-chart-native-fallback role="status">
      <span>{{ tr('美元图表更新较慢。这个链上池最近有成交；切换后图表改为 ' + nativePool.priceCurrency + ' 计价。', 'The USD chart is delayed. This on-chain pool has recent trades; switching changes the chart unit to ' + nativePool.priceCurrency + '.') }}</span>
      <button type="button" @click="emit('select-pool', nativePool.poolId)">{{ tr('查看实时池价', 'View live pool price') }} →</button>
    </div>
  </div>
</template>

<script>
const CANDLE_PERIOD_MS = {'1m':60_000,'5m':300_000,'15m':900_000,'1H':3_600_000,'4H':14_400_000,'1D':86_400_000,'1W':604_800_000};
const metricNumber = value => (typeof value === 'number' || typeof value === 'string' && value.trim() !== '') && Number.isFinite(Number(value)) ? Number(value) : null;

export function candleDelayMs(info, bar, nowMs = Date.now()) {
  if (!Number.isFinite(nowMs) || !info) return null;
  for (const key of ['providerDelayMs','delayMs']) {
    const delay = metricNumber(info[key]);
    if (delay != null && delay >= 0) return delay;
  }
  // A successful HTTP request can return old history. Its completion time
  // must never make an old candle look newer than its market timestamp.
  const tail = [info.lastCandleAt,info.rows?.at(-1)?.t].map(metricNumber).find(value => value > 0 && value <= nowMs);
  const interval = CANDLE_PERIOD_MS[bar];
  // A daily/weekly candle naturally opened hours/days ago. Only time beyond
  // its full interval can indicate a delayed tail, never its opening age.
  return tail > 0 && tail <= nowMs && interval ? Math.max(0, nowMs - tail - interval) : null;
}

export function candleDelayLabel(info, bar, nowMs = Date.now(), language = 'zh') {
  const delay = candleDelayMs(info,bar,nowMs);
  if (!(delay > 0)) return language === 'en' ? 'Market data has not updated yet' : '行情暂未更新';
  if (delay < 60_000) return language === 'en' ? 'Market data delayed less than 1 min' : '行情延迟不足 1 分钟';
  const minutes = Math.floor(delay / 60_000);
  return language === 'en' ? `Market data delayed ${minutes} min` : `行情延迟 ${minutes} 分钟`;
}

export function shouldOfferNativePool(info, bar, nowMs, selectedPool, nativePool) {
  return !selectedPool && info?.priceCurrency === 'USD' && !!nativePool?.poolId
    && !!nativePool?.priceCurrency && candleDelayMs(info, bar, nowMs) >= 600_000;
}

export function candleClosePoints(rows) {
  const points = new Map();
  for (const row of rows ?? []) {
    const time = metricNumber(row?.t), close = metricNumber(row?.c);
    if (time > 0 && close != null) points.set(Math.floor(time / 1000), close);
  }
  return [...points].sort((a,b) => a[0]-b[0]).map(([time,value]) => ({time,value}));
}

const observedVolume = value => metricNumber(value) != null && Number(value) >= 0;
export function candleVolumeField(rows) {
  // One chart must use one volume unit. Use the latest observed unit and leave
  // bars with no value in that unit blank; a measured zero remains a zero.
  for (let i = (rows?.length ?? 0) - 1; i >= 0; i--) {
    if (observedVolume(rows[i]?.vu)) return 'vu';
    if (observedVolume(rows[i]?.v)) return 'v';
  }
  return null;
}
export function candleVolumeData(rows, colors, field = candleVolumeField(rows)) {
  if (!field) return [];
  return (rows ?? []).flatMap(row => {
    const time = metricNumber(row?.t), value = metricNumber(row?.[field]);
    return time > 0 && value != null && value >= 0
      ? [{time:Math.floor(time / 1000),value,color:Number(row.c) >= Number(row.o) ? colors.upVolume : colors.downVolume}]
      : [];
  });
}
</script>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { getCandles } from '../api/client';
import { tr, useI18n } from '../i18n';
import { candleTailState, chartTimeZone, chartTime, chartDateTime, samplePoints } from '../utils/chart-time';
import { pricePrecision } from '../utils/realtime';
import { priceNumber } from '../utils/format';
import { useCandleStore } from '../stores/candles';
import { useDashboardStore } from '../stores/dashboard';
import { mergeCandleRows, candlePacketMatches, candleKey, snapshotCandleRows, candleResponseMatches, candleStreamState } from '../utils/candles';
import { loadChartLibrary } from '../utils/chart-library';
import { getLiveMarketCandles, getLiveMarkets, liveMarketSnapshotMatches, liveMarketHealthMatches, mergeLiveMarketHealth, liveMarketObservationState, openLiveMarketStream } from '../utils/live-market-stream';
import { updateChartPalette } from '../utils/chart-preferences';

const props = defineProps({
  asset: { type: Object, required: true },
  samples: { type: Array, default: () => [] },
  pool: { type: String, default: '' },
  nativePool: { type: Object, default: null },
  initialMode: { type: String, default: 'line' },
});
const emit = defineEmits(['select-pool']);

const streams = useCandleStore();
const dashboard = useDashboardStore();
const venueOf = () => props.pool ? 'dex' : (props.asset.venue === 'binance-alpha' || String(props.asset.provider).toLowerCase().includes('alpha')) ? 'binance-alpha' : props.asset.priceScope === 'exchange' && String(props.asset.provider).toLowerCase() === 'binance' ? 'binance' : 'dex';
const el = ref(null);
const mode = ref(props.initialMode === 'candle' ? 'candle' : 'line');
let modeUserSelected = false;
const bar = ref('5m');
const bars = ['1m', '5m', '15m', '1H', '4H', '1D', '1W'];
const hint = ref('');
const samplesMissingProvenance = computed(() => !props.pool && (props.samples ?? []).some(point => !point.provenance));
const displayState = ref('loading');
const libraryState = ref('loading');
const chartMessage = computed(() => libraryState.value === 'loading'
  ? tr('正在加载图表…', 'Loading chart…')
  : libraryState.value === 'unavailable'
    ? tr('图表暂时无法加载，请重试。', 'The chart could not be loaded. Please retry.')
  : displayState.value === 'loading'
  ? tr('正在读取所选市场的历史行情…', 'Loading history for this market…')
  : displayState.value === 'unavailable'
    ? tr('该市场历史行情暂时无法读取，请稍后重试或切换市场。', 'This market history could not be loaded. Retry later or switch markets.')
  : tr('该市场暂无足够的连续观测，至少需要两个真实数据点。可切换市场或 K 线周期。', 'This market needs at least two recorded observations. Try another market or candle interval.'));
const now = ref(Date.now());
const candleInfo = ref(null);
const candleTail = computed(() => candleTailState(candleInfo.value, bar.value, now.value));
const offerNativePool = computed(() => shouldOfferNativePool(candleInfo.value, bar.value, now.value, props.pool, props.nativePool));
const candleStatusLabel = computed(() => {
  const info=candleInfo.value;
  if (!Array.isArray(info?.rows) || !info.rows.length)
    return tr('此市场暂无可用 K 线', 'No candles are available for this market');
  const liveState=liveMarketObservationState(info,now.value);
  if(liveState==='quiet')return info.historicalCoverageStatus==='backfilling'
    ? tr('暂无新成交 · 较早历史补充中', 'No new trades · loading earlier history')
    : tr('观测范围内暂无新成交','No new trades in the observed range');
  if(liveState==='unverified')return tr('实时状态待确认','Live status awaiting confirmation');
  if(liveState==='live' && info.historicalCoverageStatus==='backfilling')
    return tr('实时成交 · 较早历史补充中', 'Live trades · loading earlier history');
  if(!info.liveMarket && info.staleReason==='upstream-error')
    return tr('行情更新未成功，显示上次记录', 'Refresh failed; showing the previous records');
  if(!info.liveMarket && info.staleReason==='collector-overdue')
    return tr('行情刷新较慢，等待更新', 'Market refresh is delayed; awaiting an update');
  // A recent, persisted pool trade is useful even while the historical
  // scanner is behind. Keep the gap visible without calling its live tail old.
  if(info?.coverageStatus==='backfilling' && info.lastTradeAt && now.value-Number(info.lastTradeAt)>=0 && now.value-Number(info.lastTradeAt)<=30000)
    return tr('刚有新成交 · 较早历史不完整', 'New trade · earlier history incomplete');
  if(info?.coverageStatus==='backfilling' && info.marketStatus==='live' && !info.stale)return info.liveMarket
    ? tr('实时成交 · 历史记录补充中', 'Live trades · loading earlier history')
    : tr('行情已更新 · 较早历史补充中', 'Market data updated · loading earlier history');
  if(info?.marketStatus==='quiet' && !info.stale && now.value-Number(info.scanAt||0)<=20000)return tr('观测范围内暂无新成交', 'No new trades in the observed range');
  const declared = info?.status;
  const state = info?.stale ? 'stale' : declared === 'current' && candleTail.value === 'quiet' ? 'quiet' : declared ?? candleTail.value;
  const delayLabel = () => candleDelayLabel(info,bar.value,now.value,lang.lang);
  const sourceDelay = [info?.providerDelayMs,info?.delayMs].map(metricNumber).find(value => value != null && value >= 0);
  if (state === 'current' && sourceDelay > 0) return delayLabel();
  return ({
    collecting: tr('暂无行情，等待更新', 'Waiting for market data'),
    partial: tr('历史记录不完整，正在补充', 'Incomplete history; more data is loading'),
    stale: delayLabel(),
    delayed: delayLabel(),
    quiet: delayLabel(),
    missing: tr('此市场暂无可用 K 线', 'No candles are available for this market'),
    unsupported: tr('行情源暂不支持此市场 K 线', 'The source does not support candles for this market'),
    current: tr('行情已更新', 'Market data updated'),
    unavailable: tr('此市场 K 线暂不可用', 'Candles temporarily unavailable for this market'),
  })[state] ?? tr('行情状态未知', 'Market status unknown');
});
const lastCandleAt = computed(() => candleInfo.value?.rows?.at(-1)?.t);
const lastBar = computed(() => candleInfo.value?.rows?.at(-1) ?? null);
const volumeField = computed(() => candleVolumeField(candleInfo.value?.rows));
const volumeUnit = computed(() => volumeField.value === 'vu'
  ? `${tr('成交额', 'Quote volume')} (${candleInfo.value?.volumeCurrency ?? candleInfo.value?.priceCurrency ?? tr('单位待核实', 'unit unverified')})`
  : volumeField.value === 'v' ? tr('成交量(代币)', 'Volume (token)') : tr('成交量', 'Volume'));
const { lang } = useI18n();
let paintSequence = 0;
let sourceKey = '';
let lastCandleTime = 0;
let renderedRows = [];
const selection = () => ({chainId:props.asset.chainId ?? '196',token:props.asset.token,venue:venueOf(),bar:bar.value,poolId:props.pool,marketId:props.pool || candleInfo.value?.marketId || props.asset.marketId || ''});

const subscriptionId = Symbol('chart');
watch(() => candleKey(selection()), () => streams.subscribe(subscriptionId,selection()), {immediate:true});

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let lineSeries = null;
let priceLine = null;
let candleLen = 0;
const candleMem = new Map();
const liveConnected = ref(false);
let liveStream = null, liveStreamKey = '';
let healthGeneration=0,healthRequest=null,healthController=null,lastHealthCheckAt=0;
function closeLiveStream() {
  ++healthGeneration;healthController?.abort();healthController=null;healthRequest=null;lastHealthCheckAt=0;
  liveStream?.close(); liveStream = null; liveStreamKey = ''; liveConnected.value = false;
}
async function checkLiveHealth(key,requested,cacheKey){
  const previous=candleMem.get(cacheKey);
  if(!mounted || liveStreamKey!==key || !previous?.liveMarket || healthRequest)return;
  const at=Date.now();
  if(at-lastHealthCheckAt<10000)return;
  if(previous.marketStatus!=='quiet' && Number(previous.scanAt)>0 && at-Number(previous.scanAt)<10000)return;
  lastHealthCheckAt=at;
  const generation=healthGeneration,controller=new AbortController();healthController=controller;
  const request=getLiveMarkets(requested.chainId,requested.token,{signal:controller.signal,pool:requested.poolId,bar:requested.bar});healthRequest=request;
  try{
    const result=await request;
    if(!mounted || controller.signal.aborted || generation!==healthGeneration || liveStreamKey!==key)return;
    if(result.epoch!==previous.epoch)return;
    const current=candleMem.get(cacheKey);
    const market=result.markets?.find(m=>liveMarketHealthMatches(m,requested,current));
    if(!market)return;
    const entry=mergeLiveMarketHealth(current,market);
    candleMem.set(cacheKey,entry);candleInfo.value=entry;
  }catch{/* The last scan evidence ages naturally; connection heartbeats do not refresh it. */}
  finally{if(healthRequest===request){healthRequest=null;healthController=null;}}
}
function connectLiveStream(snapshot, requested, cacheKey) {
  const key = `${candleKey(requested)}:${snapshot.epoch}:${snapshot.priceCurrency}`;
  if (liveStreamKey === key) return;
  closeLiveStream(); liveStreamKey = key;
  liveStream = openLiveMarketStream({selection:requested,snapshot,
    onStatus: state => { if (liveStreamKey === key) {liveConnected.value = state.connected;if(state.heartbeat)checkLiveHealth(key,requested,cacheKey);} },
    onCandle: packet => {
      if(liveStreamKey !== key || !candlePacketMatches(packet,selection()))return;
      // The old dashboard journal can still publish this pool. Keep this
      // chart's authoritative series separate so its older rows cannot join.
      const previous=candleMem.get(cacheKey);
      const row={...packet.row,sourceEventAt:packet.sourceEventAt,receivedAt:packet.receivedAt};
      const entry={...snapshot,...previous,...packet,rows:mergeCandleRows(previous?.liveMarket?previous.rows:snapshotCandleRows(snapshot),[row]),
        ...candleStreamState(previous,packet),pool:packet.poolId??packet.pool,streamAt:Date.now(),cachedAt:Date.now(),liveMarket:true};
      candleMem.set(cacheKey,entry); candleInfo.value=entry;
      streams.receive(packet); paint();
    },
    onReset: packet => {
      if(liveStreamKey !== key)return;
      closeLiveStream(); candleMem.delete(cacheKey); candleInfo.value=null;
      displayState.value='loading'; paintSequence++; killChart(); paint(true,true);
    },
  });
}
let lastPaintKey = '';
let lastHttpSnapshotAt = 0;
let mounted = false;

function lw() {
  return window.LightweightCharts;
}

// Canvas colors must be resolved values; CSS variables cannot be passed to the chart.
function chartPalette() {
  const styles = window.getComputedStyle(el.value);
  const token = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
  const alpha = (color, opacity) => {
    const rgb = color.match(/^#([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i);
    return rgb ? `rgba(${rgb.slice(1).map(part => parseInt(part, 16)).join(',')},${opacity})` : color;
  };
  const up = token('--up', '#137650');
  const down = token('--down', '#c14f39');
  const accent = token('--accent', '#8a682e');
  const border = token('--border', '#d8d0c2');
  return {
    up, down, accent, border,
    muted: token('--muted', '#796f60'),
    surface: token('--surface-raised', '#eee8de'),
    grid: alpha(border, .28),
    upVolume: alpha(up, .36),
    downVolume: alpha(down, .36),
    areaTop: alpha(up, .14),
    areaBottom: alpha(up, .02),
  };
}

function killChart() {
  if (chart) {
    try { chart.remove(); } catch { /* disposed */ }
  }
  chart = null;
  candleSeries = volumeSeries = lineSeries = priceLine = null;
  candleLen = 0;
  renderedRows = [];
}

function ensureChart() {
  if (!mounted || libraryState.value !== 'ready' || !lw() || !el.value) return null;
  if (!chart) {
    const colors = chartPalette();
    chart = lw().createChart(el.value, {
      width: el.value.clientWidth || 600,
      height: el.value.clientHeight || 380,
      layout: { background: { type: lw().ColorType.Solid, color: 'transparent' }, textColor: colors.muted, fontSize: 11 },
      grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
      rightPriceScale: { borderColor: colors.border },
      timeScale: { borderColor: colors.border, timeVisible: true, secondsVisible: false, rightOffset: 3, tickMarkFormatter: (time, type) => chartTime(time, type) },
      localization: { timeFormatter: time => chartDateTime(time) },
      crosshair: {
        mode: lw().CrosshairMode.Normal,
        vertLine: { color: colors.muted, labelBackgroundColor: colors.surface },
        horzLine: { color: colors.muted, labelBackgroundColor: colors.surface },
      },
    });
  }
  return chart;
}

const decOf = pricePrecision;
let axisZeroTolerance = 0;

function observeAxisPrices(values) {
  const observed = values.map(value => Math.abs(Number(value))).filter(value => Number.isFinite(value) && value > 0);
  axisZeroTolerance = observed.length
    ? Math.min(Math.min(...observed) / 2, Math.max(...observed) * Number.EPSILON * 8)
    : 0;
}

function axisPriceNumber(value) {
  const n = Number(value);
  // The scale can calculate a tiny residual at its zero tick. The tolerance
  // follows the current real observations, so a real tiny quote stays nonzero.
  return priceNumber(Number.isFinite(n) && Math.abs(n) < axisZeroTolerance ? 0 : value);
}

function linePriceFormat(points) {
  const observed = points.map(point => Number(point.value)).filter(value => Number.isFinite(value) && value !== 0);
  const precision = observed.length ? Math.max(...observed.map(decOf)) : 2;
  observeAxisPrices(observed);
  return { type: 'custom', formatter:axisPriceNumber, minMove: 10 ** -precision };
}

function priceText(value) {
  return priceNumber(value);
}

function volumeText(row) {
  const field = volumeField.value;
  const n = field ? Number(row?.[field]) : NaN;
  return field && observedVolume(row?.[field]) ? n.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '—';
}

async function loadCandles(address, barSel, limit = 500, force = false) {
  const venue = venueOf();
  const key = `${venue}:${props.asset.chainId ?? '196'}:${address}:${props.pool || props.asset.marketId || ''}:${barSel}`;
  const now = Date.now();
  const epoch=streams.resetVersion;
  const hit = candleMem.get(key);
  const requested={chainId:props.asset.chainId??'196',token:address,venue,bar:barSel,poolId:props.pool,marketId:props.pool||props.asset.marketId||''};
  if (!force && hit && now - hit.cachedAt < 20000 && (!props.pool || hit.liveMarketChecked)) {
    if(hit.liveMarket)connectLiveStream(hit,requested,key);
    return hit;
  }
  try {
    let r;
    if(requested.poolId){
      try{r=await getLiveMarketCandles(requested,limit);}catch(error){
        // Once this pool is registered, a transient new-feed failure must
        // not silently revert to the slower journal with different coverage.
        if(hit?.liveMarket)throw error;
      }
      if(r?.liveMarket && !liveMarketSnapshotMatches(r,requested))throw new Error('market-mismatch');
    }
    if(!r?.liveMarket){
      let deadline;
      try{r=await Promise.race([
        getCandles(requested.chainId, address, barSel, limit, venue, {poolId:requested.poolId,marketId:requested.poolId ? undefined : requested.marketId}),
        new Promise((_,reject)=>{deadline=setTimeout(()=>reject(new Error('legacy-history-timeout')),8000);}),
      ]);}finally{clearTimeout(deadline);}
    }
    if(!mounted || !r?.liveMarket && epoch!==streams.resetVersion)return null;
    if(!candleResponseMatches({...requested,pool:requested.poolId},selection()))return null;
    if (!r || !Array.isArray(r.rows)) return hit ?? null;
    if(!candleResponseMatches(r,requested))return hit?{...hit,stale:true,status:'stale',error:'market-mismatch'}:{...requested,rows:[],stale:true,status:'unavailable',error:'market-mismatch'};
    lastHttpSnapshotAt = Date.now();
    const live=r.liveMarket?null:streams.matching({...requested,marketId:r.marketId||requested.marketId});
    const previousLive=r.liveMarket && hit?.liveMarket && hit.epoch===r.epoch && hit.priceCurrency===r.priceCurrency ? hit.rows : [];
    const entry = { ...r, rows:mergeCandleRows(snapshotCandleRows(r),live?.rows??previousLive), cachedAt: now,liveMarketChecked:true };
    if(live)Object.assign(entry,{source:live.source,marketId:live.marketId,pool:live.poolId??live.pool??r.pool,priceCurrency:live.priceCurrency,volumeCurrency:live.volumeCurrency,streamAt:live.at},candleStreamState(r,live));
    candleMem.set(key, entry);
    if(r.liveMarket)connectLiveStream(r,requested,key);else closeLiveStream();
    return entry;
  } catch {
    return hit ? {...hit, stale: true, error: 'network-error'} : null;
  }
}

function latestPrice(info, asset, fallback) {
  if(props.pool)return fallback;
  const price = Number(asset?.price);
  if (!Number.isFinite(price)) return fallback;
  const source = String(info?.source ?? '').toLowerCase();
  const provider = String(asset?.provider ?? asset?.venue ?? '').toLowerCase();
  if (source.includes('binance') && provider.includes('binance') && asset?.priceScope === 'exchange') return price;
  if (source.includes('okx') && provider.includes('okx') && asset?.priceScope !== 'exchange') return price;
  return fallback;
}

function priceLineOptions(info, asset, fallback) {
  const price = latestPrice(info, asset, fallback);
  return {
    price,
    title: price === Number(asset?.price) ? tr('最新行情', 'Latest') : tr('最后收盘', 'Last close'),
  };
}

async function paint(reset = false, force = false) {
  if (!mounted || libraryState.value !== 'ready') return;
  const sequence = ++paintSequence;
  if (!lw() || !el.value) {
    hint.value = '';
    return;
  }
  const venue = props.asset.priceScope === 'exchange' ? props.asset.provider ?? props.asset.venue : 'dex';
  const paintKey = `${props.asset.chainId}:${props.asset.token}:${venue}:${props.asset.marketId??''}:${props.pool}:${mode.value}:${bar.value}`;
  if (paintKey !== lastPaintKey) {
    reset = true;
    lastPaintKey = paintKey;
    displayState.value = 'loading';
    candleInfo.value = null;
    hint.value = '';
    killChart();
  }
  const a = props.asset;
  const colors = chartPalette();
  if (mode.value === 'candle') {
    const info = await loadCandles(a.token, bar.value, 500, force);
    if (sequence !== paintSequence || !mounted || !el.value) return;
    candleInfo.value = info;
    const rows = info?.rows;
    const nextSource = `${info?.source ?? 'OKX'}:${info?.pool ?? ''}:${info?.priceCurrency ?? ''}:${candleVolumeField(rows) ?? ''}`;
    if (nextSource !== sourceKey) {reset = true; sourceKey = nextSource;}
    if (rows && rows.length) {
      observeAxisPrices(rows.flatMap(row => [row.o, row.h, row.l, row.c]));
      // OHLCV comes from the selected market; actual candle events update
      // its open bar. A quote from another market is never stitched into it.
      hint.value = `${bar.value} ${tr('K 线', 'candles')} · ${info.source ?? (a.priceScope === 'exchange' ? 'Binance' : 'OKX DEX')} · ${tr('计价', 'Quote')} ${info.priceCurrency ?? tr('待核实', 'unverified')}`;
      displayState.value = 'ready';
      const barsData = rows.map((r) => ({ time: Math.floor(r.t / 1000), open: r.o, high: r.h, low: r.l, close: r.c }));
      const volumePoints = candleVolumeData(rows,colors);
      if (!reset && candleSeries && volumeSeries && candleLen <= barsData.length) {
        const previous=new Map(renderedRows.map(r=>[r.t,r]));
        const correction=rows.some(r=>Math.floor(r.t/1000)<lastCandleTime && JSON.stringify(previous.get(r.t))!==JSON.stringify(r));
        if(correction)candleSeries.setData(barsData);
        else for(const r of rows)if(Math.floor(r.t/1000)>=lastCandleTime)candleSeries.update({time:Math.floor(r.t/1000),open:r.o,high:r.h,low:r.l,close:r.c});
        volumeSeries.setData(volumePoints);
        renderedRows=rows.map(r=>({...r}));
        candleLen = barsData.length;
        lastCandleTime = barsData.at(-1).time;
        if (priceLine) priceLine.applyOptions(priceLineOptions(info, a, barsData.at(-1).close));
        return;
      }
      killChart();
      const ch = ensureChart();
      if (!ch) return;
      const last = barsData[barsData.length - 1].close;
      const dec = decOf(last);
      candleSeries = ch.addCandlestickSeries({
        upColor: colors.up, downColor: colors.down,
        borderUpColor: colors.up, borderDownColor: colors.down,
        wickUpColor: colors.up, wickDownColor: colors.down,
        priceFormat: { type: 'custom', formatter:axisPriceNumber, minMove: 10 ** -dec },
      });
      candleSeries.setData(barsData);
      volumeSeries = ch.addHistogramSeries({ priceScaleId: '', priceFormat: { type: 'volume' } });
      volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      volumeSeries.setData(volumePoints);
      priceLine = candleSeries.createPriceLine({ ...priceLineOptions(info, a, last), color: colors.accent, lineWidth: 1, lineStyle: lw().LineStyle.Dashed, axisLabelVisible: true });
      renderedRows=rows.map(r=>({...r}));
      candleLen = barsData.length;
      lastCandleTime = barsData.at(-1).time;
      return;
    }
    hint.value = '';
  } else {
    // A detail summary often has no samples. Real candle closes still form a
    // valid price line; never derive a history from the latest quote/change.
    const info = await loadCandles(a.token, bar.value, 500, force);
    if (sequence !== paintSequence || !mounted || !el.value) return;
    candleInfo.value = info;
    if(info?.liveMarket && !modeUserSelected){
      // A newly observed market may have one real open candle. Candle mode
      // can display it immediately; the line still requires two data points.
      mode.value='candle'; await paint(true); return;
    }
    const closes = candleClosePoints(info?.rows);
    hint.value = closes.length >= 2
      ? `${tr('分时', 'Line')} · ${bar.value} ${tr('收盘价', 'closes')} · ${info.source ?? tr('来源待核实', 'source unverified')} · ${tr('计价', 'Quote')} ${info.priceCurrency ?? tr('待核实', 'unverified')}`
      : '';
  }
  const closes = candleClosePoints(candleInfo.value?.rows);
  const pts = closes.length >= 2 ? closes : props.pool ? [] : samplePoints(props.samples ?? [], a);
  if (mode.value === 'line' && closes.length < 2 && pts.length >= 2)
    hint.value = tr('分时 · 已保存价格观测 · 历史数据', 'Line · saved price observations · historical');
  if (pts.length < 2) {
    displayState.value = !candleInfo.value || candleInfo.value.error === 'network-error' ? 'unavailable' : 'missing';
    if (mode.value === 'line') hint.value = '';
    killChart();return;
  }
  displayState.value = 'ready';
  if (!reset && lineSeries) {
    lineSeries.applyOptions({ priceFormat: linePriceFormat(pts) });
    lineSeries.setData(pts);
    if (priceLine) priceLine.applyOptions({ price: pts[pts.length - 1].value });
    return;
  }
  killChart();
  const ch = ensureChart();
  if (!ch) return;
  lineSeries = ch.addAreaSeries({ lineColor: colors.up, topColor: colors.areaTop, bottomColor: colors.areaBottom, lineWidth: 2, priceFormat: linePriceFormat(pts) });
  lineSeries.setData(pts);
  priceLine = lineSeries.createPriceLine({ price: pts[pts.length - 1].value, color: colors.up, lineWidth: 1, lineStyle: lw().LineStyle.Dashed, axisLabelVisible: true, title: '' });
}

function setMode(m) {
  modeUserSelected = true;
  if (mode.value === m) return;
  mode.value = m;
  paint(true);
}

function setBar(b) {
  if (bar.value === b) return;
  bar.value = b;
  closeLiveStream();
  lastHttpSnapshotAt = 0;
  paint(true);
}

watch([() => props.asset?.price, () => props.asset?.fieldTimes?.price], () => {
  if (mode.value === 'candle' && priceLine && lastBar.value) {
    priceLine.applyOptions(priceLineOptions(candleInfo.value, props.asset, lastBar.value.c));
    return;
  }
  paint();
}, { deep: false });
watch(() => props.samples, () => { if (mode.value === 'line') paint(); }, { deep: false });
watch(() => lang.lang, () => paint(true));


watch(() => streams.resetVersion, (version,previous) => {
  if(candleInfo.value?.liveMarket)return;
  if(![...streams.resets.values()].some(packet=>packet.resetVersion>previous && candlePacketMatches(packet,selection())))return;
  candleMem.clear();candleInfo.value=null;displayState.value='loading';hint.value='';paintSequence++;killChart();paint(true,true);
});
watch(() => streams.version, () => {
  if(candleInfo.value?.liveMarket || liveStreamKey)return;
  const packet=streams.matching(selection());
  if(!packet)return;
  const selected=selection();
  const key=`${selected.venue}:${selected.chainId}:${selected.token}:${props.pool || props.asset.marketId || ''}:${selected.bar}`;
  const previous=candleMem.get(key);
  // Never concatenate a pool-native quote with token-wide USD history.
  const compatible=!previous || ((!previous.priceCurrency||previous.priceCurrency===packet.priceCurrency) && (!props.pool||String(previous.pool??previous.poolId??previous.marketId).toLowerCase()===props.pool.toLowerCase()));
  const entry={...(compatible?previous:{}),...packet,rows:mergeCandleRows(compatible?previous?.rows??[]:[],packet.rows),pool:packet.poolId??packet.pool,...candleStreamState(compatible?previous:null,packet),streamAt:packet.at,cachedAt:Date.now()};
  candleMem.set(key,entry);candleInfo.value=entry;
  paint();
});
// Compare each market field, not a newly allocated array on every quote.
// Frequent asset replacements must not clear an already displayed candle.
watch([() => props.pool, () => props.asset.token, () => props.asset.chainId, venueOf, () => props.pool ? '' : props.asset.marketId], () => { closeLiveStream();candleInfo.value=null;displayState.value='loading';hint.value='';lastHttpSnapshotAt=0;paint(true); });

function goLatest() {
  try { chart?.timeScale()?.scrollToRealTime?.(); } catch { /* chart not ready */ }
}

function onResize() {
  if (chart && el.value) {
    try { chart.resize(el.value.clientWidth || 600, el.value.clientHeight || 380); } catch { /* disposed */ }
  }
}

let refreshTimer = null;
let clockTimer = null;
let chartSizeObserver = null;
let libraryRetryTimer = null, libraryRetries = 0;
let preferenceFrame = null, systemAppearance = null;
function refreshChartPreferences() {
  cancelAnimationFrame(preferenceFrame);
  preferenceFrame=requestAnimationFrame(()=>{
    if(!mounted||!chart||!el.value)return;
    const colors=chartPalette();
    updateChartPalette({chart,candleSeries,lineSeries,volumeSeries,priceLine,volumeData:volumeSeries?candleVolumeData(renderedRows,colors):null},colors);
  });
}
async function retryChartLibrary(automatic = false) {
  if (!mounted) return;
  clearTimeout(libraryRetryTimer); libraryRetryTimer = null;
  if (automatic !== true) libraryRetries = 0;
  libraryState.value = 'loading';
  try {
    await loadChartLibrary();
  } catch {
    if (mounted) {
      libraryState.value = 'unavailable';
      if (libraryRetries < 2) libraryRetryTimer = setTimeout(() => retryChartLibrary(true), 1000 * 2 ** libraryRetries++);
    }
    return;
  }
  if (!mounted) return;
  libraryState.value = 'ready';
  // Quotes or matching candle packets can arrive before the script finishes.
  // Paint current props/cache once ready instead of waiting for another update.
  await paint(true);
}
onMounted(() => {
  mounted = true;
  window.addEventListener('product-preferences-change', refreshChartPreferences);
  window.addEventListener('storage', refreshChartPreferences);
  systemAppearance=window.matchMedia?.('(prefers-color-scheme: dark)');
  systemAppearance?.addEventListener?.('change',refreshChartPreferences);
  window.addEventListener('resize', onResize);
  if (typeof window.ResizeObserver === 'function' && el.value) {
    chartSizeObserver = new window.ResizeObserver(onResize);
    chartSizeObserver.observe(el.value);
  }
  clockTimer = setInterval(() => now.value=Date.now(),1000);
  refreshTimer = setInterval(() => {
    if (document.hidden) return;
    // The global projection connection alone does not prove this chart's
    // market sends candle events. Only a recent matching candle can replace
    // frequent HTTP checks; quiet or unsupported markets keep the fallback.
    const candlePacket = streams.matching(selection());
    const receivingCandles = candleInfo.value?.liveMarket ? liveConnected.value && candleInfo.value?.streamAt && Date.now()-candleInfo.value.streamAt<90000 : dashboard.hasProjectionStream && candlePacket?.at
      && Date.now() - candlePacket.at < 90000;
    if (!receivingCandles || Date.now() - lastHttpSnapshotAt >= 300000)
      paint(false, true);
  }, 31000);
  window.addEventListener('manual-refresh', onManualRefresh);
  retryChartLibrary();
});
onBeforeUnmount(() => {
  mounted = false;
  cancelAnimationFrame(preferenceFrame);
  window.removeEventListener('product-preferences-change', refreshChartPreferences);
  window.removeEventListener('storage', refreshChartPreferences);
  systemAppearance?.removeEventListener?.('change',refreshChartPreferences);
  chartSizeObserver?.disconnect(); chartSizeObserver = null;
  clearTimeout(libraryRetryTimer); libraryRetryTimer = null;
  closeLiveStream();
  streams.unsubscribe(subscriptionId);
  clearInterval(refreshTimer);
  clearInterval(clockTimer);
  paintSequence++;
  window.removeEventListener('resize', onResize);
  window.removeEventListener('manual-refresh', onManualRefresh);
  killChart();
});
function onManualRefresh() {
  if (libraryState.value === 'unavailable') retryChartLibrary();
  else paint(false, true);
}
</script>

<style scoped>
.chart-stage{position:relative;min-height:240px}.chart-empty{position:absolute;inset:0;display:flex;flex-direction:column;gap:12px;align-items:center;justify-content:center;padding:24px;text-align:center;line-height:1.6;color:var(--muted);font-size:13px;background:var(--panel)}
.chart-empty button{padding:7px 12px;border:1px solid var(--border);border-radius:2px;background:var(--surface-raised);color:var(--text);font:inherit;cursor:pointer}
.chart-ohlc-label{margin:8px 0 5px;color:var(--muted);font-size:11px;line-height:1.5}
.chart-data-notes{margin:6px 0 10px;color:var(--muted);font-size:11px}.chart-data-notes summary{cursor:pointer}.chart-data-notes p{margin:5px 0;overflow-wrap:anywhere}
.chart-native-fallback {display:flex;align-items:center;justify-content:space-between;gap:12px;margin:10px 0 14px;padding:12px 14px;border:1px solid color-mix(in srgb,var(--warning) 35%,var(--border));border-radius:2px;background:color-mix(in srgb,var(--warning) 6%,var(--bg));color:var(--text);font-size:12px;line-height:1.5;}
.chart-native-fallback button {flex:none;min-height:34px;padding:6px 10px;border:1px solid var(--warning);border-radius:999px;background:transparent;color:var(--warning);font:inherit;cursor:pointer;}
@media(max-width:700px){.chart-native-fallback{align-items:flex-start;flex-direction:column;}}
</style>
