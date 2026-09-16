// Shared Binance miniTicker stream for bStocks pairs: real-time prices with
// zero REST quota. The 30s REST poll stays as the fallback that also supplies
// catalogue and trade-count fields the stream lacks.
import { BSTOCKS, binanceState } from './bstocks';

const live = new Map<string, { price: number; open24h: number; quoteVol24h: number | null; at: number }>();
let started = false;

export function startBinanceStream() {
  if (started || process.env.NODE_ENV === 'test') return;
  started = true;
  const streams = BSTOCKS.map(b => `${b.symbol.toLowerCase()}usdt@miniTicker`).join('/');
  let retry = 0;
  const connect = () => {
    const ws = new WebSocket(`wss://stream.binance.com:9443/stream?streams=${streams}`);
    ws.onmessage = (ev: MessageEvent) => {
      try {
        const msg = JSON.parse(String(ev.data));
        const d = msg?.data;
        if (!d?.s || d.c == null) return;
        const base = String(d.s).replace(/USDT$/, '');
        live.set(base, {
          price: Number(d.c),
          open24h: Number(d.o),
          quoteVol24h: d.q != null && Number.isFinite(Number(d.q)) ? Number(d.q) : null,
          at: Date.now(),
        });
        retry = 0;
      } catch { /* ignore malformed frames */ }
    };
    ws.onclose = () => reconnect();
    ws.onerror = () => { try { ws.close(); } catch { /* already closing */ } };
  };
  const reconnect = () => {
    retry = Math.min(retry + 1, 6);
    setTimeout(connect, 1000 * 2 ** retry);
  };
  connect();
}

// Fold stream prices into the shared state on a short cadence so every
// consumer sees near-live values without extra REST calls.
export function applyBinanceLive() {
  if (!live.size || !binanceState.tokens.length) return;
  let touched = false;
  for (const t of binanceState.tokens) {
    const hit = live.get(String(t.tokenSymbol ?? '').toUpperCase());
    if (!hit) continue;
    if (t.quoteAt != null && hit.at <= t.quoteAt) continue;
    t.price = hit.price;
    if (hit.open24h > 0) t.change24h = (hit.price / hit.open24h - 1) * 100;
    if (hit.quoteVol24h != null) t.volume24h = hit.quoteVol24h;
    t.quoteAt = hit.at;
    touched = true;
  }
  if (touched) binanceState.status = binanceState.status === 'error' ? 'stale' : binanceState.status === 'starting' ? 'ready' : binanceState.status;
}

export function binanceStreamHealth() {
  const fresh = [...live.values()].filter(v => Date.now() - v.at < 120_000).length;
  return { connected: fresh > 0, freshSymbols: fresh };
}
