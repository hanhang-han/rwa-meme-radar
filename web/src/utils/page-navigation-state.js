const states = new Map();
const MAX_ENTRIES = 32, MAX_ENTRY_BYTES = 262144, MAX_TOTAL_BYTES = 1048576, TTL = 30 * 60 * 1000;
let bytes = 0;

export function clearPageState(key) {
  const entry = states.get(key);
  if (entry) bytes -= entry.bytes;
  states.delete(key);
}

export function readPageState(key) {
  const entry = states.get(key);
  if (!entry) return null;
  if (Date.now() - entry.at > TTL) { clearPageState(key); return null; }
  states.delete(key); states.set(key, entry);
  return JSON.parse(entry.json);
}

// Keep only reversible UI context for this browser session. Credentials must
// never enter this cache; callers use explicit public-data/UI whitelists.
export function writePageState(key, value) {
  let json;
  try { json = JSON.stringify(value, (name, item) => {
    if (/^(?:password|csrfToken|sessionToken|apiKey|secret|authorization|accessToken|refreshToken)$/i.test(name)) throw new Error('credential in page state');
    return item;
  }); } catch { return false; }
  if (!json || typeof key !== 'string') return false;
  const size = new TextEncoder().encode(json).length;
  if (size > MAX_ENTRY_BYTES) return false;
  clearPageState(key); states.set(key, { json, bytes: size, at: Date.now() }); bytes += size;
  while (states.size > MAX_ENTRIES || bytes > MAX_TOTAL_BYTES) clearPageState(states.keys().next().value);
  return true;
}
