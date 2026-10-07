// A native pool selects chart history only; it never replaces the asset's USD
// quote. An explicit user choice always wins over newly discovered markets.
export function selectDetailMarket(base,exchanges,pools,choice=null,now=Date.now()){
 const poolId=p=>p.poolId??p.pool??p.marketId;
 if(choice?.startsWith('pool:')){
  const pool=pools.find(p=>String(poolId(p)).toLowerCase()===choice.slice(5).toLowerCase());
  return pool?{kind:'pool',pool,id:`pool:${poolId(pool)}`}:{kind:'base',id:'dex'};
 }
 if(choice!=null){
  const exchange=exchanges.find(m=>`${m.venue}:${m.marketId}`===choice);
  return exchange?{kind:'exchange',exchange,id:choice}:{kind:'base',id:base.priceScope==='exchange'?'base':'dex'};
 }
 // A measured, current dedicated feed can supply the initial chart. The
 // headline quote remains in its own market; the view explains both units.
 const live=pools.filter(p=>p.liveMarket===true && !p.stale && p.status==='current'
  && ['live','quiet'].includes(p.marketStatus) && p.venue==='dex' && p.quoteType==='pool'
  && /^0x[0-9a-f]{40}$/i.test(String(poolId(p))) && p.priceCurrency
  && ((Number(p.scanAt)>0 && now-Number(p.scanAt)>=-1000 && now-Number(p.scanAt)<=20000)
   || (Number(p.lastTradeAt)>0 && now-Number(p.lastTradeAt)>=-1000 && now-Number(p.lastTradeAt)<=30000)))
  .sort((a,b)=>Number(b.lastTradeAt??0)-Number(a.lastTradeAt??0)||String(poolId(a)).localeCompare(String(poolId(b))))[0];
 if(live)return {kind:'pool',pool:live,id:`pool:${poolId(live)}`};
 return{kind:'base',id:base.priceScope==='exchange'?'base':'dex'};
}
