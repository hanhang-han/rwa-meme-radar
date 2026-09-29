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
 if(exchanges.length)return{kind:'exchange',exchange:exchanges[0],id:`${exchanges[0].venue}:${exchanges[0].marketId}`};
 if(base.priceScope!=='exchange'){
  const pool=pools.filter(p=>poolId(p)&&Number(p.lastTradeAt)>0).sort((a,b)=>(Number(b.liquidityUsd)||0)-(Number(a.liquidityUsd)||0)||(Number(b.lastTradeAt)||0)-(Number(a.lastTradeAt)||0))[0];
  if(pool)return{kind:'pool',pool,id:`pool:${poolId(pool)}`};
 }
 return{kind:'base',id:base.priceScope==='exchange'?'base':'dex'};
}
