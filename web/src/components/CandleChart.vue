<template>
  <div>
    <div class="tab-group" id="v2ChartTabs">
      <button class="tab" :class="{ active: mode === 'line' }" @click="setMode('line')">{{ tr('分时', 'Line') }}</button>
      <button class="tab" :class="{ active: mode === 'candle' }" @click="setMode('candle')">{{ tr('K线', 'Candles') }}</button>
      <template v-if="mode === 'candle'">
        <button v-for="b in bars" :key="b" class="tab" :class="{ active: bar === b }" @click="setBar(b)">{{ b }}</button>
      </template>
      <button class="tab" @click="goLatest">{{ tr('回到最新', 'Go live') }}</button>
    </div>
    <div v-if="lastBar" class="x-coverage" data-chart-ohlc>
      <span>O {{ priceText(lastBar.o) }}</span><span>H {{ priceText(lastBar.h) }}</span>
      <span>L {{ priceText(lastBar.l) }}</span><span>C {{ priceText(lastBar.c) }}</span>
      <span>{{ volumeUnit }} {{ volumeText(lastBar) }}</span>
    </div>
    <div ref="el" class="chart x-candles"></div>
    <p class="hint">{{ hint }}</p>
    <p class="hint" data-chart-clock>{{ tr('图表时区', 'Chart timezone') }}：{{ chartTimeZone }} · {{ tr('当前时间', 'Now') }} {{ chartDateTime(now / 1000) }}</p>
    <p v-if="candleInfo" class="hint" :class="{ 'quote-stale': candleTail !== 'current' }" data-chart-status>
      {{ candleStatusLabel }}
      · {{ tr('最后一根开盘时间', 'Last candle opened') }}：{{ lastCandleAt ? chartDateTime(lastCandleAt / 1000) : '—' }}
      <span v-if="candleInfo.lastTradeAt"> · {{ tr('最后成交', 'Last trade') }}：{{ chartDateTime(candleInfo.lastTradeAt / 1000) }}</span>
      <span v-if="candleInfo.nextRefreshAt > now && candleInfo.status !== 'current'"> · {{ tr('预计下次采集', 'Next collection due') }}：{{ chartDateTime(candleInfo.nextRefreshAt / 1000) }}</span>
      <span v-if="candleInfo.delayMs || candleInfo.providerDelayMs"> · {{ tr('源延迟', 'Source delay') }} {{ Math.ceil((candleInfo.delayMs || candleInfo.providerDelayMs) / 60000) }} {{ tr('分钟', 'min') }}</span>
      <span v-if="candleInfo.error === 'quota-exhausted'"> · {{ tr('OKX 当日额度已用尽', 'OKX daily budget exhausted') }}</span>
    </p>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { getCandles } from '../api/client';
import { tr, useI18n } from '../i18n';
import { candleTailState, chartTimeZone, chartTime, chartDateTime, samplePoints } from '../utils/chart-time';
import { pricePrecision } from '../utils/realtime';
import { useCandleStore } from '../stores/candles';
import { useDashboardStore } from '../stores/dashboard';
import { mergeCandleRows, candlePacketMatches, candleKey, snapshotCandleRows, candleResponseMatches, candleStreamState } from '../utils/candles';

const props = defineProps({
  asset: { type: Object, required: true },
  samples: { type: Array, default: () => [] },
  pool: { type: String, default: '' },
});

const streams = useCandleStore();
const dashboard = useDashboardStore();
const venueOf = () => props.pool ? 'dex' : (props.asset.venue === 'binance-alpha' || String(props.asset.provider).toLowerCase().includes('alpha')) ? 'binance-alpha' : props.asset.priceScope === 'exchange' && String(props.asset.provider).toLowerCase() === 'binance' ? 'binance' : 'dex';
const el = ref(null);
const mode = ref('candle');
const bar = ref('5m');
const bars = ['1m', '5m', '15m', '1H', '4H', '1D', '1W'];
const hint = ref('');
const now = ref(Date.now());
const candleInfo = ref(null);
const candleTail = computed(() => candleTailState(candleInfo.value, bar.value, now.value));
const candleStatusLabel = computed(() => {
  const info=candleInfo.value;
  // A recent, persisted pool trade is useful even while the historical
  // scanner is behind. Keep the gap visible without calling its live tail old.
  if(info?.coverageStatus==='backfilling' && info.lastTradeAt && now.value-Number(info.lastTradeAt)>=0 && now.value-Number(info.lastTradeAt)<=30000)
    return tr('刚收到真实成交；较早历史仍在回补', 'Recent real trade received; older history is still being backfilled');
  if(info?.coverageStatus==='backfilling' && info.marketStatus==='live' && !info.stale)return tr('实时成交已接入，历史回补中', 'Live trades connected; historical data is being backfilled');
  if(info?.marketStatus==='quiet' && !info.stale && now.value-Number(info.scanAt||0)<=20000)return tr('采集正常，观测范围内暂无新成交', 'Collection healthy; no new trades in the observed range');
  const declared = info?.status;
  const state = declared === 'current' && candleTail.value === 'quiet' ? 'quiet' : declared ?? candleTail.value;
  return ({
    collecting: tr('已加入采集队列，等待可用行情', 'Queued for collection; waiting for available market data'),
    partial: tr('部分历史已保存，缺口回补中', 'Partial history saved; gaps are being backfilled'),
    stale: tr('更新受阻，当前为历史 K 线', 'Update unavailable; historical candles shown'),
    quiet: tr('当前只返回较早的 K 线，不能据此确认没有成交', 'Only older candles are available; this does not prove there were no trades'),
    missing: tr('此市场暂无可用 K 线', 'No candles are available for this market'),
    unsupported: tr('行情源暂不支持此市场 K 线', 'The source does not support candles for this market'),
    current: tr('已同步可用行情', 'Available market data synced'),
    unavailable: tr('上游暂未返回该市场的可用 K 线', 'The source has not returned usable candles for this market'),
  })[state] ?? tr('行情采集状态待核实', 'Market collection status unknown');
});
const lastCandleAt = computed(() => candleInfo.value?.rows?.at(-1)?.t);
const lastBar = computed(() => candleInfo.value?.rows?.at(-1) ?? null);
const volumeUnit = computed(() => lastBar.value?.vu != null ? `${tr('成交额', 'Quote volume')} (${candleInfo.value?.volumeCurrency ?? (props.asset.priceScope === 'exchange' ? props.asset.priceCurrency ?? '?' : 'USD')})` : tr('成交量(代币)', 'Volume (token)'));
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
let lastPaintKey = '';
let lastHttpSnapshotAt = 0;

function lw() {
  return window.LightweightCharts;
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
  if (!lw() || !el.value) return null;
  if (!chart) {
    chart = lw().createChart(el.value, {
      width: el.value.clientWidth || 600,
      height: el.value.clientHeight || 380,
      layout: { background: { type: lw().ColorType.Solid, color: 'transparent' }, textColor: '#91a0b5', fontSize: 11 },
      grid: { vertLines: { color: 'rgba(37,48,68,.45)' }, horzLines: { color: 'rgba(37,48,68,.45)' } },
      rightPriceScale: { borderColor: 'rgba(37,48,68,.7)' },
      timeScale: { borderColor: 'rgba(37,48,68,.7)', timeVisible: true, secondsVisible: false, rightOffset: 3, tickMarkFormatter: (time, type) => chartTime(time, type) },
      localization: { timeFormatter: time => chartDateTime(time) },
      crosshair: { mode: lw().CrosshairMode.Normal },
    });
  }
  return chart;
}

const decOf = pricePrecision;

function priceText(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US', { maximumFractionDigits: decOf(n) });
}

function volumeText(row) {
  const n = Number(row?.vu ?? row?.v);
  return Number.isFinite(n) ? n.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '—';
}

async function loadCandles(address, barSel, limit = 500, force = false) {
  const venue = venueOf();
  const key = `${venue}:${props.asset.chainId ?? '196'}:${address}:${props.pool || props.asset.marketId || ''}:${barSel}`;
  const now = Date.now();
  const epoch=streams.resetVersion;
  const hit = candleMem.get(key);
  if (!force && hit && now - hit.cachedAt < 20000) return hit;
  try {
    const requested={chainId:props.asset.chainId??'196',token:address,venue,bar:barSel,poolId:props.pool,marketId:props.pool||props.asset.marketId||''};
    const r = await getCandles(requested.chainId, address, barSel, limit, venue, {poolId:requested.poolId,marketId:requested.poolId ? undefined : requested.marketId});
    if(epoch!==streams.resetVersion)return null;
    if (!r || !Array.isArray(r.rows)) return hit ?? null;
    if(!candleResponseMatches(r,requested))return hit?{...hit,stale:true,status:'stale',error:'market-mismatch'}:{...requested,rows:[],stale:true,status:'unavailable',error:'market-mismatch'};
    lastHttpSnapshotAt = Date.now();
    const live=streams.matching({...requested,marketId:r.marketId||requested.marketId});
    const entry = { ...r, rows:mergeCandleRows(snapshotCandleRows(r),live?.rows??[]), cachedAt: now };
    if(live)Object.assign(entry,{source:live.source,marketId:live.marketId,pool:live.poolId??live.pool??r.pool,priceCurrency:live.priceCurrency,volumeCurrency:live.volumeCurrency,streamAt:live.at},candleStreamState(r,live));
    candleMem.set(key, entry);
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
  const sequence = ++paintSequence;
  if (!lw() || !el.value) {
    hint.value = tr('图表组件加载中…', 'Chart component loading…');
    return;
  }
  const venue = props.asset.priceScope === 'exchange' ? props.asset.provider ?? props.asset.venue : 'dex';
  const paintKey = `${props.asset.chainId}:${props.asset.token}:${venue}:${props.asset.marketId??''}:${props.pool}:${mode.value}:${bar.value}`;
  if (paintKey !== lastPaintKey) {
    reset = true;
    lastPaintKey = paintKey;
  }
  const a = props.asset;
  if (mode.value === 'candle') {
    const info = await loadCandles(a.token, bar.value, 500, force);
    if (sequence !== paintSequence || !el.value) return;
    candleInfo.value = info;
    const rows = info?.rows;
    const nextSource = `${info?.source ?? 'OKX'}:${info?.pool ?? ''}:${info?.priceCurrency ?? ''}`;
    if (nextSource !== sourceKey) {reset = true; sourceKey = nextSource;}
    if (rows && rows.length) {
      // OHLCV comes from the selected market; actual candle events update
      // its open bar. A quote from another market is never stitched into it.
      hint.value = `${bar.value} ${tr('K 线', 'candles')} · ${info.source ?? (a.priceScope === 'exchange' ? 'Binance' : 'OKX DEX')} · ${tr('真实成交；时间标注为每根开盘时间', 'Actual trades; timestamps indicate candle open')}${info.pool ? ' · '+info.pool.slice(0,10)+'…' : ''} · ${tr('计价', 'Quote')} ${info.priceCurrency ?? '?'}${info.streamAt ? ' · '+tr('逐笔更新', 'Streaming updates') : ''}`;
      const barsData = rows.map((r) => ({ time: Math.floor(r.t / 1000), open: r.o, high: r.h, low: r.l, close: r.c }));
      if (!reset && candleSeries && volumeSeries && candleLen <= barsData.length) {
        const previous=new Map(renderedRows.map(r=>[r.t,r]));
        const correction=rows.some(r=>Math.floor(r.t/1000)<lastCandleTime && JSON.stringify(previous.get(r.t))!==JSON.stringify(r));
        const volume=r=>({time:Math.floor(r.t/1000),value:r.vu??r.v??0,color:r.c>=r.o?'rgba(45,212,167,.45)':'rgba(255,93,93,.45)'});
        if(correction){candleSeries.setData(barsData);volumeSeries.setData(rows.map(volume));}
        else for(const r of rows){if(Math.floor(r.t/1000)>=lastCandleTime){candleSeries.update({time:Math.floor(r.t/1000),open:r.o,high:r.h,low:r.l,close:r.c});volumeSeries.update(volume(r));}}
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
        upColor: '#2dd4a7', downColor: '#ff5d5d',
        borderUpColor: '#2dd4a7', borderDownColor: '#ff5d5d',
        wickUpColor: '#2dd4a7', wickDownColor: '#ff5d5d',
        priceFormat: { type: 'price', precision: dec, minMove: 10 ** -dec },
      });
      candleSeries.setData(barsData);
      volumeSeries = ch.addHistogramSeries({ priceScaleId: '', priceFormat: { type: 'volume' } });
      volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      volumeSeries.setData(rows.map((r) => ({ time: Math.floor(r.t / 1000), value: r.vu ?? r.v ?? 0, color: 'rgba(96,125,159,.5)' })));
      priceLine = candleSeries.createPriceLine({ ...priceLineOptions(info, a, last), color: '#b9fa6a', lineWidth: 1, lineStyle: lw().LineStyle.Dashed, axisLabelVisible: true });
      renderedRows=rows.map(r=>({...r}));
      candleLen = barsData.length;
      lastCandleTime = barsData.at(-1).time;
      return;
    }
    hint.value = !props.pool && props.samples.length >= 2 ? tr('暂无 K 线返回，显示 5 分钟采样线。', 'No candles returned yet; showing the 5-minute sample line.') : tr('尚无可用历史。已提交采集需求；是否返回取决于上游市场覆盖与额度。', 'No usable history yet. Collection has been requested; availability depends on source coverage and budget.');
  } else {
    hint.value = tr('分时 · 已保存的价格观测', 'Intraday · saved price observations');
  }
  if (!props.pool && (props.samples ?? []).some(p => !p.provenance)) hint.value += ' · ' + tr('部分历史样本缺少来源证明，未拼接最新价', 'Some historical samples lack provenance; latest quote is not joined');
  const pts = props.pool ? (candleInfo.value?.rows ?? []).map(r=>({time:Math.floor(r.t/1000),value:r.c})) : samplePoints(props.samples ?? [], a);
  if (pts.length < 2) {killChart();return;}
  if (!reset && lineSeries) {
    lineSeries.setData(pts);
    if (priceLine) priceLine.applyOptions({ price: pts[pts.length - 1].value });
    return;
  }
  killChart();
  const ch = ensureChart();
  if (!ch) return;
  lineSeries = ch.addAreaSeries({ lineColor: '#b9fa6a', topColor: 'rgba(185,250,106,.16)', bottomColor: 'rgba(185,250,106,.02)', lineWidth: 2 });
  lineSeries.setData(pts);
  priceLine = lineSeries.createPriceLine({ price: pts[pts.length - 1].value, color: '#b9fa6a', lineWidth: 1, lineStyle: lw().LineStyle.Dashed, axisLabelVisible: true, title: '' });
}

function setMode(m) {
  if (mode.value === m) return;
  mode.value = m;
  paint(true);
}

function setBar(b) {
  if (bar.value === b) return;
  bar.value = b;
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
  if(![...streams.resets.values()].some(packet=>packet.resetVersion>previous && candlePacketMatches(packet,selection())))return;
  candleMem.clear();candleInfo.value=null;paintSequence++;killChart();paint(true,true);
});
watch(() => streams.version, () => {
  const packet=streams.matching(selection());
  if(!packet)return;
  const selected=selection();
  const key=`${selected.venue}:${selected.chainId}:${selected.token}:${props.pool || props.asset.marketId || ''}:${selected.bar}`;
  const previous=candleMem.get(key);
  // Never concatenate a pool-native quote with token-wide USD history.
  const compatible=!previous || ((!previous.priceCurrency||previous.priceCurrency===packet.priceCurrency) && (!props.pool||String(previous.pool??previous.poolId??previous.marketId).toLowerCase()===props.pool.toLowerCase()));
  const entry={...(compatible?previous:{}),...packet,rows:mergeCandleRows(compatible?previous?.rows??[]:[],packet.rows),pool:packet.poolId??packet.pool,...candleStreamState(compatible?previous:null,packet),streamAt:packet.at,cachedAt:Date.now()};
  candleMem.set(key,entry);candleInfo.value=entry;
  if(mode.value==='candle')paint();
});
// Compare each market field, not a newly allocated array on every quote.
// Frequent asset replacements must not clear an already displayed candle.
watch([() => props.pool, () => props.asset.token, () => props.asset.chainId, venueOf, () => props.asset.marketId], () => { candleInfo.value=null;lastHttpSnapshotAt=0;paint(true); });

function goLatest() {
  try { chart?.timeScale()?.scrollToRealTime?.(); } catch { /* chart not ready */ }
}

function onResize() {
  if (chart && el.value) {
    try { chart.resize(el.value.clientWidth || 600, el.value.clientHeight || 380); } catch { /* disposed */ }
  }
}

let lwWaiter = null;
let refreshTimer = null;
let clockTimer = null;
onMounted(() => {
  window.addEventListener('resize', onResize);
  clockTimer = setInterval(() => now.value=Date.now(),1000);
  refreshTimer = setInterval(() => {
    if (document.hidden) return;
    // The global projection connection alone does not prove this chart's
    // market sends candle events. Only a recent matching candle can replace
    // frequent HTTP checks; quiet or unsupported markets keep the fallback.
    const candlePacket = streams.matching(selection());
    const receivingCandles = dashboard.hasProjectionStream && candlePacket?.at
      && Date.now() - candlePacket.at < 90000;
    if (!receivingCandles || Date.now() - lastHttpSnapshotAt >= 300000)
      paint(false, true);
  }, 31000);
  window.addEventListener('manual-refresh', onManualRefresh);
  paint(true);
  // Vendor scripts load async; retry painting once LightweightCharts lands.
  if (!lw()) {
    lwWaiter = setInterval(() => {
      if (lw()) {
        clearInterval(lwWaiter);
        paint(true);
      }
    }, 400);
  }
});
onBeforeUnmount(() => {
  streams.unsubscribe(subscriptionId);
  if (lwWaiter) clearInterval(lwWaiter);
  clearInterval(refreshTimer);
  clearInterval(clockTimer);
  paintSequence++;
  window.removeEventListener('resize', onResize);
  window.removeEventListener('manual-refresh', onManualRefresh);
  killChart();
});
function onManualRefresh() { paint(false, true); }
</script>
