import type { RwaToken } from './okx';
import type { RobinhoodToken } from './robinhood';
import type { BinanceBStock } from './bstocks';
import type { XAsset, XRelation } from './xlayer';

/**
 * The dashboard's source-neutral contract. Providers may use different APIs,
 * but every asset and relationship is exposed with the same identity fields:
 * provider, chain, contract address, observation time and evidence status.
 */
export interface DataSource {
  id: string;
  provider: string;
  chainId: string;
  chainName: string;
  status: string;
  updatedAt: number | null;
}

export type XLayerSnapshot = {
  status: string;
  updatedAt: number | null;
  coverage: unknown;
  assets: XAsset[];
  relations: XRelation[];
  signals: any[];
  groups: any[];
  sectors: any[];
  metrics: any;
  [key: string]: any;
};

/**
 * Normalise one provider snapshot without losing source-specific raw fields.
 * Binance and Robinhood adapters will add records through this same shape;
 * the UI must never infer a chain or provider from a ticker or token name.
 */
export function unifiedFromXLayer(snapshot: XLayerSnapshot, okx: { status: string; updatedAt: number | null; tokens: RwaToken[] }, robinhood?: { status: string; updatedAt: number | null; tokens: RobinhoodToken[] }, binance?: { status: string; updatedAt: number | null; tokens: BinanceBStock[] }) {
  const okxSource: DataSource = {
    id: 'okx:xlayer:196', provider: 'OKX', chainId: '196', chainName: 'X Layer',
    status: snapshot.status === 'ready' ? okx.status : snapshot.status,
    updatedAt: snapshot.updatedAt ?? okx.updatedAt,
  };
  const robinhoodSource: DataSource | null = robinhood ? {
    id: 'robinhood:chain:4663', provider: 'Robinhood', chainId: '4663', chainName: 'Robinhood Chain',
    status: robinhood.status, updatedAt: robinhood.updatedAt,
  } : null;
  const binanceSource: DataSource | null = binance ? {
    id: 'binance:bstocks:56', provider: 'Binance', chainId: '56', chainName: 'BNB Smart Chain',
    status: binance.status, updatedAt: binance.updatedAt,
  } : null;
  const withSource = <T extends Record<string, any>>(row: T) => ({
    ...row, sourceId: okxSource.id, provider: okxSource.provider,
    chainId: String(row.chain ?? okxSource.chainId), chainName: okxSource.chainName,
  });
  const robinhoodStock = (token: RobinhoodToken) => ({
    ...token, sourceId: robinhoodSource!.id, provider: robinhoodSource!.provider,
    chainId: token.chainIndex, chainName: robinhoodSource!.chainName,
  });
  const binanceStock = (token: BinanceBStock) => ({
    ...token, sourceId: binanceSource!.id, provider: binanceSource!.provider,
    chainId: token.chainIndex, chainName: token.chainIndex ? binanceSource!.chainName : null,
  });
  return {
    ...snapshot,
    sources: [okxSource, ...(robinhoodSource ? [robinhoodSource] : []), ...(binanceSource ? [binanceSource] : [])],
    assets: snapshot.assets.map(withSource),
    relations: snapshot.relations.map(withSource),
    signals: snapshot.signals.map(withSource),
    stockTokens: [
      ...okx.tokens.map(token => ({
        ...token, sourceId: okxSource.id, provider: okxSource.provider,
        chainId: String(token.chainIndex), chainName: okxSource.chainName,
      })),
      ...(robinhood?.tokens ?? []).map(robinhoodStock),
      ...(binance?.tokens ?? []).map(binanceStock),
    ],
  };
}
