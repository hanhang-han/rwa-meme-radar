import { tradeKey } from '../stores/feed.js';

// Only documented destinations. This prepares an external page, never a trade.
// PancakeSwap: github.com/pancakeswap/pancakeswap-ai, swap-planner URL contract.
// OKX: web3.okx.com/token/x-layer lists the same token-address path.
export function dexAction(chain,address){
 if(!/^0x[0-9a-f]{40}$/i.test(String(address??'')))return null;
 const token=String(address).toLowerCase();
 if(String(chain)==='56')return {provider:'PancakeSwap',url:'https://pancakeswap.finance/swap?'+new URLSearchParams({chain:'bsc',inputCurrency:'BNB',outputCurrency:token})};
 if(String(chain)==='196')return {provider:'OKX DEX',url:`https://web3.okx.com/token/x-layer/${token}`};
 return null;
}
export function detailAsset(row,chain,address){return {...(row??{}),chainId:String(chain),token:String(address)};}
export function detailTrades(data,selection){
 const poolId=selection?.kind==='pool'?String(selection.pool.poolId??selection.pool.pool??selection.pool.marketId).toLowerCase():null;
 const exchange=selection?.kind==='exchange'?selection.exchange:null;
 const map=new Map();
 for(const trade of [...(data?.marketTrades??[]),...(data?.trades??[])]){
  if(poolId&&String(trade.poolId??trade.pool??trade.marketId??'').toLowerCase()!==poolId)continue;
  if(exchange&&(trade.venue!==exchange.venue||String(trade.marketId??'')!==String(exchange.marketId??'')))continue;
  const key=tradeKey(trade),previous=map.get(key);
  map.set(key,previous?{...previous,...trade}:trade);
 }
 return [...map.values()].sort((a,b)=>Number(b.t??0)-Number(a.t??0)).slice(0,150);
}
export function pairRelationMatches(relation,chain,stock,pool){
 return String(relation?.chainId)===String(chain)&&String(relation.pool??'').toLowerCase()===String(pool).toLowerCase()&&String(relation.stock??'').toLowerCase()===String(stock).toLowerCase();
}
