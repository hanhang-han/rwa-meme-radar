import { isRecentObservation } from './stock-theme-model.js';
import { volumeLiquidityRatio } from './product-labels.js';
const finite=value=>value!=null&&value!==''&&Number.isFinite(Number(value));
export function currentBuyRatio(asset, now = Date.now()) {
  const published=asset?.productMetrics?.buyShare24h;
  if(published)return published.status==='current'&&finite(published.value)&&Number(published.value)>=0&&Number(published.value)<=100&&isRecentObservation(published.at,900000,now)?Number(published.value)/100:null;
  const metric=asset?.buyRatio,ratio=metric?.value??metric;
  if(finite(ratio)&&Number(ratio)>=0&&Number(ratio)<=1&&isRecentObservation(metric?.at??asset.fieldTimes?.buyRatio,900000,now))return Number(ratio);
  const buys=asset?.buys24h??asset?.buyTransactions,sells=asset?.sells24h??asset?.sellTransactions;
  const buyAt=asset?.fieldTimes?.buys24h??asset?.fieldTimes?.buyTransactions;
  const sellAt=asset?.fieldTimes?.sells24h??asset?.fieldTimes?.sellTransactions;
  if(!finite(buys)||!finite(sells)||Number(buys)<0||Number(sells)<0||!isRecentObservation(buyAt,900000,now)||!isRecentObservation(sellAt,900000,now)||Math.abs(Number(buyAt)-Number(sellAt))>60000)return null;
  const total=Number(buys)+Number(sells);
  return total>0?Number(buys)/total:null;
}

export function currentVolumeLiquidityRatio(asset, now = Date.now()) {
  const published=asset?.productMetrics?.volumeLiquidityRatio;
  if(published)return published.status==='current'&&finite(published.value)&&Number(published.value)>=0&&isRecentObservation(published.at,900000,now)?Number(published.value):null;
  return volumeLiquidityRatio(asset);
}

export function memeChangeObservation(asset, field) {
  const windows={change5m:'m5',change1h:'h1',change6h:'h6',change24h:'h24'};
  const published=asset?.productMetrics?.changes?.[windows[field]];
  return published??{value:asset?.[field],at:asset?.fieldTimes?.[field],source:asset?.fieldSources?.[field]};
}
