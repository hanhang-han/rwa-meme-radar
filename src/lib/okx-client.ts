import { createHmac } from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';
import { readSnapshot, writeSnapshot } from './snapshot';
export const pause = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
let queue: Promise<unknown> = Promise.resolve();
let nextAt = 0;
let loaded = false;
type Allowance={remaining:number;parent?:Allowance};
const allowance=new AsyncLocalStorage<Allowance>();
export function withRequestAllowance<T>(limit:number,action:()=>T):T{return allowance.run({remaining:limit,parent:allowance.getStore()},action);}
const usage={day:'',daily:0,round:0,startedAt:0,completedAt:0,nextAt:0,lastError:null as string|null};
const positive=(value:string|undefined,fallback:number)=>Number.isFinite(Number(value))&&Number(value)>0?Math.floor(Number(value)):fallback;
export function collectionStatus(){return {...usage,dailyLimit:positive(process.env.OKX_DAILY_REQUEST_LIMIT,8000),roundLimit:positive(process.env.OKX_ROUND_REQUEST_LIMIT,70),intervalMs:300000};}
export function startCollection(){usage.round=0;usage.startedAt=Date.now();usage.lastError=null;usage.nextAt=0;}
export function endCollection(error:string|null=null){usage.completedAt=Date.now();usage.nextAt=Date.now()+300000;usage.lastError=error;}
function chargeRequest(){
  if(!loaded){if(process.env.NODE_ENV!=='test')Object.assign(usage,readSnapshot('data/okx-usage.json')??{}, {round:usage.round,startedAt:usage.startedAt});loaded=true;}
  const day=new Date().toISOString().slice(0,10);if(usage.day!==day){usage.day=day;usage.daily=0;}
  const limits=collectionStatus();
  const budgets:Allowance[]=[];for(let b=allowance.getStore();b;b=b.parent)budgets.push(b);
  if(budgets.some(b=>b.remaining<=0))throw new Error('OKX network request allowance exhausted');
  if(usage.daily>=limits.dailyLimit||usage.round>=limits.roundLimit)throw new Error('OKX local request budget exhausted');
  for(const budget of budgets)budget.remaining--;
  usage.daily++;usage.round++;
  if(process.env.NODE_ENV!=='test')writeSnapshot('data/okx-usage.json',usage);
}

// All workers share one rate limiter. Never send these headers to another host.
export function okxGet(endpoint: string, params: Record<string, string>): Promise<any> {
  return okxRequest(endpoint + '?' + new URLSearchParams(params), 'GET');
}
export function okxPost(endpoint: string, data: unknown): Promise<any> {
  return okxRequest(endpoint, 'POST', JSON.stringify(data));
}
function okxRequest(path: string, method: 'GET' | 'POST', body = ''): Promise<any> {
  const task = async () => {
    const key = process.env.OKX_API_KEY, secret = process.env.OKX_SECRET_KEY, passphrase = process.env.OKX_PASSPHRASE;
    if (!key || !secret || !passphrase) throw new Error('OKX credentials not configured');
    if (!path.startsWith('/api/v6/dex/')) throw new Error('Invalid OKX endpoint');
    for (let attempt = 0; attempt < 4; attempt++) {
      await pause(Math.max(0, nextAt - Date.now()));
      chargeRequest();
      const timestamp = new Date().toISOString();
      const response = await fetch('https://web3.okx.com' + path, {
        method, ...(body ? {body} : {}),
        headers: { 'Content-Type': 'application/json', 'OK-ACCESS-KEY': key, 'OK-ACCESS-PASSPHRASE': passphrase,
          'OK-ACCESS-TIMESTAMP': timestamp,
          'OK-ACCESS-SIGN': createHmac('sha256', secret).update(timestamp + method + path + body).digest('base64') },
        signal: AbortSignal.timeout(15000),
      });
      nextAt = Date.now() + 400;
      const json = await response.json().catch(() => ({}));
      if ((response.status === 429 || json.code === '50011') && attempt < 3) {
        nextAt = Date.now() + 1000 * 2 ** attempt;
        continue;
      }
      if (!response.ok || json.code !== '0') {
        // Upstream diagnostics may contain echoed request fields: expose codes only.
        throw new Error(`OKX HTTP ${response.status} · code ${String(json.code ?? 'unknown').replace(/[^\w-]/g, '').slice(0, 30)}`);
      }
      return json.data;
    }
    throw new Error('OKX rate limit');
  };
  const result = queue.then(task);
  queue = result.catch(() => {});
  return result;
}
