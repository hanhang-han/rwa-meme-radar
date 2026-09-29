export const CHAINS = ['196', '56', '4663'];

export function chainScope(query) {
  const value = String(query?.chain ?? 'all').toLowerCase();
  if (value === 'xlayer') return '196';
  if (value === 'bsc' || value === 'bnb') return '56';
  if (value === 'robinhood') return '4663';
  return CHAINS.includes(value) ? value : 'all';
}

export function inChainScope(row, scope) {
  return scope === 'all' || String(row?.chainId ?? row?.chain ?? '') === scope;
}

export function savedChainScope() {
  try { return chainScope({ chain: localStorage.getItem('radar-chain') }); }
  catch { return 'all'; }
}

export function saveChainScope(scope) {
  try { localStorage.setItem('radar-chain', chainScope({ chain: scope })); }
  catch {}
}
