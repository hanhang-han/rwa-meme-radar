import { readSnapshot, writeSnapshot } from './snapshot';
import { numeric, type RwaToken } from './okx';

const API = 'https://api.robinhood.com/rhj';
const CHAIN = '4663';
const addr = (value: unknown) => typeof value === 'string' && /^0x[\da-f]{40}$/i.test(value) ? value : null;

export interface RobinhoodToken extends RwaToken {
  sourceAssetId: string;
  currentMultiplier: number | null;
  assetStatus: string;
  quoteAt: number | null;
  dailyTradingVolume: number | null;
  isTradingHalt: boolean | null;
}

export const robinhoodState: { status: string; updatedAt: number | null; tokens: RobinhoodToken[]; error: string | null } = {
  status: 'starting', updatedAt: null, tokens: [], error: null,
};

let restored = false;
async function get(path: string) {
  const response = await fetch(API + path, { headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(15_000) });
  if (!response.ok) throw new Error(`Robinhood HTTP ${response.status}`);
  return response.json();
}

/**
 * Convert Robinhood's catalogue and quote payloads into the dashboard's stock
 * token shape.  `dailyTradingVolume` is equity-market volume, not on-chain
 * token turnover, so it is retained separately and never assigned to
 * `volume24h`.
 */
export function normalizeRobinhoodSnapshot(assetBody: any, priceBody: any): RobinhoodToken[] {
  const assets = Array.isArray(assetBody?.assets) ? assetBody.assets : [];
  const quotes = Array.isArray(priceBody?.quotes) ? priceBody.quotes : [];
  if (!assets.length || !quotes.length) throw new Error('Robinhood returned an empty catalogue or price snapshot');
  const quoteBySymbol = new Map<string, any>(quotes.map((quote: any) => [String(quote.tokenSymbol).toUpperCase(), quote]));
  const tokens: RobinhoodToken[] = [];
  for (const asset of assets) {
    const quote = quoteBySymbol.get(String(asset.tokenSymbol).toUpperCase());
    const multiplier = numeric(asset.currentMultiplier);
    const rawBid = numeric(quote?.bid), rawAsk = numeric(quote?.ask);
    const stockPrice = rawBid != null && rawAsk != null ? (rawBid + rawAsk) / 2 : null;
    // Robinhood quotes are underlying-equity prices; multiply before using
    // them as a Stock Token price, preserving null when either input is absent.
    const tokenPrice = stockPrice != null && multiplier != null ? stockPrice * multiplier : null;
    for (const deployment of Array.isArray(asset.deployments) ? asset.deployments : []) {
      const contractAddress = addr(deployment.contractAddress);
      if (!contractAddress || String(deployment.chainId) !== CHAIN) continue;
      tokens.push({
        chainIndex: CHAIN, tokenContractAddress: contractAddress,
        tokenSymbol: String(asset.tokenSymbol ?? ''), tokenName: String(asset.tokenName ?? ''),
        stockCode: String(asset.tokenSymbol ?? ''), issuer: 'Robinhood Assets (Jersey) Limited',
        price: tokenPrice, stockPrice, volume24h: null, marketCap: null,
        tokenToAssetRatio: multiplier, sourceAssetId: String(asset.id ?? ''), currentMultiplier: multiplier,
        assetStatus: String(asset.status ?? ''), quoteAt: quote?.generatedAt ? Date.parse(quote.generatedAt) : null,
        dailyTradingVolume: numeric(quote?.dailyTradingVolume), isTradingHalt: typeof quote?.isTradingHalt === 'boolean' ? quote.isTradingHalt : null,
      });
    }
  }
  if (!tokens.length) throw new Error('Robinhood returned no mainnet Stock Token deployments');
  return [...new Map(tokens.map(token => [token.tokenContractAddress.toLowerCase(), token])).values()];
}

/** Read-only Robinhood Stock Tokens catalogue and all-price snapshot. */
export async function refreshRobinhood() {
  if (!restored) {
    restored = true;
    const saved = process.env.NODE_ENV === 'test' ? null : readSnapshot<typeof robinhoodState>('data/robinhood.json');
    if (saved?.updatedAt && Array.isArray(saved.tokens)) Object.assign(robinhoodState, saved, { status: 'stale' });
  }
  try {
    const [assetBody, priceBody] = await Promise.all([get('/assets'), get('/prices')]);
    robinhoodState.tokens = normalizeRobinhoodSnapshot(assetBody, priceBody);
    robinhoodState.updatedAt = Date.now(); robinhoodState.status = 'ready'; robinhoodState.error = null;
    if (process.env.NODE_ENV !== 'test') writeSnapshot('data/robinhood.json', robinhoodState);
  } catch (error) {
    robinhoodState.status = robinhoodState.updatedAt ? 'stale' : 'error';
    robinhoodState.error = error instanceof Error ? error.message : 'Robinhood unavailable';
  }
}

export function robinhoodToken(address: string) {
  return robinhoodState.tokens.find(token => token.tokenContractAddress.toLowerCase() === address.toLowerCase()) ?? null;
}
