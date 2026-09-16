import { createHmac } from 'node:crypto';
import { okxGet } from './okx-client';
import { readSnapshot, writeSnapshot } from './snapshot';

export interface RwaToken {
  chainIndex: string; tokenContractAddress: string; tokenSymbol: string;
  tokenName: string; stockCode: string; issuer: string; price: number | null;
  stockPrice: number | null; volume24h: number | null; marketCap: number | null;
  change24h?: number | null; tokenToAssetRatio?: number | null;
  logoUrl?:string;
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
let restored = false;
export const extraOkx:Record<string,typeof okxState>={};
export async function refreshOkx(chain='196') {
  const target=chain==='196'?okxState:(extraOkx[chain]??={status:'starting',updatedAt:null,tokens:[],error:null});
  if (!restored || chain!=='196' && !target.updatedAt) {
    restored = true;
    const saved = process.env.NODE_ENV === 'test' ? null : readSnapshot<typeof target>(`data/okx${chain==='196'?'':'-'+chain}.json`);
    if (saved?.updatedAt && Array.isArray(saved.tokens)) Object.assign(target, saved, { status: 'stale' });
  }
  const key = process.env.OKX_API_KEY, secret = process.env.OKX_SECRET_KEY, passphrase = process.env.OKX_PASSPHRASE;
  if (!key || !secret || !passphrase) { target.status = 'unconfigured'; return; }
  try {
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
          stockCode: String(r.stockCode ?? ''), issuer: String(r.issuer ?? ''),
          price: numeric(r.price), stockPrice: numeric(r.stockPrice),
          volume24h: numeric(r.volume24h), marketCap: numeric(r.marketCap),
          change24h: numeric(r.priceChange24H), tokenToAssetRatio: numeric(r.tokenToAssetRatio) });
      }
      cursor = data.cursor || '';
      if (!cursor) break;
      if (seen.has(cursor) || page === 19) throw new Error('OKX pagination incomplete');
      seen.add(cursor);
    }
    target.tokens = [...new Map(tokens.map(t => [t.tokenContractAddress.toLowerCase(), t])).values()];
    target.updatedAt = Date.now(); target.status = 'ready'; target.error = null;
    if (process.env.NODE_ENV !== 'test') writeSnapshot(`data/okx${chain==='196'?'':'-'+chain}.json`, target);
  } catch (e) {
    target.status = target.updatedAt ? 'stale' : 'error';
    target.error = e instanceof Error ? e.message : 'OKX unavailable';
  }
}
