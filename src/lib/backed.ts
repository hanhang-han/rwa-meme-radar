const BASE = "https://api.backed.fi/api/v2/public";

export interface BackedDeployment {
  address: string;
  network: string;
  supportsAtomicSwaps: boolean;
  stablecoins: { symbol: string; address: string; decimals: number }[];
}

export interface BackedAsset {
  id: string;
  name: string;
  symbol: string;
  underlyingSymbol: string;
  isTradingHalted: boolean;
  trading: { currency: string; currentPeriod: string; openNow: boolean; tradingHoursMode: string };
  deployments: BackedDeployment[];
}

export async function fetchAssets(maxPages = 5): Promise<BackedAsset[]> {
  const out: BackedAsset[] = [];
  let page = 0;
  for (; page < maxPages; page++) {
    const res = await fetch(`${BASE}/assets?page=${page}`, { headers: { "User-Agent": "curl/8.7.1", Accept: "application/json" } });
    if (!res.ok) throw new Error(`assets HTTP ${res.status}`);
    const json = await res.json();
    const nodes: BackedAsset[] = json.nodes ?? json.data?.nodes ?? [];
    out.push(...nodes);
    if (!json.page?.hasNextPage) break;
  }
  return out;
}

export interface PriceData {
  quote?: number;
}

export async function fetchOraclePrice(symbol: string): Promise<number | null> {
  const res = await fetch(`${BASE}/assets/${symbol}/price-data`, {
    headers: { "User-Agent": "curl/8.7.1", Accept: "application/json" },
  });
  if (!res.ok) return null;
  const json: PriceData = await res.json();
  return json.quote ?? null;
}
