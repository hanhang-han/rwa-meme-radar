export const STOCK_DIRECTORY_REUSE_MS = 2000;
export const STOCK_DIRECTORY_PREVIEW_MS = 120000;
const MAX_ENTRIES = 20;
const MAX_STORAGE_CHARS = 262144;

function sessionStorageOrNull() {
  try { return globalThis.sessionStorage ?? null; }
  catch { return null; }
}

// The cached response retains its published now/fieldTimes. Age here describes
// when it was fetched, never the freshness of individual quotes or volumes.
export function createStockDirectoryCache({ request, apiBase, now = Date.now, storage = sessionStorageOrNull } = {}) {
  const entries = new Map();
  const pending = new Map();
  const storageKey = `cliperx:stock-directory-cache:v1:${apiBase}`;
  let loaded = false;
  const keyOf = path => apiBase + path;
  const usable = entry => entry && Number.isFinite(entry.at) && now() >= entry.at && now() - entry.at <= STOCK_DIRECTORY_PREVIEW_MS;
  const getStorage = () => typeof storage === 'function' ? storage() : storage;

  function trim() {
    for (const [key, entry] of entries) if (!usable(entry)) entries.delete(key);
    while (entries.size > MAX_ENTRIES) entries.delete(entries.keys().next().value);
  }

  function load() {
    if (loaded) return;
    loaded = true;
    try {
      const encoded = getStorage()?.getItem(storageKey);
      if (!encoded || encoded.length > MAX_STORAGE_CHARS) return;
      const saved = JSON.parse(encoded);
      if (!Array.isArray(saved)) return;
      for (const [key, entry] of saved.slice(-MAX_ENTRIES)) {
        if (typeof key === 'string' && key.startsWith(`${apiBase}v2/stocks?`) && usable(entry) && entry.data && typeof entry.data === 'object') entries.set(key, entry);
      }
      trim();
    } catch { /* A private session or a malformed cache must not block data. */ }
  }

  function persist() {
    try {
      // A normal 20-row directory is about 20 KB. Keep persistence bounded even
      // if a future endpoint returns a substantially larger DTO.
      const saved = [...entries];
      let encoded = JSON.stringify(saved);
      while (encoded.length > MAX_STORAGE_CHARS && saved.length) {
        saved.shift();
        encoded = JSON.stringify(saved);
      }
      getStorage()?.setItem(storageKey, encoded);
    } catch { /* Storage quota/access errors leave the in-memory cache usable. */ }
  }

  function read(path) {
    load();
    const key = keyOf(path);
    const entry = entries.get(key);
    if (!usable(entry)) { entries.delete(key); return null; }
    entries.delete(key);
    entries.set(key, entry);
    return entry;
  }

  function peek(path) {
    const entry = read(path);
    return entry ? structuredClone(entry.data) : null;
  }

  async function get(path, { force = false } = {}) {
    const key = keyOf(path);
    if (pending.has(key)) return structuredClone(await pending.get(key));
    const entry = read(path);
    if (!force && entry && now() - entry.at <= STOCK_DIRECTORY_REUSE_MS) return structuredClone(entry.data);
    const fetching = Promise.resolve().then(() => request(path)).then(data => {
      entries.delete(key);
      entries.set(key, { at: now(), data: structuredClone(data) });
      trim();
      persist();
      return data;
    }).finally(() => pending.delete(key));
    pending.set(key, fetching);
    return structuredClone(await fetching);
  }

  return { get, peek };
}
