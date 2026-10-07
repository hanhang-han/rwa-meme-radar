const SUPPORTED_CHAINS = new Set(['196', '56', '4663', '5042']);

export function normalizeEventAddress(value, chainId = '196') {
  let address = String(value ?? '').trim();
  const chain = String(chainId ?? '196');
  while (SUPPORTED_CHAINS.has(chain) && address.startsWith(`${chain}:`)) {
    address = address.slice(chain.length + 1);
  }
  return address.toLowerCase();
}

export function eventKey(event, fallbackChain = '196') {
  const chain = String(event?.chainId ?? fallbackChain);
  const id = String(event?.id ?? '');
  if (id) return `${chain}:${id}`;
  return `${chain}:${normalizeEventAddress(event?.asset, chain)}:${Number(event?.t) || 0}:${event?.kind ?? ''}:${String(event?.pool??event?.relation?.pool??'').toLowerCase()}:${String(event?.stock??event?.relation?.stock??event?.ticker??'').toLowerCase()}`;
}

export function normalizeEvent(event, fallbackChain = '196') {
  const chainId = String(event?.chainId ?? fallbackChain);
  return {
    ...event,
    chainId,
    asset: normalizeEventAddress(event?.asset, chainId),
  };
}

export function mergeEventItems(current, incoming) {
  const byKey = new Map();
  for (const event of [...current, ...incoming]) {
    const normalized = normalizeEvent(event);
    const key = eventKey(normalized);
    const previous = byKey.get(key);
    if (!previous || Number(normalized.t ?? 0) >= Number(previous.t ?? 0)) byKey.set(key, normalized);
  }
  return [...byKey.values()].sort((a, b) =>
    Number(b.t ?? 0) - Number(a.t ?? 0) || eventKey(b).localeCompare(eventKey(a)),
  );
}

export function applyEventPage(currentItems, currentCursors, chain, page) {
  return {
    items: mergeEventItems(currentItems, (page?.items ?? []).map((item) => ({
      ...item,
      chainId: item.chainId ?? chain,
    }))),
    cursors: { ...currentCursors, [chain]: page?.next ?? null },
  };
}

export function eventFromRelationship(row) {
  const relation = row?.relation ?? {};
  const chainId = String(row?.chainId ?? relation.chainId ?? '196');
  const asset = normalizeEventAddress(relation.token ?? row?.asset, chainId);
  if (!asset) return null;
  return normalizeEvent({
    id: row?.id ?? `${chainId}:relation:${relation.id ?? asset}:${row?.at ?? row?.t ?? 0}`,
    chainId,
    asset,
    symbol: row?.symbol ?? relation.tokenSymbol,
    ticker: row?.ticker ?? relation.ticker,
    kind: row?.kind ?? 'verified',
    label: row?.label,
    relation,
    pool:row?.pool??relation.pool,
    stock:row?.stock??relation.stock,
    dex:row?.dex??relation.protocol,
    liquidityUsd:row?.liquidityUsd??relation.liquidityUsd,
    poolCreatedAt:row?.poolCreatedAt??relation.poolCreatedAt,
    t: row?.t ?? row?.at ?? relation.checkedAt,
  }, chainId);
}
