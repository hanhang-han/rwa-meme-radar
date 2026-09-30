// SSE client over fetch-streaming. API_BASE follows the mounted dashboard
// path, including /dashboardv2/, so the old and new sites use their own
// reverse-proxy API routes. Polling remains the fallback on disconnect.
//
// Events are matched by chainId + token (never token alone). hello/heartbeat
// only update connection state; quote freshness comes from market times.
// Reconnects send Last-Event-ID so missed subscribed events replay. A detail
// page scopes trade traffic to its asset; the feed snapshot restores global
// trades when that scope changes or the page returns to the market lists.
import { watch } from 'vue';
import { API_BASE } from '../api/client.js';
import { projectionByteLength, useDashboardStore } from '../stores/dashboard.js';
import { useDetailStore } from '../stores/detail.js';
import { useFeedStore } from '../stores/feed.js';
import { useComparisonStore } from '../stores/comparisons.js';
import { useCandleStore } from '../stores/candles.js';

let started = false;
let streamGeneration = 0;
let lastEventId = null;
let reconnects = 0;
let currentController = null;
let stopped = false;
let compatibilityRefreshAt = 0;
let stopScopeWatch = null;
let scopeTimer = null;
let scopeChanged = false;
let previousTradeScope = null;
const emit = (name,detail) => { if(typeof window !== 'undefined' && typeof CustomEvent !== 'undefined')window.dispatchEvent(new CustomEvent(name,{detail})); };

export function useStream() {
  return { started, lastEventId };
}

export function currentTradeScope() {
  const current=useDetailStore().current;
  return current ? `${current.chain}:${current.address.toLowerCase()}` : null;
}

export function startStream() {
  if (started) return;
  started = true;
  stopped = false;
  const generation = ++streamGeneration;
  stopScopeWatch=watch([()=>useCandleStore().subscriptionScope,currentTradeScope],()=>{
    clearTimeout(scopeTimer);
    scopeTimer=setTimeout(()=>{scopeChanged=true;currentController?.abort();},200);
  });
  connectStream(generation);
}

export function stopStream() {
  stopped = true;
  started = false;
  streamGeneration += 1;
  stopScopeWatch?.();stopScopeWatch=null;clearTimeout(scopeTimer);scopeChanged=false;
  previousTradeScope = null;
  currentController?.abort();
  currentController = null;
}

export function parseSseFrame(frame) {
  let event = 'message';
  let id = null;
  let data = '';
  for (const rawLine of frame.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('id:')) id = line.slice(3).trim();
    else if (line.startsWith('data:')) data += line.slice(5).trim();
  }
  return { event, id, data };
}

export function appendSseChunk(buffer, chunk) {
  // Nginx/upstream implementations may use either LF or CRLF. Normalize the
  // incoming chunk only: repeatedly replacing an incomplete multi-MB frame
  // would scan its entire prefix again for every network read.
  if (buffer.endsWith('\r') && chunk.startsWith('\n')) {
    return `${buffer.slice(0, -1)}\n${chunk.slice(1).replace(/\r\n/g, '\n')}`;
  }
  return buffer + chunk.replace(/\r\n/g, '\n');
}

const yieldToMainThread = () => new Promise(resolve => setTimeout(resolve, 0));
const monotonicNow = () => globalThis.performance?.now() ?? Date.now();

// Awaiting an already-resolved frame handler only drains microtasks. A replay
// backlog can therefore starve input and rendering even though this is async.
// Keep one ordered consumer and stop reading while yielding (backpressure).
// Frames are never coalesced or dropped; the caller commits each cursor only
// after its handler completes. A single JSON parse/projection is still atomic,
// so server-side frame size limits remain necessary for large projections.
export async function consumeSseStream(reader, onFrame, {
  signal,
  isCurrent = () => true,
  onChunk = () => {},
  yieldToMain = yieldToMainThread,
  now = monotonicNow,
  maxFrames = 20,
  maxChars = 256 * 1024,
  maxMs = 8,
} = {}) {
  const decoder = new TextDecoder();
  let buffer = '';
  let searchFrom = 0;
  let frames = 0;
  let chars = 0;
  let startedAt = now();
  const active = () => !signal?.aborted && isCurrent();
  async function yieldIfNeeded() {
    if (frames >= maxFrames || chars >= maxChars || now() - startedAt >= maxMs) {
      await yieldToMain();
      frames = 0;
      chars = 0;
      startedAt = now();
    }
    return active();
  }

  while (active()) {
    const { done, value } = await reader.read();
    if (!active() || done) return;
    onChunk();
    buffer = appendSseChunk(buffer, decoder.decode(value, { stream: true }));
    chars += value.byteLength;
    // Yield while receiving a large incomplete frame as well as before a
    // complete large frame, not only after JSON.parse has already run.
    if (!await yieldIfNeeded()) return;
    let consumed = 0;
    let boundary;
    while ((boundary = buffer.indexOf('\n\n', searchFrom)) >= 0) {
      const frame = buffer.slice(consumed, boundary);
      await onFrame(frame);
      consumed = boundary + 2;
      searchFrom = consumed;
      frames += 1;
      chars += frame.length;
      if (!await yieldIfNeeded()) return;
    }
    buffer = buffer.slice(consumed);
    // Only the tail can acquire a new separator. Keep two characters because
    // a trailing "\n\r" can normalize to "\n\n" when the next read starts LF.
    searchFrom = Math.max(0, buffer.length - 2);
  }
}

async function connectStream(generation) {
  let retry = 0;
  while (!stopped && generation === streamGeneration) {
    const dash = useDashboardStore();
    const controller = new AbortController();
    currentController = controller;
    let received = Date.now();
    const watchdog = setInterval(() => { if (Date.now() - received > 60000) controller.abort(); }, 10000);
    try {
      if(lastEventId == null) {
        // The compact overview carries the same projection cursor as the
        // market catalogue. Start replay from it while the larger list loads.
        const snapshot=dash.snapshot ?? await dash.poll({view:'overview'}) ?? await dash.poll();
        if(stopped || generation !== streamGeneration) break;
        if(!snapshot)throw new Error('snapshot unavailable');
        lastEventId=dash.cursor != null ? String(dash.cursor)
          : snapshot.realtime?.cursor != null ? String(snapshot.realtime.cursor) : null;
      }
      if(stopped || generation !== streamGeneration) break;
      const headers = {};
      if (lastEventId != null) headers['Last-Event-ID'] = String(lastEventId);
      const query=new URLSearchParams({snapshot:'false',protocol:'1',candles:useCandleStore().subscriptionScope});
      const tradeScope=currentTradeScope();
      query.set('trades',tradeScope || 'feed');
      // The home view loads its own scoped feed. Only reload global history
      // when leaving a detail page, whose stream intentionally excluded it.
      if(previousTradeScope && !tradeScope)useFeedStore().load();
      previousTradeScope=tradeScope;
      if(lastEventId!=null)query.set('after',String(lastEventId));
      const res = await fetch(API_BASE + `stream?${query}`, { headers, signal: controller.signal });
      if (!res.ok || !res.body) throw new Error('stream unavailable');
      dash.setStreamStatus({ connected: true, state: 'syncing', lastAt: Date.now() });
      const reader = res.body.getReader();
      await consumeSseStream(reader, async frame => {
        const { event, id, data } = parseSseFrame(frame);
        if(id&&lastEventId!=null&&Number(id)<=Number(lastEventId))return;
        if (data && event !== 'heartbeat') {
          const applied = await handleStreamEvent(event, data);
          if (id && !applied) {
            // A new server event/schema must not trap an older tab in a
            // reconnect loop on the same frame. Reconcile once and advance.
            if(Date.now()-compatibilityRefreshAt>60000){compatibilityRefreshAt=Date.now();await dash.poll();}
            dash.setStreamStatus({compatibilityWarning:event});
          }
          if (id) lastEventId = id;
        }
        retry = 0;
        dash.setStreamStatus({ connected: true, lastMessageAt: Date.now() });
      }, {
        signal: controller.signal,
        isCurrent: () => !stopped && generation === streamGeneration,
        onChunk: () => { received = Date.now(); },
      });
      if (!stopped && generation === streamGeneration && !controller.signal.aborted) throw new Error('stream closed');
    } catch {
      if(generation === streamGeneration && !scopeChanged){
        dash.setStreamStatus({ connected: false, state: stopped ? 'offline' : 'reconnecting' });
        reconnects += 1;
        dash.setStreamStatus({ reconnects });
      }
      /* stream down; polling keeps the page live */
    }
    finally {
      clearInterval(watchdog);
      controller.abort();
      if (currentController === controller) currentController = null;
    }
    if (stopped || generation !== streamGeneration) break;
    if(scopeChanged){scopeChanged=false;continue;}
    const delay = Math.min(30000, 1000 * (2 ** Math.min(retry++, 5))) + Math.floor(Math.random() * 500);
    await new Promise((r) => setTimeout(r, delay));
  }
}

export async function handleStreamEvent(event, raw) {
  let d;
  try {
    d = JSON.parse(raw);
  } catch {
    return false;
  }
  if (event === 'reset') {
    if (d.cursor == null) return false;
    const dash = useDashboardStore();
    const feed = useFeedStore();
    dash.setStreamStatus({ state: 'syncing' });
    await Promise.all([feed.load(), dash.poll()]);
    useDetailStore().invalidate();
    emit('resource-change',{kind:'all'});
    if (dash.error || feed.error) throw new Error('stream recovery snapshot failed');
    lastEventId = String(dash.cursor ?? d.cursor ?? 0);
    dash.setStreamStatus({ connected: true, state: 'live', lastAt: Date.now() });
    return true;
  }
  if(event==='checkpoint'){if(d.cursor!=null)lastEventId=String(d.cursor);return true;}
  if (event === 'hello') {
    if (lastEventId == null) lastEventId = String(d.cursor ?? 0);
    useDashboardStore().setStreamStatus({ connected: true, state: 'live', lastAt: Date.now(), projection: useDashboardStore().snapshot?.realtime?.schema === 1 });
    return true;
  }
  const dash = useDashboardStore();
  const detail = useDetailStore();
  if(event==='projection.delta'){
    // Reuse the received JSON for byte accounting; do not stringify a large
    // parsed projection again just to bound its replay history.
    if(!dash.applyProjection(d,true,projectionByteLength(raw)))return false;
    detail.syncProjection();
    for(const resource of d.invalidations??[]){
      dash.invalidate(resource);
      if(resource.kind==='detail')detail.invalidate(resource);
      if(resource.kind==='feed')useFeedStore().load();
      emit('resource-change',resource);
    }
    dash.setStreamStatus({state:'live',projection:true});
    return true;
  }
  if(event==='trade-remove'&&Array.isArray(d.ids)){useFeedStore().removeTrades(d);detail.removeTrades(d);return true;}
  if(event==='candle-reset'){useCandleStore().reset(d);return true;}
  if(event==='candle'||event==='candle.upsert'||event==='candle.correct'||event==='candle.close')return useCandleStore().receive(d);
  if (event === 'comparison') return useComparisonStore().receive(d);
  const watching = detail.current
    ? detail.current.chain === String(d.chainId ?? '196') && detail.current.address === String(d.token).toLowerCase()
    : false;
  if((event==='price'||event==='stock-quote') && dash.snapshot?.realtime?.schema===1)return true;
  if (event === 'price' && d.token && d.price != null) {
    const applied = dash.applyPrice(d.chainId ?? '196', d.token, d.price, d.at ?? d.marketAt ?? d.receivedAt, d);
    // Detail page live tail: expose a ref bump so the chart watcher re-runs.
    const detailApplied=detail.applyQuote({...d,at:d.at??d.marketAt,priceScope:d.priceScope??'dex'});
    if ((applied || detailApplied) && watching) emit('sse-price', d);
    dash.setStreamStatus({ state: 'live' });
    // A valid quote is coalescible latest-value state. applyPrice retains it
    // even before the corresponding catalogue row reaches this browser.
    return true;
  } else if (event === 'stock-quote' && d.token && d.price != null) {
    const applied = dash.applyStockQuote(d);
    const detailApplied=detail.applyQuote({...d,priceScope:d.priceScope??'exchange'});
    if ((applied || detailApplied) && watching) emit('sse-stock-quote', d);
    dash.setStreamStatus({ state: 'live' });
    return true;
  } else if (event === 'trade' && d.token && (Array.isArray(d.fresh) || d.id)) {
    const marketRows=Array.isArray(d.fresh)?d.fresh:[{...d,symbol:d.symbol??dash.stockIndex.get(`${d.chainId}:${d.token.toLowerCase()}`)?.tokenSymbol??dash.assetIndex.get(`${d.chainId}:${d.token.toLowerCase()}`)?.symbol}];
    if (marketRows.some((row) => !row?.id)) return false;
    const feed = useFeedStore();
    feed.appendTrades(marketRows.filter(t=>String(t.venue??'dex').toLowerCase()==='dex'));
    if(!Array.isArray(d.fresh)){detail.appendMarketTrade(d);dash.setStreamStatus({state:'live'});return true;}
    if (watching) emit('sse-trades', d);
    detail.appendTrades(d);
    dash.setStreamStatus({ state: 'live' });
    return true;
  } else if (event === 'discovery' && d.id && d.asset?.token && d.chainId && d.t) {
    const projected = dash.upsertDiscovery(d);
    const feed = useFeedStore();
    const stored = feed.appendRelationship({ ...d, asset: d.asset.token, symbol: d.symbol ?? d.asset.symbol });
    if (!stored && !projected) return false;
    dash.setStreamStatus({ state: 'live' });
    return true;
  } else if (event === 'relationship' && d.id && d.relation?.id && d.relation?.token) {
    const feed = useFeedStore();
    const stored = feed.appendRelationship(d);
    const projected = dash.upsertRelation(d.relation);
    detail.syncProjection();
    detail.invalidate({chainId:d.chainId,token:d.relation.token});
    if (!stored && !projected) return false;
    dash.setStreamStatus({ state: 'live' });
    return true;
  }
  return false;
}
