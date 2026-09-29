import { readSnapshot, writeSnapshot } from './snapshot';
import { numeric, type RwaToken } from './okx';

const API = 'https://api.robinhood.com/rhj';
const CHAIN = '4663';
const RATIO_TTL_MS = 10 * 60_000;
const RATIO_SOURCE = API + '/assets';
const addr = (value: unknown) => typeof value === 'string' && /^0x[\da-f]{40}$/i.test(value) ? value : null;

export interface RobinhoodToken extends RwaToken {
  sourceAssetId: string;
  ratioSource: string;
  ratioAt: number | null;
  ratioTimeKind: 'observed';
  ratioVersion: string | null;
  ratioValidUntil: number | null;
  ratioVerified: boolean;
  ratioReason: string | null;
  ratioEvidence: {sourceAssetId:string;chainId:string;tokenContractAddress:string;stockCode:string;currentMultiplier:number;observedAt:number} | null;
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
const CATALOGUE_INTERVAL_MS = 300_000;
let catalogueCache: { body: any; receivedAt: number } | null = null;
let refreshing: Promise<void> | null = null;
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
export function normalizeRobinhoodSnapshot(assetBody: any, priceBody: any, catalogueReceivedAt=Date.now()): RobinhoodToken[] {
  const assets = Array.isArray(assetBody?.assets) ? assetBody.assets : [];
  const quotes = Array.isArray(priceBody?.quotes) ? priceBody.quotes : [];
  if (!assets.length || !quotes.length) throw new Error('Robinhood returned an empty catalogue or price snapshot');
  const quoteBySymbol = new Map<string, any>(quotes.map((quote: any) => [String(quote.tokenSymbol).toUpperCase(), quote]));
  const tokens: RobinhoodToken[] = [];
  for (const asset of assets) {
    const quote = quoteBySymbol.get(String(asset.tokenSymbol).toUpperCase());
    const rawMultiplier = numeric(asset.currentMultiplier);
    const multiplier = rawMultiplier != null && rawMultiplier > 0 ? rawMultiplier : null;
    const sourceAssetId = typeof asset.id === 'string' ? asset.id.trim() : '';
    const stockCode = typeof asset.tokenSymbol === 'string' ? asset.tokenSymbol.trim().toUpperCase() : '';
    const rawBid = numeric(quote?.bid), rawAsk = numeric(quote?.ask);
    const stockPrice = rawBid != null && rawAsk != null ? (rawBid + rawAsk) / 2 : null;
    // Robinhood quotes are underlying-equity prices; multiply before using
    // them as a Stock Token price, preserving null when either input is absent.
    const tokenPrice = stockPrice != null && multiplier != null ? stockPrice * multiplier : null;
    for (const deployment of Array.isArray(asset.deployments) ? asset.deployments : []) {
      const contractAddress = addr(deployment.contractAddress);
      if (!contractAddress || /^0x0{40}$/i.test(contractAddress) || String(deployment.chainId) !== CHAIN) continue;
      // Evidence comes from the issuer's explicit deployment + multiplier,
      // never from dividing token and equity prices. Its time is receipt of
      // this catalogue response, not the older/newer equity quote timestamp.
      const verified = !!sourceAssetId && !!stockCode && multiplier != null && Number.isFinite(catalogueReceivedAt) && catalogueReceivedAt > 0;
      const ratioEvidence = verified ? {sourceAssetId,chainId:CHAIN,tokenContractAddress:contractAddress.toLowerCase(),stockCode,currentMultiplier:multiplier!,observedAt:catalogueReceivedAt} : null;
      tokens.push({
        chainIndex: CHAIN, tokenContractAddress: contractAddress,
        tokenSymbol: String(asset.tokenSymbol ?? ''), tokenName: String(asset.tokenName ?? ''),
        stockCode: String(asset.tokenSymbol ?? ''), issuer: 'Robinhood Assets (Jersey) Limited',
        price: tokenPrice, stockPrice, volume24h: null, marketCap: null,
        tokenToAssetRatio: multiplier, sourceAssetId, currentMultiplier: multiplier,
        ratioSource:RATIO_SOURCE,ratioAt:verified?catalogueReceivedAt:null,ratioTimeKind:'observed',
        ratioVersion:verified?`rh:${sourceAssetId}:${CHAIN}:${contractAddress.toLowerCase()}:${multiplier}`:null,
        ratioValidUntil:verified?catalogueReceivedAt+RATIO_TTL_MS:null,ratioVerified:verified,
        ratioReason:verified?null:'issuer-ratio-evidence-incomplete',ratioEvidence,
        assetStatus: String(asset.status ?? ''), quoteAt: quote?.generatedAt ? Date.parse(quote.generatedAt) : null,
        dailyTradingVolume: numeric(quote?.dailyTradingVolume), isTradingHalt: typeof quote?.isTradingHalt === 'boolean' ? quote.isTradingHalt : null,
      });
    }
  }
  if (!tokens.length) throw new Error('Robinhood returned no mainnet Stock Token deployments');
  const byContract = new Map<string,RobinhoodToken>();
  for(const token of tokens){const key=token.tokenContractAddress.toLowerCase(),old=byContract.get(key);
    if(old&&(old.sourceAssetId!==token.sourceAssetId||old.currentMultiplier!==token.currentMultiplier||old.stockCode!==token.stockCode)){
      token.ratioVerified=false;token.ratioVersion=null;token.ratioValidUntil=null;token.ratioEvidence=null;token.ratioReason='conflicting-issuer-deployments';
    }else if(old?.ratioReason==='conflicting-issuer-deployments'){
      token.ratioVerified=false;token.ratioVersion=null;token.ratioValidUntil=null;token.ratioEvidence=null;token.ratioReason=old.ratioReason;
    }
    byContract.set(key,token);
  }
  return [...byContract.values()];
}

/** Read-only Robinhood Stock Tokens catalogue and all-price snapshot. */
export async function refreshRobinhood() {
  if (refreshing) return refreshing;
  refreshing = refreshRobinhoodOnce().finally(() => { refreshing = null; });
  return refreshing;
}

// A price refresh must not extend the lifetime of an older issuer multiplier.
export function robinhoodCatalogueDue(receivedAt: number | null, now = Date.now()) {
  return receivedAt == null || now < receivedAt || now - receivedAt >= CATALOGUE_INTERVAL_MS;
}

async function refreshRobinhoodOnce() {
  if (!restored) {
    restored = true;
    const saved = process.env.NODE_ENV === 'test' ? null : readSnapshot<typeof robinhoodState>('data/robinhood.json');
    if (saved?.updatedAt && Array.isArray(saved.tokens)) Object.assign(robinhoodState, saved, { status: 'stale' });
  }
  try {
    const catalogueDue = robinhoodCatalogueDue(catalogueCache?.receivedAt ?? null);
    const [catalogueResult, pricesResult] = await Promise.allSettled([
      catalogueDue ? get('/assets').then(body => ({ body, receivedAt: Date.now() })) : Promise.resolve(catalogueCache!),
      get('/prices'),
    ]);
    if (catalogueResult.status === 'fulfilled') {
      if (!Array.isArray(catalogueResult.value.body?.assets) || !catalogueResult.value.body.assets.length) throw new Error('Robinhood empty catalogue');
      catalogueCache = catalogueResult.value;
    }
    if (pricesResult.status === 'rejected') throw pricesResult.reason;
    if (!catalogueCache) throw new Error('Robinhood catalogue unavailable');
    robinhoodState.tokens = normalizeRobinhoodSnapshot(catalogueCache.body, pricesResult.value, catalogueCache.receivedAt);
    robinhoodState.updatedAt = Date.now();
    robinhoodState.status = catalogueResult.status === 'rejected' ? 'partial' : 'ready';
    robinhoodState.error = catalogueResult.status === 'rejected' ? 'Issuer catalogue refresh delayed' : null;
  } catch (error) {
    robinhoodState.status = robinhoodState.updatedAt ? 'stale' : 'error';
    robinhoodState.error = error instanceof Error ? error.message : 'Robinhood unavailable';
  } finally {
    // Failures also reach the shared projection. Old timestamps are retained.
    if (process.env.NODE_ENV !== 'test') writeSnapshot('data/robinhood.json', robinhoodState);
  }
}

export function robinhoodToken(address: string) {
  return robinhoodState.tokens.find(token => token.tokenContractAddress.toLowerCase() === address.toLowerCase()) ?? null;
}
