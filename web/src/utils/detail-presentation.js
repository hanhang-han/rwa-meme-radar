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

const numeric = value => typeof value === 'number' && Number.isFinite(value);
const observed = (at, now, ttl) => numeric(at) && at > 0 && now >= at && now-at <= ttl;
export function observedField(asset, field, now = Date.now()) {
 const declared = asset?.fieldAvailability?.[field];
 const ttl = field === 'holders' ? 86_400_000 : 1_800_000;
 const at = declared?.at ?? (field === 'totalLiquidityUsd' ? asset?.totalLiquidityAt : asset?.fieldTimes?.[field]);
 const raw = declared ? declared.value : asset?.[field];
 const value = numeric(raw) && observed(at, now, ttl) && (!declared || declared.status === 'current') ? raw : null;
 const coverage = declared?.coverage ?? asset?.totalLiquidityCoverage;
 const aggregateOK = field !== 'totalLiquidityUsd' || asset?.totalLiquidityStatus === 'current' && (value !== 0 || coverage?.complete === true);
 return {value: aggregateOK ? value : null, at,
   source:declared?.source ?? asset?.fieldSources?.[field] ?? (field === 'totalLiquidityUsd' ? coverage?.provider : null),
   status: value != null && aggregateOK ? 'current' : declared?.status ?? (at ? 'stale' : 'unknown')};
}

export function recordedPool(relation) {
 return relation?.status === 'verified' && /^0x[0-9a-f]{40}$/i.test(String(relation.pool))
   && /^0x[0-9a-f]{40}$/i.test(String(relation.stock));
}

export function tradeClocks(trade, now = Date.now()) {
 const t = trade?.sourceEventAt ?? trade?.t;
 const received = trade?.receivedAt;
 return {ageMs: numeric(t) && t <= now ? now-t : null,
   receiptDelayMs: numeric(t) && numeric(received) && received >= t ? received-t : null,
   confirmed: trade?.confirmationStatus === 'confirmed' || trade?.confirmed === true};
}
