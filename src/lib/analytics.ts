export interface PricePoint { t: number; v: number; }
export interface Metric { value: number | null; reason: string | null; samples: number; }
export function correlation(meme: PricePoint[], stock: PricePoint[], now = Date.now(), windowMs = 4 * 3600_000): Metric {
  const bucket = (points: PricePoint[]) => new Map(points.filter(p => Number.isFinite(p.t) && Number.isFinite(p.v) && p.v > 0 && p.t <= now && p.t >= now - windowMs).map(p => [Math.floor(p.t / 300_000), p.v]));
  const a = bucket(meme), b = bucket(stock);
  const pairs = [...a].filter(([t]) => b.has(t)).map(([t,v]) => [v,b.get(t)!]);
  const n = pairs.length;
  if (n < 12) return { value: null, reason: '至少需要 12 个对齐的 5 分钟样本', samples: n };
  const mx = pairs.reduce((s,p) => s+p[0],0)/n, my = pairs.reduce((s,p) => s+p[1],0)/n;
  let xx=0, yy=0, xy=0;
  for (const [x,y] of pairs) { xx+=(x-mx)**2; yy+=(y-my)**2; xy+=(x-mx)*(y-my); }
  if (!xx || !yy) return { value:null, reason:'价格无变化，相关系数无定义', samples:n };
  return { value:Math.max(-1,Math.min(1,xy/Math.sqrt(xx*yy))), reason:null, samples:n };
}
export function heat(volumeGrowth: number | null, holderGrowth: number | null, lockedUsd: number | null): Metric {
  if ([volumeGrowth, holderGrowth, lockedUsd].some(x => x == null || !Number.isFinite(x))) return {value:null,reason:'缺少成交量增速、持有人增速或已核实股票锁仓金额',samples:0};
  if (lockedUsd! < 0) return {value:null,reason:'锁仓金额无效',samples:0};
  const clamp = (x:number) => Math.max(0,Math.min(1,x));
  // v1 explicit normalization: +100% volume, +20% holders, $1m locked saturate their components.
  return {value:100*(0.4*clamp(volumeGrowth!)+0.3*clamp(holderGrowth!/0.2)+0.3*clamp(lockedUsd!/1_000_000)),reason:null,samples:3};
}
export function captureRate(pairedLiquidity: number | null, totalLiquidity: number | null): Metric {
  if (pairedLiquidity == null || totalLiquidity == null || !Number.isFinite(pairedLiquidity) || !Number.isFinite(totalLiquidity) || totalLiquidity <= 0 || pairedLiquidity < 0 || pairedLiquidity > totalLiquidity) return {value:null,reason:'需要同一范围、同一时点的完整股票流动性分母',samples:0};
  return {value:100*pairedLiquidity/totalLiquidity,reason:null,samples:2};
}
export function basketIndex(members: {baseCap:number;basePrice:number;price:number}[]): Metric {
  if (!members.length || members.some(m => ![m.baseCap,m.basePrice,m.price].every(n => Number.isFinite(n) && n>0))) return {value:null,reason:'等待成分的基期市值和有效价格',samples:members.length};
  const cap = members.reduce((s,m)=>s+m.baseCap,0);
  return {value:100*members.reduce((s,m)=>s+(m.baseCap/cap)*(m.price/m.basePrice),0),reason:null,samples:members.length};
}
export const sectors: Record<string,string[]> = {
  '芯片':['NVDA','AMD','INTC','TSM','SKHY','SNDK'], '交易所':['COIN','HOOD'],
  '支付':['V','MA','PYPL','CRCL'], '托管':['BK','STT'], '加密国库':['MSTR','BMNR'],
};
