import type { StockMatch } from './stocks';
// Product taxonomy v1: proposed definitions, not a claim that evidence has been collected.
export const relationTypes = [
  { id: 'name', label: '名称 / 叙事', evidence: '名称或公开叙事提及股票，仅作为线索' },
  { id: 'pool', label: '直接配对', evidence: '同链池合约的两侧资产分别为 Meme 和已核实股票代币' },
  { id: 'treasury', label: '金库持仓', evidence: '可归属项目的金库地址及股票代币余额' },
  { id: 'buyback', label: '回购联系', evidence: '可追溯的资金来源、回购交易及接收地址' },
  { id: 'redemption', label: '兑换 / 权益', evidence: '可核验的兑换合约和权益条款' },
  { id: 'correlation', label: '统计联动', evidence: '同一时间窗口对齐的价格序列；相关不代表因果' },
];
export function assessRelation(match: StockMatch | null) {
  return {
    status: match ? 'candidate' : 'unknown',
    ticker: match?.ticker ?? null,
    types: match ? ['name'] : [],
    evidence: match ? [{ type: 'name', method: match.matchType, verified: false }] : [],
    safety: 'unchecked',
    explanation: match ? `名称匹配到 ${match.ticker}，尚无配对池、金库或回购证据。` : '尚未识别股票关联；不代表不存在关联。',
  };
}
