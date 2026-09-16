// On-chain PairRegistry (X Layer mainnet): writes verified meme×stock pairs,
// reads them back for the public API. The evidence JSON is hashed with
// keccak256 before upload so anyone can re-derive it from /api/registry.
import { ethers } from 'ethers';
import { readFileSync } from 'node:fs';
import type { XRelation } from './xlayer';

const artifact = JSON.parse(readFileSync(new URL('../../contracts/artifacts/PairRegistry.json', import.meta.url), 'utf8')) as { abi: any[]; bytecode: string };
const RPC = () => process.env.XLAYER_RPC ?? 'https://xlayerrpc.okx.com';
// Read env lazily: ESM imports run before server.ts calls loadEnvFile.
const CONTRACT = () => process.env.REGISTRY_CONTRACT ?? '';
const KEY = () => process.env.REGISTRY_OWNER_KEY ?? '';
const CHAIN = 196;

export function registryConfigured() {
  return /^0x[0-9a-fA-F]{40}$/.test(CONTRACT()) && /^0x[0-9a-fA-F]{64}$/.test(KEY());
}

// Keys are ASCII-ordered literals so JSON.stringify output is deterministic.
export function evidenceJson(r: XRelation) {
  return JSON.stringify({
    block: r.block,
    chainId: String(r.chainId ?? CHAIN),
    checkedAt: r.checkedAt,
    liquidityUsd: r.liquidityUsd ?? null,
    meme: r.token.toLowerCase(),
    pool: r.pool.toLowerCase(),
    stock: r.stock.toLowerCase(),
    stockSide: r.stockSide.toLowerCase(),
    ticker: r.ticker,
  });
}
export function evidenceHash(r: XRelation) {
  return ethers.keccak256(ethers.toUtf8Bytes(evidenceJson(r)));
}

let provider: ethers.JsonRpcProvider | null = null;
let cached: { at: number; rows: any[] } | null = null;

function readOnly() {
  provider ??= new ethers.JsonRpcProvider(RPC(), CHAIN, { staticNetwork: true });
  return new ethers.Contract(CONTRACT(), artifact.abi, provider);
}

export async function onchainPairs(): Promise<any[]> {
  if (!/^0x[0-9a-fA-F]{40}$/.test(CONTRACT())) return [];
  if (cached && Date.now() - cached.at < 60_000) return cached.rows;
  const raw = await readOnly().all();
  const rows = raw.map((p: any) => ({
    meme: p.meme.toLowerCase(), stock: p.stock.toLowerCase(), ticker: p.ticker,
    evidence: p.evidence, registeredAt: Number(p.registeredAt),
  }));
  cached = { at: Date.now(), rows };
  return rows;
}

export async function registryInfo(relations: XRelation[]) {
  const configured = registryConfigured();
  const rows = configured ? await onchainPairs().catch(() => []) : [];
  const known = new Set(relations.map(r => `${r.token.toLowerCase()}:${r.stock.toLowerCase()}`));
  return {
    contract: CONTRACT() || null, chainId: CHAIN, configured,
    explorer: CONTRACT() ? `https://www.okx.com/explorer/xlayer/address/${CONTRACT()}` : null,
    onchainCount: rows.length,
    pendingLocal: relations.filter(r => r.status === 'verified' && !rows.some(p => p.meme === r.token.toLowerCase() && p.stock === r.stock.toLowerCase())).length,
    localVerified: relations.filter(r => r.status === 'verified').length,
    pairs: rows.map(p => ({ ...p, evidenceSource: relations.find(r => r.token.toLowerCase() === p.meme && r.stock.toLowerCase() === p.stock && r.status === 'verified') ? evidenceJson(relations.find(r => r.token.toLowerCase() === p.meme && r.stock.toLowerCase() === p.stock)!) : null })),
    knownUntracked: rows.filter(p => !known.has(`${p.meme}:${p.stock}`)).length,
  };
}

// ethers waits have no built-in timeout; a hung RPC call would hold the
// syncing lock forever. Recover by allowing a fresh loop round after 15 min.
const withTimeout = <T>(p: Promise<T>, ms: number) => Promise.race([p, new Promise<T>((_, reject) => setTimeout(() => reject(new Error('registry rpc timeout')), ms))]);
let syncing = false, syncStartedAt = 0;
export async function syncRegistry(relations: XRelation[]) {
  if (!registryConfigured()) return;
  if (syncing && Date.now() - syncStartedAt < 900_000) return;
  syncing = true; syncStartedAt = Date.now();
  try {
    const onchain = await onchainPairs();
    const done = new Set(onchain.map(p => `${p.meme}:${p.stock}`));
    const pending = relations.filter(r => r.status === 'verified' && !done.has(`${r.token.toLowerCase()}:${r.stock.toLowerCase()}`));
    if (!pending.length) return;
    const wallet = new ethers.Wallet(KEY(), provider ??= new ethers.JsonRpcProvider(RPC(), CHAIN, { staticNetwork: true }));
    const balance = await wallet.provider!.getBalance(wallet.address);
    if (balance === 0n) { console.error('[registry] owner wallet empty, skip sync'); return; }
    const contract = new ethers.Contract(CONTRACT(), artifact.abi, wallet);
    for (const r of pending) {
      try {
        const tx = await withTimeout(contract.register(r.token.toLowerCase(), r.stock.toLowerCase(), r.ticker, evidenceHash(r)), 60_000);
        await withTimeout(tx.wait(), 120_000);
        console.log(`[registry] registered ${r.ticker} ${r.token.slice(0, 10)} tx=${tx.hash}`);
      } catch (e) {
        console.error(`[registry] register failed ${r.ticker}: ${e instanceof Error ? e.message : e}`);
      }
    }
    cached = null;
  } finally {
    syncing = false;
  }
}
