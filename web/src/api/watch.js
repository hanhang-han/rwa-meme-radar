import { API_BASE } from './client.js';

// These are read-only POST queries: watch identities stay out of URLs and
// request bodies are bounded by the server. No account/session is required.
export async function watchRequest(path, body, { signal, timeoutMs = 15000 } = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort(); else signal?.addEventListener('abort', abort, { once: true });
  const timer = setTimeout(abort, timeoutMs);
  try {
    const response = await fetch(`${API_BASE}v2/watch/${path}`, {
      method: 'POST', headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(body), signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer); signal?.removeEventListener('abort', abort);
  }
}
export const getWatchSummary = (body, options) => watchRequest('summary', body, options);
export const getWatchEvents = (body, options) => watchRequest('events', body, options);
