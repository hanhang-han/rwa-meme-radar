import { readSnapshot, writeSnapshot } from './snapshot';
import { stockIdentity } from './stock-identity';

type Status = 'starting'|'partial'|'ready'|'stale'|'error'|'unconfigured';
type Field = 'price'|'marketCap'|'volume24h'|'buys24h'|'sells24h'|'txs24h'|'holders'|'liquidity'|'change24h';
export type MarketObservation = {
  provider:string; updatedAt:number; fieldTimes?:Partial<Record<Field,number>>;
  price:number|null; marketCap:number|null; volume24h:number|null; buys24h:number|null;
  sells24h:number|null; txs24h:number|null; holders:number|null; holderTop10?:number|null; liquidity:number|null; change24h:number|null;
};
type PoolObservation = {provider:string;updatedAt:number;liquidityUsd:number|null;volume24h:number|null;buys24h:number|null;sells24h:number|null;txs24h:number|null};
// A single on-chain pool cannot hold anywhere near $10bn; upstream feeds have
// returned junk values like 1.6e38 for thin pools. Treat those as missing.
const SANE_USD_MAX=1e10;
const saneUsd=(v:number|null)=>v==null?null:Number.isFinite(v)&&v>=0&&v<SANE_USD_MAX?v:null;
type HolderObservation = {provider:'Blockscout';updatedAt:number;holders:number|null;transfers:number|null};
type Resolution = {id:string;symbol:string;code:string;exchange:string;name:string;currency:string|null;status:'exact-primary'|'exact';updatedAt:number};
type StockQuote = {provider:'EODHD';id:string;symbol:string;exchange:string;name:string;currency:string|null;price:number;volume:number|null;change24h:number|null;marketAt:number;observedAt:number};
type ProviderState = {status:Status;updatedAt:number|null;error:string|null;items:number};
type EnrichmentState = {
  version:1; assets:Record<string,Record<string,MarketObservation>>; pools:Record<string,Record<string,PoolObservation>>;
  holders:Record<string,HolderObservation>; resolutions:Record<string,Resolution>; stocks:Record<string,StockQuote>;
  providers:Record<'CoinGecko'|'DexScreener'|'Blockscout'|'EODHD',ProviderState>;
  eodUsage:{day:string;symbols:number};
};

const blankProvider=():ProviderState=>({status:'starting',updatedAt:null,error:null,items:0});
const initial=():EnrichmentState=>({version:1,assets:{},pools:{},holders:{},resolutions:{},stocks:{},
  providers:{CoinGecko:blankProvider(),DexScreener:blankProvider(),Blockscout:blankProvider(),EODHD:blankProvider()},
  eodUsage:{day:new Date().toISOString().slice(0,10),symbols:0}});
export const enrichmentState=initial();
let restored=false,running=false;
const snapshotFile='data/market-enrichment.json';
const chainNames:Record<string,string>={'196':'X Layer','56':'BNB Smart Chain','4663':'Robinhood Chain'};
const geckoNetworks:Record<string,string>={'196':'x-layer','56':'bsc','4663':'robinhood'};
// DexScreener currently returns no X Layer pairs; CoinGecko covers that chain.
const dexNetworks:Record<string,string>={'56':'bsc','4663':'robinhood'};
const fields:Field[]=['price','marketCap','volume24h','buys24h','sells24h','txs24h','holders','liquidity','change24h'];
const address=(value:unknown)=>typeof value==='string'&&/^0x[\da-f]{40}$/i.test(value)?value.toLowerCase():null;
const key=(chain:unknown,value:unknown)=>`${chain}:${String(value).toLowerCase()}`;
const numeric=(value:unknown,positive=false)=>{
  if(value===null||value===undefined||value==='')return null;
  const number=Number(value);return Number.isFinite(number)&&number>=(positive?Number.MIN_VALUE:0)?number:null;
};
const signed=(value:unknown)=>{if(value===null||value===undefined||value==='')return null;const number=Number(value);return Number.isFinite(number)?number:null;};
const count=(value:unknown)=>{const number=numeric(value);return number==null?null:Math.floor(number);};
const delay=(ms:number)=>new Promise(resolve=>setTimeout(resolve,ms));
const limit=(name:string,fallback:number)=>{const n=Number(process.env[name]);return Number.isFinite(n)&&n>0?Math.floor(n):fallback;};

function restore(){
  if(restored)return;restored=true;
  if(process.env.NODE_ENV==='test')return;
  const saved=readSnapshot<EnrichmentState>(snapshotFile);
  if(saved?.version===1){
    Object.assign(enrichmentState,saved);
    for(const provider of Object.values(enrichmentState.providers))if(provider.updatedAt)provider.status='stale';
  }
}
function save(){if(process.env.NODE_ENV!=='test')writeSnapshot(snapshotFile,enrichmentState);}
// Bounded growth: observations are per (asset|pool|holder, provider); drop the
// oldest-updated keys once a table passes the cap.
function prune(){
  const cap=5000;
  for(const table of ['assets','pools','holders'] as const){
    const map=enrichmentState[table] as Record<string,Record<string,{updatedAt?:number|null}>>;
    const entries=Object.entries(map);
    if(entries.length<=cap)continue;
    entries.sort((a,b)=>{
      const at=(o:Record<string,{updatedAt?:number|null}>)=>Math.max(...Object.values(o).map(v=>v?.updatedAt??0));
      return at(b[1])-at(a[1]);
    });
    for(const [k] of entries.slice(cap))delete map[k];
  }
}
async function json(url:string,headers:Record<string,string>={}){
  const response=await fetch(url,{headers:{accept:'application/json','user-agent':'CliperxRadar/1.0',...headers},signal:AbortSignal.timeout(20_000)});
  if(!response.ok)throw new Error(`HTTP ${response.status}`);
  const type=response.headers.get('content-type')??'';if(!type.includes('json'))throw new Error('non-JSON response');
  return response.json();
}
function observation(provider:string,at:number,values:Partial<MarketObservation>):MarketObservation{
  return {provider,updatedAt:at,price:null,marketCap:null,volume24h:null,buys24h:null,sells24h:null,txs24h:null,holders:null,liquidity:null,change24h:null,...values};
}
function poolObservation(provider:string,at:number,values:Partial<PoolObservation>):PoolObservation{
  return {provider,updatedAt:at,liquidityUsd:null,volume24h:null,buys24h:null,sells24h:null,txs24h:null,...values};
}
function putAsset(chain:string,token:string,value:MarketObservation){
  const id=key(chain,token);enrichmentState.assets[id]??={};const old=enrichmentState.assets[id][value.provider];
  if(!old){enrichmentState.assets[id][value.provider]=value;return;}
  const merged:any={...old,provider:value.provider,updatedAt:Math.max(old.updatedAt,value.updatedAt),fieldTimes:{...(old.fieldTimes??{}),...(value.fieldTimes??{})}};
  for(const field of [...fields,'holderTop10'] as const)if(value[field]!=null)merged[field]=value[field];
  enrichmentState.assets[id][value.provider]=merged;
}
function putPool(chain:string,pool:string,value:PoolObservation){const id=key(chain,pool);enrichmentState.pools[id]??={};enrichmentState.pools[id][value.provider]=value;}

export function normalizeDexScreener(chain:string,body:any,at=Date.now()){
  const assets:Record<string,MarketObservation>={},pools:Record<string,PoolObservation>={};
  for(const pair of Array.isArray(body)?body:[]){
    const pairAddress=address(pair?.pairAddress),base=address(pair?.baseToken?.address);if(!pairAddress||!base)continue;
    const buys=count(pair?.txns?.h24?.buys),sells=count(pair?.txns?.h24?.sells),txs=buys==null&&sells==null?null:(buys??0)+(sells??0);
    const liquidity=saneUsd(numeric(pair?.liquidity?.usd)),volume=numeric(pair?.volume?.h24),price=numeric(pair?.priceUsd,true);
    const asset=observation('DexScreener',at,{price,marketCap:numeric(pair?.marketCap),volume24h:volume,buys24h:buys,sells24h:sells,txs24h:txs,liquidity,change24h:signed(pair?.priceChange?.h24)});
    const existing=assets[key(chain,base)];if(!existing||(asset.liquidity??-1)>(existing.liquidity??-1))assets[key(chain,base)]=asset;
    pools[key(chain,pairAddress)]=poolObservation('DexScreener',at,{liquidityUsd:liquidity,volume24h:volume,buys24h:buys,sells24h:sells,txs24h:txs});
  }
  return {assets,pools};
}

const poolId=(id:unknown)=>typeof id==='string'&&id.includes('_')?address(id.slice(id.indexOf('_')+1)):null;
export function normalizeCoinGeckoTokens(chain:string,body:any,at=Date.now()){
  const assets:Record<string,MarketObservation>={},pools:Record<string,PoolObservation>={};
  const included=new Map<string,any>((Array.isArray(body?.included)?body.included:[]).map((row:any)=>[String(row.id),row]));
  for(const token of Array.isArray(body?.data)?body.data:[]){
    const tokenAddress=address(token?.attributes?.address);if(!tokenAddress)continue;
    const topId=token?.relationships?.top_pools?.data?.[0]?.id,top=included.get(String(topId)),a=top?.attributes??{};
    const buys=count(a?.transactions?.h24?.buys),sells=count(a?.transactions?.h24?.sells),txs=buys==null&&sells==null?null:(buys??0)+(sells??0);
    assets[key(chain,tokenAddress)]=observation('CoinGecko',at,{price:numeric(token.attributes?.price_usd,true),marketCap:numeric(token.attributes?.market_cap_usd),
      volume24h:numeric(token.attributes?.volume_usd?.h24),buys24h:buys,sells24h:sells,txs24h:txs,
      liquidity:numeric(token.attributes?.total_reserve_in_usd),change24h:signed(a?.price_change_percentage?.h24)});
    const p=poolId(topId);if(p)pools[key(chain,p)]=poolObservation('CoinGecko',at,{liquidityUsd:saneUsd(numeric(a?.reserve_in_usd)),volume24h:numeric(a?.volume_usd?.h24),buys24h:buys,sells24h:sells,txs24h:txs});
  }
  return {assets,pools};
}

export function normalizeCoinGeckoPools(chain:string,body:any,at=Date.now()){
  const pools:Record<string,PoolObservation>={};
  for(const row of Array.isArray(body?.data)?body.data:[]){const p=address(row?.attributes?.address)??poolId(row?.id);if(!p)continue;const a=row.attributes??{};
    const buys=count(a?.transactions?.h24?.buys),sells=count(a?.transactions?.h24?.sells);
    pools[key(chain,p)]=poolObservation('CoinGecko',at,{liquidityUsd:saneUsd(numeric(a?.reserve_in_usd)),volume24h:numeric(a?.volume_usd?.h24),buys24h:buys,sells24h:sells,txs24h:buys==null&&sells==null?null:(buys??0)+(sells??0)});
  }return pools;
}

export function normalizeCoinGeckoInfo(body:any,at=Date.now()):MarketObservation|null{
  const holders=body?.data?.attributes?.holders,value=count(holders?.count);if(value==null)return null;
  const observed=typeof holders?.last_updated==='string'&&Number.isFinite(Date.parse(holders.last_updated))?Date.parse(holders.last_updated):at;
  return observation('CoinGecko',at,{holders:value,holderTop10:numeric(holders?.distribution_percentage?.top_10),fieldTimes:{holders:observed}});
}

function sourceResult(provider:'CoinGecko'|'DexScreener',assetRows:Record<string,MarketObservation>,poolRows:Record<string,PoolObservation>,at:number){
  for(const [id,value] of Object.entries(assetRows)){const [,token]=id.split(':');putAsset(id.split(':')[0],token,value);}
  for(const [id,value] of Object.entries(poolRows)){const [,pool]=id.split(':');putPool(id.split(':')[0],pool,value);}
  const p=enrichmentState.providers[provider];p.status='partial';p.updatedAt=at;p.error=null;p.items=Object.keys(assetRows).length+Object.keys(poolRows).length;
}
function ordered<T extends {chainId?:string;token?:string;pool?:string;status?:string}>(rows:T[],chain:string,kind:'asset'|'pool'){
  const records=kind==='asset'?enrichmentState.assets:enrichmentState.pools;
  return rows.filter(row=>String(row.chainId)===chain&&address(kind==='asset'?row.token:row.pool)).sort((a,b)=>{
    const ak=key(chain,kind==='asset'?a.token:a.pool),bk=key(chain,kind==='asset'?b.token:b.pool);
    const aa=Math.max(0,...Object.values(records[ak]??{}).map((x:any)=>x.updatedAt??0)),ba=Math.max(0,...Object.values(records[bk]??{}).map((x:any)=>x.updatedAt??0));return aa-ba;
  });
}

async function refreshDex(assets:any[],relations:any[]){
  const provider=enrichmentState.providers.DexScreener;let successes=0,items=0,lastError='';
  for(const chain of Object.keys(dexNetworks)){
    const selected=ordered(assets,chain,'asset').slice(0,30),pools=ordered(relations.filter(r=>r.status==='verified'),chain,'pool').slice(0,30);
    for(const [path,kind] of [[`tokens/v1/${dexNetworks[chain]}/${selected.map(a=>a.token).join(',')}`,'asset'],[`latest/dex/pairs/${dexNetworks[chain]}/${pools.map(r=>r.pool).join(',')}`,'pool']] as const){
      if((kind==='asset'&&!selected.length)||(kind==='pool'&&!pools.length))continue;
      try{const at=Date.now(),result=normalizeDexScreener(chain,await json(`https://api.dexscreener.com/${path}`),at);sourceResult('DexScreener',result.assets,result.pools,at);successes++;items+=Object.keys(result.assets).length+Object.keys(result.pools).length;}catch(error){lastError=String(error);}
    }
  }
  provider.items=items;if(successes){provider.status='partial';provider.updatedAt=Date.now();provider.error=lastError||null;}else{provider.status=provider.updatedAt?'stale':'error';provider.error=lastError||'No response';}
}

async function refreshCoinGecko(assets:any[],relations:any[]){
  const provider=enrichmentState.providers.CoinGecko;let successes=0,items=0,lastError='';
  const apiKey=process.env.COINGECKO_API_KEY,base=apiKey?'https://pro-api.coingecko.com/api/v3/onchain':'https://api.geckoterminal.com/api/v2';
  const headers:Record<string,string>=apiKey?{'x-cg-pro-api-key':apiKey}:{};
  for(const chain of Object.keys(geckoNetworks)){
    const selected=ordered(assets,chain,'asset').slice(0,30),poolRows=ordered(relations.filter(r=>r.status==='verified'),chain,'pool').slice(0,30),network=geckoNetworks[chain];
    const requests:[string,'tokens'|'pools'][]=[];
    if(selected.length)requests.push([`${base}/networks/${network}/tokens/multi/${selected.map(a=>a.token).join(',')}?include=top_pools`,'tokens']);
    if(poolRows.length)requests.push([`${base}/networks/${network}/pools/multi/${poolRows.map(r=>r.pool).join(',')}`,'pools']);
    for(const [url,kind] of requests){
      try{const at=Date.now(),body=await json(url,headers);if(kind==='tokens'){const result=normalizeCoinGeckoTokens(chain,body,at);sourceResult('CoinGecko',result.assets,result.pools,at);items+=Object.keys(result.assets).length+Object.keys(result.pools).length;}else{const pools=normalizeCoinGeckoPools(chain,body,at);sourceResult('CoinGecko',{},pools,at);items+=Object.keys(pools).length;}successes++;}
      catch(error){lastError=String(error);}await delay(apiKey?250:2200);
    }
  }
  // Public GeckoTerminal quotas are shared by IP. Rotate holder lookups globally,
  // instead of spending one request per chain every round.
  const related=new Set(relations.filter(r=>r.status==='verified').map(r=>key(r.chainId,r.token)));
  const holderWork=assets.filter(a=>geckoNetworks[String(a.chainId)]&&address(a.token)).sort((a,b)=>
    Number(related.has(key(b.chainId,b.token)))-Number(related.has(key(a.chainId,a.token)))||
    (a.fieldTimes?.holders??0)-(b.fieldTimes?.holders??0)).slice(0,limit('COINGECKO_INFO_ROUND_LIMIT',apiKey?10:1));
  for(const asset of holderWork){const chain=String(asset.chainId),network=geckoNetworks[chain];try{const at=Date.now(),body=await json(`${base}/networks/${network}/tokens/${asset.token}/info`,headers),value=normalizeCoinGeckoInfo(body,at);if(value){putAsset(chain,asset.token,value);items++;}successes++;}catch(error){lastError=String(error);}await delay(apiKey?250:2200);}
  provider.items=items;if(successes){provider.status='partial';provider.updatedAt=Date.now();provider.error=null;}else{provider.status=provider.updatedAt?'stale':'error';provider.error=lastError||'No response';}
}

export function normalizeBlockscout(body:any,at=Date.now()):HolderObservation{
  return {provider:'Blockscout',updatedAt:at,holders:count(body?.holders_count??body?.token_holders_count),transfers:count(body?.transfers_count)};
}
async function refreshBlockscout(assets:any[]){
  const provider=enrichmentState.providers.Blockscout,apiKey=process.env.BLOCKSCOUT_API_KEY;
  if(!apiKey){provider.status='unconfigured';provider.error=null;return;}
  const selected=ordered(assets,'4663','asset').sort((a,b)=>(enrichmentState.holders[key('4663',a.token)]?.updatedAt??0)-(enrichmentState.holders[key('4663',b.token)]?.updatedAt??0)).slice(0,limit('BLOCKSCOUT_ROUND_LIMIT',10));
  let ok=0,lastError='';for(const asset of selected){try{const at=Date.now(),body=await json(`https://api.blockscout.com/4663/api/v2/tokens/${asset.token}?apikey=${encodeURIComponent(apiKey)}`);enrichmentState.holders[key('4663',asset.token)]=normalizeBlockscout(body,at);ok++;}catch(error){lastError=String(error);}await delay(250);}
  provider.items=ok;if(ok){provider.status='partial';provider.updatedAt=Date.now();provider.error=lastError||null;}else{provider.status=provider.updatedAt?'stale':'error';provider.error=lastError||'No response';}
}

const eodId=(row:any)=>row.stockIdentity?.id??stockIdentity(row).id;
const directEod=(row:any,at:number):Resolution|null=>{
  const identity=row.stockIdentity??stockIdentity(row);if(identity.status!=='identified'||identity.market!=='HKEX')return null;
  const code=String(Number(identity.code)).padStart(4,'0');return {id:identity.id,symbol:`${code}.HK`,code,exchange:'HK',name:identity.nameEn??identity.code,currency:'HKD',status:'exact-primary',updatedAt:at};
};
export function normalizeEodSearch(id:string,query:string,body:any,at=Date.now()):Resolution|null{
  const exact=(Array.isArray(body)?body:[]).filter(row=>String(row?.Code??'').toUpperCase()===query.toUpperCase()&&['Common Stock','ETF','Fund'].includes(String(row?.Type??'')));
  exact.sort((a,b)=>Number(Boolean(b.isPrimary))-Number(Boolean(a.isPrimary))||Number(String(b.Exchange)==='US')-Number(String(a.Exchange)==='US'));
  const row=exact[0];if(!row)return null;const exchange=String(row.Exchange??'');return {id,symbol:`${row.Code}.${exchange}`,code:String(row.Code),exchange,name:String(row.Name??row.Code),currency:row.Currency?String(row.Currency):null,status:row.isPrimary?'exact-primary':'exact',updatedAt:at};
}
export function normalizeEodQuotes(body:any,resolutions:Record<string,Resolution>,observedAt=Date.now()){
  const rows=Array.isArray(body)?body:[body],bySymbol=new Map(Object.values(resolutions).map(r=>[r.symbol.toUpperCase(),r]));const quotes:Record<string,StockQuote>={};
  for(const row of rows){const r=bySymbol.get(String(row?.code??'').toUpperCase()),price=numeric(row?.close,true),marketAt=numeric(row?.timestamp);if(!r||price==null||marketAt==null)continue;
    quotes[r.id]={provider:'EODHD',id:r.id,symbol:r.symbol,exchange:r.exchange,name:r.name,currency:r.currency,price,volume:numeric(row?.volume),change24h:signed(row?.change_p),marketAt:marketAt*1000,observedAt};
  }return quotes;
}
function resetEodUsage(){const day=new Date().toISOString().slice(0,10);if(enrichmentState.eodUsage.day!==day)enrichmentState.eodUsage={day,symbols:0};}
async function refreshEodhd(stocks:any[]){
  const provider=enrichmentState.providers.EODHD,apiToken=process.env.EODHD_API_TOKEN;if(!apiToken){provider.status='unconfigured';provider.error=null;return;}resetEodUsage();
  const daily=limit('EODHD_DAILY_SYMBOL_LIMIT',500),round=limit('EODHD_ROUND_SYMBOL_LIMIT',20);let remaining=Math.min(round,daily-enrichmentState.eodUsage.symbols);if(remaining<=0){provider.status=provider.updatedAt?'stale':'partial';provider.error='local daily symbol limit reached';return;}
  const unique=new Map<string,any>();for(const row of stocks){const id=eodId(row);if(id&&!String(id).startsWith('unresolved:')&&!unique.has(id))unique.set(id,row);}
  const at=Date.now();for(const [id,row] of unique){const direct=directEod(row,at);if(direct)enrichmentState.resolutions[id]=direct;}
  const unresolved=[...unique].filter(([id,row])=>!enrichmentState.resolutions[id]&&/^[A-Z][A-Z0-9.\-]{0,9}$/i.test(String(row.stockIdentity?.code??row.stockCode??''))).slice(0,Math.min(5,remaining));
  let ok=0,lastError='';for(const [id,row] of unresolved){const query=String(row.stockIdentity?.code??row.stockCode);try{const body=await json(`https://eodhd.com/api/search/${encodeURIComponent(query)}?api_token=${encodeURIComponent(apiToken)}&fmt=json&type=stock&limit=10`);const resolution=normalizeEodSearch(id,query,body);if(resolution)enrichmentState.resolutions[id]=resolution;ok++;}catch(error){lastError=String(error);}enrichmentState.eodUsage.symbols++;remaining--;if(remaining<=0)break;}
  const quoteWork=[...unique.keys()].map(id=>enrichmentState.resolutions[id]).filter(Boolean).sort((a,b)=>(enrichmentState.stocks[a.id]?.observedAt??0)-(enrichmentState.stocks[b.id]?.observedAt??0)).slice(0,remaining);
  if(quoteWork.length){try{const [first,...rest]=quoteWork;const suffix=rest.length?`&s=${encodeURIComponent(rest.map(r=>r.symbol).join(','))}`:'';const body=await json(`https://eodhd.com/api/real-time/${encodeURIComponent(first.symbol)}?api_token=${encodeURIComponent(apiToken)}&fmt=json${suffix}`);Object.assign(enrichmentState.stocks,normalizeEodQuotes(body,enrichmentState.resolutions));ok+=quoteWork.length;}catch(error){lastError=String(error);}enrichmentState.eodUsage.symbols+=quoteWork.length;}
  provider.items=Object.keys(enrichmentState.stocks).length;if(ok){provider.status='partial';provider.updatedAt=Date.now();provider.error=lastError||null;}else{provider.status=provider.updatedAt?'stale':'error';provider.error=lastError||'No response';}
}

function best<T extends {provider:string;updatedAt:number}>(record:Record<string,T>|undefined,preferred:string[]){return Object.values(record??{}).sort((a,b)=>b.updatedAt-a.updatedAt||preferred.indexOf(a.provider)-preferred.indexOf(b.provider));}
export function enrichAsset<T extends Record<string,any>>(row:T):T{
  restore();const records=enrichmentState.assets[key(row.chainId??row.chain,row.token)],holder=enrichmentState.holders[key(row.chainId??row.chain,row.token)];if(!records&&!holder)return row;
  const result:any={...row,fieldTimes:{...(row.fieldTimes??{})},fieldSources:{...(row.fieldSources??{})},providers:[...new Set([...(row.providers??[row.provider].filter(Boolean)),...Object.keys(records??{}),holder?.provider].filter(Boolean))]};
  for(const field of fields){const candidates=best(records,field==='price'||field==='marketCap'||field==='volume24h'?['CoinGecko','DexScreener']:['DexScreener','CoinGecko']).filter(o=>o[field]!=null);if(field==='holders'&&holder?.holders!=null)candidates.unshift(observation('Blockscout',holder.updatedAt,{holders:holder.holders}));const chosen=candidates[0];if(!chosen)continue;const at=chosen.fieldTimes?.[field]??chosen.updatedAt;if(result[field]==null||at>(result.fieldTimes[field]??0)){result[field]=chosen[field];result.fieldTimes[field]=at;result.fieldSources[field]=chosen.provider;}}
  const distribution=records?.CoinGecko;if(distribution?.holderTop10!=null){const at=distribution.fieldTimes?.holders??distribution.updatedAt;if(!result.risk||at>(result.risk.checkedAt??0))result.risk={level:result.risk?.level??'unknown',tags:result.risk?.tags??[],top10:distribution.holderTop10,checkedAt:at,provider:'CoinGecko'};}
  result.updatedAt=Math.max(result.updatedAt??0,...Object.values(result.fieldTimes).map(Number));return result;
}
export function enrichRelation<T extends Record<string,any>>(row:T):T{
  restore();const records=enrichmentState.pools[key(row.chainId,row.pool)],chosen=best(records,['CoinGecko','DexScreener']).find(o=>saneUsd(o.liquidityUsd)!=null);const own=saneUsd(row.liquidityUsd);
  if(!chosen||((row.liquidityAt??0)>chosen.updatedAt&&own!=null))return own===row.liquidityUsd?row:{...row,liquidityUsd:own};
  if(!chosen)return {...row,liquidityUsd:own};
  return {...row,liquidityUsd:saneUsd(chosen.liquidityUsd),liquidityAt:chosen.updatedAt,liquidityProvider:chosen.provider,poolMarket:{volume24h:chosen.volume24h,buys24h:chosen.buys24h,sells24h:chosen.sells24h,txs24h:chosen.txs24h,provider:chosen.provider,updatedAt:chosen.updatedAt}};
}
export function enrichStock<T extends Record<string,any>>(row:T):T{
  restore();const id=eodId(row),quote=enrichmentState.stocks[id],resolution=enrichmentState.resolutions[id];let result:any=row;
  if(resolution&&row.stockIdentity?.status==='provider-code')result={...result,stockIdentity:{...row.stockIdentity,market:resolution.exchange,nameEn:resolution.name,status:'market-data-match',source:'EODHD'}};
  if(quote&&(result.stockPrice==null||quote.marketAt>(result.referenceAt??0)))result={...result,stockPrice:quote.price,referenceAt:quote.marketAt,referenceObservedAt:quote.observedAt,referenceProvider:'EODHD',referenceSymbol:quote.symbol,referenceVolume:quote.volume,referenceChange24h:quote.change24h};
  return result;
}

export function enrichmentSources(){restore();return Object.entries(enrichmentState.providers).map(([provider,state])=>({id:`${provider.toLowerCase()}:enrichment`,provider,chainId:provider==='Blockscout'?'4663':provider==='EODHD'?'exchange':'multi',chainName:provider==='Blockscout'?'Robinhood Chain':provider==='EODHD'?'Global equities':'Three networks',status:state.status,updatedAt:state.updatedAt}));}
export function enrichmentCapabilities(){restore();return Object.entries(enrichmentState.providers).map(([provider,state])=>({provider,chainName:provider==='Blockscout'?'Robinhood Chain':provider==='EODHD'?'Global equities':'Three networks',catalogue:'not-applicable',market:state.status,trades:provider==='CoinGecko'||provider==='DexScreener'?state.status:'not-applicable',relations:'not-applicable',updatedAt:state.updatedAt,error:state.error}));}

export async function refreshMarketEnrichment(feed:{assets:any[];relations:any[];stockTokens:any[]}){
  restore();if(running)return;running=true;
  try{await Promise.allSettled([refreshDex(feed.assets,feed.relations),refreshBlockscout(feed.assets),refreshEodhd(feed.stockTokens)]);await refreshCoinGecko(feed.assets,feed.relations);prune();save();}
  finally{running=false;}
}
