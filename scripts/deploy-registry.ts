// Compile contracts/PairRegistry.sol, optionally deploy to X Layer mainnet.
// Run on the server (network reaches X Layer RPC there):
//   REGISTRY_OWNER_KEY=0x... npx tsx scripts/deploy-registry.ts          # compile + deploy
//   npx tsx scripts/deploy-registry.ts --compile-only                     # compile only
// Deployed address is written to stdout; record it as REGISTRY_CONTRACT in .env.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { execSync } from 'node:child_process';

const RPC = process.env.XLAYER_RPC ?? 'https://xlayerrpc.okx.com';

const source = readFileSync('contracts/PairRegistry.sol', 'utf8');
const solcMod: any = await import('solc');
const solc = solcMod.default ?? solcMod;
const input = {
  language: 'Solidity',
  sources: { 'PairRegistry.sol': { content: source } },
  settings: { optimizer: { enabled: true, runs: 200 }, outputSelection: { '*': { '*': ['abi', 'evm.bytecode.object'] } } },
};
const compiled = JSON.parse(solc.compile(JSON.stringify(input)));
const errors: any[] = compiled.errors ?? [];
for (const e of errors) console.error(e.formattedMessage);
if (errors.some((e) => e.severity === 'error')) process.exit(1);
const contract = compiled.contracts['PairRegistry.sol'].PairRegistry;
mkdirSync('contracts/artifacts', { recursive: true });
writeFileSync('contracts/artifacts/PairRegistry.json', JSON.stringify({ abi: contract.abi, bytecode: '0x' + contract.evm.bytecode.object }, null, 2));
console.log('compiled ok -> contracts/artifacts/PairRegistry.json');

if (process.argv.includes('--compile-only')) process.exit(0);

const key = process.env.REGISTRY_OWNER_KEY;
if (!key || !/^0x[0-9a-fA-F]{64}$/.test(key)) {
  console.error('REGISTRY_OWNER_KEY missing or malformed (expect 0x + 64 hex). Generate one with: openssl rand -hex 32');
  process.exit(1);
}

const { ethers } = await import('ethers');
const provider = new ethers.JsonRpcProvider(RPC);
const wallet = new ethers.Wallet(key, provider);
const network = await provider.getNetwork();
if (Number(network.chainId) !== 196) {
  console.error(`wrong chain ${network.chainId}, expect 196 (X Layer mainnet)`);
  process.exit(1);
}
const balance = await provider.getBalance(wallet.address);
console.log(`deployer ${wallet.address} chain=196 balance=${ethers.formatEther(balance)} OKB`);
if (balance === 0n) {
  console.error('deployer has no OKB for gas. Fund it on X Layer mainnet first.');
  process.exit(1);
}

const factory = new ethers.ContractFactory(contract.abi, '0x' + contract.evm.bytecode.object, wallet);
console.log('deploying…');
const registry = await factory.deploy();
await registry.deploymentTransaction()!.wait();
const address = await registry.getAddress();
console.log(`DEPLOYED PairRegistry at ${address} (tx ${registry.deploymentTransaction()!.hash})`);
console.log(`add to .env: REGISTRY_CONTRACT=${address}`);
