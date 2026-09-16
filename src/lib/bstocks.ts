export interface BStockDef {
  symbol: string;
  name: string;
  underlying: string;
  addr: string;
}

const raw: [string, string, string][] = [
  ["NVDAB", "NVIDIA", "0x02Fca66C1D1aFB4E2A7884261eB00F63598a7436"],
  ["SPCXB", "SpaceX", "0xbe9D156892E55e7154BcD3cB0FEA677F9D3103E1"],
  ["QQQB", "Invesco QQQ", "0x205812CdBed920aFf76C6580abD681a46D11efc7"],
  ["SKHYB", "SK Hynix", "0xCA750eF65f295BBECd685Abf54e82CAf297BDB61"],
  ["BABAB", "Alibaba", "0x4eF9d3062c7F6ebA4AAE4990c5036598C6eff4ec"],
  ["INTCB", "Intel", "0xe614E2fc6C787035FF51f452e8E826Bfd32D5283"],
  ["MSTRB", "Strategy Inc.", "0xE87afb3076AeB0f9B14E368DE8145ae6a2826A14"],
  ["AAPLB", "Apple", "0x431a3BEE82E2ca41e49895CbECE5bB0F76A89b7A"],
  ["TSLAB", "Tesla", "0x5b1910eAaD6450E50f816082Aa078C41F10C292f"],
  ["SPYB", "SPY", "0x7138b48df7D98D7e3cc221BfE7192D0a178182D8"],
  ["GMEB", "GameStop", "0x46cEeFDa28Dd7207059ed19B0acdc026955bb15C"],
  ["HOODB", "Robinhood", "0xA394dCEa3fd3847fD793afBFd163E2e3858B7c65"],
  ["DJTB", "Trump Media & Technology Group", "0xF2ec508422174Ee564de98187db9359D318AFB6b"],
  ["MRNAB", "Moderna", "0x5fd86da9B05abE396fe9d02a4A213A7c00556503"],
  ["SOXLB", "Semicon Bull 3X ETF", "0xd97d097a89113fa59b76c572E5b2Eb647E8eefaf"],
  ["SNDKB", "Sandisk", "0x3eE4dF61bd4F867E349BEaE8bFE07bc31b4850fb"],
  ["CRCLB", "Circle Internet Group", "0x80f3D493EBCe97e343c53D29a137942416B4ffC0"],
  ["NFLXB", "Netflix", "0xD6829Ea836b6FA224d099D40E54B31262f874631"],
  ["MSFTB", "Microsoft", "0x80106cb3EAD06659A5ad19DF39D9b4733863B9b0"],
  ["SQQQB", "ProShares UltraPro Short QQQ", "0x25e572B466D152604D9E6C3e53B432B978825342"],
  ["GOOGLB", "Alphabet", "0x3F53De71c126BdaBAe20f9cD64848d317f6C3238"],
  ["SOXSB", "Direxion Semiconductor Bear 3X", "0xE28Cd11C99AF2df76bb8aDA4Cd0ef3904378280F"],
  ["TQQQB", "ProShares UltraPro QQQ", "0x462B5F13B7C7748279358962925c5De83BB9E598"],
  ["METAB", "Meta Platforms", "0x7425889FE94F9d693E8daefE88BCCed6AcFEf4c0"],
  ["TSMB", "TSMC", "0xAB78b89B5bb00236Be0B4B20704cBfa04EfC711c"],
  ["NOKB", "Nokia", "0x7c4d7a180D737Dd5A70d8065a90E6746a69C37EA"],
  ["BMNRB", "BitMine Immersion", "0x3548Da95a9eFFE481e8604664d75e95821e557F5"],
  ["SNXXB", "Tradr 2X Long SNDK ETF", "0x9e82e3da8f1115B73d24bB24113ab836FfDAb6b6"],
  ["DRAMB", "Roundhill Memory ETF", "0x93862d63fd9Fd488B1328E9b47717d75e994a84B"],
];

export const BSTOCKS: BStockDef[] = raw.map(([symbol, name, addr]) => ({
  symbol,
  name: `${name} bStock`,
  underlying: symbol.endsWith("B") ? symbol.slice(0, -1) : symbol,
  addr,
}));

const UA = { "User-Agent": "curl/8.7.1", Accept: "application/json" };

export interface BinanceQuote {
  price: number;
  chg24h: number | null;
  volume24h: number | null;
  trades24h: number | null;
}

export async function fetchBinanceQuotes(symbols: string[]): Promise<Map<string, BinanceQuote>> {
  const out = new Map<string, BinanceQuote>();
  const pairs = symbols.map((s) => `${s}USDT`);
  const url = `https://api.binance.com/api/v3/ticker/24hr?symbols=${encodeURIComponent(JSON.stringify(pairs))}`;
  const res = await fetch(url, { headers: UA });
  if (!res.ok) throw new Error(`binance HTTP ${res.status}`);
  const arr = (await res.json()) as any[];
  for (const t of arr) {
    const base = String(t.symbol).replace(/USDT$/, "");
    out.set(base, {
      price: Number(t.lastPrice),
      chg24h: t.priceChangePercent != null ? Number(t.priceChangePercent) : null,
      volume24h: t.quoteVolume != null && Number.isFinite(Number(t.quoteVolume)) ? Number(t.quoteVolume) : null,
      trades24h: t.count != null && Number.isFinite(Number(t.count)) ? Number(t.count) : null,
    });
  }
  return out;
}

export interface BinanceBStock {
  chainIndex: string | null;
  tokenContractAddress: string | null;
  instrumentId: string;
  tokenSymbol: string;
  tokenName: string;
  stockCode: string;
  issuer: string;
  price: number | null;
  stockPrice: number | null;
  volume24h: number | null;
  marketCap: number | null;
  change24h: number | null;
  tokenToAssetRatio: number | null;
  quoteAt: number | null;
  exchangeTrades24h: number | null;
  volumeScope: 'exchange';
  onchain: boolean;
  multiplierValid: boolean | null;
}

export const binanceState: { status: string; updatedAt: number | null; tokens: BinanceBStock[]; error: string | null } = {
  status: 'starting', updatedAt: null, tokens: [], error: null,
};
let restored = false;

export type BinanceTokenizedAsset = { assetCode: string; assetName: string; underlyingEquitySymbol: string; multiplier: string | number | null; multiplierValid: boolean | null };

export function normalizeBinanceBstocks(assets: BinanceTokenizedAsset[], quotes: Map<string, BinanceQuote>, at = Date.now()): BinanceBStock[] {
  if (!assets.length) throw new Error('Binance returned no tokenized assets');
  const known = new Map(BSTOCKS.map(token => [token.symbol, token]));
  return assets.map(asset => {
    const code = String(asset.assetCode ?? '').toUpperCase();
    const contract = known.get(code);
    const quote = quotes.get(code);
    return {
      chainIndex: contract ? '56' : null, tokenContractAddress: contract?.addr ?? null,
      instrumentId: `binance:${code}`, tokenSymbol: code, tokenName: String(asset.assetName ?? contract?.name ?? code),
      stockCode: String(asset.underlyingEquitySymbol ?? contract?.underlying ?? code), issuer: 'BTech Holdings Limited',
      price: quote?.price ?? null, stockPrice: null,
      // This is Binance Spot turnover, not DEX turnover. It remains marked
      // as exchange scope so consumers cannot treat it as pool activity.
      volume24h: quote?.volume24h ?? null, marketCap: null, change24h: quote?.chg24h ?? null,
      tokenToAssetRatio: asset.multiplier == null || asset.multiplier === '' || !Number.isFinite(Number(asset.multiplier)) ? null : Number(asset.multiplier),
      quoteAt: quote ? at : null, exchangeTrades24h: quote?.trades24h ?? null,
      volumeScope: 'exchange' as const, onchain: !!contract, multiplierValid: asset.multiplierValid ?? null,
    };
  });
}

async function tokenizedAssets(): Promise<BinanceTokenizedAsset[] | null> {
  const key = process.env.BINANCE_API_KEY;
  if (!key) return null;
  const response = await fetch('https://api.binance.com/sapi/v1/equity/market/tokenized-assets', {
    headers: { 'X-MBX-APIKEY': key, Accept: 'application/json' }, signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`Binance tokenized-assets HTTP ${response.status}`);
  const body = await response.json();
  if (!Array.isArray(body)) throw new Error('Binance tokenized-assets response is invalid');
  return body.map(asset => ({
    assetCode: String(asset.assetCode ?? ''), assetName: String(asset.assetName ?? ''),
    underlyingEquitySymbol: String(asset.underlyingEquitySymbol ?? ''), multiplier: asset.multiplier ?? null,
    multiplierValid: typeof asset.multiplierValid === 'boolean' ? asset.multiplierValid : null,
  })).filter(asset => asset.assetCode && asset.underlyingEquitySymbol);
}

/** Binance Spot bStocks mapped into the common stock-token catalogue. */
export async function refreshBinanceBstocks() {
  if (!restored) {
    restored = true;
    const saved = process.env.NODE_ENV === 'test' ? null : readSnapshot<typeof binanceState>('data/binance.json');
    if (saved?.updatedAt && Array.isArray(saved.tokens)) Object.assign(binanceState, saved, { status: 'stale' });
  }
  try {
    const catalogue = await tokenizedAssets();
    // The public Spot endpoint supplies market data for known deployable
    // bStocks. Catalogue-only records remain valid assets but never receive a
    // fabricated contract address or on-chain turnover.
    const assets = catalogue?.length ? catalogue : BSTOCKS.map(b => ({ assetCode: b.symbol, assetName: b.name, underlyingEquitySymbol: b.underlying, multiplier: 1, multiplierValid: true }));
    const quotes = await fetchBinanceQuotes(BSTOCKS.map(b => b.symbol));
    const at = Date.now();
    binanceState.tokens = normalizeBinanceBstocks(assets, quotes, at);
    binanceState.updatedAt = at; binanceState.status = 'ready'; binanceState.error = null;
    if (process.env.NODE_ENV !== 'test') writeSnapshot('data/binance.json', binanceState);
  } catch (error) {
    binanceState.status = binanceState.updatedAt ? 'stale' : 'error';
    binanceState.error = error instanceof Error ? error.message : 'Binance unavailable';
  }
}

export function binanceBstockToken(address: string): (BinanceBStock & { tokenContractAddress: string; chainIndex: string }) | null {
  const token = binanceState.tokens.find(token => token.tokenContractAddress?.toLowerCase() === address.toLowerCase());
  return token?.tokenContractAddress && token.chainIndex ? token as BinanceBStock & { tokenContractAddress: string; chainIndex: string } : null;
}
import { readSnapshot, writeSnapshot } from './snapshot';
