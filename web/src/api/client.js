// Base path auto-detect: cliperx serves this app under /dashboard/ (the
// legacy nginx sub_filter trick), any other host uses the root path.
export const API_BASE =
  typeof location !== 'undefined' && location.pathname.includes('/dashboard/')
    ? '/dashboard/api/'
    : '/api/';

export async function getJSON(path) {
  const res = await fetch(API_BASE + path.replace(/^\//, ''));
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function getDashboard() {
  return getJSON('/dashboard');
}

export async function getDetail(chain, address) {
  return getJSON(`/token/${encodeURIComponent(chain)}/${encodeURIComponent(address)}`);
}

export async function getCandles(chain, address, bar, limit = 180) {
  return getJSON(`/candles/${encodeURIComponent(chain)}/${encodeURIComponent(address)}?bar=${bar}&limit=${limit}`);
}

export async function getEvents(chain, before) {
  const q = new URLSearchParams({ chain });
  if (before?.t) { q.set('before', String(before.t)); q.set('id', before.id); }
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
