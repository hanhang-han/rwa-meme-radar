export function currentMetric(metric, now = Date.now()) {
  if (!metric) return { value: null, status: 'unavailable', reason: 'pending' };
  if (metric.value != null && (!Number.isFinite(metric.value) || now > (metric.validUntil ?? 0))) {
    return { ...metric, value: null, status: 'unavailable', reason: 'stale' };
  }
  if (metric.status === 'realtime' && now > (metric.realtimeUntil ?? 0)) return { ...metric, status: 'snapshot' };
  return metric;
}

export function packetKey(packet) {
  return `${packet.chainId}:${String(packet.token).toLowerCase()}`;
}

export function mergePacket(previous, next) {
  if (!next?.token || !next?.chainId) return previous;
  if (previous && (previous.calculatedAt ?? 0) > (next.calculatedAt ?? 0)) return previous;
  return next;
}

export function normalizedHistory(history) {
  const rows = history.filter(p => p.adjustmentVersion && Number.isFinite(p.stock) && p.stock > 0 && Number.isFinite(p.meme) && p.meme > 0);
  if (!rows.length) return [];
  // Source changes require a new comparison baseline. Never splice providers.
  const last = rows.at(-1);
  const key = r => JSON.stringify([r.stockSource, r.memeSource, r.referenceSymbol, r.adjustmentVersion, r.memeCurrency, r.memeScope]);
  let start = rows.length - 1;
  while (start > 0 && key(rows[start - 1]) === key(last)) start--;
  const base = rows[start];
  return rows.slice(start).map(p => ({ at: p.at, stock: 100 * p.stock / base.stock, meme: 100 * p.meme / base.meme }));
}

export function lineSegments(rows, field, maxGap = 300000) {
  const result = []; let segment = [];
  for (const row of rows) {
    if (!Number.isFinite(row[field]) || !Number.isFinite(row.at)) {
      if (segment.length) result.push(segment);
      segment = []; continue;
    }
    if (segment.length && ((row.gapBefore ?? (row.at - segment.at(-1).at > maxGap)) || row.at <= segment.at(-1).at || row.version !== segment.at(-1).version)) {
      result.push(segment); segment = [];
    }
    segment.push(row);
  }
  if (segment.length) result.push(segment);
  return result;
}

// Select original points, keeping extremes and gap boundaries. Never average
// prices or make new timestamps merely to reduce SVG/DOM work.
export function chartSamples(rows, fields, buckets = 200) {
  if (rows.length <= buckets * 2) return rows;
  const indices = new Set([0, rows.length - 1]);
  for (let i = 1; i < rows.length; i++) {
    if (rows[i].at - rows[i - 1].at > 300000) { indices.add(i - 1); indices.add(i); }
  }
  const width = Math.ceil(rows.length / buckets);
  for (let i = 0; i < rows.length; i += width) {
    for (const field of fields) {
      let lo = i, hi = i;
      for (let j = i + 1; j < Math.min(rows.length, i + width); j++) {
        if (rows[j][field] < rows[lo][field]) lo = j;
        if (rows[j][field] > rows[hi][field]) hi = j;
      }
      indices.add(lo); indices.add(hi);
    }
  }
  return [...indices].sort((a,b) => a-b).map(i => ({...rows[i], gapBefore: i > 0 && rows[i].at-rows[i-1].at > 300000}));
}

export function windowHistory(history, windowMs, now = Date.now()) {
  return history.filter(p => Number.isFinite(p.at) && p.at >= now - windowMs && p.at <= now + 1000);
}
