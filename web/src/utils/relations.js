import { assetKey } from './realtime.js';

export function relationMatchesAsset(relation, asset) {
  return assetKey(relation?.chainId ?? relation?.chain ?? relation?.chainIndex, relation?.token)
    === assetKey(asset?.chainId ?? asset?.chain ?? asset?.chainIndex, asset?.token);
}

export function relationMatchesStock(relation, stock) {
  return assetKey(relation?.chainId ?? relation?.chain ?? relation?.chainIndex, relation?.stock)
    === assetKey(stock?.chainId ?? stock?.chain ?? stock?.chainIndex, stock?.tokenContractAddress ?? stock?.instrumentId);
}
