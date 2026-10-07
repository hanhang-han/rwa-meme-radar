import { VISIBLE_QUOTE_LIMIT } from './visible-quotes.js';

// A page owns one bounded subscription. Quotes never carry list membership,
// risk, stock identity, reference prices, or projection freshness.
export function createVisibleQuoteStream({ apiBase, tokens, getCursor, onQuote, onStatus, onReset,
  fetchImpl = (...args) => fetch(...args), schedule = setTimeout, cancel = clearTimeout,
  now = Date.now, maxFrameChars = 65536 }) {
  const identities = [...new Set(tokens ?? [])];
  if (!identities.length || identities.length > VISIBLE_QUOTE_LIMIT || identities.some(key => !/^[1-9]\d*:0x[0-9a-f]{40}$/.test(key))) {
    return { start() {}, stop() {} };
  }
  let active = false, generation = 0, controller, reconnectTimer, watchdog, cursor = null;
  const status = patch => onStatus?.(patch);
  const commitCursor = value => {
    if (value != null && /^\d+$/.test(String(value)) && (cursor == null || Number(value) > Number(cursor))) cursor = String(value);
  };
  async function connect(run, attempt = 0) {
    if (!active || run !== generation) return;
    const abort = controller = new AbortController();
    let reader;
    const live = () => active && run === generation && !abort.signal.aborted;
    const arm = () => { cancel(watchdog); watchdog = schedule(() => abort.abort(), 65000); };
    const query = new URLSearchParams({ protocol: '2', scope: 'quotes', tokens: identities.join(','),
      snapshot: 'false', trades: 'none', candles: 'none' });
    if (cursor != null) query.set('after', cursor);
    status({ connected: false, state: attempt ? 'reconnecting' : 'connecting' });
    try {
      arm();
      const response = await fetchImpl(`${apiBase}stream?${query}`, { signal: abort.signal,
        headers: cursor == null ? {} : { 'Last-Event-ID': cursor } });
      if (!live()) return;
      if (!response.ok || !response.body) throw new Error('visible-quote-stream-unavailable');
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (live()) {
        const chunk = await reader.read();
        if (!live()) return;
        if (chunk.done) throw new Error('visible-quote-stream-closed');
        arm();
        buffer += decoder.decode(chunk.value, { stream: true });
        buffer = buffer.replace(/\r\n/g, '\n');
        let boundary;
        while ((boundary = buffer.indexOf('\n\n')) >= 0) {
          if (boundary > maxFrameChars) throw new Error('visible-quote-frame-too-large');
          const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
          let event = 'message', id = null, encoded = '';
          for (const line of frame.split('\n')) {
            if (line.startsWith('event:')) event = line.slice(6).trim();
            else if (line.startsWith('id:')) id = line.slice(3).trim();
            else if (line.startsWith('data:')) encoded += line.slice(5).trim();
          }
          let packet;
          try { packet = encoded ? JSON.parse(encoded) : {}; } catch { throw new Error('invalid-visible-quote-frame'); }
          if (!live()) return;
          if (event === 'hello' || event === 'heartbeat') {
            attempt = 0;
            status({ connected: true, state: 'live', lastMessageAt: now() });
            // hello precedes replay and announces the upper bound; advancing
            // an existing cursor here would silently discard missed quotes.
            if (event === 'hello' && cursor == null) commitCursor(packet.cursor);
            continue;
          }
          if (event === 'reset') {
            cursor = packet.cursor != null ? String(packet.cursor) : null;
            onReset?.(packet);
            throw new Error('visible-quote-reset');
          }
          if (id != null && cursor != null && Number(id) <= Number(cursor)) continue;
          if (event === 'price' || event === 'stock-quote') {
            const identity = `${packet.chainId}:${String(packet.token ?? '').toLowerCase()}`;
            if (identities.includes(identity)) onQuote?.(packet);
            status({ connected: true, state: 'live', lastMessageAt: now() });
          }
          if (event === 'price' || event === 'stock-quote' || event === 'checkpoint') commitCursor(id ?? packet.cursor);
        }
        if (buffer.length > maxFrameChars) throw new Error('visible-quote-frame-too-large');
      }
    } catch {
      if (active && run === generation) {
        status({ connected: false, state: 'reconnecting' });
        reconnectTimer = schedule(() => connect(run, Math.min(attempt + 1, 5)), Math.min(15000, 1000 * 2 ** attempt));
      }
    } finally {
      cancel(watchdog);
      abort.abort();
      try { await reader?.cancel(); } catch {}
    }
  }
  return {
    start() { if (active) return; active = true; cursor = null; commitCursor(getCursor?.()); connect(++generation); },
    stop() { active = false; generation++; controller?.abort(); cancel(watchdog); cancel(reconnectTimer); status({ connected: false, state: 'paused' }); },
  };
}
