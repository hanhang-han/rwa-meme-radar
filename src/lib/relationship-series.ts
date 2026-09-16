import { Timeseries } from './timeseries';
import { findPools, readPoolPrice, type PoolHit } from './pancake';
import { tokens, decimalsOf } from './erc20';
import { rpc } from './rpc';
import { correlation, basketIndex, sectors } from './analytics';
const history = new Timeseries('data/relationships.json', 2016, 300_000);
history.load();
const pricePools = new Map<string, {at:number; pool:PoolHit|null}>();
export async function sampleRelationship(meme:string, stock:{oracle:number|null;oracleAt:number|null;underlying:string}, priceUsd:number|null, supply:number|null): Promise<string> {
  if (!stock.oracle || !stock.oracleAt || Date.now()-stock.oracleAt > 900_000) return 'oracle-stale';
  let usd = priceUsd;
  let capSupply = supply;
  if (!usd) {
    let cached = pricePools.get(meme);
    if (!cached || Date.now()-cached.at > 600_000) {
      const pools = await findPools(meme, {USDT:tokens.USDT, USDC:tokens.USDC});
      cached = {at:Date.now(),pool:pools[0] ?? null}; pricePools.set(meme,cached);
    }
    // Independent USD pool avoids deriving the meme price from the stock quote itself.
    if (!cached.pool) return 'no-pool';
    const quote = await readPoolPrice(cached.pool,meme);
    if (!Number.isFinite(quote.priceInQuote) || quote.priceInQuote<=0) return 'no-quote';
    usd = quote.priceInQuote;
  }
  if (!capSupply) {
    const raw = await rpc.call(meme,'0x18160ddd');
    capSupply = Number(BigInt(raw))/10**(await decimalsOf(meme));
  }
  if (!Number.isFinite(usd) || usd<=0 || !Number.isFinite(capSupply) || capSupply<=0) return 'invalid-values';
  history.push('meme:'+meme,usd);
  history.push('stock:'+meme,stock.oracle);
  history.push('cap:'+meme,capSupply*usd);
  return 'ok';
}
export function relationshipAnalytics(meme:string) {
  const prices=history.get('meme:'+meme), stocks=history.get('stock:'+meme);
  return { correlation:correlation(prices,stocks), prices:prices.slice(-48), stockPrices:stocks.slice(-48),
    window:'4h', interval:'5m', method:'Pearson price-level correlation; independent stablecoin pool; stablecoins assumed $1',
    updatedAt:prices.at(-1)?.t ?? null };
}
export function sectorAnalytics(members:{token:string;ticker:string}[]) {
  const now=Date.now();
  return Object.entries(sectors).map(([sector,tickers])=>{
    const eligible=members.filter(m=>!tickers.length || tickers.includes(m.ticker));
    const series=eligible.map(m=>({m,prices:history.get('meme:'+m.token),caps:history.get('cap:'+m.token)}));
    const valid=series.filter(s=>s.prices.length>=2 && s.caps.length>=2 && now-s.prices.at(-1)!.t<86_400_000 && now-s.caps.at(-1)!.t<86_400_000);
    if (!eligible.length) return {sector,value:null,reason:'没有已验证直接配对成分',members:0,baseAt:null};
    if (!valid.length) return {sector,value:null,reason:'等待已验证成分的价格与市值样本',members:eligible.length,baseAt:null};

    // Use timestamps shared by every member. This keeps the basket from
    // mixing stale and fresh observations when individual samplers drift.
    let common = [...new Set(valid[0].prices.map(p=>p.t))].filter(t => valid.every(s => s.prices.some(p=>p.t===t) && s.caps.some(p=>p.t===t)));
    common = common.sort((a,b)=>a-b);
    if (common.length < 2) return {sector,value:null,reason:'至少需要两个共同采样时点',members:valid.length,baseAt:null};
    const baseAt=common[0], latestAt=common.at(-1)!;
    const metric=basketIndex(valid.map(s=>({
      baseCap:s.caps.find(p=>p.t===baseAt)!.v,
      basePrice:s.prices.find(p=>p.t===baseAt)!.v,
      price:s.prices.find(p=>p.t===latestAt)!.v,
    })));
    return {sector,value:metric.value,reason:metric.reason,members:valid.length,baseAt,updatedAt:latestAt};
  });
}
export function saveRelationships(){history.save();}
