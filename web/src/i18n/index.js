// Bilingual dictionary ported from the legacy public/i18n.js; tr(zh,en)
// inline pairs keep the 60+ call sites unchanged.
import { reactive } from 'vue';

const DICT = {
  subtitle: ["BSC · meme × tokenized stocks", "BSC · meme × tokenized stocks"],
  bnbLabel: ["BNB/USD · Chainlink", "BNB/USD · Chainlink"],
  updated: ["页面同步", "Synced"],
  refresh: ["立即刷新", "Refresh now"],
  health: ["数据源健康", "Data source health"],
  paused: ["已暂停", "Paused"],

  kAssets: ["监控 xStocks", "xStocks tracked"],
  kAssetsNote: ["BSC 链上部署", "deployed on BSC"],
  kPools: ["AMM 池发现", "AMM pools found"],
  kPoolsNote: ["其余流动性走 RFQ/CEX", "rest trades via RFQ/CEX"],
  kScan: ["链上扫描进度", "On-chain scan"],
  kMeme: ["four.meme 活跃度", "four.meme activity"],
  kMemeNote: ["事件 / 最近500块", "events / last 500 blocks"],
  kAttention: ["BSC 活跃评分 · 旧口径", "BSC activity · legacy"],
  kAttentionNote: ["交易+创建+溢价合成", "trades + creates + premium"],
  heatCool: ["冰冷", "Cold"],
  heatWarm: ["温热", "Warm"],
  heatHot: ["火热", "Hot"],

  tickerTitle: ["实时交易流", "Live trades"],
  buy: ["买", "BUY"],
  sell: ["卖", "SELL"],

  spreadTitle: ["双价差监控 · DEX vs Oracle", "Spread Monitor · DEX vs Oracle"],
  spreadHint: ["链上成交价偏离锚定价 = 注意力做市的实时信号", "DEX price deviating from oracle = real-time attention market-making signal"],
  spreadChartTitle: ["Spread 趋势（近 24h，每分钟采样）", "Spread trend (24h, 1-min samples)"],
  thStock: ["xStock", "xStock"],
  thUnderlying: ["底层标的", "Underlying"],
  thOracle: ["Oracle 锚定价", "Oracle price"],
  thDex: ["DEX 价", "DEX price"],
  thPool: ["池", "Pool"],
  thSpread: ["Spread", "Spread"],
  thLocked: ["池内锁股价值", "Stock locked"],
  thChg24h: ["24h", "24h"],
  spreadEmpty: ["正在扫描链上池子…", "Scanning on-chain pools…"],
  minutesAgo: ["分钟前", "min ago"],
  watchHint: ["星标可置顶关注", "Star to pin"],

  discTitle: ["免责声明 · 你在看的是什么", "Disclosures · What you are looking at"],
  discBody: [
    "本页所有代币均为链上资产，不是股票，不享受分红、投票权或与所提及公司的任何关联。行情数据来自 BSC 链上直读与 Backed (xStocks) 官方 oracle，仅供研究参考，不构成投资建议。meme 币波动极大且多数会归零；四.meme 上发行的代币与任何上市公司均无关，蹭名仅为社区行为。",
    "Every token on this page is an on-chain asset — not a share. No dividends, no votes, no connection to any named company. Market data is read directly from BSC and the Backed (xStocks) oracle for research purposes only. Nothing here is financial advice. Memecoins are extremely volatile and most go to zero; tokens launched on four.meme reference listed companies but are not issued, endorsed by, or affiliated with them in any way.",
  ],

  radarTitle: ["four.meme 新币雷达 · bonding curve", "four.meme Radar · bonding curve"],
  radarNewest: ["最新", "Newest"],
  radarHot: ["热度", "Hottest"],
  radarMatch: ["股票名称线索", "Name candidates"],
  thTime: ["时间", "Time"],
  thName: ["名字", "Name"],
  thTrades: ["买/卖", "B/S"],
  thVolume: ["成交额", "Volume"],
  thMeme: ["Meme / CA", "Meme / CA"],
  thStockLink: ["名称线索 / 待验证", "Name candidate / unverified"],
  radarEmpty: ["正在抓取链上新币创建事件…", "Fetching on-chain token creation events…"],
  windowHint: ["窗口", "Window"],

  memeTitle: ["four.meme · 事件分布", "four.meme · Event mix"],
  funnelTitle: ["新币漏斗", "Token funnel"],
  funnelCreated: ["创建", "Created"],
  funnelHasTrade: ["有首笔交易", "First trade"],
  funnelActive: ["活跃 ≥5 笔", "Active ≥5"],
  blocksUnit: ["块", "blocks"],
  newCoins: ["新币", "new coins"],
  tradesUnit: ["笔交易", "trades"],

  allTitle: ["全部 xStocks", "All xStocks"],
  searchPh: ["筛选 symbol…", "Filter symbols…"],
  thPeriod: ["时段", "Session"],
  rfq: ["RFQ/CEX", "RFQ/CEX"],
  periodMarket: ["盘中", "Market"],
  periodExtended: ["盘外", "Extended"],
  periodClosed: ["闭市", "Closed"],
  periodAllday: ["24/7", "24/7"],
  halted: ["暂停", "halted"],
  creator: ["创建者", "Creator"],
  viewOnBscScan: ["在 BscScan 查看", "View on BscScan"],
  poolAddr: ["池地址", "Pool address"],
  oracleUpdatedAt: ["Oracle 更新", "Oracle updated"],

  footer: [
    "数据源：BSC 链上直读 (publicnode) · api.backed.fi oracle · four.meme factory 事件流",
    "Sources: BSC on-chain (publicnode) · api.backed.fi oracle · four.meme factory events",
  ],
};

const state = reactive({ lang: (typeof localStorage !== 'undefined' && localStorage.getItem('lang')) || 'zh' });

export function tr(zh, en) {
  return state.lang === 'en' ? (en ?? zh) : zh;
}

export function t(key) {
  const pair = DICT[key];
  if (!pair) return key;
  return state.lang === 'en' ? pair[1] : pair[0];
}

export function setLang(lang) {
  state.lang = lang;
  try { localStorage.setItem('lang', lang); } catch {}
}

export function useI18n() {
  return { t, tr, lang: state };
}
