import { rpc } from './rpc';
import { padAddr } from './erc20';
import { V2_FACTORY } from './pancake';
export interface PoolEvidence { pool: string; meme: string; stock: string; stockSymbol: string; checkedAt: number; }
export interface Verification { status: 'verified' | 'not_found' | 'error'; checkedAt: number; scope: string; pools: PoolEvidence[]; }
const address = (raw: string) => /^0x[\da-f]{64}$/i.test(raw) ? '0x' + raw.slice(-40).toLowerCase() : null;
export async function verifyDirectPools(meme: string, stocks: {addr: string; symbol: string}[], call = rpc.call.bind(rpc)): Promise<Verification> {
  const pools: PoolEvidence[] = [];
  let failed = false;
  for (const stock of stocks.slice(0, 4)) {
    try {
      const pool = address(await call(V2_FACTORY, '0xe6a43905' + padAddr(meme) + padAddr(stock.addr)));
      if (!pool) throw new Error('Invalid factory response');
      if (/^0x0{40}$/.test(pool)) continue;
      const t0 = address(await call(pool, '0x0dfe1681'));
      const t1 = address(await call(pool, '0xd21220a7'));
      if (!t0 || !t1 || new Set([t0, t1]).size !== 2 || ![t0,t1].includes(meme.toLowerCase()) || ![t0,t1].includes(stock.addr.toLowerCase())) throw new Error('Pool token mismatch');
      pools.push({pool, meme, stock: stock.addr, stockSymbol: stock.symbol, checkedAt: Date.now()});
    } catch { failed = true; }
  }
  return { status: pools.length ? 'verified' : failed ? 'error' : 'not_found', checkedAt: Date.now(),
    scope: 'BSC PancakeSwap V2 · 同股票代码候选，最多 4 个；不覆盖其他 DEX 或 V3，不验证流动性和安全性', pools };
}
