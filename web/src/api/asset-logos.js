import { getJSON } from './client.js';
import { avatarUrl } from '../utils/asset-avatar.js';

export function assetLogoIdentity(asset) {
  const chain = String(asset?.chainId ?? '');
  const address = String(asset?.token || asset?.tokenContractAddress || '').toLowerCase();
  return /^(196|56|4663|5042)$/.test(chain) && /^0x[0-9a-f]{40}$/.test(address) ? `${chain}:${address}` : '';
}

// One shared batch/cache for all lists and detail pages. Quote changes never
// invalidate metadata; absent logos and failed reads may retry on a later visit.
export function createLogoLoader(request = getJSON, { now = Date.now, schedule = callback => setTimeout(callback, 20) } = {}) {
  const cache = new Map(), pending = new Map();
  let scheduled = false;
  async function flush() {
    scheduled = false;
    const entries = [...pending.entries()].filter(([, entry]) => !entry.running).slice(0, 50);
    if (!entries.length) return;
    for (const [, entry] of entries) entry.running = true;
    if ([...pending.values()].some(entry => !entry.running)) enqueue();
    let logos = {}, failed = false;
    try {
      const data = await request(`v2/asset-logos?ids=${encodeURIComponent(entries.map(([key]) => key).join(','))}`, 8000);
      logos = data?.logos || {};
    } catch { failed = true; }
    for (const [key, entry] of entries) {
      const logo = avatarUrl({ logoUrl: logos[key] });
      cache.delete(key);
      cache.set(key, { logo, expires: now() + (logo ? 3600000 : failed ? 15000 : 60000) });
      pending.delete(key);
      entry.resolve(logo);
    }
    while (cache.size > 1024) cache.delete(cache.keys().next().value);
  }
  function enqueue() {
    if (!scheduled) { scheduled = true; schedule(flush); }
  }
  return function load(key, { refresh = false } = {}) {
    if (!/^(196|56|4663|5042):0x[0-9a-f]{40}$/.test(key)) return Promise.resolve('');
    const hit = cache.get(key);
    if (!refresh && hit?.expires > now()) return Promise.resolve(hit.logo);
    if (pending.has(key)) return pending.get(key).promise;
    let resolve;
    const promise = new Promise(done => resolve = done);
    pending.set(key, { promise, resolve, running: false });
    enqueue();
    return promise;
  };
}

export const loadAssetLogo = createLogoLoader();
