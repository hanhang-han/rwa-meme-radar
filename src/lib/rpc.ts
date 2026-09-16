const HEADERS = {
  "Content-Type": "application/json",
  "User-Agent": "curl/8.7.1",
  Accept: "application/json",
};

interface PoolNode {
  url: string;
  health: number;
  cooldownUntil: number;
  latency: number;
  failStreak: number;
  disabled: boolean;
}

const NODES: PoolNode[] = [
  "https://bsc.publicnode.com",
  "https://bsc.rpc.blxrbdn.com",
  "https://api.zan.top/bsc-mainnet",
  "https://bsc-dataseed.binance.org/",
  "https://bsc-dataseed1.defibit.io/",
  "https://bsc-dataseed1.ninicoin.io/",
  "https://bsc-dataseed.bnbchain.org/",
  "https://bsc-dataseed2.bnbchain.org/",
  "https://bsc-mainnet.public.blastapi.io",
  "https://bsc.meowrpc.com",
].map((url) => ({ url, health: 100, cooldownUntil: 0, latency: 300, failStreak: 0, disabled: false }));

function pickNode(): PoolNode {
  const now = Date.now();
  let avail = NODES.filter((n) => !n.disabled && n.cooldownUntil < now);
  if (!avail.length) {
    const live = NODES.filter((n) => !n.disabled);
    if (!live.length) {
      NODES.forEach((n) => (n.disabled = false));
      live.push(...NODES);
    }
    avail = live.sort((a, b) => b.health - a.health).slice(0, 3);
  }
  const pool = avail;
  const weights = pool.map((n) => (n.health / 100) ** 2 * (300 / (n.latency + 100)));
  const total = weights.reduce((s, w) => s + w, 0);
  let r = Math.random() * total;
  for (let i = 0; i < pool.length; i++) {
    r -= weights[i];
    if (r <= 0) return pool[i];
  }
  return pool[pool.length - 1];
}

function markSuccess(n: PoolNode, ms: number) {
  n.health = Math.min(100, n.health + 4);
  n.latency = Math.round(n.latency * 0.6 + ms * 0.4);
  n.cooldownUntil = 0;
  n.failStreak = 0;
}

function markFailure(n: PoolNode, status: number) {
  n.health = Math.max(5, n.health - 25);
  n.failStreak++;
  if (n.failStreak >= 8) n.disabled = true;
  n.cooldownUntil = Date.now() + (status === 403 || status === 429 ? 20_000 : 5_000);
}

async function postOnce(url: string, body: string, timeoutMs: number): Promise<any> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { method: "POST", headers: HEADERS, body, signal: ctrl.signal });
    if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
    return await res.json();
  } finally {
    clearTimeout(t);
  }
}

export class RpcClient {
  private async raw(method: string, params: unknown[]): Promise<any> {
    const body = JSON.stringify({ jsonrpc: "2.0", id: 1, method, params });
    let lastErr = "";
    for (let attempt = 0; attempt < 6; attempt++) {
      const node = pickNode();
      const t0 = Date.now();
      try {
        const json = await postOnce(node.url, body, 15000);
        if (json.error) throw new Error(json.error.message);
        markSuccess(node, Date.now() - t0);
        return json.result;
      } catch (e: any) {
        lastErr = e?.message ?? String(e);
        markFailure(node, e?.status ?? 0);
        await new Promise((r) => setTimeout(r, 300 * (attempt + 1)));
      }
    }
    throw new Error(`rpc ${method} failed: ${lastErr}`);
  }

  async call(to: string, data: string): Promise<string> {
    return this.raw("eth_call", [{ to, data }, "latest"]);
  }

  async callInt(to: string, data: string): Promise<bigint> {
    const res = await this.call(to, data);
    return BigInt(res === "0x" ? 0 : res);
  }

  async blockNumber(): Promise<number> {
    return parseInt(await this.raw("eth_blockNumber", []), 16);
  }

  async getLogs(address: string, topics: unknown[], fromBlock: number, toBlock: number | "latest"): Promise<any[]> {
    return this.raw("eth_getLogs", [
      { address, topics, fromBlock: hex(fromBlock), toBlock: toBlock === "latest" ? "latest" : hex(toBlock) },
    ]);
  }

  async getBlockTimestamp(num: number): Promise<number> {
    const res = await this.raw("eth_getBlockByNumber", [hex(num), false]);
    return parseInt(res.timestamp, 16);
  }

  async batchCall(calls: { to: string; data: string }[]): Promise<string[]> {
    const out: string[] = [];
    const CHUNK = 40;
    for (let i = 0; i < calls.length; i += CHUNK) {
      const chunk = calls.slice(i, i + CHUNK);
      const body = JSON.stringify(
        chunk.map((c, j) => ({ jsonrpc: "2.0", id: j, method: "eth_call", params: [{ to: c.to, data: c.data }, "latest"] })),
      );
      let done = false;
      for (let attempt = 0; attempt < 5 && !done; attempt++) {
        const node = pickNode();
        const t0 = Date.now();
        try {
          const res = await postOnce(node.url, body, 25000);
          if (!Array.isArray(res)) throw new Error("batch: not array");
          const bad = res.find((r: any) => r.error && !r.result);
          if (bad) throw new Error(bad.error?.message ?? "batch error");
          const map = new Map<number, string>(res.map((r: any) => [r.id, r.result ?? "0x"]));
          for (let j = 0; j < chunk.length; j++) out.push(map.get(j) ?? "0x");
          markSuccess(node, Date.now() - t0);
          done = true;
        } catch (e: any) {
          markFailure(node, e?.status ?? 0);
          await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
        }
      }
      if (!done) for (let j = 0; j < chunk.length; j++) out.push("0x");
      await new Promise((r) => setTimeout(r, 250));
    }
    return out;
  }

  nodeStats() {
    return NODES.map(({ url, health, cooldownUntil, latency, failStreak, disabled }) => ({
      url,
      health,
      latency,
      failStreak,
      disabled,
      cooling: cooldownUntil > Date.now(),
    }));
  }
}

export const hex = (n: number) => "0x" + n.toString(16);
export const rpc = new RpcClient();
