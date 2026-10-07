const MAX_ENTRIES = 12;
const PREVIEW_MS = 120000;
const REUSE_MS = 2000;

// A page/graph cache is independent of the global projection store. Its key
// includes the mount and every server filter, and never replaces source times.
export function createMemeDirectoryCache({ request, apiBase, now = Date.now } = {}) {
  const entries = new Map(), pending = new Map();
  const keyOf = path => `${apiBase}${path}`;
  function peek(path) {
    const entry = entries.get(keyOf(path));
    const age = entry ? now() - entry.at : Infinity;
    if (age < 0 || age > PREVIEW_MS) { entries.delete(keyOf(path)); return null; }
    return entry ? structuredClone(entry.data) : null;
  }
  async function get(path, { force = false } = {}) {
    const key = keyOf(path);
    if (pending.has(key)) return structuredClone(await pending.get(key));
    const cached = peek(path), entry = entries.get(key);
    if (!force && cached && now() - entry.at <= REUSE_MS) return cached;
    const fetching = Promise.resolve().then(() => request(path)).then(data => {
      entries.delete(key);
      entries.set(key, { at: now(), data: structuredClone(data) });
      for (const [savedKey, saved] of entries) if (now() - saved.at > PREVIEW_MS) entries.delete(savedKey);
      while (entries.size > MAX_ENTRIES) entries.delete(entries.keys().next().value);
      return data;
    }).finally(() => pending.delete(key));
    pending.set(key, fetching);
    return structuredClone(await fetching);
  }
  return { peek, get };
}
