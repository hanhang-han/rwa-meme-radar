const assetIdentity = row => `${row?.chainId}:${String(row?.token ?? '').toLowerCase()}`;
const has = (row, field) => Object.hasOwn(row ?? {}, field);
const marketFields = ['price', 'change24h', 'change5m', 'change1h', 'change6h', 'volume24h', 'marketCap',
  'txs24h', 'buys24h', 'sells24h', 'holders', 'totalLiquidityUsd'];
const observationMaps = ['fieldTimes', 'fieldSources', 'fieldScopes', 'fieldTimeKinds', 'fieldObservations',
  'fieldStatus', 'fieldRevisions', 'fieldAvailability'];
const priceMetadata = ['provider', 'quoteAt', 'marketAt', 'receivedAt', 'timeKind', 'quoteStatus', 'quoteReason',
  'quoteType', 'venue', 'marketId', 'priceScope', 'priceCurrency', 'priceProvenance'];
const windowKeys = { change5m: 'm5', change1h: 'h1', change6h: 'h6' };
const fieldTime = (row, field) => Number(row?.productMetrics?.changes?.[windowKeys[field]]?.at
  ?? row?.fieldAvailability?.[field]?.at ?? row?.fieldTimes?.[field]
  ?? (field === 'totalLiquidityUsd' ? row?.totalLiquidityAt
    : field === 'price' || field.startsWith('change') ? row?.quoteAt ?? row?.marketAt : null)) || 0;
export const isOlderMemePublication = (before, after) => {
  const old = Number(before?.realtime?.revision), next = Number(after?.realtime?.revision);
  return Number.isFinite(old) && Number.isFinite(next) && next < old;
};
function retainField(target, previous, field) {
  if (has(previous, field)) target[field] = previous[field]; else delete target[field];
  for (const map of observationMaps) {
    if (has(previous[map], field)) target[map] = { ...target[map], [field]: previous[map][field] };
    else if (target[map]) delete target[map][field];
  }
  if (windowKeys[field] && (target.productMetrics?.changes || previous.productMetrics?.changes)) {
    const key = windowKeys[field], metric = previous.productMetrics?.changes?.[key];
    target.productMetrics = { ...target.productMetrics, changes: { ...target.productMetrics?.changes } };
    if (metric) target.productMetrics.changes[key] = metric; else delete target.productMetrics.changes[key];
  }
}
function retainMetadata(target, previous, fields) {
  for (const field of fields) if (has(previous, field)) target[field] = previous[field]; else delete target[field];
}

// Publications own relationship facts and primary-market selection. A slower
// HTTP response cannot roll back newer observations of that same market. An
// explicitly invalidated value is authoritative; an omitted field is unchanged.
export function mergeDirectoryAsset(previous, incoming) {
  if (!previous || assetIdentity(previous) !== assetIdentity(incoming)) return incoming;
  const target = { ...previous, ...incoming };
  for (const map of observationMaps) if (has(previous, map) || has(incoming, map))
    target[map] = { ...previous[map], ...incoming[map] };
  const marketChanged = ['marketId', 'venue', 'priceCurrency', 'priceScope', 'quoteType'].some(field =>
    has(incoming, field) && incoming[field] != null && previous[field] != null && incoming[field] !== previous[field]);
  for (const field of marketFields) {
    const metric = incoming.productMetrics?.changes?.[windowKeys[field]];
    const availability = incoming.fieldAvailability?.[field];
    const published = has(incoming, field) || has(availability, 'value') || has(metric, 'value');
    const invalidated = has(incoming, field) && incoming[field] === null
      || has(availability, 'value') && availability.value === null && availability.status === 'missing';
    const after = fieldTime(incoming, field), before = fieldTime(previous, field);
    const revision = Number(incoming.fieldRevisions?.[field]) || 0;
    const oldRevision = Number(previous.fieldRevisions?.[field]) || 0;
    // Field revisions order publication facts, not market observations. A
    // higher directory revision may still carry a quote older than a push.
    // For the same market use source time first, with revision only breaking
    // ties; invalidation and a real selected-market switch remain authoritative.
    const observationWins = after > before
      || after === before && (!revision || !oldRevision || revision >= oldRevision);
    const incomingWins = published && (invalidated || marketChanged && (field === 'price' || field.startsWith('change'))
      || observationWins);
    if (!incomingWins) {
      retainField(target, previous, field);
      if (field === 'price') retainMetadata(target, previous, priceMetadata);
      if (field === 'volume24h') retainMetadata(target, previous, ['volumeCurrency', 'volumeScope']);
      if (field === 'totalLiquidityUsd') retainMetadata(target, previous, ['totalLiquidityAt', 'totalLiquidityStatus', 'totalLiquidityCoverage']);
    } else {
      if (has(incoming, field)) target[field] = incoming[field];
      else if (has(availability, 'value')) target[field] = availability.value;
      if (field === 'price') for (const key of priceMetadata) if (has(incoming, key)) target[key] = incoming[key];
      if (field === 'volume24h') for (const key of ['volumeCurrency', 'volumeScope']) if (has(incoming, key)) target[key] = incoming[key];
      if (field === 'totalLiquidityUsd') for (const key of ['totalLiquidityAt', 'totalLiquidityStatus', 'totalLiquidityCoverage']) if (has(incoming, key)) target[key] = incoming[key];
      if (windowKeys[field] && metric) target.productMetrics = { ...target.productMetrics, changes: { ...target.productMetrics?.changes, [windowKeys[field]]: metric } };
      if (!has(incoming.fieldAvailability, field) && target.fieldAvailability) {
        // Old availability.value must not hide a newly accepted observation.
        delete target.fieldAvailability[field];
      }
    }
  }
  return target;
}

export function mergeMemeDirectoryQuotes(previous, packet, memberRows = []) {
  if (!previous) return packet;
  const index = new Map([...memberRows, ...(previous.groups ?? []).map(group => group.representative).filter(Boolean)]
    .map(row => [assetIdentity(row), row]));
  return { ...packet, groups: (packet.groups ?? []).map(group => group.representative
    ? { ...group, representative: mergeDirectoryAsset(index.get(assetIdentity(group.representative)), group.representative) }
    : group) };
}

// Commit the incoming ranking, membership, relationships and totals together.
// Older directory facts are ignored; newer field observations survive merging.
export function planMemeDirectoryRefresh(previous, packet) {
  if (isOlderMemePublication(previous, packet)) return { snapshot: previous, ignored: true };
  return { snapshot: mergeMemeDirectoryQuotes(previous, packet) };
}
