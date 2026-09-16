import { rpc } from "./rpc";

export const FOUR_MEME_FACTORY = "0x5c952063c7fc8610FFDB798152D69F0B9550762b";

const TOKEN_CREATE = "0x396d5e902b675b032348d3d2e9517ee8f0c4a926603fbc075d3d282ff00cad20";
const TOKEN_PURCHASE = "0x7db52723a3b2cdd6164364b3b766e65e540d7be48ffa89582956d8eaebe62942";
const TOKEN_SALE = "0x0a5575b3648bae2210cee56bf33254cc1ddfbc7bf637c0af2ac18b14fb1bae19";
const FACTORY_TOPICS = [TOKEN_CREATE, TOKEN_PURCHASE, TOKEN_SALE];

export interface EventHistogram {
  total: number;
  fromBlock: number;
  toBlock: number;
  byTopic: { topic: string; count: number }[];
  sampleTx: string;
}

export async function recentActivity(fromBlocks = 500): Promise<EventHistogram> {
  const latest = await rpc.blockNumber();
  const logs = await rpc.getLogs(FOUR_MEME_FACTORY, [FACTORY_TOPICS], latest - fromBlocks, "latest");
  const counts = new Map<string, number>();
  for (const l of logs) counts.set(l.topics[0], (counts.get(l.topics[0]) ?? 0) + 1);
  return {
    total: logs.length,
    fromBlock: latest - fromBlocks,
    toBlock: latest,
    byTopic: [...counts.entries()].map(([topic, count]) => ({ topic, count })).sort((a, b) => b.count - a.count),
    sampleTx: logs.at(-1)?.transactionHash ?? "",
  };
}

export interface NewToken {
  token: string;
  creator: string;
  supply: number;
  name: string;
  symbol: string;
  block: number;
  ts: number | null;
  buys: number;
  sells: number;
  volumeRaw: bigint;
  lastPriceWei: bigint;
}

export interface TradeEvent {
  kind: "buy" | "sell";
  token: string;
  symbol: string;
  bnb: number;
  block: number;
  tsSec: number;
}

export interface Funnel {
  created: number;
  hasTrade: number;
  active: number;
}

export interface RadarData {
  fromBlock: number;
  toBlock: number;
  tokens: NewToken[];
  // Full window is retained for detail pages and lead persistence; `tokens`
  // remains capped for the fast radar table.
  allTokens?: NewToken[];
  trades: TradeEvent[];
  funnel: Funnel;
  tradeCount: number;
  updatedAt: number;
}

const slotAddr = (h: string, i: number) => "0x" + h.slice(2 + i * 64 + 24, 2 + (i + 1) * 64);
const slotUint = (h: string, i: number) => BigInt("0x" + h.slice(2 + i * 64, 2 + (i + 1) * 64));

async function getFactoryLogs(from: number, to: number): Promise<any[]> {
  // Public BSC RPCs frequently reject a 3,000-block eth_getLogs request with
  // "limit exceeded". Small deterministic chunks keep radar refreshes
  // updating instead of leaving the previous window frozen.
  const chunk = 500;
  const logs: any[] = [];
  for (let start = from; start <= to; start += chunk) {
    const end = Math.min(to, start + chunk - 1);
    logs.push(...(await rpc.getLogs(FOUR_MEME_FACTORY, [FACTORY_TOPICS], start, end)));
  }
  return logs.sort((a, b) => {
    const block = parseInt(a.blockNumber, 16) - parseInt(b.blockNumber, 16);
    if (block) return block;
    const tx = parseInt(a.transactionIndex ?? "0x0", 16) - parseInt(b.transactionIndex ?? "0x0", 16);
    return tx || parseInt(a.logIndex ?? "0x0", 16) - parseInt(b.logIndex ?? "0x0", 16);
  });
}

function decodeString(head: string, slotIndex: number): string {
  const offset = Number(slotUint(head, slotIndex));
  const abs = 2 + offset * 2;
  if (abs + 64 > head.length) return "";
  const len = Number(slotUint(head, offset / 32));
  const bytes = head.slice(abs + 64, abs + 64 + len * 2);
  const hex = bytes.padEnd(Math.ceil(len / 32) * 32 * 2, "0");
  try {
    return Buffer.from(hex, "hex").slice(0, len).toString("utf8");
  } catch {
    return "";
  }
}

export async function fetchRadar(windowBlocks = 2000): Promise<RadarData> {
  const latest = await rpc.blockNumber();
  const from = latest - windowBlocks;
  const logs = await getFactoryLogs(from, latest);
  const nowSec = Date.now() / 1000;
  const estTs = (block: number) => Math.round(nowSec - (latest - block) * 0.75);

  const tokens = new Map<string, NewToken>();
  const tsCache = new Map<number, number>();
  const trades: TradeEvent[] = [];
  let tradeCount = 0;

  for (const l of logs) {
    const data: string = l.data;
    const block = parseInt(l.blockNumber, 16);
    switch (l.topics[0]) {
      case TOKEN_CREATE: {
        const token = slotAddr(data, 0).toLowerCase();
        tokens.set(token, {
          token,
          creator: slotAddr(data, 1),
          supply: Number(slotUint(data, 5)) / 1e18,
          name: decodeString(data, 3),
          symbol: decodeString(data, 4),
          block,
          ts: null,
          buys: 0,
          sells: 0,
          volumeRaw: 0n,
          lastPriceWei: 0n,
        });
        break;
      }
      case TOKEN_PURCHASE:
      case TOKEN_SALE: {
        tradeCount++;
        const kind = l.topics[0] === TOKEN_PURCHASE ? "buy" : "sell";
        const addr = slotAddr(data, 1).toLowerCase();
        const t = tokens.get(addr);
        if (t) {
          if (kind === "buy") t.buys++;
          else t.sells++;
          t.volumeRaw += slotUint(data, 4);
          t.lastPriceWei = slotUint(data, 2);
        }
        trades.push({
          kind,
          token: addr,
          symbol: t?.symbol || addr.slice(0, 6) + "…" + addr.slice(-4),
          bnb: Number(slotUint(data, 4)) / 1e18,
          block,
          tsSec: estTs(block),
        });
        break;
      }
    }
  }

  const list = [...tokens.values()].sort((a, b) => b.block - a.block);
  const uniqueBlocks = [...new Set(list.map((t) => t.block))].slice(0, 30);
  for (const b of uniqueBlocks) {
    try {
      tsCache.set(b, await rpc.getBlockTimestamp(b));
    } catch {
      break;
    }
  }
  // A block timestamp is useful context, but a transient RPC failure should
  // not turn a newly-created token into an apparently undated record. The
  // fallback uses the same BSC block-time estimate used by the trade ticker.
  for (const t of list) t.ts = tsCache.get(t.block) ?? estTs(t.block);

  const funnel: Funnel = {
    created: list.length,
    hasTrade: list.filter((t) => t.buys + t.sells > 0).length,
    active: list.filter((t) => t.buys + t.sells >= 5).length,
  };

  return {
    fromBlock: from,
    toBlock: latest,
    tokens: list.slice(0, 100),
    allTokens: list,
    trades: trades.slice(-40).reverse(),
    funnel,
    tradeCount,
    updatedAt: Date.now(),
  };
}
