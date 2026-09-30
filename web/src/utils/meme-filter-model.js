import { hasBasicMarketData, isQualifiedMeme, isRecentObservation } from './stock-theme-model.js';

export const marketCatalogReady = snapshot => !!snapshot?.unified && snapshot.unified.snapshotScope !== 'overview';

// Entry links provide defaults. Explicit choices still work while the user
// keeps the discovery window or volume-ranking condition selected.
export function memeFilterValues(query = {}) {
  const qualified=query.qualified==='1',newAssets=query.new==='24h',ranking=query.rank==='volume24h';
  const legacy=String(query.filter??'');
  return {
    qualified,newAssets,ranking,
    showMissing:query.showMissing!=null ? query.showMissing==='1' : newAssets||ranking,
    relation:qualified?'all':String(query.rel??(newAssets||ranking?'all':({all:'all',name:'B',history:'all',verified:'A',related:'A'}[legacy]??'A'))),
    fresh:qualified?'1':String(query.fresh??(newAssets||ranking||['all','verified','name','history'].includes(legacy)?'0':'1')),
    minLiquidity:qualified?'1000':String(query.minLiq??query.minLiquidity??'0'),
    hideRisk:query.risk==='hide',
  };
}
export function memeQuoteMatches(asset,filters,now=Date.now()) {
  if(filters.qualified)return isQualifiedMeme(asset,now);
  // Showing incomplete volume/liquidity never disables an explicit price filter.
  if(filters.fresh==='1' && !(asset.price!=null && Number.isFinite(Number(asset.price)) && Number(asset.price)>0
      && isRecentObservation(asset.fieldTimes?.price??asset.quoteAt,900_000,now)))return false;
  return filters.showMissing||hasBasicMarketData(asset);
}
