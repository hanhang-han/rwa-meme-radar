import { collectDashboard, dashboardState, dashboardDetail, dashboardEvents } from './lib/dashboard-v2';
import { serve } from "@hono/node-server";
import { Hono } from "hono";
import { compress } from 'hono/compress';
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
import { rpc } from "./lib/rpc";
import { tokens, padAddr, decimalsOf } from "./lib/erc20";
import { findPools, readPoolPrice, PoolHit } from "./lib/pancake";
import { fetchAssets, fetchOraclePrice, BackedAsset } from "./lib/backed";
import { recentActivity, fetchRadar, EventHistogram, RadarData, NewToken } from "./lib/fourmeme";
import { matchStock, buildTickerSet } from "./lib/stocks";
import { ts, tsHourly } from "./lib/timeseries";
import { BSTOCKS, binanceBstockToken, binanceState, refreshBinanceBstocks } from "./lib/bstocks";
import { startBinanceStream, applyBinanceLive } from "./lib/binance-ws";

import { readSnapshot, writeSnapshot } from "./lib/snapshot";
import { sampleRelationship, relationshipAnalytics, sectorAnalytics, saveRelationships } from "./lib/relationship-series";
import { verifyDirectPools, Verification } from "./lib/verification";
import { okxState, refreshOkx } from "./lib/okx";
import { assessRelation, relationTypes } from "./lib/relations";
import { refreshXLayer, refreshLiveQuotes, refreshSideQuotes, refreshAssetOnDemand, xLayerState, xLayerDetail } from "./lib/xlayer";
import { unifiedFromXLayer } from "./lib/unified-state";
import { refreshRobinhood, robinhoodState, robinhoodToken } from "./lib/robinhood";
import { refreshMarketEnrichment } from "./lib/market-enrichment";
import { aiEnabled, aiNarrate } from "./lib/ai";
import { registryConfigured, registryInfo, syncRegistry } from "./lib/registry";
import { candleSeries, CANDLE_BARS } from "./lib/candles";

if (existsSync(".env")) process.loadEnvFile(".env");
const require = createRequire(import.meta.url);
const QUOTES = { USDT: tokens.USDT, USDC: tokens.USDC, WBNB: tokens.WBNB };
const BNB_USD_FEED = "0x0567F2323251f0Aab15c8dFb1967E4e8A7D42aeE";

export interface AssetRow {
  symbol: string;
  name: string;
  underlying: string;
  addr: string;
  halted: boolean;
  period: string;
  oracle: number | null;
  oracleAt: number | null;
  pools: PoolHit[];
  dexPrice: number | null;
  spreadPct: number | null;
  priceAt: number | null;
  lockedUsd: number | null;
  chg24h: number | null;
  source: "xstock" | "bstock";
  scanTried?: boolean;
}

interface State {
  startedAt: number;
  bnbUsd: number | null;
  bnbAt: number | null;
  assets: AssetRow[];
  scanned: number;
  poolsFound: number;
  fourMeme: EventHistogram | null;
  radar: RadarData | null;
  health: Record<string, number>;
  attention: number | null;
  attentionParts: { trades: number; creates: number; premium: number } | null;
}

const state: State = {
  startedAt: Date.now(),
  bnbUsd: null,
  bnbAt: null,
  assets: [],
  scanned: 0,
  poolsFound: 0,
  fourMeme: null,
  radar: null,
  health: {},
  attention: null,
  attentionParts: null,
};
const verifications = new Map<string, Verification>();
const bySymbol = new Map<string, AssetRow>();
// Keep a bounded archive of every token seen in a radar window. The table is
// intentionally capped at 100 for response speed, but details must remain
// addressable after a token rolls out of that table.
const radarArchive = new Map<string, NewToken>();

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function loop(name: string, intervalMs: number, fn: () => Promise<void>) {
  for (;;) {
    try {
      await fn();
      state.health[name] = Date.now();
    } catch (e) {
      console.error(`[${name}]`, e instanceof Error ? e.message : e);
    }
    await sleep(intervalMs);
  }
}

function toRow(a: BackedAsset): AssetRow {
  const dep = a.deployments.find((d) => d.network === "BinanceSmartChain");
  return {
    symbol: a.symbol,
    name: a.name,
    underlying: a.underlyingSymbol,
    addr: dep?.address ?? "",
    halted: a.isTradingHalted,
    period: a.trading?.currentPeriod ?? "?",
    oracle: null,
    oracleAt: null,
    pools: [],
    dexPrice: null,
    spreadPct: null,
    priceAt: null,
    lockedUsd: null,
    chg24h: null,
    source: "xstock" as const,
  };
}

async function refreshAssets() {
  const assets = await fetchAssets(5);
  const bsc = assets.map(toRow).filter((r) => r.addr);
  for (const row of bsc) {
    const old = bySymbol.get(row.symbol);
    if (old) {
      old.name = row.name;
      old.halted = row.halted;
      old.period = row.period;
      old.addr = row.addr;
    } else {
      bySymbol.set(row.symbol, row);
      state.assets.push(row);
    }
  }
  console.log(`[assets] ${state.assets.length} BSC assets tracked`);
}

let oracleCursor = 0;
async function refreshOracles() {
  const list = state.assets;
  if (list.length === 0) return;
  const batch = [];
  for (let i = 0; i < 40 && i < list.length; i++) batch.push(list[(oracleCursor + i) % list.length]);
  oracleCursor = (oracleCursor + batch.length) % Math.max(list.length, 1);
  for (const row of batch) {
    const q = await fetchOraclePrice(row.symbol);
    if (q !== null) {
      row.oracle = q;
      row.oracleAt = Date.now();
    }
    await sleep(250);
  }
}

let scanCursor = 0;
async function scanPools() {
  const list = state.assets;
  if (list.length === 0) return;
  const pendingBstock = list.find((a) => a.source === "bstock" && a.pools.length === 0 && !a.scanTried);
  const row = pendingBstock ?? list[scanCursor % list.length];
  if (pendingBstock) pendingBstock.scanTried = true;
  else scanCursor = (scanCursor + 1) % list.length;
  if (row.pools.length > 0) {
    state.scanned++;
    return;
  }
  const pools = await findPools(row.addr, QUOTES);
  if (pools.length > 0) {
    row.pools = pools;
    state.poolsFound++;
    console.log(`[pools] ${row.symbol}: ${pools.map((p) => `${p.kind}/${p.quote}`).join(", ")}`);
  }
  state.scanned++;
}

async function refreshPoolPrices() {
  const bnb = state.bnbUsd ?? 0;
  for (const row of state.assets) {
    if (row.pools.length === 0 || !row.oracle) continue;
    const pool = row.pools[0];
    try {
      const { priceInQuote } = await readPoolPrice(pool, row.addr);
      const usd = pool.quote === "WBNB" ? priceInQuote * bnb : priceInQuote;
      row.dexPrice = usd;
      row.spreadPct = (usd / row.oracle - 1) * 100;
      row.priceAt = Date.now();
      try {
        let bal: number;
        if (pool.kind === "V2") {
          const reserves = await rpc.call(pool.address, "0x0902f1ac");
          const t0 = (await rpc.call(pool.address, "0x0dfe1681")).slice(26).toLowerCase();
          const tokenIs0 = t0 === row.addr.toLowerCase();
          const r = BigInt("0x" + reserves.slice(2 + (tokenIs0 ? 0 : 64), 66 + (tokenIs0 ? 0 : 64)));
          bal = Number(r) / 10 ** (await decimalsOf(row.addr));
        } else {
          const balRaw = await rpc.call(row.addr, "0x70a08231" + padAddr(pool.address));
          bal = Number(BigInt(balRaw)) / 10 ** (await decimalsOf(row.addr));
        }
        row.lockedUsd = bal * row.oracle;
      } catch {
        row.lockedUsd = row.lockedUsd ?? null;
      }
    } catch {
      row.dexPrice = null;
      row.spreadPct = null;
    }
    await sleep(500);
  }
}

async function sampleHourly() {
  for (const a of state.assets) {
    if (a.oracle != null) tsHourly.push(`oracle:${a.symbol}`, a.oracle);
  }
  const dayAgo = Date.now() - 86_400_000;
  for (const a of state.assets) {
    const p = tsHourly.pointAt(`oracle:${a.symbol}`, dayAgo);
    a.chg24h = p && a.oracle ? (a.oracle / p.v - 1) * 100 : null;
  }
}

async function refreshBnb() {
  const raw = await rpc.call(BNB_USD_FEED, "0xfeaf968c");
  state.bnbUsd = Number(BigInt("0x" + raw.slice(66, 130))) / 1e8;
  state.bnbAt = Date.now();
}

function seedBstocks() {
  for (const b of BSTOCKS) {
    if (bySymbol.has(b.symbol)) continue;
    const row: AssetRow = {
      symbol: b.symbol,
      name: b.name,
      underlying: b.underlying,
      addr: b.addr,
      halted: false,
      period: "allday",
      oracle: null,
      oracleAt: null,
      pools: [],
      dexPrice: null,
      spreadPct: null,
      priceAt: null,
      lockedUsd: null,
      chg24h: null,
      source: "bstock",
    };
    bySymbol.set(b.symbol, row);
    state.assets.push(row);
  }
  console.log(`[bstocks] seeded ${BSTOCKS.length} bStocks`);
}

async function refreshBinance() {
  await refreshBinanceBstocks();
  let n = 0;
  for (const token of binanceState.tokens) {
    const row = bySymbol.get(token.tokenSymbol);
    if (!row || token.price == null) continue;
    row.oracle = token.price;
    row.oracleAt = token.quoteAt;
    row.chg24h = token.change24h ?? null;
    n++;
  }
}

async function refreshFourMeme() {
  state.fourMeme = await recentActivity(500);
}

async function refreshRadar() {
  // The live table uses a compact window so refreshes stay within public RPC
  // rate limits. Older detail links are handled by the bounded endpoint below.
  state.radar = await fetchRadar(3000);
  for (const token of (state.radar.allTokens ?? state.radar.tokens)) {
    const previous = radarArchive.get(token.token);
    radarArchive.set(token.token, previous && previous.volumeRaw > token.volumeRaw
      ? { ...token, volumeRaw: previous.volumeRaw, lastPriceWei: previous.lastPriceWei || token.lastPriceWei }
      : { ...token });
  }
  if (radarArchive.size > 2_000) {
    const remove = [...radarArchive.entries()].sort((a, b) => a[1].block - b[1].block).slice(0, radarArchive.size - 2_000);
    for (const [token] of remove) radarArchive.delete(token);
  }
  const minutes = ((state.radar.toBlock - state.radar.fromBlock) * 0.75) / 60;
  const tradesPerMin = state.radar.tradeCount / minutes;
  const createsPer10 = (state.radar.allTokens?.length ?? state.radar.tokens.length) / (minutes / 10);
  const premium = state.assets.some((a) => (a.spreadPct ?? -1) > 0) ? 15 : 0;
  const parts = {
    trades: Math.min(60, tradesPerMin * 6),
    creates: Math.min(25, createsPer10 * 1.8),
    premium,
  };
  state.attentionParts = { ...parts, trades: tradesPerMin, creates: createsPer10, premium };
  state.attention = Math.round(Math.min(100, parts.trades + parts.creates + parts.premium));
  console.log(`[radar] ${state.radar.tokens.length} 新币 / ${state.radar.tradeCount} 笔交易 | attention=${state.attention}`);
}

interface LeadCandidate {
  symbol: string;
  name: string;
  firstSeen: number;
  lastPriceWei: bigint;
  lastPriceAt: number;
  volumeRaw: bigint;
  buys: number;
  sells: number;
  supply: number | null;
  creator: string;
  block: number;
  ts: number | null;
}
const candidates = new Map<string, LeadCandidate>();

async function refreshRelations() {
  if (!state.radar) return;
  const tickers = buildTickerSet(state.assets.map(a => a.underlying));
  for (const token of (state.radar.allTokens ?? state.radar.tokens)) {
    const match = matchStock(token.symbol, token.name, tickers);
    if (!match) continue;
    const existing = candidates.get(token.token);
    if (existing) {
      existing.buys = token.buys;
      existing.sells = token.sells;
      if (token.volumeRaw > existing.volumeRaw) existing.volumeRaw = token.volumeRaw;
      existing.creator = token.creator;
      existing.block = token.block;
      existing.ts = token.ts;
      if (token.lastPriceWei > 0n) {
        existing.lastPriceWei = token.lastPriceWei;
        existing.lastPriceAt = Date.now();
      }
      existing.supply = token.supply || existing.supply;
      continue;
    }
    candidates.set(token.token, {
      symbol: token.symbol,
      name: token.name,
      firstSeen: Date.now(),
      lastPriceWei: token.lastPriceWei,
      lastPriceAt: token.lastPriceWei > 0n ? Date.now() : 0,
      volumeRaw: token.volumeRaw,
      buys: token.buys,
      sells: token.sells,
      supply: token.supply || null,
      creator: token.creator,
      block: token.block,
      ts: token.ts,
    });
    if (candidates.size > 400) {
      // Keep the newest 400 leads deterministically. This prevents a burst of
      // name matches from growing memory without bound while preserving the
      // most recent clues users are likely to open.
      const remove = [...candidates.entries()]
        .sort((a, b) => a[1].firstSeen - b[1].firstSeen)
        .slice(0, candidates.size - 400);
      for (const [key] of remove) candidates.delete(key);
    }
  }
  for (const [token, meta] of candidates) {
    if (Date.now() - (verifications.get(token)?.checkedAt ?? 0) < 600_000) continue;
    const match = matchStock(meta.symbol, meta.name, tickers);
    if (!match) continue;
    const stocks = state.assets.filter(a => a.underlying.toUpperCase() === match.ticker);
    if (!stocks.length) continue;
    verifications.set(token, await verifyDirectPools(token, stocks));
    await sleep(600);
  }
  if (verifications.size > 500) {
    for (const [key, value] of verifications) if (Date.now() - value.checkedAt > 3_600_000) verifications.delete(key);
  }
}

async function sampleRelations() {
  const tickers = buildTickerSet(state.assets.map(a => a.underlying));
  const list = [...candidates.entries()].slice(0, 80);
  let sampled = 0, noStock = 0, noPrice = 0, noMatch = 0, noVerification = 0;
  const reasons: Record<string, number> = {};
  for (const [meme, meta] of list) {
    const match = matchStock(meta.symbol, meta.name, tickers);
    if (!match) { noMatch++; continue; }
    // A name match is only a lead. Price-series sampling and sector baskets
    // require a verified direct pool so the stock and meme prices are not
    // accidentally presented as an established relationship.
    if (verifications.get(meme)?.status !== 'verified') { noVerification++; continue; }
    const stock = state.assets.find(a => a.underlying.toUpperCase() === match.ticker && a.oracle != null);
    if (!stock) { noStock++; continue; }
    const priceFresh = meta.lastPriceWei > 0n && Date.now() - meta.lastPriceAt < 3_600_000;
    if (!priceFresh) { noPrice++; continue; }
    const priceUsd = Number(meta.lastPriceWei) / 1e18 * (state.bnbUsd ?? 0);
    try {
      const r = await sampleRelationship(meme, stock, priceUsd, meta.supply);
      if (r === 'ok') sampled++;
      else reasons[r] = (reasons[r] ?? 0) + 1;
    } catch { /* absence remains absence */ }
    await sleep(600);
  }
  console.log(`[relations] sampled ${sampled} | noStock ${noStock} | noPrice ${noPrice} | noMatch ${noMatch} | noVerification ${noVerification} | reasons ${JSON.stringify(reasons)} | total ${list.length}`);
  saveRelationships();
}

function sampleSeries() {
  const spreads = state.assets.filter((a) => a.spreadPct != null);
  for (const a of spreads) ts.push(`spread:${a.symbol}`, a.spreadPct as number, a.period);
  if (state.radar) {
    const minutes = ((state.radar.toBlock - state.radar.fromBlock) * 0.75) / 60;
    ts.push("rate:create", (state.radar.allTokens?.length ?? state.radar.tokens.length) / (minutes / 10));
    ts.push("rate:trade", state.radar.tradeCount / minutes);
  }
  if (spreads.length) {
    ts.push("avgSpread", spreads.reduce((s, a) => s + (a.spreadPct as number), 0) / spreads.length);
  }
  if (state.attention != null) ts.push("attention", state.attention);
}

const cachedState = readSnapshot<State>("data/state.json");
if (cachedState && Array.isArray(cachedState.assets) && cachedState.assets.every(a => a && typeof a.symbol === 'string' && typeof a.underlying === 'string' && typeof a.addr === 'string' && Array.isArray(a.pools))) {
  for (const key of ['bnbUsd', 'bnbAt', 'assets', 'scanned', 'poolsFound', 'fourMeme', 'radar', 'health', 'attention', 'attentionParts'] as const) {
    if (cachedState[key] !== undefined) (state as any)[key] = cachedState[key];
  }
  for (const asset of state.assets) bySymbol.set(asset.symbol, asset);
  const cached = cachedState as (State & {
    leadStore?: [string, Partial<LeadCandidate> & { symbol: string; name: string; firstSeen: number; lastPriceWei?: string | bigint }][];
    verificationStore?: [string, Verification][];
    radarArchive?: [string, NewToken][];
  });
  if (Array.isArray(cached.radarArchive)) {
    for (const [token, value] of cached.radarArchive) {
      if (value && typeof value.symbol === "string") radarArchive.set(token, value);
    }
  }
  // Deploys take a JSON snapshot from /api/state, where the bounded archive
  // lives under radar.archiveTokens. Restore that representation as well so
  // a restart cannot discard detail links collected in the previous run.
  const archivedFromApi = (cachedState.radar as (RadarData & { archiveTokens?: NewToken[] }) | null)?.archiveTokens;
  if (Array.isArray(archivedFromApi)) {
    for (const value of archivedFromApi) {
      if (value && typeof value.token === "string") radarArchive.set(value.token, value);
    }
  }
  if (Array.isArray(cached.leadStore)) {
    for (const [token, meta] of cached.leadStore) {
      if (meta && typeof meta.symbol === "string") {
        candidates.set(token, {
          symbol: meta.symbol,
          name: meta.name,
          firstSeen: meta.firstSeen,
          lastPriceWei: typeof meta.lastPriceWei === "string" ? BigInt(meta.lastPriceWei) : meta.lastPriceWei ?? 0n,
          lastPriceAt: meta.lastPriceAt ?? 0,
          volumeRaw: typeof meta.volumeRaw === "string" ? BigInt(meta.volumeRaw) : meta.volumeRaw ?? 0n,
          buys: meta.buys ?? 0,
          sells: meta.sells ?? 0,
          supply: meta.supply ?? null,
          creator: meta.creator ?? "",
          block: meta.block ?? 0,
          ts: meta.ts ?? null,
        });
      }
    }
  }
  if (Array.isArray(cached.verificationStore)) {
    for (const [token, v] of cached.verificationStore) {
      if (v && v.status && typeof v.checkedAt === "number") verifications.set(token, v);
    }
  }
  console.log(`[snapshot] restored ${state.assets.length} assets, ${candidates.size} lead candidates, ${verifications.size} verifications; original sample timestamps retained`);
}
ts.load();
tsHourly.load();
seedBstocks();
void loop("relationshipSamples", 300_000, sampleRelations);
void loop("relations", 120_000, refreshRelations);
// Keep the OKX/X Layer collection cadence conservative for API quotas.
// The pipeline runs immediately at startup, then waits five minutes after
// each completed round (the round itself may take additional time).
void loop("dashboard", 300_000, collectDashboard);
void loop("marketEnrichment", 300_000, async () => refreshMarketEnrichment(dashboardState()));
void loop("robinhood", 300_000, refreshRobinhood);
void loop("bnb", 60_000, refreshBnb);
startBinanceStream();
void loop("binanceApply", 10_000, async () => { applyBinanceLive(); });
void loop("binance", 30_000, refreshBinance);
// Sub-minute price freshness for displayed assets; quota is enforced inside okxPost.
void loop("liveQuotes", 90_000, refreshLiveQuotes);
void loop("sideQuotes", 300_000, refreshSideQuotes);
void loop("assets", 600_000, refreshAssets);
void loop("oracles", 30_000, refreshOracles);
void loop("scan", 500, scanPools);
void loop("prices", 60_000, refreshPoolPrices);
void loop("fourmeme", 300_000, refreshFourMeme);
void loop("radar", 120_000, refreshRadar);
void loop("sample", 60_000, async () => sampleSeries());
void loop("hourly", 3_600_000, sampleHourly);
void loop("snapshot", 60_000, async () =>
  writeSnapshot("data/state.json", {
    ...state,
    leadStore: [...candidates.entries()],
    verificationStore: [...verifications.entries()],
    radarArchive: [...radarArchive.entries()],
  }),
);
void loop("persist", 300_000, async () => {
  ts.save();
  tsHourly.save();
});
void sampleHourly();

const app = new Hono();
app.use('/api/*',compress());
let dashboardJson='',dashboardJsonAt=0;
app.get('/api/dashboard',c=>{
  if(!dashboardJson||Date.now()-dashboardJsonAt>10000){
    const u=dashboardState();
    const pick=(row:any,keys:string[])=>Object.fromEntries(keys.filter(k=>row[k]!==undefined).map(k=>[k,row[k]]));
    const assets=u.assets.map(a=>pick(a,['token','chainId','chain','symbol','name','kind','firstSeen','updatedAt','fieldTimes','fieldSources','price','marketCap','volume24h','buys24h','sells24h','txs24h','holders','liquidity','change24h','match','risk','provider','providers','logoUrl','tradeAt']));
    const stockTokens=u.stockTokens.map(s=>pick(s,['assetId','chainId','tokenContractAddress','stockCode','tokenSymbol','tokenName','issuer','provider','providers','price','stockPrice','volume24h','volumeScope','priceScope','updatedAt','referenceAt','referenceObservedAt','referenceProvider','referenceSymbol','referenceVolume','referenceChange24h','premium','instrumentId','logoUrl','stockIdentity','fieldTimes']));
    dashboardJson=JSON.stringify({now:Date.now(),unified:{version:2,sources:u.sources,assets,stockTokens,relations:u.relations,signals:u.signals.slice(0,30),
      groups:u.groups.map(g=>({...g,members:g.members.map((a:any)=>`${a.chainId}:${a.token}`)})),
      quality:u.quality,metrics:u.metrics,sectors:u.sectors,distribution:u.distribution,capabilities:u.capabilities,collection:u.collection}});
    dashboardJsonAt=Date.now();
  }
  return c.body(dashboardJson,200,{'Content-Type':'application/json','Cache-Control':'no-store'});
});
function radarTokenJson(t: NewToken, tickers: Set<string>) {
  const match = matchStock(t.symbol, t.name, tickers);
  return {
    ...t,
    volumeRaw: t.volumeRaw.toString(),
    lastPriceWei: t.lastPriceWei.toString(),
    volumeBnb: Number(t.volumeRaw) / 1e18,
    volumeUsd: state.bnbUsd != null ? (Number(t.volumeRaw) / 1e18) * state.bnbUsd : null,
    lastPriceBnb: Number(t.lastPriceWei) / 1e18,
    lastPriceUsd: state.bnbUsd != null ? (Number(t.lastPriceWei) / 1e18) * state.bnbUsd : null,
    supplyMarketCapUsd: state.bnbUsd != null && t.supply > 0 && t.lastPriceWei > 0n
      ? t.supply * (Number(t.lastPriceWei) / 1e18) * state.bnbUsd : null,
    match,
    verification: verifications.get(t.token) ?? null,
    analytics: relationshipAnalytics(t.token),
    relation: assessRelation(match),
  };
}
app.get("/api/token/:chain/:address", async (c) => {
  const chain = c.req.param("chain");
  const address = c.req.param("address").toLowerCase();
  if (['196','56','4663'].includes(chain) && /^0x[\da-f]{40}$/.test(address)) {
    if(chain==='196')void refreshAssetOnDemand(address);
    const detail=dashboardDetail(chain,address);
    if(detail)return c.json(detail);
  }
  if (chain === "4663" && /^0x[\da-f]{40}$/.test(address)) {
    const token = robinhoodToken(address);
    if (!token) return c.json({ error: '该地址尚未进入持续追踪索引' }, 404);
    const now = robinhoodState.updatedAt ?? Date.now();
    const asset = {
      token: token.tokenContractAddress.toLowerCase(), chain: '4663', symbol: token.tokenSymbol, name: token.tokenName,
      firstSeen: now, updatedAt: token.quoteAt ?? now, source: 'Robinhood Stock Tokens', price: token.price,
      marketCap: null, volume24h: null, txs24h: null, buys24h: null, sells24h: null, holders: null,
      liquidity: null, change24h: null, kind: 'stock', match: null,
      sourceId: 'robinhood:chain:4663', provider: 'Robinhood', chainId: '4663', chainName: 'Robinhood Chain',
      tokenToAssetRatio: token.tokenToAssetRatio, assetStatus: token.assetStatus, isTradingHalt: token.isTradingHalt,
    };
    return c.json({ asset, stock: { ...token, provider: 'Robinhood', chainId: '4663', chainName: 'Robinhood Chain' }, relations: [],
      trades: [], events: [], samples: [], activity: { count: 0, buys: 0, sells: 0, volume: null },
      analysis: { conclusion: '该股票代币已进入统一目录；关联 Meme 池扫描尚未接入。', correlation: { reason: '等待独立 Meme 池和对齐样本' }, capture: { reason: '等待完整池覆盖' }, safety: '当前仅包含发行方目录与行情状态，链上风险与池证据待采集。' } });
  }
  if (chain === "56" && /^0x[\da-f]{40}$/.test(address)) {
    const token = binanceBstockToken(address);
    if (token) {
      const now = binanceState.updatedAt ?? Date.now();
      const asset = {
        token: token.tokenContractAddress.toLowerCase(), chain: '56', symbol: token.tokenSymbol, name: token.tokenName,
        firstSeen: now, updatedAt: token.quoteAt ?? now, price: token.price, marketCap: null,
        volume24h: token.volume24h, txs24h: token.exchangeTrades24h, buys24h: null, sells24h: null,
        holders: null, liquidity: null, change24h: token.change24h ?? null, kind: 'stock', match: null,
        sourceId: 'binance:bstocks:56', provider: 'Binance', chainId: '56', chainName: 'BNB Smart Chain',
        tokenToAssetRatio: 1, volumeScope: 'exchange',
      };
      return c.json({ asset, stock: { ...token, provider: 'Binance', chainId: '56', chainName: 'BNB Smart Chain' }, relations: [],
        trades: [], events: [], samples: [], activity: { count: 0, buys: 0, sells: 0, volume: null },
        analysis: { conclusion: '该股票代币已进入统一目录；关联 Meme 池扫描尚未接入。', correlation: { reason: '等待独立 Meme 池和对齐样本' }, capture: { reason: '等待完整池覆盖' }, safety: '当前仅包含发行方目录与行情状态，链上风险与池证据待采集。' } });
    }
  }
  if (chain !== "56" || !/^0x[\da-f]{40}$/.test(address)) return c.json({ error: "unsupported token" }, 400);
  const tickers = buildTickerSet(state.assets.map(a => a.underlying));
  const cached = [...radarArchive.values(), ...(state.radar?.allTokens ?? state.radar?.tokens ?? [])]
    .find(t => t.token.toLowerCase() === address);
  if (cached) return c.json(radarTokenJson(cached, tickers));
  return c.json({error:'Asset not yet in the persistent index'},404);
});
app.get('/api/events',c=>{
  const chain=c.req.query('chain')??'196';if(!['196','56','4663'].includes(chain))return c.json({error:'Unsupported chain'},400);
  let before;try{before=c.req.query('before')?JSON.parse(c.req.query('before')!):undefined;if(before&&(!Number.isFinite(before.t)||typeof before.id!=='string'))throw 0;}catch{return c.json({error:'Invalid cursor'},400);}
  return c.json(dashboardEvents(chain,before));
});
app.get('/api/pair/:chain/:stock',c=>{
 const chain=c.req.param('chain'),stock=c.req.param('stock').toLowerCase();
 if(!['196','56','4663'].includes(chain)||!/^0x[\da-f]{40}$/.test(stock))return c.json({error:'Invalid identity'},400);
 const data=dashboardDetail(chain,stock);return data?c.json(data):c.json({error:'Not indexed'},404);
});
const aiLang=(c:any)=>c.req.query('lang')==='en'?'en':'zh';
app.get('/api/ai/insight/:chain/:address',async c=>{
 const lang=aiLang(c),chain=c.req.param('chain'),address=c.req.param('address').toLowerCase();
 if(!['196','56','4663'].includes(chain)||!/^0x[\da-f]{40}$/.test(address))return c.json({error:'Invalid identity'},400);
 const d:any=dashboardDetail(chain,address);
 if(!d?.asset)return c.json({error:'Not indexed'},404);
 const mins=(t?:number)=>t==null?null:Math.max(0,Math.round((Date.now()-t)/60000));
 const a=d.asset;
 const data={asset:{symbol:a.symbol,name:a.name,chain:a.chainName,kind:a.kind,firstSeenMinutesAgo:mins(a.firstSeen),priceUsd:a.price,change24hPercent:a.change24h,marketCapUsd:a.marketCap,volume24hUsd:a.volume24h,trades24h:a.txs24h,buys24h:a.buys24h,sells24h:a.sells24h,holders:a.holders,liquidityUsd:a.liquidity,top10HolderPercent:a.risk?.top10,riskLevel:a.risk?.level},
  relations:(d.relations??[]).map((r:any)=>({ticker:r.ticker??r.stockCode,status:r.status,pairPoolLiquidityUsd:r.liquidityUsd,verifiedMinutesAgo:mins(r.checkedAt)})),
  nameMatch:a.match?{ticker:a.match.ticker,type:a.match.matchType??a.match.type}:null,
  stockReference:d.stock?{symbol:d.stock.tokenSymbol,stockPriceUsd:d.stock.stockPrice,tokenPriceUsd:d.stock.price,crossMarketPremiumPercent:d.stock.premium}:null};
 const r=await aiNarrate(`insight:${chain}:${address}`,lang,15*60_000,data,
  '解读这个资产与股票的关系和资金面：先给关系结论（已核验配对/仅名称线索/与股票无关），再从成交量、流动性、持仓集中度、涨跌幅、溢价中选有值的两个到三个最有信息量的数字补充。');
 return r?c.json({text:r.text,at:r.at,cached:(r as any).cached??false}):c.json({text:null,reason:aiEnabled()?'upstream_failed':'disabled'});
});
const BRIEFING_TASK=`为加密雷达首页写今日简报。必须用以下固定结构（节标题逐字保留，中文版用中文标题，英文版用 Today in one line / Worth a look / Caution / Data bounds）：
今日一句话：<一句话给今天定性，点出最重要的一个发现或异常>
值得看：
- <2到4条，每条一个资产，资产符号必须用【】包裹（如【BNC】），格式：资产+关键数字+流动性定性+为什么值得看>
警惕：
- <1到3条风险信号：同一ticker在24小时内出现3条以上同名新币属批量碰币模式要点名；仅名称匹配无已验证关系；疑似池子异常>
数据边界：<一句话覆盖率>
规则：
1. liquidityTierLabel 只是流动性规模描述，不是可交易性结论；严禁使用"可交易""适合买入"等判断词，只陈述"池流动性约X美元"；低于$1千的可补充"进出场可能困难"。
2. streakDays 大于等于2的资产标注"连续第N天上榜"（英文 the Nth straight day）。
3. 新增关系记录数必须表述为"新增关系记录X条（含未核验名称线索）"，严禁说成"已验证关系"或"已核验关系"。
4. 同名新币数量只能描述为"同名新币密集出现，符合批量发行特征"，不得断言"骗局"或"碰瓷模式成立"；无已核验关系就说"仅有名称匹配"。
5. 正股类代币跌幅超过50%时表述为"跌幅远超正股市场正常范围，价格数据待核查"，不下"异常"结论以外的判断。
6. 流动性为0或字段缺失的配对不进"值得看"；只依据输入JSON的数字，不预测涨跌不给建议；英文版禁止出现中文；总长150到220字。`;
const STREAK_FILE = "data/ai-streaks.json";
function loadStreaks(): Record<string, string[]> { try { return JSON.parse(readFileSync(STREAK_FILE, "utf8")); } catch { return {}; } }
function streakOf(hist: Record<string, string[]>, symbol: string, currentSet: string[]): number {
  const has = (i: number) => { const d = new Date(Date.now() - i * 86400_000).toISOString().slice(0, 10); return (hist[d] ?? []).includes(symbol) || (i === 0 && currentSet.includes(symbol)); };
  let n = 0; for (let i = 0; i < 15; i++) { if (has(i)) n++; else if (i === 0) continue; else break; } return n;
}
function updateStreaks(symbols: string[]) {
  if (!symbols.length) return;
  const day = new Date().toISOString().slice(0, 10);
  const hist = loadStreaks();
  hist[day] = [...new Set([...(hist[day] ?? []), ...symbols])].slice(0, 20);
  const days = Object.keys(hist).sort().slice(-15);
  try { writeFileSync(STREAK_FILE, JSON.stringify(Object.fromEntries(days.map(d => [d, hist[d]])))); } catch (e) { console.error("[aiBriefing] streaks write failed", e); }
}
function tierLabel(v: number | null | undefined, lang: string): string | null {
  if (v == null || !Number.isFinite(Number(v))) return null;
  const n = Number(v);
  return lang === "en" ? (n >= 10000 ? "pool liquidity ≥$10k" : n >= 1000 ? "pool liquidity $1k–$10k" : "pool liquidity <$1k")
    : (n >= 10000 ? "池流动性≥$1万" : n >= 1000 ? "池流动性$1千-$1万" : "池流动性<$1千");
}
function briefingBase() {
 const u=dashboardState();
 const dayAgo=Date.now()-86400_000;
 const tickers=buildTickerSet(state.assets.map(a=>a.underlying));
 const leadRows=[...candidates.values()].filter(x=>x.firstSeen>=dayAgo);
 const leadTickerCount=new Map<string,number>();
 for(const x of leadRows){const m=matchStock(x.symbol,x.name,tickers);if(m?.ticker)leadTickerCount.set(m.ticker,(leadTickerCount.get(m.ticker)??0)+1);}
 const newLeads={total:leadRows.length,byTicker:[...leadTickerCount.entries()].sort((a,b)=>b[1]-a[1]).slice(0,6).map(([ticker,count])=>({ticker,count,batchImpersonationSuspected:count>=3}))};
 const newVerifiedRaw=(u.relations as any[]).filter(r=>r.status==='verified'&&r.firstSeen>=dayAgo).slice(0,8)
  .map(r=>({ticker:r.ticker??r.stockCode,chain:r.chainName,poolLiquidityUsd:(r.liquidityUsd??0)>0?r.liquidityUsd:null}));
 const verifiedTokens=new Set((u.relations as any[]).filter(r=>r.status==='verified').map(r=>r.token));
 const moverSymbols=(u.assets as any[]).filter(a=>verifiedTokens.has(a.token)&&a.change24h!=null&&Number.isFinite(a.change24h))
  .sort((a,b)=>Math.abs(Number(b.change24h))-Math.abs(Number(a.change24h))).slice(0,4).map((a:any)=>a.symbol);
 const hist=loadStreaks();
 const movers=(u.assets as any[]).filter(a=>verifiedTokens.has(a.token)&&a.change24h!=null&&Number.isFinite(a.change24h))
  .sort((a,b)=>Math.abs(Number(b.change24h))-Math.abs(Number(a.change24h))).slice(0,4)
  .map(a=>({symbol:a.symbol,chain:a.chainName,change24hPercent:a.change24h,volume24hUsd:a.volume24h,liquidityUsd:a.liquidity,streakDays:streakOf(hist,a.symbol,moverSymbols),priceAnomalySuspected:Number(a.change24h)<=-50,note:Number(a.change24h)<=-50?'跌幅远超正股市场正常范围，价格数据待核查':null}));
 const premiums=(u.stockTokens as any[]).filter(s=>s.premium!=null&&Number.isFinite(Number(s.premium)))
  .sort((a,b)=>Math.abs(Number(b.premium))-Math.abs(Number(a.premium))).slice(0,4)
  .map(s=>({symbol:s.tokenSymbol??s.stockCode,chain:s.chainName,premiumPercent:s.premium,stockPriceUsd:s.stockPrice}));
 const m=u.metrics as any;
 return {metrics:{verifiedPools:m.verifiedPools,verifiedAssets:m.verifiedAssets,newRelationRecords24h:m.newRelations24h,newRelationRecordsNote:'含未核验的名称线索记录，不等于已核验关系'},
  distribution:u.distribution,newNameLeads24h:newLeads,newVerifiedPairs24h:newVerifiedRaw,verifiedAssetMovers24h:movers,moverSymbols,crossMarketPremiumTop:premiums};
}
const ASCII_ONLY=/^[\x20-\x7E]+$/;
function briefingFor(base:ReturnType<typeof briefingBase>,lang:string){
 const movers=base.verifiedAssetMovers24h.map(x=>({...x,liquidityTierLabel:tierLabel(x.liquidityUsd,lang)}));
 const verified=base.newVerifiedPairs24h.map(x=>({...x,liquidityTierLabel:tierLabel(x.poolLiquidityUsd,lang)}));
 if(lang==='en'){
  // Upstream symbols can be localised (e.g. a Chinese meme name); an English
  // briefing must never leak non-ASCII symbols, so drop those rows.
  return {...base,verifiedAssetMovers24h:movers.filter(x=>ASCII_ONLY.test(x.symbol)),newVerifiedPairs24h:verified.filter(x=>ASCII_ONLY.test(x.ticker)),newNameLeads24h:{...base.newNameLeads24h,byTicker:base.newNameLeads24h.byTicker.filter(x=>ASCII_ONLY.test(x.ticker))},crossMarketPremiumTop:base.crossMarketPremiumTop.filter(x=>ASCII_ONLY.test(x.symbol))};
 }
 return {...base,verifiedAssetMovers24h:movers,newVerifiedPairs24h:verified};
}
app.get('/api/ai/briefing',async c=>{
 const lang=aiLang(c);
 const r=await aiNarrate('briefing',lang,30*60_000,briefingFor(briefingBase(),lang),BRIEFING_TASK);
 return r?c.json({text:r.text,at:r.at,cached:(r as any).cached??false}):c.json({text:null,reason:aiEnabled()?'upstream_failed':'disabled'});
});
void loop("aiBriefing", 30*60_000, async () => {
  if (!aiEnabled()) return;
  const base = briefingBase();
  // One snapshot for both languages, force-refreshed together so zh/en never
  // disagree on numbers across cache generations.
  for (const lang of ["zh", "en"]) await aiNarrate("briefing", lang, 30*60_000, briefingFor(base, lang), BRIEFING_TASK, true);
  updateStreaks(base.moverSymbols);
});
// On-chain PairRegistry (X Layer 196): public read API + idempotent sync of
// verified pairs. Unconfigured (no REGISTRY_CONTRACT) degrades to metadata only.
app.get("/api/registry", async (c) => c.json(await registryInfo(xLayerState().relations)));
// Real OHLCV candles from OKX DEX REST, cached per bar and persisted so
// history extends beyond the API's recent window.
app.get("/api/candles/:chain/:address", async (c) => {
  const chain = c.req.param("chain"), address = c.req.param("address").toLowerCase();
  const bar = c.req.query("bar") ?? "5m";
  if (!CANDLE_BARS.includes(bar) || !["196", "56", "4663"].includes(chain) || !/^0x[\da-f]{40}$/.test(address)) return c.json({ error: "bad request" }, 400);
  try {
    const limit = Math.min(Math.max(Number(c.req.query("limit") ?? 150) || 150, 30), 300);
    return c.json(await candleSeries(chain, address, bar, limit));
  } catch (e) {
    return c.json({ error: e instanceof Error ? e.message : "upstream failed" }, 502);
  }
});
void loop("registrySync", 600_000, async () => {
  if (!registryConfigured()) return;
  await syncRegistry(xLayerState().relations);
});
app.get("/api/state", (c) => {
  const tickers = buildTickerSet(state.assets.map((a) => a.underlying));
  const xlayer = xLayerState();
  return c.json({
    ...state,
    rpcPool: rpc.nodeStats(),
    leadCandidates: [...candidates.entries()].map(([token, meta]) => {
      const v = verifications.get(token);
      const match = matchStock(meta.symbol, meta.name, tickers);
      return { token, symbol: meta.symbol, name: meta.name, firstSeen: meta.firstSeen,
        creator: meta.creator, block: meta.block, ts: meta.ts,
        buys: meta.buys, sells: meta.sells, volumeRaw: meta.volumeRaw.toString(),
        volumeBnb: Number(meta.volumeRaw) / 1e18,
        volumeUsd: state.bnbUsd != null ? (Number(meta.volumeRaw) / 1e18) * state.bnbUsd : null,
        supply: meta.supply, lastPriceBnb: Number(meta.lastPriceWei) / 1e18,
        lastPriceUsd: state.bnbUsd != null ? (Number(meta.lastPriceWei) / 1e18) * state.bnbUsd : null,
        status: v?.status ?? "pending", pools: v?.pools?.length ?? 0, checkedAt: v?.checkedAt ?? null,
        verification: v ?? null, analytics: relationshipAnalytics(token), relation: assessRelation(match) , match };
    }).sort((a, b) => b.firstSeen - a.firstSeen).slice(0, 120),
    okx: okxState,
    // `unified` is the source-neutral feed consumed by the new relationship
    // screens. Keep `xlayer` temporarily for the legacy API contract.
    unified: dashboardState(),
    xlayer,
    relationTypes,
    // Baskets are made from verified direct-pair members only. Name matches
    // remain visible as leads, but cannot move an industry index.
    sectors: sectorAnalytics(
      [...candidates.entries()]
        .filter(([token]) => verifications.get(token)?.status === 'verified')
        .map(([token, meta]) => ({ token, ticker: matchStock(meta.symbol, meta.name, tickers)?.ticker ?? "" }))
        .filter((x) => x.ticker),
    ),
    radar: state.radar
      ? {
          fromBlock: state.radar.fromBlock,
          toBlock: state.radar.toBlock,
          trades: state.radar.trades,
          funnel: state.radar.funnel,
          tradeCount: state.radar.tradeCount,
          updatedAt: state.radar.updatedAt,
          tokens: state.radar.tokens.map((t) => ({
            ...t,
            volumeRaw: t.volumeRaw.toString(),
            lastPriceWei: t.lastPriceWei.toString(),
            volumeBnb: Number(t.volumeRaw) / 1e18,
            volumeUsd: state.bnbUsd != null ? (Number(t.volumeRaw) / 1e18) * state.bnbUsd : null,
            lastPriceBnb: Number(t.lastPriceWei) / 1e18,
            lastPriceUsd: state.bnbUsd != null ? (Number(t.lastPriceWei) / 1e18) * state.bnbUsd : null,
            supplyMarketCapUsd:
              state.bnbUsd != null && t.supply > 0 && t.lastPriceWei > 0n
                ? t.supply * (Number(t.lastPriceWei) / 1e18) * state.bnbUsd
                : null,
            match: matchStock(t.symbol, t.name, tickers),
            verification: verifications.get(t.token) ?? null,
            analytics: relationshipAnalytics(t.token),
            relation: assessRelation(matchStock(t.symbol, t.name, tickers)),
          })),
          archiveTokens: [...new Map([
            ...radarArchive.entries(),
            ...(state.radar.allTokens ?? state.radar.tokens).map(t => [t.token, t] as [string, NewToken]),
          ]).values()].sort((a, b) => b.block - a.block).slice(0, 500).map((t) => ({
            ...t,
            volumeRaw: t.volumeRaw.toString(),
            lastPriceWei: t.lastPriceWei.toString(),
            volumeBnb: Number(t.volumeRaw) / 1e18,
            volumeUsd: state.bnbUsd != null ? (Number(t.volumeRaw) / 1e18) * state.bnbUsd : null,
            lastPriceBnb: Number(t.lastPriceWei) / 1e18,
            lastPriceUsd: state.bnbUsd != null ? (Number(t.lastPriceWei) / 1e18) * state.bnbUsd : null,
            supplyMarketCapUsd:
              state.bnbUsd != null && t.supply > 0 && t.lastPriceWei > 0n
                ? t.supply * (Number(t.lastPriceWei) / 1e18) * state.bnbUsd
                : null,
            match: matchStock(t.symbol, t.name, tickers),
            verification: verifications.get(t.token) ?? null,
            analytics: relationshipAnalytics(t.token),
            relation: assessRelation(matchStock(t.symbol, t.name, tickers)),
          })),
        }
      : null,
    // `scanned` is a number of polling attempts and can exceed the asset count
    // after the scanner has made several passes. Report actual pool coverage
    // instead of an ever-growing percentage.
    scanProgress: Math.round((state.assets.filter((a) => a.pools.length > 0).length / Math.max(state.assets.length, 1)) * 100),
    now: Date.now(),
  });
});
app.get("/", (c) => c.body(readFileSync("public/index.html", "utf-8"), 200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache" }));
app.get("/app.js", (c) => c.body(readFileSync("public/app.js", "utf-8"), 200, { "Content-Type": "application/javascript", "Cache-Control": "no-cache" }));
app.get("/workspace.js", (c) => c.body(readFileSync("public/workspace.js", "utf-8"), 200, { "Content-Type": "application/javascript", "Cache-Control": "no-cache" }));
app.get("/xlayer-workspace.js", (c) => c.body(readFileSync("public/xlayer-workspace.js", "utf-8"), 200, { "Content-Type": "application/javascript", "Cache-Control": "no-cache" }));
app.get('/radar-v2.js',c=>c.body(readFileSync('public/radar-v2.js','utf8'),200,{'Content-Type':'application/javascript','Cache-Control':'no-cache'}));
app.get("/i18n.js", (c) => c.body(readFileSync("public/i18n.js", "utf-8"), 200, { "Content-Type": "application/javascript", "Cache-Control": "no-cache" }));
app.get("/style.css", (c) => c.body(readFileSync("public/style.css", "utf-8"), 200, { "Content-Type": "text/css", "Cache-Control": "no-cache" }));
app.get("/echarts.min.js", (c) =>
  c.body(readFileSync(require.resolve("echarts/dist/echarts.min.js")), 200, { "Content-Type": "application/javascript", "Cache-Control": "public, max-age=86400" }),
);
app.get("/lightweight-charts.js", (c) =>
  c.body(readFileSync("public/lightweight-charts.js"), 200, { "Content-Type": "application/javascript", "Cache-Control": "public, max-age=86400" }),
);
app.get("/api/history", (c) => {
  const out: Record<string, { t: number; v: number; p?: string }[]> = {};
  for (const key of ts.keys()) out[key] = ts.get(key, 1440);
  return c.json({ series: out, now: Date.now() });
});

const port = Number(process.env.PORT || 3456);
serve({ fetch: app.fetch, port }, (info) => console.log(`memedashboard MVP → http://localhost:${info.port}`));
