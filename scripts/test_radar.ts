import { fetchRadar } from "../src/lib/fourmeme";
import { matchStock, buildTickerSet } from "../src/lib/stocks";
import { fetchAssets } from "../src/lib/backed";

async function main() {
  const assets = await fetchAssets(1);
  const tickers = buildTickerSet(assets.map((a) => a.underlyingSymbol));
  console.log(`股票代码表: ${tickers.size} 个\n`);

  const radar = await fetchRadar(2000);
  console.log(`窗口: 区块 ${radar.fromBlock} → ${radar.toBlock} (${radar.toBlock - radar.fromBlock} 块)`);
  console.log(`新币创建: ${radar.tokens.length} 个 | 交易事件: ${radar.tradeCount} 笔\n`);

  for (const t of radar.tokens.slice(0, 20)) {
    const m = matchStock(t.symbol, t.name, tickers);
    const time = t.ts ? new Date(t.ts).toLocaleTimeString("zh-CN", { hour12: false }) : "?";
    const flag = m ? ` ← 蹭名 ${m.ticker} (${m.matchType})` : "";
    console.log(`${time} ${t.symbol.padEnd(12)} ${t.name.slice(0, 28).padEnd(28)} 买${t.buys}/卖${t.sells}${flag}`);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
