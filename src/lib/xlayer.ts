import { AsyncLocalStorage } from 'node:async_hooks';
import { okxGet, okxPost, withRequestAllowance } from './okx-client';
import { numeric, okxState, type RwaToken } from './okx';
import { ResearchStore } from './research-store';
import { matchStock, buildTickerSet } from './stocks';
import { sectors, basketIndex } from './analytics';

const context = new AsyncLocalStorage<{chain:string;catalog:typeof okxState}>();
const chainId = () => context.getStore()?.chain ?? '196';
const catalog = () => context.getStore()?.catalog ?? okxState;
export const networks:Record<string,{name:string;rpc:string[]}> = {
  '196':{name:'X Layer',rpc:['https://rpc.xlayer.tech','https://xlayerrpc.okx.com']},
  '56':{name:'BNB Smart Chain',rpc:['https://bsc.publicnode.com']},
  '4663':{name:'Robinhood Chain',rpc:['https://rpc.mainnet.chain.robinhood.com']},
};
export function inNetwork<T>(chain:string, source:typeof okxState, action:()=>T):T {
  if(!networks[chain]) throw new Error('Unsupported network');
  return context.run({chain,catalog:source},action);
} 
const addr = (v: unknown) => typeof v === 'string' && /^0x[\da-f]{40}$/i.test(v) ? v.toLowerCase() : null;
const wordAddress = (v: string) => /^0x[\da-f]{64}$/i.test(v) && !/^0x0{64}$/.test(v) ? addr('0x' + v.slice(-40)) : null;
const items = (data: any): any[] => Array.isArray(data) ? data : Array.isArray(data?.list) ? data.list : [];
const fresh = (at?: number | null, age = 900000) => !!at && Date.now() - at < age;
const quoteSymbols = new Set(['USDT','USDC','USDG','DAI','WOKB','OKB','WETH','ETH','WBTC','BTC','XBTC']);
const stores = new Map<string,ResearchStore>();
function db() { const chain=chainId(); if(!stores.has(chain))stores.set(chain,new ResearchStore(process.env.RESEARCH_DB || 'data/research.sqlite',chain));return stores.get(chain)!; }

export interface XAsset {
  fieldTimes?:Record<string,number>; logoUrl?:string; volume5m?:number|null; volume1h?:number|null;
  token: string; chain: string; symbol: string; name: string; firstSeen: number; updatedAt: number;
  source: string; price: number | null; marketCap: number | null; volume24h: number | null;
  buys24h: number | null; sells24h: number | null; countsAt?: number;
  txs24h: number | null; holders: number | null; liquidity: number | null; change24h: number | null;
  match: {ticker:string;matchType:string} | null; kind: 'stock'|'wrapped_stock'|'quote'|'candidate';
  underlying?: string; detailAt?: number; tradeAt?: number; tradeCoverage?: string; error?: string | null;
  oldestTradeAt?: number; tradeCursor?: string; gapDetected?: boolean; tradeGaps?: string[];
  risk?: { level: string; tags: string[]; top10: number|null; checkedAt: number };
  profile?: { companyName:string; exchange:string; industry:string; stockCode:string } | null;
}
export interface XRelation {
  chainId?:string; feePct?:number|null; amounts?:any[];
  id: string; token: string; stock: string; stockSide: string; ticker: string; pool: string;
  token0: string; token1: string; wrapper: boolean; protocol: string; firstSeen: number;
  checkedAt: number; block: number; liquidityUsd: number|null; liquidityAt?:number; stockBalance: string|null;
  status: 'verified'|'error'|'invalid'; error: string|null;
}
interface Scan { token:string; checkedAt:number; poolCount:number; status:string; error:string|null }
const initialRuntime = { lastProgressAt:null as number|null, stages:{} as Record<string,{status:string;at:number}>, yielded:false, status: 'starting', updatedAt: null as number|null, error: null as string|null, hotAt: 0, running: false };
const runtimes = new Map<string,typeof initialRuntime>();
function runState(){const chain=chainId();if(!runtimes.has(chain))runtimes.set(chain,{...initialRuntime});return runtimes.get(chain)!;}


export async function xRpc(method: string, params: unknown[]): Promise<any> {
  let last = '';
  for (const url of networks[chainId()].rpc) {
    try {
      const response = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({jsonrpc:'2.0',id:1,method,params}), signal:AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      if (result.error || result.result == null) throw new Error(result.error?.message || 'RPC missing result');
      return result.result;
    } catch (error) { last = error instanceof Error ? error.message : 'RPC failed'; }
  }
  throw new Error(`X Layer ${method}: ${last.slice(0,120)}`);
}
type Call = (to:string, data:string, block?:string) => Promise<string>;
const call: Call = (to,data,block='latest') => xRpc('eth_call',[{to,data},block]);

export async function resolveStock(address:string, stocks:RwaToken[], rpcCall:Call = call, block='latest') {
  const direct = stocks.find(s=>s.tokenContractAddress.toLowerCase()===address.toLowerCase());
  if (direct) return { stock:direct, wrapped:false };
  // Symbol similarity is never sufficient to certify a wrapper.
  let incomplete=false;
  for (const selector of ['0x38d52e0f','0x6f307dc3']) {
    try {
      const underlying = wordAddress(await rpcCall(address,selector,block));
      const stock = stocks.find(s=>s.tokenContractAddress.toLowerCase()===underlying);
      if (stock) return {stock,wrapped:true};
    } catch(error) {
      // A genuine revert indicates an unsupported wrapper interface; transport
      // failures leave identity unresolved rather than proving absence.
      if(!/revert|execution|invalid opcode|method unavailable/i.test(String(error))) incomplete=true;
    }
  }
  if(incomplete) throw new Error('Wrapper identity lookup incomplete');
  return null;
}
export async function verifyXPool(pool:string, stocks:RwaToken[], rpcCall:Call=call, block='latest') {
  if (!addr(pool)) throw new Error('Unsupported pool identifier (requires address-based token0/token1 interface)');
  const [raw0,raw1] = await Promise.all([rpcCall(pool,'0x0dfe1681',block),rpcCall(pool,'0xd21220a7',block)]);
  const token0=wordAddress(raw0), token1=wordAddress(raw1);
  if (!token0 || !token1 || token0===token1) throw new Error('Pool token addresses invalid');
  const [stock0,stock1] = await Promise.all([resolveStock(token0,stocks,rpcCall,block),resolveStock(token1,stocks,rpcCall,block)]);
  if (!!stock0 === !!stock1) return {token0,token1,relation:null};
  const resolved = stock0 || stock1!;
  return {token0,token1,relation:{token:stock0?token1:token0,stockSide:stock0?token0:token1,
    stock:resolved.stock,wrapper:resolved.wrapped}};
}

export function normalizeXAsset(row:any, previous?:XAsset|null, now=Date.now()): XAsset|null {
  const token=addr(row.tokenContractAddress);
  if (!token || String(row.chainIndex)!==chainId()) return null;
  const symbol=String(row.tokenSymbol ?? previous?.symbol ?? token.slice(0,8));
  const stocks=catalog().tokens;
  const stock=stocks.find(s=>s.tokenContractAddress.toLowerCase()===token);
  // Explicit blanks mean unknown. Absent fields retain their separately dated observation.
  const value=(key:string,old:number|null|undefined) => Object.hasOwn(row,key)?numeric(row[key]):old??null;
  const observed=numeric(row.time) && numeric(row.time)! <= now + 60000 ? numeric(row.time)! : now;
  const fieldTimes={...previous?.fieldTimes};
  const aliases:Record<string,string>={price:'price',marketCap:'marketCap',volume:'volume24h',txs:'txs24h',txsBuy:'buys24h',txsSell:'sells24h',holders:'holders',liquidity:'liquidity',change:'change24h',volume5M:'volume5m',volume1H:'volume1h'};
  for(const [raw,field] of Object.entries(aliases))if(Object.hasOwn(row,raw))fieldTimes[field]=observed;
  return { ...previous, fieldTimes, logoUrl:row.tokenLogoUrl??row.logoUrl??previous?.logoUrl,
    volume5m:value('volume5M',previous?.volume5m),volume1h:value('volume1H',previous?.volume1h), token, chain:chainId(), symbol, name:String(row.tokenName??previous?.name??symbol),
    firstSeen:previous?.firstSeen??now,updatedAt:numeric(row.time) && numeric(row.time)! <= now + 60000 ? numeric(row.time)! : now,source:'OKX · '+networks[chainId()].name,
    price:value('price',previous?.price),marketCap:value('marketCap',previous?.marketCap),
    volume24h:value('volume',previous?.volume24h),txs24h:value('txs',previous?.txs24h),
    buys24h:value('txsBuy',previous?.buys24h),sells24h:value('txsSell',previous?.sells24h),
    countsAt:Object.hasOwn(row,'txsBuy')?now:previous?.countsAt,
    holders:value('holders',previous?.holders),liquidity:value('liquidity',previous?.liquidity),
    change24h:value('change',previous?.change24h),
    kind:stock?'stock':quoteSymbols.has(symbol.toUpperCase())?'quote':previous?.kind??'candidate',
    match:stock?null:matchStock(symbol,String(row.tokenName??previous?.name??symbol),buildTickerSet(stocks.map(s=>s.stockCode))) };
}
function saveAsset(row:any) {
  const key=addr(row.tokenContractAddress); if (!key) return null;
  const asset=normalizeXAsset(row,db().get<XAsset>('asset',key)); if (!asset) return null;
  db().put('asset',key,asset);
  if (asset.price != null && Object.hasOwn(row,'price')) db().sample(key,asset.price,asset.marketCap,asset.fieldTimes?.price??asset.updatedAt);
  return asset;
}
async function metadata(address:string, force=false) {
  const old=db().get<XAsset>('asset',address);
  if (!force && old && fresh(old.updatedAt,300000)) return old;
  const data=await okxGet('/api/v6/dex/market/token/search',{chains:chainId(),search:address});
  const row=items(data).find(r=>String(r.chainIndex)===chainId() && addr(r.tokenContractAddress)===address);
  if (!row) throw new Error('OKX 未返回该链/地址的元数据');
  return saveAsset(row)!;
}
async function scanPools(address:string, block:string) {
  const old=db().get<Scan>('scan',address);
  const result:Scan={token:address,checkedAt:Date.now(),poolCount:0,status:'ready',error:null};
  try {
    const data=await okxGet('/api/v6/dex/market/token/top-liquidity',{chainIndex:chainId(),tokenContractAddress:address});
    if (!Array.isArray(data)) throw new Error('Invalid pool response');
    result.poolCount=data.length;
    for (const pool of data) {
      const poolAddress=addr(pool.poolAddress);
      if (!poolAddress) { result.status='partial'; continue; }
      try {
        const checked=await verifyXPool(poolAddress,catalog().tokens,call,block);
        const p={pool:poolAddress,queryToken:address,checkedAt:Date.now(),block:parseInt(block,16),
          token0:checked.token0,token1:checked.token1,liquidityUsd:numeric(pool.liquidityUsd),protocol:String(pool.protocolName??''),feePct:numeric(String(pool.liquidityProviderFeePercent??'').replace('%','')),amounts:Array.isArray(pool.liquidityAmount)?pool.liquidityAmount:[],name:String(pool.pool??'')};
        db().put('pool',poolAddress,p);
        if (!checked.relation) continue;
        const r=checked.relation;
        // A top-pool response must actually contain the queried asset or its verified wrapper.
        if (![checked.token0,checked.token1,r.stock.tokenContractAddress.toLowerCase()].includes(address)) continue;
        let asset=await metadata(r.token);
        if (asset.kind==='quote'||asset.kind==='stock'||asset.kind==='wrapped_stock') continue;
        const id=`${chainId()}:${poolAddress}:${r.token}:${r.stock.tokenContractAddress.toLowerCase()}`;
        const previous=db().get<XRelation>('relation',id);
        let stockBalance:string|null=null;
        try { stockBalance=BigInt(await call(r.stockSide,'0x70a08231'+poolAddress.slice(2).padStart(64,'0'),block)).toString(); } catch {}
        const relation:XRelation={id,chainId:chainId(),token:r.token,stock:r.stock.tokenContractAddress.toLowerCase(),stockSide:r.stockSide,
          ticker:r.stock.stockCode,pool:poolAddress,token0:checked.token0,token1:checked.token1,wrapper:r.wrapper,
          protocol:p.protocol,firstSeen:previous?.firstSeen??Date.now(),checkedAt:Date.now(),block:p.block,
          liquidityUsd:p.liquidityUsd,liquidityAt:p.checkedAt,stockBalance,feePct:numeric(String(pool.liquidityProviderFeePercent??'').replace('%','')),amounts:Array.isArray(pool.liquidityAmount)?pool.liquidityAmount:[],status:'verified',error:null};
        db().put('relation',id,relation);
        if (!previous || previous.status==='invalid') db().event(id+':verified:'+relation.checkedAt,r.token,
          {kind:'verified',symbol:asset.symbol,ticker:relation.ticker,pool:poolAddress,label: r.wrapper?'包装股票配对已核验':'股票直接配对已核验'});
      } catch (error) {
        if(/request (allowance|budget) exhausted/.test(String(error)))throw error;
        result.status='partial';
        db().put('pool-error',poolAddress,{pool:poolAddress,checkedAt:Date.now(),reason:'池接口、包装关系或元数据未完成核验'});
      }
    }
  } catch(error) { if(/request (allowance|budget) exhausted/.test(String(error)))throw error;result.status='error'; result.error=error instanceof Error?error.message:'Pool scan failed'; result.poolCount=old?.poolCount??0; }
  db().put('scan',address,result);
}

async function refreshProfile(asset:XAsset) {
  const data=await okxGet('/api/v6/dex/market/token/advanced-info',{chainIndex:chainId(),tokenContractAddress:asset.token});
  const row=Array.isArray(data)?data[0]:data;
  if (!row || String(row.chainIndex)!==chainId() || addr(row.tokenContractAddress)!==asset.token) throw new Error('Advanced info chain/address mismatch');
  asset.risk={level:String(row.riskControlLevel??'0'),tags:Array.isArray(row.tokenTags)?row.tokenTags.map(String):[],top10:numeric(row.top10HoldPercent),checkedAt:Date.now()};
  asset.profile=row.stockProfile?{companyName:String(row.stockProfile.companyName??''),industry:String(row.stockProfile.industry??''),exchange:String(row.stockProfile.exchange??''),stockCode:String(row.stockProfile.stockCode??'')}:null;
  asset.detailAt=Date.now(); db().put('asset',asset.token,asset);
}

export function normalizeTrade(row:any, token:string) {
  const t=numeric(row.time);
  if (String(row.chainIndex)!==chainId() || addr(row.tokenContractAddress)!==token || !row.id || !t || !['buy','sell'].includes(row.type)) return null;
  return {id:String(row.id),t,type:row.type,price:numeric(row.price),volume:numeric(row.volume),
    user:addr(row.userAddress),hash:/^0x[\da-f]{64}$/i.test(row.txHashUrl)?row.txHashUrl:null,
    dex:String(row.dexName??''),source:'OKX trades',providerFiltered:String(row.isFiltered??'0')!=='0'};
}
async function refreshTrades(asset:XAsset) {
  // Incremental pagination stops at a stored ID. Keep explicit gaps when the
  // bounded batch cannot bridge to the last scan; never claim full history.
  let after='', reached=false, oldest=Date.now(), lastCursor='';
  for(let page=0;page<3;page++) {
    const data=await okxGet('/api/v6/dex/market/trades',{chainIndex:chainId(),tokenContractAddress:asset.token,limit:'100',...(after?{after}:{})});
    if (!Array.isArray(data)) throw new Error('Invalid trades response');
    const normalized=data.map(r=>normalizeTrade(r,asset.token)).filter(Boolean) as any[];
    if (normalized.some(r=>db().hasTrade(asset.token,r.id))) reached=true;
    db().trades(asset.token,normalized);
    for(const row of normalized) oldest=Math.min(oldest,row.t);
    if(data.length<100) reached=true;
    lastCursor=String(data.at(-1)?.id??'');
    if(reached || !lastCursor || lastCursor===after) break;
    after=lastCursor;
  }
  asset.oldestTradeAt=Math.min(asset.oldestTradeAt??oldest,oldest);
  const gaps=asset.tradeGaps??[];
  if(!reached && lastCursor && !gaps.includes(lastCursor))gaps.push(lastCursor);
  asset.tradeGaps=gaps;
  asset.gapDetected=gaps.length>0;
  // Resume one older gap per cycle, independently of the newest transactions.
  if(gaps.length) {
    const cursor=gaps[0];
    const data=await okxGet('/api/v6/dex/market/trades',{chainIndex:chainId(),tokenContractAddress:asset.token,limit:'100',after:cursor});
    if(!Array.isArray(data))throw new Error('Invalid backfill response');
    const rows=data.map(r=>normalizeTrade(r,asset.token)).filter(Boolean) as any[];
    const bridged=data.length<100||rows.some(r=>db().hasTrade(asset.token,r.id));
    db().trades(asset.token,rows);
    for(const row of rows)asset.oldestTradeAt=Math.min(asset.oldestTradeAt,row.t);
    const next=String(data.at(-1)?.id??'');
    if(bridged||!next)gaps.shift();
    else if(next!==cursor)gaps[0]=next;
  }
  asset.tradeGaps=gaps;
  asset.gapDetected=gaps.length>0;
  asset.tradeCoverage=asset.gapDetected?'partial-gap': 'observed';
  asset.tradeCursor=lastCursor;asset.tradeAt=Date.now();asset.error=null;
  db().put('asset',asset.token,asset);
}

// Tokens OKX no longer prices would otherwise dominate every oldest-first
// round. Track consecutive misses and cool them down for an hour.
const quoteMiss=new Map<string,number>();
function coolStale(tokens:string[],answered:Set<string>) {
  for(const t of tokens){
    if(answered.has(t)) quoteMiss.delete(t);
    else quoteMiss.set(t,(quoteMiss.get(t)??0)+1);
  }
  if(quoteMiss.size>800) for(const [t] of quoteMiss) { quoteMiss.delete(t); if(quoteMiss.size<=600) break; }
}
export async function refreshQuotes(assets:XAsset[]) {
  // One malformed address rejects the whole batch (OKX 51000), so validate
  // locally and cap batch size.
  const tokens=[...new Set(assets.map(a=>a.token).filter(t=>/^0x[0-9a-fA-F]{40}$/.test(t)))];
  if (!tokens.length) return;
  const answered=new Set<string>();
  for(let i=0;i<tokens.length;i+=50) {
    try {
      const data=await okxPost('/api/v6/dex/market/price-info',tokens.slice(i,i+50).map(t=>({chainIndex:chainId(),tokenContractAddress:t})),{skipRound:true,urgent:true});
      if (!Array.isArray(data)) throw new Error('Invalid quote response');
      for(const row of data) { const a=addr(row.tokenContractAddress); if(a) answered.add(a); }
      for(const row of data) {
        const old=db().get<XAsset>('asset',addr(row.tokenContractAddress)??'');
        if (!old || String(row.chainIndex)!==chainId()) continue;
        const mapped={...row};
        for(const [raw,target] of [['volume24H','volume'],['txs24H','txs'],['priceChange24H','change']])if(Object.hasOwn(row,raw))mapped[target]=row[raw];
        const updated=saveAsset(mapped);
        if(updated) db().put('quote-time',updated.token,{at:numeric(row.time)??Date.now()});
      }
    } catch (e) {
      // One rejected chunk must not starve the rest of the round.
      console.error(`[quotes] chunk ${i}-${i+50} skipped: ${e instanceof Error ? e.message : e}`);
    }
  }
  coolStale(tokens, answered);
}

// Fast price lane: one batched price-info call per round keeps displayed
// meme/stock prices under a minute stale without touching scan budgets.
export async function refreshLiveQuotes(){
  // Pin the scope to X Layer: the dashboard loop rotates chains via
  // AsyncLocalStorage, and db()/chainId() follow it.
  await inNetwork('196', okxState, async () => {
    const relationAssets=new Set(db().all<XRelation>('relation').filter(r=>r.status!=='invalid').flatMap(r=>[r.token,r.stock]));
    const basketAssets=new Set(db().all<any>('basket').flatMap(b=>b.members.map((m:any)=>m.token)));
    const work=db().all<XAsset>('asset').filter(a=>a.kind==='stock'||(a.kind==='candidate'&&(relationAssets.has(a.token)||basketAssets.has(a.token))))
      .filter(a=>(quoteMiss.get(a.token)??0)<3)
      .sort((a,b)=>(a.fieldTimes?.price??0)-(b.fieldTimes?.price??0)).slice(0,150);
    if(work.length)await refreshQuotes(work);
  });
}
let sideTurn=0;
export async function refreshSideQuotes(){
  const chains=['56','4663'];
  const chain=chains[sideTurn%chains.length];sideTurn++;
  await inNetwork(chain, okxState, async () => {
    const work=db().all<XAsset>('asset').filter(a=>a.kind==='stock'||a.kind==='candidate')
      .sort((a,b)=>(a.fieldTimes?.price??0)-(b.fieldTimes?.price??0)).slice(0,100);
    if(work.length)await refreshQuotes(work);
  });
}

export async function refreshXLayer() {
  if(runState().running) return;
  if(!process.env.OKX_API_KEY) {runState().status='unconfigured';return;}
  if(!catalog().updatedAt || !catalog().tokens.length) {runState().status='waiting-rwa';return;}
  runState().running=true;runState().yielded=false;runState().stages={};
  const stage=async(name:string,limit:number,action:()=>Promise<void>)=>{
    try{await withRequestAllowance(limit,action);runState().stages[name]={status:'completed',at:Date.now()};}
    catch(error){runState().yielded=true;runState().stages[name]={status:/request (allowance|budget) exhausted/.test(String(error))?'budget-wait':'error',at:Date.now()};}
  };
  try {
    if(BigInt(await xRpc('eth_chainId',[])).toString()!==chainId()) throw new Error('RPC chain identity mismatch');
    const block=await xRpc('eth_blockNumber',[]);
    // Reference prices are persisted independently from the moving RWA table.
    for(const stock of catalog().tokens) {
      db().put('stock',stock.tokenContractAddress.toLowerCase(),stock);
      if(!db().get('asset',stock.tokenContractAddress.toLowerCase()))saveAsset({...stock,time:catalog().updatedAt,volume:stock.volume24h});
      if(stock.price && fresh(catalog().updatedAt)) db().sample(stock.tokenContractAddress.toLowerCase(),stock.price,stock.marketCap,catalog().updatedAt!);
    }
    // Recheck old pool endpoints even if they leave OKX's top five listing.
    const relations=db().all<XRelation>('relation').sort((a,b)=>a.checkedAt-b.checkedAt).filter(r=>!fresh(r.checkedAt,600000)).slice(0,16);
    for(const r of relations) {
      try {
        const checked=await verifyXPool(r.pool,catalog().tokens,call,block);
        const valid=checked.relation?.token===r.token && checked.relation.stock.tokenContractAddress.toLowerCase()===r.stock;
        if(!valid && r.status!=='invalid') db().event(`${r.id}:invalid:${Date.now()}`,r.token,{kind:'invalid',ticker:r.ticker,pool:r.pool,label:'配对证据变化，需复核'});
        r.status=valid?'verified':'invalid';r.error=null;r.checkedAt=Date.now();r.block=parseInt(block,16);
      }catch{r.error='本次复核失败，保留历史证据';}
      db().put('relation',r.id,r);
    }
    const savedAssets=db().all<XAsset>('asset');
    const scans=new Map(db().all<Scan>('scan').map(s=>[s.token,s]));
    await stage('activity',8,async()=>{
    const all=db().all<XAsset>('asset').filter(a=>a.kind==='candidate');
    const related=new Set(db().all<XRelation>('relation').map(r=>r.token));
    const selected=all.sort((a,b)=>Number(related.has(b.token))-Number(related.has(a.token)) || (a.tradeAt??0)-(b.tradeAt??0)).slice(0,1);
    // Also service oldest waiting candidates so a popular relation cannot starve them.
    const oldest=all.sort((a,b)=>(a.tradeAt??0)-(b.tradeAt??0)).slice(0,1);
    const work=[...new Map([...selected,...oldest].map(a=>[a.token,a])).values()];
    await refreshQuotes(work);
    for(let asset of work) {
      asset=db().get<XAsset>('asset',asset.token)!;
      try {
        if(!fresh(asset.fieldTimes?.holders,3600000))asset=await metadata(asset.token,true);
        await refreshTrades(asset);
        if(!fresh(asset.detailAt,3600000)) await refreshProfile(asset);
        if(asset.match && !fresh(scans.get(asset.token)?.checkedAt,1800000)) await scanPools(asset.token,block);
      }catch(error) {if(/request (allowance|budget) exhausted/.test(String(error)))throw error;asset.error=error instanceof Error?error.message:'Update failed';db().put('asset',asset.token,asset);}
    }
    });
    await stage('pool-maintenance',6,async()=>{
    const priorityStocks=[...new Set(db().all<XRelation>('relation').filter(r=>r.status==='verified'&&(r.liquidityUsd??0)>=1000).sort((a,b)=>(a.liquidityAt??0)-(b.liquidityAt??0)).map(r=>r.stock))].slice(0,2);
    for(const address of priorityStocks)await scanPools(address,block);
    });
    await stage('discovery',4,async()=>{
    if(!fresh(runState().hotAt,300000)) {
      try {
        const data=await okxGet('/api/v6/dex/market/token/hot-token',{chainIndex:chainId(),rankingType:'4',rankingTimeFrame:'4',limit:'40'});
        const rows=items(data);
        if(!Array.isArray(data)&&!Array.isArray(data?.list)) throw new Error('Invalid discovery response');
        for(const row of rows) saveAsset(row);
        runState().hotAt=Date.now();
      } catch(error) {db().put('source','hot',{status:'error',at:Date.now(),error:String(error)});}
    }
    // Every catalog asset eventually gets a turn; highest volume breaks ties.
    const stocks=[...catalog().tokens].sort((a,b)=>(scans.get(a.tokenContractAddress.toLowerCase())?.checkedAt??0)-(scans.get(b.tokenContractAddress.toLowerCase())?.checkedAt??0)||(b.volume24h??0)-(a.volume24h??0)).slice(0,2);
    for(const stock of stocks) {
      const address=stock.tokenContractAddress.toLowerCase();
      await scanPools(address,block);
      try {const a=await metadata(address);if(!fresh(a.detailAt,86400000)) await refreshProfile(a);}catch{}
    }
    });
    runState().status=runState().yielded?'partial':'ready';runState().updatedAt=Date.now();runState().error=null;
    db().put('source','pipeline',{...runState(),running:false});
  }catch(error) {
    const message=error instanceof Error?error.message:'Collection failed';
    if(/request (allowance|budget) exhausted/.test(message)){runState().status='partial';runState().yielded=true;runState().error=null;db().put('source','pipeline',{...runState(),running:false});}
    else{runState().status=runState().updatedAt?'stale':'error';runState().error=message;}
  }
  finally {
    runState().running=false;
    const latest=Math.max(0,...db().all<XAsset>('asset').map(a=>Math.max(a.tradeAt??0,...Object.values(a.fieldTimes??{}))),...db().all<XRelation>('relation').map(r=>r.checkedAt));
    runState().lastProgressAt=latest||null;
    db().put('source','pipeline',{...runState(),running:false});
  }
}

export function groupCandidates(assets:XAsset[],relations:XRelation[]) {
  const groups=new Map<string,XAsset[]>();
  for(const a of assets.filter(a=>a.kind==='candidate')) {
    const key=a.symbol.normalize('NFKC').trim().toLowerCase().replace(/\s+/g,' ');
    groups.set(key,[...(groups.get(key)??[]),a]);
  }
  const score=(a:XAsset)=> (relations.some(r=>r.token===a.token&&(r.chainId??'196')===a.chain&&r.status==='verified')?1e9:0)
    +(a.match?1e6:0)+Math.log10(1+Math.max(0,a.volume24h??0))*100;
  return [...groups.values()].map(list=>{list.sort((a,b)=>score(b)-score(a));return {symbol:list[0].symbol,count:list.length,members:list};})
    .sort((a,b)=>score(b.members[0])-score(a.members[0]));
}
function sectorViews(assets:XAsset[],relations:XRelation[]) {
  return Object.entries(sectors).map(([name,tickers])=>{
    const addresses=[...new Set(relations.filter(r=>r.status==='verified'&&tickers.includes(r.ticker)&&(r.liquidityUsd??0)>=1000).map(r=>r.token))];
    // Relationship membership is durable. With a quota-friendly five-minute
    // pipeline, each candidate quote is rotated rather than refreshed every
    // round, so do not hide a previously observed component merely because its
    // latest quote is older than the display freshness window. Keep the index
    // value null until every base component has a fresh quote.
    const members=assets.filter(a=>addresses.includes(a.token)&&a.price!=null&&a.marketCap!=null&&a.price>0&&a.marketCap>0);
    const freshMembers=members.filter(a=>fresh(a.fieldTimes?.price??a.updatedAt));
    let base=db().get<any>('basket',name);
    if(!base&&members.length>=3&&freshMembers.length>=3) {
      base={baseAt:Date.now(),members:freshMembers.map(a=>({token:a.token,basePrice:a.price,baseCap:a.marketCap})),version:1};
      db().put('basket',name,base);
    }
    if(!base) return {sector:name,value:null,members:members.length,reason:members.length>=3&&freshMembers.length<3?`已具备 ${members.length}/3 个合格成分；等待 ${members.length-freshMembers.length} 个行情更新`:`已具备 ${members.length}/3 个合格成分`,components:members.map(a=>({token:a.token,symbol:a.symbol}))};
    const complete=base.members.every((m:any)=>members.some(a=>a.token===m.token));
    const freshComplete=base.members.every((m:any)=>freshMembers.some(a=>a.token===m.token));
    const metric=complete&&freshComplete?basketIndex(base.members.map((m:any)=>({...m,price:freshMembers.find(a=>a.token===m.token)!.price}))):null;
    if(metric?.value!=null){const at=Math.min(...base.members.map((m:any)=>{const a=freshMembers.find(a=>a.token===m.token)!;return a.fieldTimes?.price??a.updatedAt;}));db().put('basket-last',name,{value:metric.value,at});db().sample('basket:'+name,metric.value,null,at);}
    const last=db().get<any>('basket-last',name);
    return {sector:name,value:metric?.value??null,lastValue:last?.value??null,lastAt:last?.at??null,history:db().samples('basket:'+name),chainId:chainId(),members:base.members.length,baseAt:base.baseAt,
      reason:!complete?'成分过期或不再合格，指数暂停':freshComplete?'OKX 市值权重 · 固定首版成分':`固定首版成分；等待 ${base.members.filter((m:any)=>!freshMembers.some(a=>a.token===m.token)).length} 个行情更新`,
      components:base.members.map((m:any)=>({token:m.token,symbol:assets.find(a=>a.token===m.token)?.symbol??m.token,weight:m.baseCap/base.members.reduce((n:number,x:any)=>n+x.baseCap,0)}))};
  });
}
export function xLayerState() {
  const assets=db().all<XAsset>('asset').map(a=>quoteSymbols.has(a.symbol.toUpperCase())?{...a,kind:'quote' as const}:a);
  const candidateIds=new Set(assets.filter(a=>a.kind==='candidate').map(a=>a.token));
  const relations=db().all<XRelation>('relation').filter(r=>candidateIds.has(r.token)), scans=db().all<Scan>('scan');
  const verified=relations.filter(r=>r.status==='verified'&&fresh(r.checkedAt,3600000));
  const valued=verified.filter(r=>r.liquidityUsd!=null&&fresh(r.liquidityAt??r.checkedAt,3600000));
  const old=db().get<any>('source','pipeline');
  return { ...runState(),updatedAt:runState().updatedAt??old?.updatedAt??null,
    coverage:{catalog:catalog().tokens.length,scanned:scans.filter(s=>catalog().tokens.some(t=>t.tokenContractAddress.toLowerCase()===s.token)).length,
      errors:scans.filter(s=>s.status!=='ready').length,pools:db().all('pool').length,
      scope:'OKX 每资产流动性前 5 池；链上核验 token0/token1 与 asset()/underlying()；不覆盖无独立池地址的池接口'},
    assets,relations,pools:db().all('pool'),signals:db().events(undefined,100).filter(event=>candidateIds.has(event.asset)),groups:groupCandidates(assets,relations),sectors:sectorViews(assets,relations),
    metrics:{verifiedPools:verified.length,verifiedAssets:new Set(verified.map(r=>r.token)).size,
      actionableAssets:new Set(verified.filter(r=>(r.liquidityUsd??0)>=1000&&fresh(r.liquidityAt??r.checkedAt,3600000)).map(r=>r.token)).size,
      pairedLiquidityUsd:valued.length?valued.reduce((n,r)=>n+r.liquidityUsd!,0):null,
      liquidityCoverage:{valued:valued.length,total:verified.length},
      heat:{value:null,reason:'股票实际锁定量及同比持有人输入未齐备；交易活跃度单列'},
      stockFlow:{value:null,reason:'待归集配对池 LP 加入/退出事件；流动性余额不作为净流量'}} };
}
export function xLayerDetail(address:string) {
  const token=addr(address); if(!token) return null;
  let asset=db().get<XAsset>('asset',token);
  const stock=catalog().tokens.find(s=>s.tokenContractAddress.toLowerCase()===token)??db().get<RwaToken>('stock',token);
  if(!asset&&stock) asset=normalizeXAsset({...stock,volume:stock.volume24h})!;
  if(!asset) return null;
  const relations=db().all<XRelation>('relation').filter(r=>r.token===token||r.stock===token);
  const recent=db().recentTrades(token);
  return {asset,stock,relations,trades:recent,activity:db().activity(token,Date.now()-86400000),samples:db().samples(token),
    events:db().events(token),pools:db().all<any>('pool').filter(p=>p.token0===token||p.token1===token),scan:db().get<Scan>('scan',token),
    stocks:relations.map(r=>({relation:r,stock:catalog().tokens.find(s=>s.tokenContractAddress.toLowerCase()===r.stock),profile:db().get<XAsset>('asset',r.stock)?.profile})),
    analysis:{method:'规则解读 · 链上证据与 OKX 行情',
      conclusion:relations.some(r=>r.status==='verified')?'已核验股票配对；请结合证据时效和资金规模判断。':asset.match?`名称命中 ${asset.match.ticker}，尚无已核验资金关系。`:'当前覆盖范围尚未核验股票配对关系。',
      correlation:{value:null,reason:'尚未验证独立股票现货行情与独立 Meme 价格，避免配对计价造成机械相关'},
      capture:{value:null,reason:'当前仅前 5 池，缺少同口径股票侧流动性分母'},
      safety:'OKX 风险标签为辅助信息；配对核验不代表安全认证。'},
  };
}

export function networkEvents(before?:{t:number;id:string},limit=50){return db().eventsPage(before,limit);}
