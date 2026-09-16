const EXTRA_TICKERS = [
  "AAPL", "TSLA", "NVDA", "GOOG", "GOOGL", "AMZN", "META", "MSFT", "AMD", "INTC", "COIN",
  "PLTR", "SOFI", "HIMS", "GME", "AMC", "DJT", "MSTR", "RIVN", "LCID", "OPEN", "AI",
  "SHEIN", "NIKE", "NKE", "DIS", "NFLX", "BA", "F", "GM", "SBUX", "MCD", "NIO", "XPEV", "LI",
  "BABA", "JD", "PDD", "BIDU", "NTES", "TME", "IQ", "TSLA", "BYDDY", "MOUTAI",
];
const COMPANY_KEYWORDS: Record<string, string[]> = {
  TSLA: ["tesla"],
  NVDA: ["nvidia"],
  AAPL: ["apple", "iphone"],
  GME: ["gamestop"],
  AMC: ["amc"],
  HIMS: ["hims"],
  DJT: ["trump"],
  SHEIN: ["shein"],
  PLTR: ["palantir"],
  MSTR: ["microstrategy"],
  NIO: ["nio", "weilai"],
  BABA: ["alibaba"],
  PDD: ["pinduoduo", "temu"],
  JD: ["jingdong", "jd.com"],
  COIN: ["coinbase"],
};

export interface StockMatch {
  ticker: string;
  matchType: "exact" | "startsWith" | "contains" | "company";
}

const norm = (s: string) => s.toUpperCase().replace(/[^A-Z0-9]/g, "");

export function matchStock(symbol: string, name: string, knownTickers: Set<string>): StockMatch | null {
  const s = norm(symbol);
  const n = name.toLowerCase();
  if (!s || s.length < 2) return null;

  for (const t of knownTickers) {
    if (t.length < 3) continue;
    if (s === t) return { ticker: t, matchType: "exact" };
  }
  for (const t of knownTickers) {
    if (t.length < 3) continue;
    if (s.startsWith(t) && s.length - t.length <= 3) return { ticker: t, matchType: "startsWith" };
  }
  for (const t of knownTickers) {
    if (t.length >= 5 && s.includes(t)) return { ticker: t, matchType: "contains" };
  }
  for (const [t, kws] of Object.entries(COMPANY_KEYWORDS)) {
    if (kws.some((k) => n.includes(k))) return { ticker: t, matchType: "company" };
  }
  return null;
}

export function buildTickerSet(underlyingSymbols: string[]): Set<string> {
  const set = new Set<string>(EXTRA_TICKERS);
  for (const u of underlyingSymbols) {
    const n = norm(u);
    if (n.length >= 2 && n.length <= 8 && /^[A-Z0-9]+$/.test(n)) set.add(n);
  }
  return set;
}
