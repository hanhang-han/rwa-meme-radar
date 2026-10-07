// Keep the API on the same mounted dashboard path as the current app. The
// original and v2 sites can therefore run side by side on one host.
const mountedDashboard = typeof location === 'undefined'
  ? null
  : location.pathname.match(/^\/(dashboardv2|dashboard)(?:\/|$)/);
export const API_BASE = mountedDashboard ? `/${mountedDashboard[1]}/api/` : '/api/';

const inFlight = new Map();
export async function getJSON(path, timeoutMs = 25000) {
  const url = API_BASE + path.replace(/^\//, '');
  // Page initialization and stream bootstrap share one request. Consumers
  // receive independent objects because stores annotate and merge their rows.
  if (!inFlight.has(url)) {
    const request = fetchJSON(url, timeoutMs).finally(() => inFlight.delete(url));
    inFlight.set(url, request);
  }
  return structuredClone(await inFlight.get(url));
}

async function fetchJSON(url, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {signal: controller.signal});
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (error) {
    if (controller.signal.aborted) throw new Error('request-timeout');
    throw error;
  } finally { clearTimeout(timer); }
}

export async function getDashboard(view = 'market') {
  return getJSON(`/dashboard?view=${view}`, view === 'overview' ? 15000 : 45000);
}

export async function getFeed(chainId, { fresh = false } = {}) {
  const query = new URLSearchParams();
  if (chainId) query.set('chain', chainId);
  if (fresh) query.set('fresh', '1');
  return getJSON(`/feed${query.size ? `?${query}` : ''}`);
}

export async function getDataHealth() {
  return getJSON('/health/data');
}

export async function getDetail(chain, address, options = {}) {
  const q = new URLSearchParams();
  if(options.section) q.set('section',options.section);
  if(options.offset) q.set('offset',String(options.offset));
  if(options.limit) q.set('limit',String(options.limit));
  return getJSON(`/token/${encodeURIComponent(chain)}/${encodeURIComponent(address)}${q.size?'?'+q:''}`);
}

export async function getCandles(chain, address, bar, limit = 180, venue = 'dex', options = {}) {
  const query = new URLSearchParams({bar,limit:String(limit),venue});
  if(options.marketId)query.set('market',options.marketId);
  if(options.poolId)query.set('pool',options.poolId);
  return getJSON(`/candles/${encodeURIComponent(chain)}/${encodeURIComponent(address)}?${query}`);
}

export async function getEvents(chain, before) {
  const q = new URLSearchParams({ chain });
  if (before) q.set('before', typeof before === 'string' ? before : JSON.stringify(before));
  return getJSON(`/events?${q}`);
}

export async function getRegistry() {
  return getJSON('/registry');
}

export async function getBriefing(lang) {
  return getJSON(`/ai/briefing?lang=${lang}`);
}

export async function getInsight(chain, address, lang) {
  return getJSON(`/ai/insight/${encodeURIComponent(chain)}/${encodeURIComponent(address)}?lang=${lang}`);
}
