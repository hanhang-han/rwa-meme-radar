import { API_BASE, getJSON } from '../api/client.js';
import { candlePacketMatches, candleResponseMatches } from './candles.js';

export function liveMarketSnapshotMatches(snapshot, selection) {
  return snapshot?.liveMarket === true && typeof snapshot.epoch === 'string'
    && Number.isSafeInteger(snapshot.cursor) && snapshot.cursor >= 0
    && snapshot.priceCurrency && snapshot.volumeCurrency
    && String(snapshot.chainId) === String(selection.chainId)
    && String(snapshot.token).toLowerCase() === String(selection.token).toLowerCase()
    && snapshot.venue === selection.venue && snapshot.bar === selection.bar
    && candleResponseMatches(snapshot, selection)
    && String(snapshot.poolId ?? snapshot.pool).toLowerCase() === String(selection.poolId).toLowerCase();
}

export async function getLiveMarketCandles(selection, limit = 500) {
  const query = new URLSearchParams({ pool: selection.poolId, bar: selection.bar, limit: String(limit) });
  return getJSON(`live-market/candles/${encodeURIComponent(selection.chainId)}/${encodeURIComponent(selection.token)}?${query}`, 3000);
}

export async function getLiveMarkets(chain, token, options = {}) {
  const signal = options.signal;
  if(signal?.aborted)throw new DOMException('Aborted','AbortError');
  const query = new URLSearchParams();
  if(options.pool)query.set('pool',options.pool);
  if(options.bar)query.set('bar',options.bar);
  const request = getJSON(`live-market/markets/${encodeURIComponent(chain)}/${encodeURIComponent(token)}${query.size?'?'+query:''}`, 3000);
  if(!signal)return request;
  // Cancel this component's waiter without cancelling the shared HTTP read
  // also used by another chart or the market selector.
  return new Promise((resolve,reject)=>{
    const abort=()=>reject(new DOMException('Aborted','AbortError'));
    signal.addEventListener('abort',abort,{once:true});
    request.then(resolve,reject).finally(()=>signal.removeEventListener('abort',abort));
  });
}

export function liveMarketHealthMatches(market,selection,snapshot){
  return snapshot?.liveMarket===true && market?.liveMarket===true && market.venue==='dex' && String(market.chainId)===String(selection.chainId)
    && String(market.token).toLowerCase()===String(selection.token).toLowerCase()
    && String(market.poolId??market.pool).toLowerCase()===String(selection.poolId).toLowerCase()
    && market.priceCurrency===snapshot.priceCurrency && market.volumeCurrency===snapshot.volumeCurrency
    && (!snapshot.quoteToken || String(market.quoteToken).toLowerCase()===String(snapshot.quoteToken).toLowerCase());
}

export function mergeLiveMarketHealth(previous,market){
  if(Number(market.scanAt)>0 && Number(previous.scanAt)>Number(market.scanAt))return previous;
  const next={...previous};
  for(const key of ['marketStatus','coverageStatus','stale','scanAt','scanThroughBlock','headBlock','scanBlockTime','transportStatus','lastTradeAt',
    'historicalCoverageStatus','historicalScanThroughBlock','historicalScanAt','recentCoverageStatus',
    'nearTipVerifiedAt','nearTipFromBlock','nearTipThroughBlock','nearTipBlockTime','nearTipPool'])
    if(Object.hasOwn(market,key))next[key]=market[key];
  if(Number(previous.lastTradeAt)>Number(next.lastTradeAt))next.lastTradeAt=previous.lastTradeAt;
  return next;
}

export function liveMarketObservationState(info,now=Date.now()){
  if(!info?.liveMarket)return null;
  const age=at=>now-Number(at),valid=at=>Number(at)>0&&age(at)>=-1000;
  if(!info.stale && valid(info.lastTradeAt??info.lastSourceEventAt) && age(info.lastTradeAt??info.lastSourceEventAt)<=30000)
    return 'live';
  // A completed scan of this exact pool can prove a quiet live tail even
  // while the separate websocket transport reconnects or history backfills.
  const nearTip=info.recentCoverageStatus==='current'
    && String(info.nearTipPool??'').toLowerCase()===String(info.poolId??info.pool??'').toLowerCase()
    && !!info.nearTipPool && valid(info.nearTipVerifiedAt) && age(info.nearTipVerifiedAt)<=20000
    && valid(info.nearTipBlockTime) && age(info.nearTipBlockTime)<=30000
    && Number.isSafeInteger(info.nearTipFromBlock) && info.nearTipFromBlock>0
    && Number.isSafeInteger(info.nearTipThroughBlock) && info.nearTipThroughBlock>=info.nearTipFromBlock;
  const scanned=!info.stale && info.coverageStatus==='current' && (['live','catching-up'].includes(info.transportStatus)||nearTip)
    && valid(info.scanAt) && age(info.scanAt)<=20000;
  return scanned ? info.marketStatus==='quiet'?'quiet':'live' : 'unverified';
}

// This connection has its own cursor realm. Never read the dashboard's SSE
// cursor/localStorage or allow its older pool candles into this stream.
export function openLiveMarketStream({ selection, snapshot, onCandle, onReset, onStatus,
  apiBase = API_BASE, EventSourceImpl = globalThis.EventSource, schedule = setTimeout, cancel = clearTimeout }) {
  if (!EventSourceImpl || !liveMarketSnapshotMatches(snapshot, selection)) return { close() {} };
  let source, timer, closed = false, cursor = snapshot.cursor, epoch = snapshot.epoch, retries = 0;
  const state = patch => onStatus?.(patch);
  const parse = event => { try { return JSON.parse(event.data); } catch { return null; } };
  const advance = event => { const n = Number(event.lastEventId); if (Number.isSafeInteger(n) && n >= cursor) cursor = n; };
  const connect = () => {
    if (closed) return;
    const query = new URLSearchParams({ chain: selection.chainId, token: selection.token,
      pool: selection.poolId, bar: selection.bar, after: String(cursor), epoch });
    const connection = source = new EventSourceImpl(`${apiBase}live-market/stream?${query}`);
    const active = () => !closed && source === connection;
    state({ connected: false });
    source.addEventListener('hello', event => {
      const packet = parse(event);
      if (!active() || !packet?.liveMarket || packet.epoch !== epoch) return;
      advance(event); retries = 0; state({ connected: true });
    });
    source.addEventListener('heartbeat', event => {
      const packet = parse(event);
      if (active() && packet?.liveMarket && packet.epoch === epoch) { advance(event); state({ connected: true, heartbeat: true }); }
    });
    const matching = packet => active() && packet?.liveMarket === true && candlePacketMatches(packet, selection)
      && packet.priceCurrency === snapshot.priceCurrency && packet.volumeCurrency === snapshot.volumeCurrency;
    source.addEventListener('market.candle', event => {
      const packet = parse(event);
      if (!matching(packet) || !packet.row) return;
      advance(event); state({ connected: true }); onCandle?.(packet);
    });
    source.addEventListener('market.reset', event => {
      const packet = parse(event);
      if (!matching(packet)) return;
      advance(event); onReset?.(packet);
    });
    source.addEventListener('reset', event => {
      const packet = parse(event);
      if (!active() || !packet?.liveMarket) return;
      connection.close(); closed = true; state({ connected: false });
      onReset?.({ ...selection, ...packet, liveMarket: true, reload: true });
    });
    source.onerror = () => {
      if(!active())return;
      connection.close(); state({ connected: false });
      if (!closed) timer = schedule(connect, Math.min(1000 * 2 ** retries++, 15000));
    };
  };
  connect();
  return { close() { closed = true; source?.close(); cancel(timer); state({ connected: false }); } };
}
