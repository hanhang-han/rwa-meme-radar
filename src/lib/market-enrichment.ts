import { readSnapshot, writeSnapshot } from './snapshot';
import { stockIdentity, US_SECURITIES } from './stock-identity';

type Status = 'starting'|'partial'|'ready'|'stale'|'error'|'unconfigured'|'quota-exhausted'|'entitlement-required';
type Field = 'price'|'marketCap'|'volume24h'|'buys24h'|'sells24h'|'txs24h'|'holders'|'liquidity'|'change24h'|'holderTop10';
export type MarketObservation = {
  provider:string; updatedAt:number; fieldTimes?:Partial<Record<Field,number>>; fieldScopes?:Partial<Record<Field,string>>; fieldTimeKinds?:Partial<Record<Field,string>>;
  priceProvenance?:{dependencies:string[];dependenciesComplete:boolean;timeKind:'observed'|'market'};
  price:number|null; marketCap:number|null; volume24h:number|null; buys24h:number|null;
  sells24h:number|null; txs24h:number|null; holders:number|null; holderTop10?:number|null; liquidity:number|null; change24h:number|null;
};
type PoolObservation = {chainId?:string;pool?:string;baseToken?:{address:string;symbol:string};quoteToken?:{address:string;symbol:string};priceNative?:number|null;observedAt?:number;marketAt?:number|null;provider:string;updatedAt:number;liquidityUsd:number|null;volume24h:number|null;buys24h:number|null;sells24h:number|null;txs24h:number|null};
// A single on-chain pool cannot hold anywhere near $10bn; upstream feeds have
// returned junk values like 1.6e38 for thin pools. Treat those as missing.
const SANE_USD_MAX=1e10;
const saneUsd=(v:number|null)=>v==null?null:Number.isFinite(v)&&v>=0&&v<SANE_USD_MAX?v:null;
type HolderObservation = {provider:'Blockscout';updatedAt:number;holders:number|null;transfers:number|null};
type Resolution = {id:string;symbol:string;code:string;exchange:string;name:string;currency:string|null;status:'exact-primary'|'exact';updatedAt:number};
type StockQuote = {identityVerified:boolean;realtime:false;delayMs:number;marketSession:'unknown'|'closed';sessionEvidence:string;provider:'EODHD';id:string;symbol:string;exchange:string;name:string;currency:string|null;price:number;volume:number|null;change24h:number|null;marketAt:number;observedAt:number};
type LiquidityCoverage = {known:number;fresh:number;latestAt:number|null;asOf:number;maxAgeMs:number;status:'current'|'partial'|'stale'|'unavailable'};
type ProviderState = {status:Status;updatedAt:number|null;error:string|null;items:number;
  lastAttemptAt?:number|null;lastCompletedAt?:number|null;reason?:string|null;liquidityCoverage?:LiquidityCoverage};
type EnrichmentState = {
  version:1; assets:Record<string,Record<string,MarketObservation>>; pools:Record<string,Record<string,PoolObservation>>;
  holders:Record<string,HolderObservation>; resolutions:Record<string,Resolution>; stocks:Record<string,StockQuote>;
  providers:Record<'CoinGecko'|'DexScreener'|'Blockscout'|'EODHD',ProviderState>;
  eodUsage:{day:string;symbols:number};
  attempts:Record<string,{lastAttemptAt:number;lastSuccessAt:number|null;nextRetryAt:number;failureCount:number;reason:string|null}>;
  eodEntitlement?:{plan:string;dailyRateLimit:number;apiRequests:number;day:string;checkedAt:number;realtimeAllowed:boolean};
  quarantined:Record<string,{reason:string;at:number;resolution?:Resolution;quote?:StockQuote}>;
};

const blankProvider=():ProviderState=>({status:'starting',updatedAt:null,error:null,items:0});
const initial=():EnrichmentState=>({version:1,attempts:{},quarantined:{},assets:{},pools:{},holders:{},resolutions:{},stocks:{},
  providers:{CoinGecko:blankProvider(),DexScreener:blankProvider(),Blockscout:blankProvider(),EODHD:blankProvider()},
  eodUsage:{day:new Date().toISOString().slice(0,10),symbols:0}});
export const enrichmentState=initial();
let restored=false,marketRunning=false,geckoRunning=false;
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
// GeckoTerminal's public ceiling is 30 requests/minute. Keep one shared,
// deliberately smaller budget for token, pool and holder lookups alike.
export const GECKO_REQUESTS_PER_MINUTE=12;
const GECKO_PUBLIC_REQUESTS_PER_MINUTE=6;
const GECKO_PUBLIC_SPACING_MS=4_000;
const GECKO_BATCH_SIZE=30;
// Thirty tokens per chain per round need about 17 rounds to revisit the full
// catalogue. A five-minute pause made every token aggregate expire before its
// next turn (the KPI requires observations younger than 30 minutes). Six
// token/pool requests per roughly minute-long round stay below the shared
// twelve-request-per-minute guard, even before the upstream 429 backoff.
export const COINGECKO_REFRESH_INTERVAL_MS=60_000;
const GECKO_LIQUIDITY_MAX_AGE_MS=30*60_000;
const GECKO_ROUND_STALL_MS=10*60_000;
const GECKO_ROUND_TIMEOUT_MS=8*60_000;
let geckoCallTimes:number[]=[];
let geckoBlockedUntil=0;
let geckoLastCallAt=0;

class GeckoDeferred extends Error {
  constructor(message:string,readonly upstream:boolean){super(message);}
}

export function retryAfterMs(value:string|null,now=Date.now()){
  if(!value)return null;
  const seconds=Number(value);
  if(Number.isFinite(seconds)&&seconds>=0)return Math.min(86_400_000,Math.ceil(seconds*1000));
  const at=Date.parse(value);
  return Number.isFinite(at)?Math.min(86_400_000,Math.max(0,at-now)):null;
}

async function geckoJson(url:string,headers:Record<string,string>={},roundSignal?:AbortSignal){
  const isPublic=!headers['x-cg-pro-api-key'];
  if(isPublic&&process.env.NODE_ENV!=='test'){
    const wait=Math.max(0,GECKO_PUBLIC_SPACING_MS-(Date.now()-geckoLastCallAt));
    if(wait)await delay(wait);
  }
  const now=Date.now();
  if(now<geckoBlockedUntil)throw new GeckoDeferred('CoinGecko HTTP 429 backoff',true);
  geckoCallTimes=geckoCallTimes.filter(at=>at<=now&&now-at<60_000);
  if(geckoCallTimes.length>=(isPublic?GECKO_PUBLIC_REQUESTS_PER_MINUTE:GECKO_REQUESTS_PER_MINUTE))
    throw new GeckoDeferred('CoinGecko local request budget',false);
  geckoCallTimes.push(now);
  geckoLastCallAt=now;
  const requestSignal=AbortSignal.timeout(20_000);
  const response=await fetch(url,{headers:{accept:'application/json','user-agent':'CliperxRadar/1.0',...headers},
    signal:roundSignal?AbortSignal.any([requestSignal,roundSignal]):requestSignal});
  if(response.status===429){
    geckoBlockedUntil=Math.max(geckoBlockedUntil,Date.now()+Math.max(60_000,retryAfterMs(response.headers.get('retry-after'))??0));
    throw new GeckoDeferred('CoinGecko HTTP 429 backoff',true);
  }
  if(!response.ok)throw new Error(`HTTP ${response.status}`);
  const type=response.headers.get('content-type')??'';
  if(!type.includes('json'))throw new Error('non-JSON response');
  return response.json();
}

function restore(){
  if(restored)return;restored=true;
  if(process.env.NODE_ENV==='test')return;
  const saved=readSnapshot<EnrichmentState>(snapshotFile);
  if(saved?.version===1){
    Object.assign(enrichmentState,saved);
    enrichmentState.attempts??={};enrichmentState.quarantined??={};quarantineEodCache();
    for(const provider of Object.values(enrichmentState.providers))if(provider.updatedAt)provider.status='stale';
  }
}
function save(){if(process.env.NODE_ENV!=='test')writeSnapshot(snapshotFile,enrichmentState);}
function coinGeckoLiquidityCoverage(now=Date.now()):LiquidityCoverage{
  let known=0,fresh=0,latestAt:number|null=null;
  for(const records of Object.values(enrichmentState.assets)){
    const row=records.CoinGecko,at=row?.fieldTimes?.liquidity;
    if(row?.fieldScopes?.liquidity!=='token-aggregate'||saneUsd(row.liquidity)==null||typeof at!=='number'||!Number.isFinite(at)||at<=0)continue;
    known++;
    latestAt=Math.max(latestAt??0,at);
    if(0<=now-at&&now-at<=GECKO_LIQUIDITY_MAX_AGE_MS)fresh++;
  }
  return {known,fresh,latestAt,asOf:now,maxAgeMs:GECKO_LIQUIDITY_MAX_AGE_MS,
    status:!known?'unavailable':!fresh?'stale':fresh===known?'current':'partial'};
}
function reconcileCoinGeckoHealth(now=Date.now()){
  const provider=enrichmentState.providers.CoinGecko;
  provider.liquidityCoverage=coinGeckoLiquidityCoverage(now);
  const unfinished=provider.lastAttemptAt&&(!provider.lastCompletedAt||provider.lastAttemptAt>provider.lastCompletedAt);
  if(unfinished&&now-(provider.lastAttemptAt??0)>GECKO_ROUND_STALL_MS){
    provider.status='stale';provider.reason='collector-round-stalled';
  }else if(provider.updatedAt&&now-provider.updatedAt>GECKO_LIQUIDITY_MAX_AGE_MS
           &&(provider.status==='partial'||provider.status==='ready'||provider.status==='stale')){
    provider.status='stale';provider.reason='last-success-expired';
  }else if(provider.status==='partial'||provider.status==='ready')provider.reason=null;
}
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
  const row:MarketObservation={provider,updatedAt:at,price:null,marketCap:null,volume24h:null,buys24h:null,sells24h:null,txs24h:null,holders:null,liquidity:null,change24h:null,...values};
  row.fieldTimes={};row.fieldTimeKinds={};
  for(const field of [...fields,'holderTop10'] as Field[])if(row[field]!=null){row.fieldTimes[field]=values.fieldTimes?.[field]??at;row.fieldTimeKinds[field]='observed';}
  return row;
}
function poolObservation(provider:string,at:number,values:Partial<PoolObservation>):PoolObservation{
  return {provider,updatedAt:at,liquidityUsd:null,volume24h:null,buys24h:null,sells24h:null,txs24h:null,...values};
}
export function putAsset(chain:string,token:string,value:MarketObservation){
  const id=key(chain,token);enrichmentState.assets[id]??={};const old=enrichmentState.assets[id][value.provider];
  if(!old){enrichmentState.assets[id][value.provider]=value;return;}
  const merged:any={...old,provider:value.provider,updatedAt:Math.max(old.updatedAt,value.updatedAt),fieldTimes:{...(old.fieldTimes??{})}};
  merged.fieldScopes={...(old.fieldScopes??{})};merged.fieldTimeKinds={...(old.fieldTimeKinds??{})};
  for(const field of [...fields,'holderTop10'] as const)if(value[field]!=null&&(value.fieldTimes?.[field]??0)>=(old.fieldTimes?.[field]??0)){
    merged[field]=value[field];merged.fieldTimes[field]=value.fieldTimes?.[field];merged.fieldScopes[field]=value.fieldScopes?.[field];merged.fieldTimeKinds[field]=value.fieldTimeKinds?.[field]??'observed';
  }
  if(value.price!=null&&(value.fieldTimes?.price??0)>=(old.fieldTimes?.price??0))merged.priceProvenance=value.priceProvenance;
  enrichmentState.assets[id][value.provider]=merged;
}
function putPool(chain:string,pool:string,value:PoolObservation){const id=key(chain,pool);enrichmentState.pools[id]??={};enrichmentState.pools[id][value.provider]=value;}

export function normalizeDexScreener(chain:string,body:any,at=Date.now()){
  const assets:Record<string,MarketObservation>={},pools:Record<string,PoolObservation>={};
  for(const pair of Array.isArray(body)?body:(Array.isArray(body?.pairs)?body.pairs:[])){
    const pairAddress=address(pair?.pairAddress),base=address(pair?.baseToken?.address);if(!pairAddress||!base)continue;
    const buys=count(pair?.txns?.h24?.buys),sells=count(pair?.txns?.h24?.sells),txs=buys==null||sells==null?null:buys+sells;
    const liquidity=saneUsd(numeric(pair?.liquidity?.usd)),volume=numeric(pair?.volume?.h24),price=numeric(pair?.priceUsd,true);
    const asset=observation('DexScreener',at,{price,marketCap:numeric(pair?.marketCap),volume24h:volume,buys24h:buys,sells24h:sells,txs24h:txs,liquidity,change24h:signed(pair?.priceChange?.h24)});
    // The quoted pool is known, but the provider's USD conversion path is
    // opaque. Recording it is not proof of independence from another pool.
    asset.fieldScopes=Object.fromEntries(fields.map(f=>[f,`pool:${pairAddress}`]));
    asset.priceProvenance={dependencies:[pairAddress],dependenciesComplete:false,timeKind:'observed'};
    const existing=assets[key(chain,base)];if(!existing||(asset.liquidity??-1)>(existing.liquidity??-1))assets[key(chain,base)]=asset;
    pools[key(chain,pairAddress)]=poolObservation('DexScreener',at,{chainId:chain,pool:pairAddress,baseToken:{address:base,symbol:String(pair.baseToken?.symbol??'')},quoteToken:address(pair.quoteToken?.address)?{address:address(pair.quoteToken.address)!,symbol:String(pair.quoteToken?.symbol??'')}:undefined,priceNative:numeric(pair.priceNative,true),observedAt:at,marketAt:null,liquidityUsd:liquidity,volume24h:volume,buys24h:buys,sells24h:sells,txs24h:txs});
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
    const buys=count(a?.transactions?.h24?.buys),sells=count(a?.transactions?.h24?.sells),txs=buys==null||sells==null?null:buys+sells;
    assets[key(chain,tokenAddress)]=observation('CoinGecko',at,{price:numeric(token.attributes?.price_usd,true),marketCap:numeric(token.attributes?.market_cap_usd),
      volume24h:numeric(token.attributes?.volume_usd?.h24),buys24h:buys,sells24h:sells,txs24h:txs,
      liquidity:saneUsd(numeric(token.attributes?.total_reserve_in_usd)),change24h:signed(a?.price_change_percentage?.h24)});
    assets[key(chain,tokenAddress)].fieldScopes={price:'token-aggregate',marketCap:'token',volume24h:'token-aggregate',liquidity:'token-aggregate',buys24h:`pool:${poolId(topId)??'unknown'}`,sells24h:`pool:${poolId(topId)??'unknown'}`,txs24h:`pool:${poolId(topId)??'unknown'}`,change24h:`pool:${poolId(topId)??'unknown'}`};
    const p=poolId(topId);if(p)pools[key(chain,p)]=poolObservation('CoinGecko',at,{liquidityUsd:saneUsd(numeric(a?.reserve_in_usd)),volume24h:numeric(a?.volume_usd?.h24),buys24h:buys,sells24h:sells,txs24h:txs});
  }
  return {assets,pools};
}

export function normalizeCoinGeckoPools(chain:string,body:any,at=Date.now()){
  const pools:Record<string,PoolObservation>={};
  for(const row of Array.isArray(body?.data)?body.data:[]){const p=address(row?.attributes?.address)??poolId(row?.id);if(!p)continue;const a=row.attributes??{};
    const buys=count(a?.transactions?.h24?.buys),sells=count(a?.transactions?.h24?.sells);
    pools[key(chain,p)]=poolObservation('CoinGecko',at,{liquidityUsd:saneUsd(numeric(a?.reserve_in_usd)),volume24h:numeric(a?.volume_usd?.h24),buys24h:buys,sells24h:sells,txs24h:buys==null||sells==null?null:buys+sells});
  }return pools;
}

export function normalizeCoinGeckoInfo(body:any,at=Date.now()):MarketObservation|null{
  const holders=body?.data?.attributes?.holders,value=count(holders?.count);if(value==null)return null;
  const observed=typeof holders?.last_updated==='string'&&Number.isFinite(Date.parse(holders.last_updated))?Date.parse(holders.last_updated):at;
  return observation('CoinGecko',at,{holders:value,holderTop10:numeric(holders?.distribution_percentage?.top_10),fieldTimes:{holders:observed,holderTop10:observed}});
}

function sourceResult(provider:'CoinGecko'|'DexScreener',assetRows:Record<string,MarketObservation>,poolRows:Record<string,PoolObservation>,at:number){
  for(const [id,row] of Object.entries(assetRows))if(!fields.some(f=>row[f]!=null))delete assetRows[id];
  for(const [id,row] of Object.entries(poolRows))if(!['liquidityUsd','volume24h','buys24h','sells24h','txs24h','priceNative'].some(f=>(row as any)[f]!=null))delete poolRows[id];
  for(const [id,value] of Object.entries(assetRows)){const [,token]=id.split(':');putAsset(id.split(':')[0],token,value);}
  for(const [id,value] of Object.entries(poolRows)){const [,pool]=id.split(':');putPool(id.split(':')[0],pool,value);}
  if(!Object.keys(assetRows).length&&!Object.keys(poolRows).length)return;
  const p=enrichmentState.providers[provider];p.status='partial';p.updatedAt=at;p.error=null;p.items=Object.keys(assetRows).length+Object.keys(poolRows).length;
}
function ordered<T extends {chainId?:string;token?:string;pool?:string;status?:string}>(rows:T[],chain:string,kind:'asset'|'pool',provider='shared'){
  const records=kind==='asset'?enrichmentState.assets:enrichmentState.pools;
  return rows.filter(row=>String(row.chainId)===chain&&address(kind==='asset'?row.token:row.pool)&&(enrichmentState.attempts[`${provider}:${kind}:${key(chain,kind==='asset'?row.token:row.pool)}`]?.nextRetryAt??0)<=Date.now()).sort((a,b)=>{
    const ak=key(chain,kind==='asset'?a.token:a.pool),bk=key(chain,kind==='asset'?b.token:b.pool);
    const aa=enrichmentState.attempts[`${provider}:${kind}:${ak}`]?.lastAttemptAt??Math.max(0,...Object.values(records[ak]??{}).map((x:any)=>x.updatedAt??0)),ba=enrichmentState.attempts[`${provider}:${kind}:${bk}`]?.lastAttemptAt??Math.max(0,...Object.values(records[bk]??{}).map((x:any)=>x.updatedAt??0));return aa-ba;
  });
}

async function refreshDex(assets:any[],relations:any[]){
  const provider=enrichmentState.providers.DexScreener;let successes=0,items=0,lastError='';
  for(const chain of Object.keys(dexNetworks)){
    const selected=ordered(assets,chain,'asset','DexScreener').slice(0,30),pools=ordered(relations.filter(r=>r.status==='verified'),chain,'pool','DexScreener').slice(0,30);
    for(const [path,kind] of [[`tokens/v1/${dexNetworks[chain]}/${selected.map(a=>a.token).join(',')}`,'asset'],[`latest/dex/pairs/${dexNetworks[chain]}/${pools.map(r=>r.pool).join(',')}`,'pool']] as const){
      if((kind==='asset'&&!selected.length)||(kind==='pool'&&!pools.length))continue;
      const work=kind==='asset'?selected:pools;
      try{const at=Date.now(),result=normalizeDexScreener(chain,await json(`https://api.dexscreener.com/${path}`),at);sourceResult('DexScreener',result.assets,result.pools,at);const accepted=Object.keys(result.assets).length+Object.keys(result.pools).length;successes+=Number(accepted>0);items+=accepted;for(const row of work){const id=key(chain,kind==='asset'?row.token:row.pool),ok=!!(kind==='asset'?result.assets[id]:result.pools[id]);attempt(`DexScreener:${kind}:${id}`,ok,ok?null:'unsupported-or-empty',at,!ok);}}catch(error){lastError=String(error);for(const row of work)attempt(`DexScreener:${kind}:${key(chain,kind==='asset'?row.token:row.pool)}`,false,lastError);}
    }
  }
  provider.items=items;if(successes){provider.status='partial';provider.updatedAt=Date.now();provider.error=lastError||null;}else{provider.status=provider.updatedAt?'stale':'error';provider.error=lastError||'No response';}
}

async function refreshCoinGecko(assets:any[],relations:any[],roundSignal:AbortSignal){
  const provider=enrichmentState.providers.CoinGecko;let successes=0,items=0,lastError='',deferred:GeckoDeferred|null=null;
  const apiKey=process.env.COINGECKO_API_KEY,base=apiKey?'https://pro-api.coingecko.com/api/v3/onchain':'https://api.geckoterminal.com/api/v2';
  const headers:Record<string,string>=apiKey?{'x-cg-pro-api-key':apiKey}:{};
  chainRounds:for(const chain of Object.keys(geckoNetworks)){
    const chainAssets=assets.filter(row=>String(row.chainId)===chain&&address(row.token));
    const selected=ordered(chainAssets,chain,'asset','CoinGecko').slice(0,GECKO_BATCH_SIZE);
    const poolRows=ordered(relations.filter(r=>r.status==='verified'),chain,'pool','CoinGecko').slice(0,GECKO_BATCH_SIZE),network=geckoNetworks[chain];
    const requests:{url:string;kind:'tokens'|'pools';work:any[]}[]=[];
    for(let start=0;start<selected.length;start+=GECKO_BATCH_SIZE){
      const work=selected.slice(start,start+GECKO_BATCH_SIZE);
      requests.push({url:`${base}/networks/${network}/tokens/multi/${work.map(a=>a.token).join(',')}?include=top_pools`,kind:'tokens',work});
    }
    if(poolRows.length)requests.push({url:`${base}/networks/${network}/pools/multi/${poolRows.map(r=>r.pool).join(',')}`,kind:'pools',work:poolRows});
    for(const {url,kind,work} of requests){
      const workKind=kind==='tokens'?'asset':'pool';
      try{const body=await geckoJson(url,headers,roundSignal),at=Date.now(),result=kind==='tokens'?normalizeCoinGeckoTokens(chain,body,at):{assets:{},pools:normalizeCoinGeckoPools(chain,body,at)};
        sourceResult('CoinGecko',result.assets,result.pools,at);const accepted=Object.keys(result.assets).length+Object.keys(result.pools).length;items+=accepted;successes+=Number(accepted>0);
        for(const row of work){
          const id=key(chain,kind==='tokens'?row.token:row.pool);
          // A token-wide liquidity observation deserves a normal refresh turn
          // even when this source has no price for the same contract.
          const present=kind==='tokens'?
            result.assets[id]?.price!=null||result.assets[id]?.liquidity!=null:!!result.pools[id];
          attempt(`CoinGecko:${workKind}:${id}`,present,present?null:kind==='tokens'?'no-verified-market-or-liquidity':'unsupported-or-empty',at,!present);
        }}
      catch(error){
        lastError=String(error);
        if(roundSignal.aborted){deferred=new GeckoDeferred('CoinGecko collection round timed out',false);break chainRounds;}
        // A provider-wide 429 or our local ceiling is not evidence that any
        // selected token is unsupported. Leave its queue position intact.
        if(error instanceof GeckoDeferred){deferred=error;break chainRounds;}
        for(const row of work)attempt(`CoinGecko:${workKind}:${key(chain,kind==='tokens'?row.token:row.pool)}`,false,lastError);
      }
      await delay(apiKey?250:2200);
    }
  }
  // Public GeckoTerminal quotas are shared by IP. Rotate holder lookups globally,
  // instead of spending one request per chain every round.
  // On the public endpoint, the six token/pool batches succeeded while the
  // seventh /info request repeatedly received HTTP 429. Keep the essential
  // token and pool observations flowing; holder lookups need a paid key or a
  // separately verified public allowance.
  const holderLimit=apiKey?limit('COINGECKO_INFO_ROUND_LIMIT',10):0;
  const holderWork=assets.filter(a=>geckoNetworks[String(a.chainId)]&&address(a.token)&&(enrichmentState.attempts[`holders:${key(a.chainId,a.token)}`]?.nextRetryAt??0)<=Date.now()).sort((a,b)=>
    (enrichmentState.attempts[`holders:${key(a.chainId,a.token)}`]?.lastAttemptAt??a.fieldTimes?.holders??0)-(enrichmentState.attempts[`holders:${key(b.chainId,b.token)}`]?.lastAttemptAt??b.fieldTimes?.holders??0)).slice(0,holderLimit);
  if(!deferred)for(const asset of holderWork){
    const chain=String(asset.chainId),network=geckoNetworks[chain],id=`holders:${key(chain,asset.token)}`;
    try{const body=await geckoJson(`${base}/networks/${network}/tokens/${asset.token}/info`,headers,roundSignal),at=Date.now(),value=normalizeCoinGeckoInfo(body,at);attempt(id,!!value,value?null:'holders-unsupported',at,!value);if(value){putAsset(chain,asset.token,value);items++;successes++;}}
    catch(error){lastError=String(error);if(roundSignal.aborted){deferred=new GeckoDeferred('CoinGecko collection round timed out',false);break;}if(error instanceof GeckoDeferred){deferred=error;break;}attempt(id,false,lastError);}
    await delay(apiKey?250:2200);
  }
  provider.items=items;
  if(successes)provider.updatedAt=Date.now();
  if(deferred){provider.status=deferred.upstream?'quota-exhausted':successes?'partial':provider.updatedAt?'stale':'starting';provider.error=deferred.message;}
  else if(successes){provider.status='partial';provider.updatedAt=Date.now();provider.error=lastError||null;}
  else{provider.status=provider.updatedAt?'stale':'error';provider.error=lastError||'No response';}
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

const identityFor=(row:any)=>{const known=stockIdentity(row);return known.status==='identified'?known:(row.stockIdentity??known);};
const eodId=(row:any)=>identityFor(row).id;
export const validEodResolution=(r:Resolution|undefined)=>!!r&&((r.exchange==='US'&&r.currency==='USD'&&!!US_SECURITIES[r.code])||(r.exchange==='HK'&&r.currency==='HKD'&&/^XHKG:/.test(r.id)));
export function quarantineEodCache(at=Date.now()){
  for(const [id,r] of Object.entries(enrichmentState.resolutions))if(!validEodResolution(r)){
    enrichmentState.quarantined[id]={reason:'underlying-market-or-currency-mismatch',at,resolution:r,quote:enrichmentState.stocks[id]};
    delete enrichmentState.resolutions[id];delete enrichmentState.stocks[id];
  }
  for(const [id,q] of Object.entries(enrichmentState.stocks))if(!enrichmentState.resolutions[id]||q.symbol!==enrichmentState.resolutions[id].symbol||q.currency!==enrichmentState.resolutions[id].currency){
    enrichmentState.quarantined[id]={reason:'reference-without-verified-resolution',at,quote:q};delete enrichmentState.stocks[id];
  }
}
const directEod=(row:any,at:number):Resolution|null=>{
  const identity=identityFor(row);if(identity.status!=='identified')return null;
  if(identity.market==='HKEX'){const code=String(Number(identity.code)).padStart(4,'0');return {id:identity.id,symbol:`${code}.HK`,code,exchange:'HK',name:identity.nameEn??identity.code,currency:'HKD',status:'exact-primary',updatedAt:at};}
  if(identity.market==='US'&&US_SECURITIES[identity.code])return {id:identity.id,symbol:`${identity.code}.US`,code:identity.code,exchange:'US',name:US_SECURITIES[identity.code],currency:'USD',status:'exact-primary',updatedAt:at};
  return null;
};
export function normalizeEodSearch(id:string,query:string,body:any,at=Date.now()):Resolution|null{
  // Matching a ticker abroad is not a fallback for the desired security.
  const expectedUS=!!US_SECURITIES[query.toUpperCase()],expectedHK=id.startsWith('XHKG:');
  if(!expectedUS&&!expectedHK)return null;
  const exact=(Array.isArray(body)?body:[]).filter(row=>String(row?.Code??'').toUpperCase()===query.toUpperCase()&&['Common Stock','ETF','Fund'].includes(String(row?.Type??''))&&String(row.Exchange)===(expectedHK?'HK':'US')&&String(row.Currency)===(expectedHK?'HKD':'USD'));
  const row=exact[0];if(!row)return null;const exchange=String(row.Exchange);return {id,symbol:`${row.Code}.${exchange}`,code:String(row.Code),exchange,name:String(row.Name??row.Code),currency:String(row.Currency),status:row.isPrimary?'exact-primary':'exact',updatedAt:at};
}
export function equitySession(exchange:string,at:number):'closed'|'unknown'{
  // A clock can prove weekends/out-of-hours, but cannot prove a trading day
  // without a holiday/early-close calendar. Never assert "regular" here.
  const zone=exchange==='HK'?'Asia/Hong_Kong':'America/New_York';
  const p=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:zone,weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(at).map(x=>[x.type,x.value]));
  const minute=Number(p.hour)*60+Number(p.minute);
  return ['Sat','Sun'].includes(p.weekday)||minute<570||minute>=960||(exchange==='HK'&&minute>=720&&minute<780)?'closed':'unknown';
}
export function normalizeEodQuotes(body:any,resolutions:Record<string,Resolution>,observedAt=Date.now()){
  const rows=Array.isArray(body)?body:[body],bySymbol=new Map(Object.values(resolutions).filter(validEodResolution).map(r=>[r.symbol.toUpperCase(),r]));const quotes:Record<string,StockQuote>={};
  for(const row of rows){const r=bySymbol.get(String(row?.code??'').toUpperCase()),price=numeric(row?.close,true),marketAt=numeric(row?.timestamp);if(!r||price==null||marketAt==null||marketAt*1000>observedAt+60000)continue;
    quotes[r.id]={provider:'EODHD',id:r.id,symbol:r.symbol,exchange:r.exchange,name:r.name,currency:r.currency,price,volume:numeric(row?.volume),change24h:signed(row?.change_p),marketAt:marketAt*1000,observedAt,identityVerified:true,realtime:false,delayMs:1200000,marketSession:equitySession(r.exchange,observedAt),sessionEvidence:'weekday-clock-not-holiday-calendar'};
  }return quotes;
}
function resetEodUsage(){const day=new Date().toISOString().slice(0,10);if(enrichmentState.eodUsage.day!==day)enrichmentState.eodUsage={day,symbols:0};}
// Weighted elapsed regular-session minutes: spread a small budget across the
// UTC day's HK and US sessions instead of exhausting it before US opens.
export function eodAllowedSpend(at:number,daily:number){
  const day=new Date(at);let elapsed=0,total=0;
  const midnight=Date.UTC(day.getUTCFullYear(),day.getUTCMonth(),day.getUTCDate());
  for(let minute=0;minute<1440;minute+=5){const t=midnight+minute*60000;for(const exchange of ['HK','US'])if(equitySession(exchange,t)!=='closed'){total+=5;if(t<=at)elapsed+=5;}}
  return total?Math.min(daily,Math.floor(daily*elapsed/total)):0;
}
function attempt(id:string,accepted:boolean,reason:string|null,at=Date.now(),negative=false){
  const old=enrichmentState.attempts[id],failures=accepted?0:(old?.failureCount??0)+1;
  enrichmentState.attempts[id]={lastAttemptAt:at,lastSuccessAt:accepted?at:(old?.lastSuccessAt??null),failureCount:failures,reason,
    nextRetryAt:at+(accepted?300000:negative?86400000:Math.min(21600000,300000*2**Math.min(failures,6)))};
}
export async function refreshEodEntitlement(apiToken:string,at=Date.now()){
  const day=new Date(at).toISOString().slice(0,10),old=enrichmentState.eodEntitlement;
  // apiRequestsDate is the provider's billing day and may remain on the last
  // day with usage. Capability checks are cached by our actual check date.
  if(old&&Number.isFinite(old.checkedAt)&&old.checkedAt>0&&old.checkedAt<=at&&new Date(old.checkedAt).toISOString().slice(0,10)===day)return old;
  // Retain only capability and quota information, never account identifiers.
  const body=await json(`https://eodhd.com/api/user?api_token=${encodeURIComponent(apiToken)}`);
  const cap=numeric(body?.dailyRateLimit),used=numeric(body?.apiRequests);
  if(cap==null||used==null||typeof body?.subscriptionType!=='string')throw new Error('EODHD entitlement unavailable');
  const plan=body.subscriptionType.toLowerCase();
  const entitlement={plan,dailyRateLimit:cap,apiRequests:used,day:String(body.apiRequestsDate??day).slice(0,10),checkedAt:at,realtimeAllowed:plan!=='free'};
  enrichmentState.eodEntitlement=entitlement;
  if(entitlement.day===enrichmentState.eodUsage.day)enrichmentState.eodUsage.symbols=Math.max(enrichmentState.eodUsage.symbols,used);
  save();return entitlement;
}
export async function refreshEodhd(stocks:any[]){
  const provider=enrichmentState.providers.EODHD,apiToken=process.env.EODHD_API_TOKEN;if(!apiToken){provider.status='unconfigured';provider.error=null;return;}resetEodUsage();quarantineEodCache();
  const at=Date.now();let entitlement:NonNullable<EnrichmentState['eodEntitlement']>;
  try{entitlement=await refreshEodEntitlement(apiToken,at);}catch{provider.status='entitlement-required';provider.error='unable to verify provider entitlement';return;}
  const daily=Math.min(limit('EODHD_DAILY_SYMBOL_LIMIT',20),entitlement.dailyRateLimit),round=limit('EODHD_ROUND_SYMBOL_LIMIT',20);
  if(enrichmentState.eodUsage.symbols>=daily){provider.status='quota-exhausted';provider.error='provider daily quota exhausted';return;}
  if(!entitlement.realtimeAllowed){provider.status='entitlement-required';provider.error='free plan does not include the intraday reference endpoint';return;}
  let remaining=Math.min(round,eodAllowedSpend(at,daily)-enrichmentState.eodUsage.symbols);
  if(remaining<=0){provider.status=provider.updatedAt?'stale':'partial';provider.error=enrichmentState.eodUsage.symbols>=daily?'local daily symbol limit reached':'budget paced to trading sessions';return;}
  const unique=new Map<string,any>();for(const row of stocks){const id=eodId(row);if(id&&!String(id).startsWith('unresolved:')&&!unique.has(id))unique.set(id,row);}
  for(const [id,row] of unique){const direct=directEod(row,at);if(direct)enrichmentState.resolutions[id]=direct;else if(!enrichmentState.resolutions[id]&&(enrichmentState.attempts[`identity:${id}`]?.nextRetryAt??0)<=at)attempt(`identity:${id}`,false,'identity-market-unverified',at,true);}
  const quoteWork=[...unique.keys()].map(id=>enrichmentState.resolutions[id]).filter((r):r is Resolution=>!!r&&validEodResolution(r)&&equitySession(r.exchange,at)!=='closed'&&(enrichmentState.attempts[`eod:${r.id}`]?.nextRetryAt??0)<=at)
    .sort((a,b)=>(enrichmentState.attempts[`eod:${a.id}`]?.lastAttemptAt??0)-(enrichmentState.attempts[`eod:${b.id}`]?.lastAttemptAt??0)||(enrichmentState.stocks[a.id]?.observedAt??0)-(enrichmentState.stocks[b.id]?.observedAt??0)).slice(0,remaining);
  if(!quoteWork.length){provider.status=provider.updatedAt?'partial':'starting';provider.error='outside trading session or no verified identity due';return;}
  let accepted=0,lastError='';enrichmentState.eodUsage.symbols+=quoteWork.length;save();
  try{const [first,...rest]=quoteWork,suffix=rest.length?`&s=${encodeURIComponent(rest.map(r=>r.symbol).join(','))}`:'';
    const body=await json(`https://eodhd.com/api/real-time/${encodeURIComponent(first.symbol)}?api_token=${encodeURIComponent(apiToken)}&fmt=json${suffix}`),quotes=normalizeEodQuotes(body,enrichmentState.resolutions);
    for(const r of quoteWork){const q=quotes[r.id];attempt(`eod:${r.id}`,!!q,q?null:'no-valid-quote',at,!q);if(q){accepted++;if(q.marketAt>=(enrichmentState.stocks[r.id]?.marketAt??0))enrichmentState.stocks[r.id]=q;}}
  }catch(error){lastError=String(error);for(const r of quoteWork)attempt(`eod:${r.id}`,false,lastError,at);}
  provider.items=Object.keys(enrichmentState.stocks).length;if(accepted){provider.status='partial';provider.updatedAt=Date.now();provider.error=lastError||null;}else{provider.status=provider.updatedAt?'stale':'error';provider.error=lastError||'zero accepted quotes';}
}

function best<T extends {provider:string;updatedAt:number}>(record:Record<string,T>|undefined,preferred:string[]){return Object.values(record??{}).sort((a,b)=>b.updatedAt-a.updatedAt||preferred.indexOf(a.provider)-preferred.indexOf(b.provider));}
export function enrichAsset<T extends Record<string,any>>(row:T):T{
  restore();const records=enrichmentState.assets[key(row.chainId??row.chain,row.token)],holder=enrichmentState.holders[key(row.chainId??row.chain,row.token)];
  const result:any={...row,fieldTimes:{...(row.fieldTimes??{})},fieldSources:{...(row.fieldSources??{})},fieldScopes:{...(row.fieldScopes??{})},fieldTimeKinds:{...(row.fieldTimeKinds??{})},providers:[...new Set([...(row.providers??[row.provider].filter(Boolean)),...Object.keys(records??{}),holder?.provider].filter(Boolean))]};
  const priorScope=String(result.fieldScopes.liquidity??'');
  if(priorScope.startsWith('pool:')){result.singlePoolLiquidityUsd=saneUsd(result.liquidity);result.singlePoolLiquidityScope=priorScope;result.singlePoolLiquidityAt=result.fieldTimes.liquidity??null;result.liquidity=null;result.fieldTimes.liquidity=null;result.fieldSources.liquidity=null;result.fieldScopes.liquidity='unknown';result.fieldTimeKinds.liquidity='unknown';}
  if(!records&&!holder){result.totalLiquidityUsd=result.fieldScopes.liquidity==='token-aggregate'?saneUsd(result.liquidity):null;result.totalLiquidityAt=result.totalLiquidityUsd!=null?result.fieldTimes.liquidity??null:null;result.totalLiquidityStatus='unknown';result.totalLiquidityCoverage={scope:result.totalLiquidityUsd!=null?'token-aggregate':'unknown',coverage:result.totalLiquidityUsd!=null?'provider-indexed-pools':'unverified',complete:false};return result;}
  for(const field of fields){const candidates=best(records,field==='price'||field==='marketCap'||field==='volume24h'?['CoinGecko','DexScreener']:['DexScreener','CoinGecko']).filter(o=>o[field]!=null&&(field!=='liquidity'||o.fieldScopes?.liquidity==='token-aggregate')).sort((a,b)=>(b.fieldTimes?.[field]??0)-(a.fieldTimes?.[field]??0));if(field==='holders'&&holder?.holders!=null)candidates.unshift(observation('Blockscout',holder.updatedAt,{holders:holder.holders}));const chosen=candidates[0];if(!chosen)continue;const at=chosen.fieldTimes?.[field]??null;if(result[field]==null||(at!=null&&at>(result.fieldTimes[field]??0))){result[field]=chosen[field];result.fieldTimes[field]=at;result.fieldSources[field]=chosen.provider;result.fieldScopes[field]=chosen.fieldScopes?.[field]??'unknown';result.fieldTimeKinds[field]=at?(chosen.fieldTimeKinds?.[field]??'observed'):'unknown';}}
  const aggregates=Object.values(records??{}).filter(o=>o.fieldScopes?.liquidity==='token-aggregate'&&saneUsd(o.liquidity)!=null&&Number.isFinite(o.fieldTimes?.liquidity)&&Number(o.fieldTimes?.liquidity)>0).sort((a,b)=>(b.fieldTimes?.liquidity??0)-(a.fieldTimes?.liquidity??0));
  const aggregate=aggregates[0],priorAggregate=result.fieldScopes.liquidity==='token-aggregate'&&saneUsd(result.liquidity)!=null&&Number.isFinite(result.fieldTimes.liquidity)&&result.fieldTimes.liquidity>0?{liquidity:result.liquidity,provider:result.fieldSources.liquidity,fieldTimes:{liquidity:result.fieldTimes.liquidity}}:null;
  const chosenAggregate=!aggregate?priorAggregate:!priorAggregate||(aggregate.fieldTimes?.liquidity??0)>=priorAggregate.fieldTimes.liquidity?aggregate:priorAggregate;
  result.totalLiquidityUsd=chosenAggregate?.liquidity??null;
  result.totalLiquidityAt=chosenAggregate?.fieldTimes?.liquidity??null;
  result.totalLiquidityStatus=!chosenAggregate?'unknown':Date.now()-(chosenAggregate.fieldTimes?.liquidity??0)<=1_800_000&&Date.now()>=(chosenAggregate.fieldTimes?.liquidity??0)?'current':'stale';
  result.totalLiquidityCoverage=chosenAggregate?{provider:chosenAggregate.provider,scope:'token-aggregate',coverage:'provider-indexed-pools',complete:false}:{scope:'unknown',coverage:'unverified',complete:false};
  const distribution=records?.CoinGecko;if(distribution?.holderTop10!=null){const at=distribution.fieldTimes?.holderTop10??distribution.fieldTimes?.holders??null;if(!result.risk||(at!=null&&at>(result.risk.checkedAt??0)))result.risk={level:result.risk?.level??'unknown',tags:result.risk?.tags??[],top10:distribution.holderTop10,checkedAt:at,provider:'CoinGecko'};}
  result.updatedAt=Math.max(result.updatedAt??0,...Object.values(result.fieldTimes).map(Number));return result;
}
export function enrichRelation<T extends Record<string,any>>(row:T):T{
  restore();const records=enrichmentState.pools[key(row.chainId,row.pool)],chosen=best(records,['CoinGecko','DexScreener']).find(o=>saneUsd(o.liquidityUsd)!=null);const own=saneUsd(row.liquidityUsd);
  if(!chosen||((row.liquidityAt??0)>chosen.updatedAt&&own!=null))return {...row,liquidityUsd:own,liquidityScope:row.pool?`pool:${String(row.pool).toLowerCase()}`:'unknown'};
  if(!chosen)return {...row,liquidityUsd:own};
  return {...row,liquidityUsd:saneUsd(chosen.liquidityUsd),liquidityAt:chosen.updatedAt,liquidityProvider:chosen.provider,liquidityScope:row.pool?`pool:${String(row.pool).toLowerCase()}`:'unknown',poolMarket:{volume24h:chosen.volume24h,buys24h:chosen.buys24h,sells24h:chosen.sells24h,txs24h:chosen.txs24h,provider:chosen.provider,updatedAt:chosen.updatedAt,scope:row.pool?`pool:${String(row.pool).toLowerCase()}`:'unknown'}};
}
export function enrichStock<T extends Record<string,any>>(row:T):T{
  restore();quarantineEodCache();const id=eodId(row),quote=enrichmentState.stocks[id],resolution=enrichmentState.resolutions[id];let result:any={...row,stockIdentity:identityFor(row)};
  if(resolution&&row.stockIdentity?.status==='provider-code')result={...result,stockIdentity:{...row.stockIdentity,market:resolution.exchange,nameEn:resolution.name,status:'market-data-match',source:'EODHD'}};
  if(quote&&(result.stockPrice==null||quote.marketAt>(result.referenceAt??0)))result={...result,stockPrice:quote.price,referenceAt:quote.marketAt,referenceObservedAt:quote.observedAt,referenceProvider:'EODHD',referenceSymbol:quote.symbol,referenceCurrency:quote.currency,referenceVolume:quote.volume,referenceChange24h:quote.change24h,referenceDelayMs:quote.delayMs??1200000,referenceRealtime:false,referenceIdentityVerified:true,marketSession:equitySession(quote.exchange,Date.now()),referenceStatus:'delayed'};
  return result;
}

export function enrichmentSources(){restore();reconcileCoinGeckoHealth();return Object.entries(enrichmentState.providers).map(([provider,state])=>({error:state.error,reason:state.reason??null,lastAttemptAt:state.lastAttemptAt??null,lastCompletedAt:state.lastCompletedAt??null,liquidityCoverage:state.liquidityCoverage??null,id:`${provider.toLowerCase()}:enrichment`,provider,chainId:provider==='Blockscout'?'4663':provider==='EODHD'?'exchange':'multi',chainName:provider==='Blockscout'?'Robinhood Chain':provider==='EODHD'?'Global equities':'Three networks',status:state.status,updatedAt:state.updatedAt}));}
export function enrichmentCapabilities(){restore();reconcileCoinGeckoHealth();return Object.entries(enrichmentState.providers).map(([provider,state])=>({provider,chainName:provider==='Blockscout'?'Robinhood Chain':provider==='EODHD'?'Global equities':'Three networks',catalogue:'not-applicable',market:state.status,trades:provider==='CoinGecko'||provider==='DexScreener'?state.status:'not-applicable',relations:'not-applicable',updatedAt:state.updatedAt,error:state.error,reason:state.reason??null,liquidityCoverage:state.liquidityCoverage??null}));}

export async function refreshMarketEnrichment(feed:{assets:any[];relations:any[];stockTokens:any[]}){
  restore();if(marketRunning)return;marketRunning=true;
  try{await Promise.allSettled([refreshDex(feed.assets,feed.relations),refreshBlockscout(feed.assets),refreshEodhd(feed.stockTokens)]);reconcileCoinGeckoHealth();prune();save();}
  finally{marketRunning=false;}
}

export async function refreshCoinGeckoEnrichment(feed:{assets:any[];relations:any[]}){
  restore();if(geckoRunning)return;geckoRunning=true;
  const provider=enrichmentState.providers.CoinGecko;
  provider.lastAttemptAt=Date.now();provider.reason=null;
  try{await refreshCoinGecko(feed.assets,feed.relations,AbortSignal.timeout(GECKO_ROUND_TIMEOUT_MS));}
  catch(error){provider.status='error';provider.error=String(error);throw error;}
  finally{provider.lastCompletedAt=Date.now();reconcileCoinGeckoHealth();prune();save();geckoRunning=false;}
}
