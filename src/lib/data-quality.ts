// Product coverage is measured from saved observations, never from catalogue size alone.
const known=(v:unknown)=>typeof v==='number'&&Number.isFinite(v);
export function fieldCoverage(rows:any[],field:string,time:(row:any)=>number|null|undefined,now=Date.now()){
  const available=rows.filter(r=>known(r[field]));
  const fresh=available.filter(r=>{const at=time(r);return !!at&&at<=now+60000&&now-at<=900000;}).length;
  return {total:rows.length,available:available.length,fresh,stale:available.length-fresh,missing:rows.length-available.length,zero:available.filter(r=>r[field]===0).length};
}
export function dataQuality(assets:any[],stocks:any[],relations:any[],now=Date.now()){
  const byChain=[...new Set([...assets,...stocks].map(a=>String(a.chainId??'exchange')))].map(chainId=>{
    const a=assets.filter(a=>String(a.chainId??'exchange')===chainId),s=stocks.filter(s=>String(s.chainId??'exchange')===chainId);
    return {chainId,candidates:a.length,deployments:s.length,
      fields:Object.fromEntries(['price','volume24h','txs24h','buys24h','sells24h','holders'].map(f=>[f,fieldCoverage(a,f,r=>r.fieldTimes?.[f],now)])),
      stockPrices:fieldCoverage(s,'price',r=>r.fieldTimes?.price??r.updatedAt,now),
      recentTradeSync:a.filter(r=>r.tradeAt&&now-r.tradeAt<=900000).length};
  });
  const verified=relations.filter(r=>r.status==='verified');
  const pools=[...new Map(verified.map(r=>[r.chainId+':'+r.pool,r])).values()];
  return {asOf:now,freshnessMs:900000,byChain,candidates:assets.length,deployments:stocks.length,
    stockGroups:new Set(stocks.map(s=>s.stockIdentity?.id??s.stockCode??s.assetId)).size,
    unresolvedNumeric:stocks.filter(s=>/^\d+$/.test(s.stockCode)&&s.stockIdentity?.status!=='identified').length,
    verifiedPools:pools.length,recentProofs:pools.filter(r=>now-r.checkedAt<=3600000).length,
    poolLiquidity:fieldCoverage(pools,'liquidityUsd',r=>r.liquidityAt,now),
    stockReferences:fieldCoverage(stocks,'stockPrice',r=>r.referenceAt,now)};
}
