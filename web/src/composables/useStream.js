// SSE client over fetch-streaming: cliperx nginx sub_filter rewrites
// fetch('/api/...') literals but never EventSource URLs, so the stream is
// read manually. Polling remains the fallback whenever the stream is down.
import { API_BASE } from '../api/client';
import { useDashboardStore } from '../stores/dashboard';
import { useDetailStore } from '../stores/detail';

let started = false;

export function useStream() {
  return { started };
}

export function startStream() {
  if (started) return;
  started = true;
  connectStream();
}

async function connectStream() {
  for (;;) {
    try {
      const res = await fetch(API_BASE + 'stream');
      if (!res.ok || !res.body) throw new Error('stream unavailable');
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let event = 'message';
          let data = '';
          for (const line of frame.split('\n')) {
            if (line.startsWith('event:')) event = line.slice(6).trim();
            else if (line.startsWith('data:')) data += line.slice(5).trim();
          }
          if (data && event !== 'heartbeat') handleStreamEvent(event, data);
        }
      }
    } catch {
      /* stream down; polling keeps the page live */
    }
    await new Promise((r) => setTimeout(r, 5000));
  }
}

function handleStreamEvent(event, raw) {
  let d;
  try {
    d = JSON.parse(raw);
  } catch {
    return;
  }
  const dash = useDashboardStore();
  const detail = useDetailStore();
  if (event === 'price' && d.token && d.price != null) {
    dash.applyPrice(d.token, d.price, d.at);
    // Detail page live tail: expose a ref bump so the chart watcher re-runs.
    if (detail.current && detail.currentData?.asset?.token === d.token) {
      window.dispatchEvent(new CustomEvent('sse-price', { detail: d }));
    }
  } else if (event === 'trade' && d.token && Array.isArray(d.fresh)) {
    if (detail.current && detail.currentData?.asset?.token === d.token) {
      window.dispatchEvent(new CustomEvent('sse-trades', { detail: d }));
    }
  }
}
