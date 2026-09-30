// A native pool selects chart history only; it never replaces the asset's USD
// quote. An explicit user choice always wins over newly discovered markets.
export function selectDetailMarket(base,exchanges,pools,choice=null){
 const poolId=p=>p.poolId??p.pool??p.marketId;
 if(choice?.startsWith('pool:')){
  const pool=pools.find(p=>String(poolId(p)).toLowerCase()===choice.slice(5).toLowerCase());
  return pool?{kind:'pool',pool,id:`pool:${poolId(pool)}`}:{kind:'base',id:'dex'};
 }
 if(choice!=null){
  const exchange=exchanges.find(m=>`${m.venue}:${m.marketId}`===choice);
  return exchange?{kind:'exchange',exchange,id:choice}:{kind:'base',id:base.priceScope==='exchange'?'base':'dex'};
 }
 // New markets must not change the initial chart's currency or venue.
 return{kind:'base',id:base.priceScope==='exchange'?'base':'dex'};
}
