<template>
  <div>
    <div class="tab-group" id="v2ChartTabs">
      <button class="tab" :class="{ active: mode === 'line' }" @click="setMode('line')">{{ tr('分时', 'Line') }}</button>
      <button class="tab" :class="{ active: mode === 'candle' }" @click="setMode('candle')">{{ tr('K线', 'Candles') }}</button>
      <template v-if="mode === 'candle'">
        <button v-for="b in bars" :key="b" class="tab" :class="{ active: bar === b }" @click="setBar(b)">{{ b }}</button>
      </template>
    </div>
    <div ref="el" class="chart x-candles"></div>
    <p class="hint">{{ hint }}</p>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { getCandles } from '../api/client';
import { tr } from '../i18n';
import { usd } from '../utils/format';

const props = defineProps({
  asset: { type: Object, required: true },
  samples: { type: Array, default: () => [] },
});

const el = ref(null);
const mode = ref('candle');
const bar = ref('5m');
const bars = ['1m', '5m', '15m', '1H'];
const hint = ref('');

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let lineSeries = null;
let priceLine = null;
let candleLen = 0;
const candleMem = new Map();
let lastPaintKey = '';

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
      timeScale: { borderColor: 'rgba(37,48,68,.7)', timeVisible: true, secondsVisible: false, rightOffset: 3 },
      crosshair: { mode: lw().CrosshairMode.Normal },
    });
  }
  return chart;
}

function decOf(v) {
  return v >= 100 ? 2 : v >= 1 ? 4 : v >= 0.01 ? 6 : 8;
}

async function loadCandles(address, barSel, limit = 180) {
  const key = `${address}:${barSel}`;
  const now = Date.now();
  const hit = candleMem.get(key);
  if (hit && now - hit.at < 20000) return hit.rows;
  try {
    const r = await getCandles(props.asset.chainId ?? '196', address, barSel, limit);
    if (!r || !Array.isArray(r.rows)) return hit?.rows ?? null;
    candleMem.set(key, { rows: r.rows, at: now });
    return r.rows;
  } catch {
    return hit?.rows ?? null;
  }
}

async function paint(reset = false) {
  if (!lw() || !el.value) {
    hint.value = tr('图表组件加载中…', 'Chart component loading…');
    return;
  }
  const paintKey = `${mode.value}:${bar.value}`;
  if (paintKey !== lastPaintKey) {
    reset = true;
    lastPaintKey = paintKey;
  }
  const a = props.asset;
  if (mode.value === 'candle') {
    let rows = await loadCandles(a.token, bar.value);
    if (rows && rows.length) {
      if (a.price != null) {
        rows = [...rows];
        const last = rows.length - 1;
        rows[last] = { ...rows[last], c: a.price, h: Math.max(rows[last].h, a.price), l: Math.min(rows[last].l, a.price) };
      }
      hint.value = (bar.value === '1m' ? tr('1 分钟', '1m') : bar.value === '1H' ? tr('1 小时', '1H') : bar.value + ' ') + tr('K 线 · OKX DEX 真实成交 · 虚线为最新价', 'candles · real OKX DEX trades · dashed line marks the latest price');
      const barsData = rows.map((r) => ({ time: Math.floor(r.t / 1000), open: r.o, high: r.h, low: r.l, close: r.c }));
      if (!reset && candleSeries && volumeSeries && candleLen === barsData.length) {
        candleSeries.update(barsData[barsData.length - 1]);
        const vr = rows[rows.length - 1];
        volumeSeries.update({ time: Math.floor(vr.t / 1000), value: vr.vu ?? vr.v });
        if (priceLine && a.price != null) priceLine.applyOptions({ price: a.price });
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
      volumeSeries.setData(rows.map((r) => ({ time: Math.floor(r.t / 1000), value: r.vu ?? r.v, color: 'rgba(96,125,159,.5)' })));
      priceLine = candleSeries.createPriceLine({ price: a.price ?? last, color: '#b9fa6a', lineWidth: 1, lineStyle: lw().LineStyle.Dashed, axisLabelVisible: true, title: '' });
      candleLen = barsData.length;
      return;
    }
    hint.value = props.samples.length >= 2 ? tr('暂无 K 线返回，显示 5 分钟采样线。', 'No candles returned yet; showing the 5-minute sample line.') : tr('K 线与采样点暂缺，成交后自动出现。', 'Candles and samples are pending; they appear after trades.');
  } else {
    hint.value = tr('分时 · 5 分钟采样 + 最新价', 'Intraday · 5-minute samples plus the live price');
  }
  const data = (props.samples ?? []).map((p) => [p.t, p.price]);
  if (a.price != null) data.push([Date.now(), a.price]);
  if (data.length < 2) {
    killChart();
    return;
  }
  let lastT = 0;
  const pts = data.map(([t, p]) => {
    const s = Math.max(Math.floor(t / 1000), lastT + 1);
    lastT = s;
    return { time: s, value: p };
  });
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
  paint(true);
}

watch(() => [props.asset?.price, props.samples], () => paint(), { deep: false });

function onResize() {
  if (chart && el.value) {
    try { chart.resize(el.value.clientWidth || 600, el.value.clientHeight || 380); } catch { /* disposed */ }
  }
}

let lwWaiter = null;
onMounted(() => {
  window.addEventListener('resize', onResize);
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
  if (lwWaiter) clearInterval(lwWaiter);
  window.removeEventListener('resize', onResize);
  killChart();
});
</script>
