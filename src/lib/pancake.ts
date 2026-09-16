import { rpc } from "./rpc";
import { padAddr, padUint, decimalsOf } from "./erc20";

export const V2_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73";
export const V3_FACTORY = "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865";
export const PAIR_CREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8350ab5d20c5f76a44f6e0";

const V3_FEES = [100, 500, 2500, 3000, 10000];

export interface PoolHit {
  kind: "V2" | `V3-${number}`;
  quote: string;
  quoteAddress: string;
  address: string;
}

export async function findPools(token: string, quotes: Record<string, string>): Promise<PoolHit[]> {
  const tasks: { key: string; kind: "V2" | `V3-${number}`; quote: string; quoteAddress: string; data: string; to: string }[] = [];
  for (const [qn, qa] of Object.entries(quotes)) {
    if (qa.toLowerCase() === token.toLowerCase()) continue;
    tasks.push({ key: "v2", kind: "V2", quote: qn, quoteAddress: qa, to: V2_FACTORY, data: "0xe6a43905" + padAddr(token) + padAddr(qa) });
    for (const fee of V3_FEES) {
      tasks.push({ key: `v3-${fee}`, kind: `V3-${fee}` as `V3-${number}`, quote: qn, quoteAddress: qa, to: V3_FACTORY, data: "0x1698ee82" + padAddr(token) + padAddr(qa) + padUint(fee) });
    }
  }
  const results = await Promise.all(tasks.map((t) => rpc.call(t.to, t.data).catch(() => "0x")));
  const hits: PoolHit[] = [];
  tasks.forEach((t, i) => {
    if (/^0x[0-9a-f]{64}$/i.test(results[i]) && BigInt(results[i]) !== 0n) hits.push({ kind: t.kind, quote: t.quote, quoteAddress: t.quoteAddress, address: "0x" + results[i].slice(26) });
  });
  return hits;
}

export interface PoolPrice {
  pool: PoolHit;
  priceInQuote: number;
  liquidityRaw?: bigint;
}

export async function readPoolPrice(pool: PoolPrice["pool"], token: string): Promise<PoolPrice> {
  const t0 = "0x" + (await rpc.call(pool.address, "0x0dfe1681")).slice(26);
  const tokenIs0 = t0.toLowerCase() === token.toLowerCase();

  if (pool.kind === "V2") {
    const reserves = await rpc.call(pool.address, "0x0902f1ac");
    const r0 = BigInt("0x" + reserves.slice(2, 66));
    const r1 = BigInt("0x" + reserves.slice(66, 130));
    const d0 = await decimalsOf(t0);
    const d1 = await decimalsOf(await token1(pool.address));
    const tokenDec = tokenIs0 ? d0 : d1;
    const quoteDec = tokenIs0 ? d1 : d0;
    const raw = tokenIs0 ? Number(r1) / Number(r0) : Number(r0) / Number(r1);
    return { pool, priceInQuote: raw * 10 ** (tokenDec - quoteDec) };
  }

  const slot0 = await rpc.call(pool.address, "0x3850c7bd");
  const sqrtP = BigInt("0x" + slot0.slice(2, 66));
  const liquidity = BigInt(await rpc.call(pool.address, "0x1a686502"));
  const t1 = await token1(pool.address);
  const d0 = await decimalsOf(t0);
  const d1 = await decimalsOf(t1);
  const price0In1 = (Number(sqrtP) / 2 ** 96) ** 2 * 10 ** (d0 - d1);
  const price = tokenIs0 ? price0In1 : 1 / price0In1;
  return { pool, priceInQuote: price, liquidityRaw: liquidity };
}

async function token1(pool: string): Promise<string> {
  return "0x" + (await rpc.call(pool, "0xd21220a7")).slice(26);
}
