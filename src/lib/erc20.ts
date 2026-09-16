import { rpc } from "./rpc";

const padAddr = (a: string) => a.toLowerCase().replace(/^0x/, "").padStart(64, "0");
const padUint = (n: number) => n.toString(16).padStart(64, "0");

export const ERC20 = {
  decimals: () => "0x313ce567",
  symbol: () => "0x95d89b41",
  name: () => "0x06fdde03",
};

export const tokens = {
  WBNB: "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
  USDT: "0x55d398326f99059fF775485246999027B3197955",
  USDC: "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
} as const;

export async function decimalsOf(addr: string): Promise<number> {
  return Number(await rpc.callInt(addr, ERC20.decimals()));
}

export async function symbolOf(addr: string): Promise<string> {
  const raw = await rpc.call(addr, ERC20.symbol());
  if (raw === "0x") return "?";
  const len = parseInt(raw.slice(2 + 64, 2 + 128), 16);
  if (len > 32) return "N/A(string)";
  return raw.slice(2, 2 + len * 2).match(/.{2}/g)!.map((b) => String.fromCharCode(parseInt(b, 16))).join("");
}

export { padAddr, padUint };
