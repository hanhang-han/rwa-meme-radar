import { createHmac } from 'node:crypto';
import { existsSync } from 'node:fs';
import { okxGet } from './okx-client';
import { readSnapshot, writeSnapshot } from './snapshot';
import { canonicalStockCode } from './stock-identity';
import { ResearchStore } from './research-store';

export interface RwaToken {
  chainIndex: string; tokenContractAddress: string; tokenSymbol: string;
  tokenName: string; stockCode: string; issuer: string; price: number | null;
  stockPrice: number | null; volume24h: number | null; marketCap: number | null;
  change24h?: number | null; tokenToAssetRatio?: number | null;
  quoteAt?: number | null;
  logoUrl?:string; reportedStockCode?:string; stockCodeSource?:string;
  stockCodeSourceUrl?:string|null;
}
export const okxState: { status: string; updatedAt: number | null; tokens: RwaToken[]; error: string | null } = {
  status: 'unconfigured', updatedAt: null, tokens: [], error: null,
};
export function numeric(v: unknown): number | null {
  if (v == null || (typeof v === 'string' && v.trim() === '') || (typeof v !== 'string' && typeof v !== 'number')) return null;
  const n = Number(v); return Number.isFinite(n) ? n : null;
}
export function signature(timestamp: string, path: string, secret: string): string {
  return createHmac('sha256', secret).update(timestamp + 'GET' + path).digest('base64');
}
const snapshotRestored = new Set<string>();
const durableRestored = new Set<string>();
export const extraOkx:Record<string,typeof okxState>={};
const addressKey=(value:unknown)=>String(value??'').toLowerCase();
const validAddress=(value:unknown)=>typeof value==='string'&&/^0x[\da-f]{40}$/i.test(value);
const observationTime=(value:unknown)=>typeof value==='number'&&Number.isFinite(value)&&value>0&&value<=Date.now()?value:null;
function storedToken(row:RwaToken,chain:string,fallbackAt:number|null=null):RwaToken|null {
  if(!row||!validAddress(row.tokenContractAddress))return null;
  const declaredChain=row.chainIndex??(row as RwaToken&{chainId?:string}).chainId;
  if(declaredChain!=null&&String(declaredChain)!==chain)return null;
  return {...row,chainIndex:chain,tokenContractAddress:addressKey(row.tokenContractAddress),
    ...canonicalStockCode(row),quoteAt:Object.hasOwn(row,'quoteAt')?observationTime(row.quoteAt):observationTime(fallbackAt)};
}
function catalogueStore(chain:string){
  // Ordinary tests do not touch the working catalogue; focused persistence
  // tests opt in with their own temporary database.
  if(process.env.NODE_ENV==='test'&&!process.env.RESEARCH_DB)return null;
  const path=process.env.RESEARCH_DB||'data/research.sqlite';
  if(!existsSync(path))return null;
  return new ResearchStore(path,chain,{readOnly:true});
}
function mergeDurable(chain:string,tokens:RwaToken[],store:ResearchStore|null){
  const previous=new Map<string,RwaToken>();
  for(const row of tokens){const token=storedToken(row,chain);if(token)previous.set(addressKey(token.tokenContractAddress),token);}
  // SQLite is the durable catalogue. The JSON snapshot supplies observation
  // times for legacy stock facts that predate per-token quoteAt.
  for(const row of store?.all<RwaToken>('stock')??[]){
    const address=addressKey(row?.tokenContractAddress);
    // A discovery/firstSeen timestamp says nothing about when a price was
    // observed. Only a preserved row clock or a priced asset field can supply
    // the missing legacy quote time.
    const fallback=previous.get(address)?.quoteAt??store?.get<{fieldTimes?:{price?:number}}>('asset',address)?.fieldTimes?.price??null;
    const token=storedToken(row,chain,fallback);
    const existing=previous.get(address);
    if(token&&(!existing||(token.quoteAt??0)>=(existing.quoteAt??0)))previous.set(address,{...existing,...token});
  }
  return previous;
}
export function restoreOkxCatalogue(chain='196') {
  const target=chain==='196'?okxState:(extraOkx[chain]??={status:'starting',updatedAt:null,tokens:[],error:null});
  const path=`data/okx${chain==='196'?'':'-'+chain}.json`;
  let saved:typeof target|null=null;
  if(!snapshotRestored.has(chain)){
    snapshotRestored.add(chain);
    saved=process.env.NODE_ENV === 'test' ? null : readSnapshot<typeof target>(path);
    if (saved?.updatedAt && Array.isArray(saved.tokens)) {
      Object.assign(target, saved, {
        // The file's refresh time cannot make every retained row current.
        tokens:saved.tokens.map(token=>storedToken(token,chain)).filter((token):token is RwaToken=>!!token),
        status:saved.status==='partial'?'partial':'stale',
      });
    }
  }
  if(durableRestored.has(chain))return target;
  let store:ResearchStore|null=null;
  try {
    store=catalogueStore(chain);
    const before=JSON.stringify(target.tokens);
    const previousCount=target.tokens.length;
    target.tokens=[...mergeDurable(chain,target.tokens,store).values()];
    if(target.tokens.length>previousCount){
      target.status=target.updatedAt?'partial':'stale';
      target.error=target.updatedAt?`SQLite catalogue contains ${target.tokens.length-previousCount} token${target.tokens.length-previousCount===1?'':'s'} absent from JSON snapshot`:null;
    }else if(target.tokens.length&&(target.status==='starting'||target.status==='unconfigured'))target.status='stale';
    if(process.env.NODE_ENV!=='test'&&before!==JSON.stringify(target.tokens))writeSnapshot(path,target);
    if(store||process.env.NODE_ENV==='test'&&!process.env.RESEARCH_DB)durableRestored.add(chain);
  }catch(e){
    target.status=target.tokens.length?'stale':'error';
    target.error=e instanceof Error?e.message:'Catalogue unavailable';
  }finally{store?.close();}
  return target;
}
export async function refreshOkx(chain='196') {
  const target=restoreOkxCatalogue(chain);
  let store:ResearchStore|null=null;
  let previous=new Map<string,RwaToken>();
  let catalogueReadError:string|null=null;
  try {
    try{
      store=catalogueStore(chain);
      previous=mergeDurable(chain,target.tokens,store);
    }catch(error){
      catalogueReadError=error instanceof Error?error.message:'SQLite catalogue unavailable';
      previous=mergeDurable(chain,target.tokens,null);
    }finally{store?.close();store=null;}
    target.tokens=[...previous.values()];
    const key = process.env.OKX_API_KEY, secret = process.env.OKX_SECRET_KEY, passphrase = process.env.OKX_PASSPHRASE;
    if (!key || !secret || !passphrase) { target.status = 'unconfigured'; return; }
    const tokens: RwaToken[] = [];
    let cursor = '';
    const seen = new Set<string>();
    for (let page = 0; page < 20; page++) {
      // Although the public reference marks category as optional, the live
      // endpoint currently rejects requests without it (51000). Category 47
      // means all RWA categories, so it does not narrow the X Layer result.
      const query = new URLSearchParams({ chainIndex: chain, category: '47', limit: '100' });
      if (cursor) query.set('cursor', cursor);
      const data = await okxGet('/api/v6/dex/market/rwa/tokens', Object.fromEntries(query));
      if (!Array.isArray(data?.list)) throw new Error('OKX invalid RWA response');
      for (const r of data.list) {
        if (String(r.chainIndex) !== chain || !/^0x[\da-f]{40}$/i.test(r.tokenContractAddress)) continue;
        tokens.push({ chainIndex: chain, tokenContractAddress: r.tokenContractAddress,
          tokenSymbol: String(r.tokenSymbol ?? ''), tokenName: String(r.tokenName ?? ''),
          logoUrl:typeof r.logoUrl==='string'?r.logoUrl:undefined,
          ...canonicalStockCode({chainIndex:chain,tokenContractAddress:r.tokenContractAddress,
            tokenSymbol:String(r.tokenSymbol??''),stockCode:String(r.stockCode??'')}),
          issuer: String(r.issuer ?? ''),
          price: numeric(r.price), stockPrice: numeric(r.stockPrice),
          volume24h: numeric(r.volume24h), marketCap: numeric(r.marketCap),
          change24h: numeric(r.priceChange24H), tokenToAssetRatio: numeric(r.tokenToAssetRatio) });
      }
      cursor = data.cursor || '';
      if (!cursor) break;
      if (seen.has(cursor) || page === 19) throw new Error('OKX pagination incomplete');
      seen.add(cursor);
    }
    const observedAt=Date.now();
    const fetched=new Map(tokens.map(t=>[addressKey(t.tokenContractAddress),{...t,quoteAt:observedAt}]));
    for(const [address,token] of fetched)previous.set(address,token);
    target.tokens=[...previous.values()];
    target.updatedAt=observedAt;
    const retained=target.tokens.length-fetched.size;
    target.status=retained||catalogueReadError?'partial':'ready';
    target.error=[retained?`OKX omitted ${retained} previously catalogued stock token${retained===1?'':'s'}`:null,
      catalogueReadError?`SQLite catalogue unavailable: ${catalogueReadError}`:null].filter(Boolean).join('; ')||null;
    if (process.env.NODE_ENV !== 'test') writeSnapshot(`data/okx${chain==='196'?'':'-'+chain}.json`, target);
  } catch (e) {
    target.status = target.tokens.length ? 'stale' : 'error';
    target.error = e instanceof Error ? e.message : 'OKX unavailable';
  }
}
