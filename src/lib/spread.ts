export interface SpreadResult {
  symbol: string;
  dexPrice: number;
  oraclePrice: number;
  spreadPct: number;
  poolKind: string;
  quote: string;
}

export function computeSpread(symbol: string, dexPrice: number, oraclePrice: number, poolKind: string, quote: string): SpreadResult {
  return {
    symbol,
    dexPrice,
    oraclePrice,
    spreadPct: (dexPrice / oraclePrice - 1) * 100,
    poolKind,
    quote,
  };
}
