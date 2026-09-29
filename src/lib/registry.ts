// On-chain PairRegistry (X Layer mainnet): writes verified meme×stock pairs,
// reads them back for the public API. The evidence JSON is hashed with
// keccak256 before upload so anyone can re-derive it from /api/registry.
import { ethers } from 'ethers';
import { readFileSync } from 'node:fs';
import type { XRelation } from './xlayer';
import { officialStockIdentity } from './stock-identity';

const artifact = JSON.parse(readFileSync(new URL('../../contracts/artifacts/PairRegistry.json', import.meta.url), 'utf8')) as { abi: any[]; bytecode: string };
const RPC = () => process.env.XLAYER_RPC ?? 'https://xlayerrpc.okx.com';
// Read env lazily: ESM imports run before server.ts calls loadEnvFile.
const CONTRACT = () => process.env.REGISTRY_CONTRACT ?? '';
const KEY = () => process.env.REGISTRY_OWNER_KEY ?? '';
const CHAIN = 196;
const ADDRESS = /^0x[0-9a-f]{40}$/i;

function currentQualified(r: XRelation): boolean {
  if (r.status !== 'verified' || String(r.chainId ?? CHAIN) !== String(CHAIN)) return false;
  if (![r.token, r.stock, r.stockSide, r.pool, r.token0, r.token1].every(value => ADDRESS.test(value))) return false;
  if (new Set([r.token.toLowerCase(), r.stockSide.toLowerCase()]).size !== 2) return false;
  if (new Set([r.token0.toLowerCase(), r.token1.toLowerCase()]).size !== 2) return false;
  const sides = new Set([r.token0.toLowerCase(), r.token1.toLowerCase()]);
  if (!sides.has(r.token.toLowerCase()) || !sides.has(r.stockSide.toLowerCase())) return false;
  const stock = officialStockIdentity(CHAIN, r.stock, r.ticker);
  const side = officialStockIdentity(CHAIN, r.stockSide, r.ticker);
  const now = Date.now();
  return stock.eligibleForPair && side.eligibleForPair && stock.underlyingId === side.underlyingId
    && Number.isFinite(Number(r.liquidityUsd)) && (r.liquidityUsd ?? 0) >= 1000
    && Number.isFinite(Number(r.liquidityAt)) && (r.liquidityAt ?? 0) <= now
    && now - (r.liquidityAt ?? 0) <= 900_000;
}

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
  const configured = /^0x[0-9a-fA-F]{40}$/.test(CONTRACT());
  const rows = configured ? await onchainPairs().catch(() => []) : [];
  const known = new Set(relations.map(r => `${r.token.toLowerCase()}:${r.stock.toLowerCase()}`));
  const qualified = relations.filter(currentQualified);
  const qualifiedKeys = new Set(qualified.map(r => `${r.token.toLowerCase()}:${r.stock.toLowerCase()}`));
  const done = new Set(rows.map(p => `${p.meme}:${p.stock}`));
  return {
    contract: CONTRACT() || null, chainId: CHAIN, configured,
    writeConfigured: registryConfigured(), autoSyncEnabled: false,
    explorer: CONTRACT() ? `https://www.okx.com/explorer/xlayer/address/${CONTRACT()}` : null,
    onchainCount: rows.length, onchainCountMeaning: 'historical-records-not-current-verification',
    pendingLocal: [...qualifiedKeys].filter(key => !done.has(key)).length,
    localVerified: qualifiedKeys.size,
    pairs: rows.map(p => ({ ...p, evidenceSource: qualified.find(r => r.token.toLowerCase() === p.meme && r.stock.toLowerCase() === p.stock) ? evidenceJson(qualified.find(r => r.token.toLowerCase() === p.meme && r.stock.toLowerCase() === p.stock)!) : null })),
    knownUntracked: rows.filter(p => !known.has(`${p.meme}:${p.stock}`)).length,
  };
}

// ethers waits have no built-in timeout; a hung RPC call would hold the
// syncing lock forever. Recover by allowing a fresh loop round after 15 min.
const withTimeout = <T>(p: Promise<T>, ms: number) => Promise.race([p, new Promise<T>((_, reject) => setTimeout(() => reject(new Error('registry rpc timeout')), ms))]);
let syncing = false, syncStartedAt = 0;
export async function syncRegistry(relations: XRelation[]) {
  if (!registryConfigured() || process.env.REGISTRY_MANUAL_SYNC_APPROVED !== 'true') return;
  if (syncing && Date.now() - syncStartedAt < 900_000) return;
  syncing = true; syncStartedAt = Date.now();
  try {
    const onchain = await onchainPairs();
    const done = new Set(onchain.map(p => `${p.meme}:${p.stock}`));
    const pending = relations.filter(r => currentQualified(r) && !done.has(`${r.token.toLowerCase()}:${r.stock.toLowerCase()}`));
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
