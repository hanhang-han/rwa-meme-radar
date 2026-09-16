// SSE fan-out: collectors call broadcastStream, /api/stream drains it to
// every open client. Events only carry data the collectors already produced;
// no upstream calls happen on this path.
const clients = new Set<ReadableStreamDefaultController<Uint8Array>>();

export function streamClients() {
  return clients;
}

export function broadcastStream(event: string, data: unknown) {
  if (!clients.size) return;
  const frame = new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  for (const c of clients) {
    try { c.enqueue(frame); } catch { /* client is closing */ }
  }
}
