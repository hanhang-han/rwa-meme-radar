import { rpc } from "../src/lib/rpc";
import { tokens } from "../src/lib/erc20";
import { findPools, readPoolPrice } from "../src/lib/pancake";
import { fetchAssets, fetchOraclePrice } from "../src/lib/backed";
import { computeSpread, SpreadResult } from "../src/lib/spread";
import { recentActivity } from "../src/lib/fourmeme";

const BNB_USD_FEED = "0x0567F2323251f0Aab15c8dFb1967E4e8A7D42aeE";
const QUOTES = { USDT: tokens.USDT, USDC: tokens.USDC, WBNB: tokens.WBNB };
const WATCH = ["SHEINx", "DJTx", "TSLAx", "AAPLx", "NVDAx"];

async function bnbUsd(): Promise<number> {
  const raw = await rpc.call(BNB_USD_FEED, "0xfeaf968c");
  return Number(BigInt("0x" + raw.slice(66, 130))) / 1e8;
}

async function main() {
  console.log("=== memedashboard 数据层原型 ===\n");

  const bnb = await bnbUsd();
  console.log(`[Chainlink] BNB/USD = $${bnb.toFixed(2)}\n`);

  const assets = await fetchAssets();
  const bscAssets = assets
    .map((a) => ({ symbol: a.symbol, addr: a.deployments.find((d) => d.network === "BinanceSmartChain")?.address }))
    .filter((a): a is { symbol: string; addr: string } => Boolean(a.addr));
  console.log(`[Backed API] 资产总数 ${assets.length}，BSC 部署 ${bscAssets.length}\n`);

  const spreads: SpreadResult[] = [];
  for (const symbol of WATCH) {
    const hit = bscAssets.find((a) => a.symbol === symbol);
    if (!hit) {
      console.log(`${symbol}: 不在资产列表`);
      continue;
    }
    const pools = await findPools(hit.addr, QUOTES);
    if (pools.length === 0) {
      const oracle = await fetchOraclePrice(symbol);
      console.log(`${symbol}: 无 AMM 池 (oracle=$${oracle?.toFixed(4) ?? "N/A"}) → 流动性走 RFQ/CEX`);
      continue;
    }
    for (const pool of pools) {
      const { priceInQuote } = await readPoolPrice(pool, hit.addr);
      const dexUsd = pool.quote === "WBNB" ? priceInQuote * bnb : priceInQuote;
      const oracle = await fetchOraclePrice(symbol);
      if (!oracle) continue;
      spreads.push(computeSpread(symbol, dexUsd, oracle, pool.kind, pool.quote));
      console.log(`${symbol} [${pool.kind}/${pool.quote}] DEX=$${dexUsd.toFixed(4)} oracle=$${oracle.toFixed(4)} spread=${((dexUsd / oracle - 1) * 100).toFixed(2)}%`);
    }
  }

  console.log("\n[four.meme] bonding curve 活跃度:");
  const act = await recentActivity(500);
  console.log(`  最近 ${act.toBlock - act.fromBlock} 块共 ${act.total} 个事件`);
  for (const t of act.byTopic.slice(0, 5)) console.log(`  ${t.topic.slice(0, 22)}… ×${t.count}`);

  console.log(`\n=== 结果: ${spreads.length} 个双价差样本 ===`);
}

main().catch((e) => {
  console.error("demo failed:", e);
  process.exit(1);
});
