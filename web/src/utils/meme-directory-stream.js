// This stream carries invalidations only. It never opens a market projection,
// consumes a full catalogue, or acknowledges a publication as a quote time.
export function createMemeDirectoryStream({ apiBase, onInvalidate, onStatus, getCursor, fetchImpl = (...args) => fetch(...args) }) {
  let active = false, controller, reconnectTimer, heartbeatTimer, generation = 0, cursor = null;
  const status = patch => onStatus?.(patch);
  async function connect(currentGeneration, attempt = 0) {
    if (!active || currentGeneration !== generation) return;
    controller = new AbortController();
    const abort = controller;
    const observedCursor = getCursor?.();
    if (observedCursor != null && (cursor == null || Number(observedCursor) > Number(cursor))) cursor = observedCursor;
    const query = new URLSearchParams({ protocol: '2', scope: 'memes', snapshot: 'false', trades: 'none', candles: 'none' });
    if (cursor != null) query.set('after', String(cursor));
    status({ connected: false, state: attempt ? 'reconnecting' : 'connecting' });
    let reader;
    try {
      heartbeatTimer = setTimeout(() => abort.abort(), 65000);
      const response = await fetchImpl(`${apiBase}v2/stream?${query}`, { signal: abort.signal, headers: cursor == null ? {} : { 'Last-Event-ID': String(cursor) } });
      if (!response.ok || !response.body) throw new Error('directory-stream-unavailable');
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (active && currentGeneration === generation && !abort.signal.aborted) {
        const { value, done } = await reader.read();
        if (done) throw new Error('directory-stream-closed');
        clearTimeout(heartbeatTimer);
        heartbeatTimer = setTimeout(() => abort.abort(), 65000);
        buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n');
        if (buffer.length > 65536) throw new Error('directory-stream-frame-too-large');
        let separator;
        while ((separator = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, separator); buffer = buffer.slice(separator + 2);
          let event = 'message', id, encoded = '';
          for (const line of frame.split('\n')) {
            if (line.startsWith('event:')) event = line.slice(6).trim();
            else if (line.startsWith('id:')) id = line.slice(3).trim();
            else if (line.startsWith('data:')) encoded += line.slice(5).trim();
          }
          let data = {};
          if (encoded) { try { data = JSON.parse(encoded); } catch { continue; } }
          if (event === 'hello' || event === 'heartbeat') status({ connected: true, state: 'live', lastMessageAt: Date.now() });
          if ((event === 'directory.invalidate' && data.scope === 'memes') || event === 'reset') {
            if (event === 'reset' || id == null || cursor == null || Number(id) > Number(cursor)) onInvalidate?.(data);
          }
          // hello announces the server's latest cursor before replay. Advancing
          // an existing HTTP cursor here would incorrectly skip that replay.
          const commitsCursor = event === 'checkpoint' || event === 'reset' || (event === 'directory.invalidate' && data.scope === 'memes');
          if (commitsCursor && id != null && (cursor == null || Number(id) > Number(cursor))) cursor = id;
          else if (commitsCursor && data.cursor != null && (cursor == null || Number(data.cursor) > Number(cursor))) cursor = data.cursor;
          else if (event === 'hello' && cursor == null && data.cursor != null) cursor = data.cursor;
        }
      }
    } catch {
      if (active && currentGeneration === generation) {
        status({ connected: false, state: 'reconnecting' });
        reconnectTimer = setTimeout(() => connect(currentGeneration, Math.min(attempt + 1, 5)), Math.min(15000, 1000 * 2 ** attempt));
      }
    } finally {
      clearTimeout(heartbeatTimer);
      try { await reader?.cancel(); } catch {}
    }
  }
  return {
    start() { if (active) return; active = true; connect(++generation); },
    stop() { active = false; generation++; controller?.abort(); clearTimeout(reconnectTimer); clearTimeout(heartbeatTimer); status({ connected: false, state: 'paused' }); },
  };
}
