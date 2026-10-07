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
import { mergeEntity } from '../utils/realtime.js';

let started = false;
let streamGeneration = 0;
let lastEventId = null;
let reconnects = 0;
let currentController = null;
let stopped = false;
let compatibilityRefreshAt = 0;
let scopedCompatibilityRefreshAt = 0;
let stopScopeWatch = null;
let scopeTimer = null;
let scopeChanged = false;
let previousTradeScope = null;
let previousProjectionKey = null;
const emit = (name,detail) => { if(typeof window !== 'undefined' && typeof CustomEvent !== 'undefined')window.dispatchEvent(new CustomEvent(name,{detail})); };

export function useStream() {
  return { started, lastEventId };
}

export function currentTradeScope() {
  const current=useDetailStore().current;
  return current ? `${current.chain}:${current.address.toLowerCase()}` : null;
}

export function currentProjectionScope() {
  return currentTradeScope() ? 'asset' : useDashboardStore().requestedView==='market' ? 'market' : 'overview';
}

export function startStream() {
  if (started) return;
  started = true;
  stopped = false;
  const generation = ++streamGeneration;
  stopScopeWatch=watch([()=>useCandleStore().subscriptionScope,currentTradeScope,()=>useDashboardStore().requestedView],()=>{
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
  previousProjectionKey = null;
  scopedCompatibilityRefreshAt = 0;
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
      const projectionScope=currentProjectionScope(),tradeScope=currentTradeScope();
      const projectionKey=projectionScope==='asset'?`asset:${tradeScope}`:projectionScope;
      const changedProjection=previousProjectionKey!==projectionKey;
      if(changedProjection){dash.setProjectionScope(projectionScope);lastEventId=null;}
      if(lastEventId == null) {
        // The compact overview carries the same projection cursor as the
        // market catalogue. Start replay from it while the larger list loads.
        const view=projectionScope;
        // Asset replay starts at the SAME detail snapshot which initialized
        // its cache. A later overview cursor could skip quotes committed
        // between two parallel HTTP requests, or acknowledge an uncached row.
        const selected=useDetailStore().current;
        const snapshot=projectionScope==='asset'
          ?await useDetailStore().fetch(selected.chain,selected.address,{force:true})
          :(!changedProjection||previousProjectionKey===null)&&dash.snapshot?.unified?.snapshotScope===view
            ?dash.snapshot:await dash.poll({view});
        if(stopped || generation !== streamGeneration) break;
        if(!snapshot)throw new Error('snapshot unavailable');
        lastEventId=snapshot.realtime?.cursor != null ? String(snapshot.realtime.cursor) : null;
      }
      if(stopped || generation !== streamGeneration) break;
      // A route can change while its HTTP bootstrap is in flight. Never open
      // a stream for that obsolete page or overwrite the next page's cursor.
      if(projectionKey!==(currentProjectionScope()==='asset'?`asset:${currentTradeScope()}`:currentProjectionScope())){lastEventId=null;continue;}
      previousProjectionKey=projectionKey;
      const headers = {};
      if (lastEventId != null) headers['Last-Event-ID'] = String(lastEventId);
      const query=new URLSearchParams({snapshot:'false',protocol:'2',scope:projectionScope,candles:useCandleStore().subscriptionScope});
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
          if (id && event!=='reset' && !controller.signal.aborted) lastEventId = String(Math.max(Number(lastEventId??0),Number(id)));
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
    const page=currentProjectionScope();
    const selected=useDetailStore().current;
    const [,snapshot]=await Promise.all([feed.load(undefined,{fresh:true}),page==='asset'
      ?useDetailStore().fetch(selected.chain,selected.address,{force:true})
      :dash.poll({view:page})]);
    useDetailStore().invalidate();
    emit('resource-change',{kind:'all'});
    if (dash.error || feed.error) throw new Error('stream recovery snapshot failed');
    lastEventId = String(snapshot?.realtime?.cursor ?? dash.cursor ?? d.cursor ?? 0);
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
    if(dash.stream.scoped&&Number(d.schema??1)===1&&d.scope!=='asset'){
      // Old API nodes silently ignore the new scope query. Keep the current
      // page bounded and use its published HTTP view until this connection
      // starts delivering scoped packets; never merge a global catalogue.
      dash.setStreamStatus({projection:false,compatibilityWarning:'scoped-stream-unavailable'});
      if(Date.now()-scopedCompatibilityRefreshAt>=10000){
        scopedCompatibilityRefreshAt=Date.now();
        const selected=detail.current;
        const snapshot=selected?await detail.fetch(selected.chain,selected.address,{force:true})
          :await dash.poll({view:currentProjectionScope()});
        if(!snapshot)throw new Error('scoped-compatibility-snapshot-failed');
      }
      return true;
    }
    // Reuse the received JSON for byte accounting; do not stringify a large
    // parsed projection again just to bound its replay history.
    if(d.scope==='asset'){
      if(d.asset!==currentTradeScope())return true;
      if(!applyAssetProjection(d,detail)){
        const selected=detail.current;
        await detail.fetch(selected.chain,selected.address,{force:true});
        if(d.asset!==currentTradeScope())return true;
        if(!applyAssetProjection(d,detail))throw new Error('asset-snapshot-unavailable');
      }
    }else{
      try{if(!dash.applyProjection(d,true,projectionByteLength(raw)))return false;}
      catch(error){
        // Ordered field patches require their exact predecessor. The current
        // published HTTP snapshot plus journal replay is the recovery source.
        if(!String(error?.message).startsWith('projection-')&&!String(error?.message).startsWith('invalid-projection-'))throw error;
        const snapshot=await dash.poll({view:currentProjectionScope()==='market'?'market':'overview'});
        if(!snapshot)throw error;
        lastEventId=String(snapshot.realtime?.cursor??0);
        currentController?.abort();return true;
      }
      detail.syncProjection();
    }
    for(const resource of d.invalidations??[]){
      dash.invalidate(resource);
      if(resource.kind==='detail')detail.invalidate(resource);
      if(resource.kind==='feed')useFeedStore().load();
      emit('resource-change',resource);
    }
    dash.setStreamStatus({state:'live',projection:true,compatibilityWarning:null});
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
    const projected = dash.stream.scoped ? true : dash.upsertDiscovery(d);
    const feed = useFeedStore();
    const stored = feed.appendRelationship({ ...d, asset: d.asset.token, symbol: d.symbol ?? d.asset.symbol });
    if (!stored && !projected) return false;
    dash.setStreamStatus({ state: 'live' });
    return true;
  } else if (event === 'relationship' && d.id && d.relation?.id && d.relation?.token) {
    const feed = useFeedStore();
    const stored = feed.appendRelationship(d);
    const projected = dash.stream.scoped ? true : dash.upsertRelation(d.relation);
    if(!dash.stream.scoped)detail.syncProjection();
    detail.invalidate({chainId:d.chainId,token:d.relation.token});
    if (!stored && !projected) return false;
    dash.setStreamStatus({ state: 'live' });
    return true;
  }
  return false;
}

const DETAIL_MARKET_FIELDS=new Set(['exchangeMarkets','poolMarkets','marketQuotes']);
const FALLBACK_CANONICAL_FIELDS='price change24h volume24h volumeCurrency volumeScope marketCap buys24h sells24h txs24h holders stockPrice quoteAt quoteStatus quoteReason priceCurrency priceScope provider venue quoteType fieldTimes fieldSources fieldScopes fieldTimeKinds fieldStatus priceProvenance primaryQuote quoteAlternatives'.split(' ');

function replaceDetailCanonical(current, incoming, revision, fields) {
  const owned=new Set(fields??FALLBACK_CANONICAL_FIELDS);
  const retained=Object.fromEntries(Object.entries(current??{}).filter(([key])=>!owned.has(key)||DETAIL_MARKET_FIELDS.has(key)));
  return mergeEntity(current??{},{...retained,...incoming},revision,true);
}

export function applyAssetProjection(packet, detail=useDetailStore()) {
  const hit=detail.cache.get(packet.asset);if(!hit)return false;
  const data=hit.data,revision=Number(packet.revision)||0;
  if(revision&&revision<=Number(data.revision??0))return true;
  const stockRemoved=(packet.removes?.stockTokens??[]).includes(packet.asset);
  if(stockRemoved)data.stock=null;
  if((packet.removes?.assets??[]).includes(packet.asset)){
    const [chainId,token]=packet.asset.split(':');
    data.asset={chainId,token,price:null,quoteStatus:'missing',quoteReason:'asset-removed',_revision:revision};
  }
  for(const row of packet.upserts?.assets??[])data.asset=replaceDetailCanonical(data.asset,row,revision,packet.canonicalFields?.assets);
  for(const row of packet.upserts?.stockTokens??[]){
    data.stock=replaceDetailCanonical(data.stock,row,revision,packet.canonicalFields?.stockTokens);
    if(data.asset?.kind!=='candidate'&&!(packet.upserts?.assets??[]).length)
      data.asset=replaceDetailCanonical(data.asset,{...row,token:row.tokenContractAddress},revision,packet.canonicalFields?.stockTokens);
  }
  const removed=new Set(packet.removes?.relations??[]);
  const relations=new Map((data.relations??[]).filter(row=>!removed.has(row.projectionKey??`${row.chainId??'196'}:${row.id}`)).map(row=>[row.projectionKey??`${row.chainId??'196'}:${row.id}`,row]));
  for(const row of packet.upserts?.relations??[]){const key=row.projectionKey??`${row.chainId??'196'}:${row.id}`;relations.set(key,mergeEntity(relations.get(key),row,revision));}
  data.relations=[...relations.values()];
  // A truncated summary's relationCount remains server-owned until the full
  // relations section has loaded; never turn a preview length into its total.
  if(hit.sections?.relations)data.relationCount=data.relations.length;
  data.revision=revision;data.realtime={...data.realtime,revision};
  if(stockRemoved){
    // Removing a stock mapping does not remove a coexisting candidate asset.
    // Reconcile this identity's summary while retaining its canonical quote.
    const [chainId,token]=packet.asset.split(':');
    detail.invalidate({kind:'detail',chainId,token});
  }
  return true;
}
