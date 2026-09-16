// Real OHLCV candles from OKX DEX REST (free tier), cached in memory per
// asset+bar and merged into a persistent candles table so history survives
// beyond the API's recent-window limit. Unclosed bars are overwritten by
// newer fetches; closed bars come back identical from the API.
import { okxGet } from './okx-client';
import { ResearchStore } from './research-store';

const BAR_TTL: Record<string, number> = { '1m': 25_000, '5m': 120_000, '15m': 300_000, '1H': 600_000 };
export const CANDLE_BARS = Object.keys(BAR_TTL);

export interface Candle { t: number; o: number; h: number; l: number; c: number; v: number | null; vu: number | null; confirmed: boolean }

const mem = new Map<string, { rows: Candle[]; at: number }>();
const stores = new Map<string, ResearchStore>();
function store(chain: string) {
  if (!stores.has(chain)) stores.set(chain, new ResearchStore(process.env.RESEARCH_DB || 'data/research.sqlite', chain));
  return stores.get(chain)!;
}

const num = (v: unknown) => { const n = Number(v); return Number.isFinite(n) && n > 0 ? n : null; };

// One in-flight request per asset+bar keeps concurrent viewers from
// multiplying upstream calls.
const inflight = new Map<string, Promise<Candle[]>>();

export async function candleSeries(chain: string, address: string, bar: string, limit: number): Promise<{ bar: string; rows: Candle[]; at: number }> {
  const key = `${chain}:${address}:${bar}`;
  const hit = mem.get(key);
  if (hit && Date.now() - hit.at < BAR_TTL[bar]) return { bar, rows: hit.rows, at: hit.at };
  const pending = inflight.get(key);
  if (pending) return pending.then(rows => ({ bar, rows, at: mem.get(key)!.at }));
  const task = (async () => {
    const data = await okxGet('/api/v6/dex/market/candles', { chainIndex: chain, tokenContractAddress: address, bar, limit: String(Math.min(limit, 300)) });
    if (!Array.isArray(data)) throw new Error('Invalid candles response');
    const parsed: Candle[] = [];
    for (const r of data as unknown[][]) {
      const [t, o, h, l, c, v, vu, confirmed] = r as [string, string, string, string, string, string, string, string];
      if (!t || !o || !c) continue;
      parsed.push({ t: Number(t), o: Number(o), h: Number(h ?? o), l: Number(l ?? o), c: Number(c), v: num(v), vu: num(vu), confirmed: confirmed === '1' });
    }
    const s = store(chain);
    s.candles(address, bar, parsed);
    const rows = s.candleRange(address, bar, Math.min(limit, 300)) as Candle[];
    mem.set(key, { rows, at: Date.now() });
    return rows;
  })();
  inflight.set(key, task);
  try {
    const rows = await task;
    return { bar, rows, at: mem.get(key)!.at };
  } finally { inflight.delete(key); }
}
