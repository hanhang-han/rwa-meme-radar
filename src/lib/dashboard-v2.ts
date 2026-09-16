import { okxState, extraOkx, refreshOkx } from './okx';
import { robinhoodState } from './robinhood';
import { binanceState } from './bstocks';
import { inNetwork, networks, refreshXLayer, xLayerState, xLayerDetail, networkEvents, groupCandidates } from './xlayer';
import { unifiedFromXLayer } from './unified-state';
import { dataQuality } from './data-quality';
import { stockIdentity } from './stock-identity';
import { startCollection, endCollection, collectionStatus, withRequestAllowance } from './okx-client';
import { enrichAsset, enrichRelation, enrichStock, enrichmentCapabilities, enrichmentSources } from './market-enrichment';

export const identity=(chain:unknown,address:unknown)=>`${chain}:${String(address).toLowerCase()}`;
const current=(time:number|null|undefined,now=Date.now(),window=900000)=>!!time&&now-time<=window;
const empty=()=>({status:'starting',updatedAt:null,tokens:[],error:null});
function source(chain:string){return chain==='196'?okxState:extraOkx[chain]??(extraOkx[chain]=empty());}
let cycle=0;
export async function collectDashboard(){
  startCollection();
  try {
    // Rotate first service for fairness; one shared budget bounds all chains.
    const chains=['196','56','4663'];const offset=cycle++%3;
    for(const chain of [...chains.slice(offset),...chains.slice(0,offset)]){
      await withRequestAllowance(Math.max(1,Math.floor(collectionStatus().roundLimit/3)),async()=>{
        if(!source(chain).updatedAt||Date.now()-source(chain).updatedAt!>3600000)await refreshOkx(chain);
        await inNetwork(chain,source(chain),refreshXLayer);
      });
    }
    endCollection();
  }catch(error){endCollection(String(error));}
}
export function marketPremium(token:number|null,reference:number|null,ratio:number|null,at:number|null,referenceAt:number|null,independent=true,now=Date.now()){
  if(!independent)return {value:null,reason:'dependent-reference'};
  if(token==null||reference==null||ratio==null||token<=0||reference<=0||ratio<=0)return {value:null,reason:'missing-price-or-ratio'};
  if(!current(at,now,14_400_000)||!current(referenceAt,now,14_400_000)||Math.abs(at!-referenceAt!)>7_200_000)return {value:null,reason:'unaligned-or-stale'};
  return {value:(token/(ratio*reference)-1)*100,reason:null};
}
export function sumKnown(rows:any[],field:string){const values=rows.map(r=>r[field]).filter(v=>typeof v==='number'&&Number.isFinite(v));return {value:values.length?values.reduce((a,b)=>a+b,0):null,known:values.length,total:rows.length};}
// Quote-style base assets are passive counterparts of a stock token's own
// liquidity pool; they are never stock-impersonating memes, so they stay in
// pool structures (pair detail) but leave the meme/relationship rankings.
const BASE_QUOTE_SYMBOLS = new Set(["WBNB","BTCB","WBTC","WETH","USDT","USDC","USD1","xBTC","xUSD"]);
export function dashboardState(){
  const snapshots=Object.keys(networks).map(chain=>({chain,data:inNetwork(chain,source(chain),xLayerState)}));
  const base=unifiedFromXLayer(snapshots.find(s=>s.chain==='196')!.data,okxState,robinhoodState,binanceState);
  const attach=(row:any,chain:string)=>({...row,chainId:chain,chainName:networks[chain].name,provider:'OKX',sourceId:`okx:${chain}`,assetId:identity(chain,row.token),fieldTimes:row.fieldTimes??{}});
  const assets=snapshots.flatMap(s=>s.data.assets.filter(a=>a.kind==='candidate'&&!BASE_QUOTE_SYMBOLS.has(String(a.symbol??"").toUpperCase())).map(a=>enrichAsset(attach(a,s.chain))));
  const symbolByToken=new Map<string,string>();
  for(const s of snapshots)for(const a of s.data.assets)symbolByToken.set(identity(s.chain,a.token),String(a.symbol??"").toUpperCase());
  const relations=snapshots.flatMap(s=>s.data.relations.map(r=>enrichRelation(attach(r,s.chain))))
    .filter(r=>{const sym=String((r as any).symbol??symbolByToken.get(identity(r.chainId,r.token))??"" ).toUpperCase();return !BASE_QUOTE_SYMBOLS.has(sym);});
  const signals=snapshots.flatMap(s=>s.data.signals.map(r=>attach(r,s.chain))).sort((a,b)=>b.t-a.t);
  const quotes=new Map(snapshots.flatMap(s=>s.data.assets.filter(a=>a.kind==='stock').map(a=>[identity(s.chain,a.token),a] as const)));
  const catalogues=base.stockTokens.map((r:any)=>({...r,updatedAt:r.quoteAt??(r.provider==='OKX'?okxState.updatedAt:r.provider==='Robinhood'?robinhoodState.updatedAt:binanceState.updatedAt),volumeScope:r.volumeScope??'dex',priceScope:r.provider==='Robinhood'?'issuer-derived':r.provider==='Binance'?'exchange':'dex'}));
  for(const chain of ['56','4663'])for(const t of source(chain).tokens)catalogues.push({...t,provider:'OKX',chainId:chain,chainName:networks[chain].name,updatedAt:source(chain).updatedAt,volumeScope:'dex',priceScope:'dex'} as any);
  const grouped=new Map<string,any[]>();
  for(const t of catalogues){
    t.referenceAt=t.updatedAt;
    t.fieldTimes={price:t.updatedAt,volume24h:t.updatedAt,marketCap:t.updatedAt,change24h:t.updatedAt};
    const quote=quotes.get(identity(t.chainId,t.tokenContractAddress));
    if(t.provider==='OKX'&&quote){
      for(const field of ['price','volume24h','marketCap','change24h'] as const)if((quote.fieldTimes?.[field]??0)>(t.updatedAt??0)){t[field]=quote[field] as any;t.fieldTimes[field]=quote.fieldTimes![field];}
      t.updatedAt=Math.max(t.updatedAt??0,quote.fieldTimes?.price??0);
    }
    const id=t.tokenContractAddress?identity(t.chainId,t.tokenContractAddress):`exchange:Binance:${t.instrumentId}`;grouped.set(id,[...(grouped.get(id)??[]),t]);}
  const stockTokens=[...grouped].map(([assetId,observations])=>{
    const ordered=[...observations].sort((a,b)=>Number(current(b.updatedAt))-Number(current(a.updatedAt))||Number(b.priceScope==='dex')-Number(a.priceScope==='dex')||(b.updatedAt??0)-(a.updatedAt??0));
    const row=ordered[0];
    // Only merge an independently sourced reference for this exact deployment.
    // A Robinhood derived token quote is never compared to its own input price.
    const reference=ordered.find(o=>o.stockPrice!=null&&current(o.referenceAt??o.updatedAt));
    const enriched=enrichStock({...row,stockIdentity:stockIdentity({...row,assetId}),stockPrice:reference?.stockPrice??row.stockPrice,referenceProvider:reference?.provider,referenceAt:reference?.referenceAt??reference?.updatedAt,
      assetId,observations:observations.map(o=>({provider:o.provider,price:o.price,stockPrice:o.stockPrice,volume24h:o.volume24h,volumeScope:o.volumeScope,updatedAt:o.updatedAt})),providers:[...new Set(observations.map(o=>o.provider))],premium:marketPremium(row.price,reference?.stockPrice??null,row.tokenToAssetRatio??null,row.updatedAt,reference?.referenceAt??reference?.updatedAt??null,row.priceScope==='dex')});
    return {...enriched,premium:marketPremium(enriched.price,enriched.stockPrice??null,enriched.tokenToAssetRatio??null,enriched.updatedAt,enriched.referenceAt??null,enriched.priceScope==='dex')};
  });
  const verified=relations.filter(r=>r.status==='verified'&&Date.now()-r.checkedAt<=3600000);
  const pools=[...new Map(verified.map(r=>[identity(r.chainId,r.pool),r])).values()];
  const valued=pools.filter(r=>current(r.liquidityAt??r.checkedAt));
  const capabilities=[...snapshots.map(({chain,data})=>({chainId:chain,chainName:networks[chain].name,provider:'OKX',catalogue:source(chain).status,market:data.assets.some(a=>a.price!=null)?'partial':'pending',trades:data.assets.some(a=>a.tradeAt)?'partial':'pending',relations:data.relations.length?'partial':'pending',status:data.status,yielded:data.yielded,updatedAt:data.updatedAt,lastProgressAt:data.lastProgressAt,error:data.error,coverage:data.coverage})),...enrichmentCapabilities()];
  const metrics={...base.metrics,verifiedPools:pools.length,verifiedAssets:new Set(verified.map(r=>identity(r.chainId,r.token))).size,
    actionableAssets:new Set(valued.filter(r=>(r.liquidityUsd??0)>=1000).map(r=>identity(r.chainId,r.token))).size,
    pairedLiquidityUsd:sumKnown(valued,'liquidityUsd').value,liquidityCoverage:{valued:valued.filter(r=>r.liquidityUsd!=null).length,total:pools.length},
    newRelations24h:relations.filter(r=>r.firstSeen>=Date.now()-86400000).length};
  return {...base,version:2,sources:[...base.sources,...enrichmentSources()],assets,relations,signals,stockTokens,metrics,capabilities,collection:collectionStatus(),
    quality:dataQuality(assets,stockTokens,relations),groups:groupCandidates(assets,relations),sectors:snapshots.flatMap(s=>s.data.sectors.map(b=>({...b,chainId:s.chain}))),
    distribution:snapshots.map(s=>{const list=assets.filter(a=>a.chainId===s.chain&&a.kind==='candidate');return {chainId:s.chain,name:networks[s.chain].name,assets:list.length,volume:sumKnown(list.filter(a=>current(a.fieldTimes?.volume24h)),'volume24h'),liquidity:sumKnown(valued.filter(r=>r.chainId===s.chain),'liquidityUsd')};})};
}
export function dashboardDetail(chain:string,address:string){
  if(!networks[chain])return null;
  return inNetwork(chain,source(chain),()=>{
    const result=xLayerDetail(address);if(!result)return null;
    return {...result,asset:enrichAsset({...result.asset,chainId:chain,chainName:networks[chain].name,provider:'OKX'}),stock:result.stock?enrichStock({...result.stock,chainId:chain,stockIdentity:stockIdentity(result.stock)}):result.stock,
      relations:result.relations.map(r=>enrichRelation({...r,chainId:chain})),stocks:result.stocks?.map((s:any)=>({...s,stock:s.stock?enrichStock({...s.stock,chainId:chain,stockIdentity:stockIdentity(s.stock)}):s.stock}))};
  });
}
export function dashboardEvents(chain:string,before?:{t:number;id:string},limit=50){return inNetwork(chain,source(chain),()=>networkEvents(before,limit));}
