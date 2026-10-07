import { defineStore } from 'pinia';
import { API_BASE } from '../api/client.js';
import { getMemeChart, getMemeDirectory, getMemeGroup, peekMemeDirectory } from '../api/product.js';
import { memeDirectoryPath, memeFilterKey, MEME_GROUP_SEGMENT_SIZE } from '../utils/meme-directory-query.js';
import { createMemeDirectoryStream } from '../utils/meme-directory-stream.js';
import { mergeDirectoryAsset, mergeMemeDirectoryQuotes, planMemeDirectoryRefresh } from '../utils/meme-directory-refresh.js';
import { applyVisibleQuote } from '../utils/visible-quotes.js';
import { applyVisibleExchangeQuote } from '../utils/visible-exchange-quote.js';

const runtimes = new WeakMap();
const identity = row => `${row.chainId}:${String(row.token).toLowerCase()}`;
function runtime(store) {
  if (!runtimes.has(store)) runtimes.set(store, { active: false, request: 0, pending: null, pendingKey: null, key: null, stream: null, timer: null, invalidateTimer: null, dirty: false, lastRefreshAt: 0, visibility: null, arrivalTimer: null, cells: new Map(), memberTasks: new Map(), chartRequest: 0 });
  return runtimes.get(store);
}
const revisionChanged = error => error?.message === 'HTTP 409';
const samePublication = (a, b) => a?.realtime?.revision != null && b?.realtime?.revision != null
  ? String(a.realtime.revision) === String(b.realtime.revision)
  : Number(a?.now) === Number(b?.now);

function commitMembers(store, entry, next) {
  const oldKeys = new Set(entry.rows.map(identity));
  if (entry.rows.length) store.markAdditions(next.rows.map(identity).filter(key => !oldKeys.has(key)));
  Object.assign(entry, next, { stale: false });
}

export const useMemeDirectoryStore = defineStore('meme-directory', {
  state: () => ({ snapshot: null, query: null, loading: false, error: null, chartVisible: false, chartLoading: false, chartError: null,
    members: {}, addedKeys: [], stream: { connected: false, state: 'connecting', lastMessageAt: 0 } }),
  getters: {
    ready: state => !!state.snapshot,
    groups: state => state.snapshot?.groups ?? [],
    summary: state => state.snapshot?.summary ?? {},
    directory: state => state.snapshot?.directory ?? {},
    relations: state => [...(state.snapshot?.unified?.relations ?? []), ...Object.values(state.members).filter(entry => !entry.stale).flatMap(entry => entry.relations ?? [])],
  },
  actions: {
    async setQuery(query, { force = false } = {}) {
      const rt = runtime(this), key = memeDirectoryPath(query);
      if (rt.key === key && !force) return rt.pending ?? this.snapshot;
      const changed = rt.key !== key;
      if (changed) {
        rt.key = key; rt.request++; rt.chartRequest++;
        this.query = { ...query };
        this.snapshot = peekMemeDirectory(query);
        this.members = {}; this.addedKeys = []; this.chartError = null; this.chartLoading = false;
        rt.memberTasks.clear();
      }
      return this.refresh({ force });
    },
    commitPacket(packet) {
      const rt = runtime(this), previous = this.snapshot;
      packet = mergeMemeDirectoryQuotes(previous, packet, Object.values(this.members).flatMap(entry => entry.rows ?? []));
      if (samePublication(previous, packet) && packet.chart?.requiresFetch && previous?.chart && !previous.chart.requiresFetch) {
        packet = { ...packet, chart: previous.chart };
      }
      if (previous) {
        const previousKeys = new Set((previous.groups ?? []).map(group => group.representative).filter(Boolean).map(identity));
        this.markAdditions((packet.groups ?? []).map(group => group.representative).filter(Boolean).map(identity).filter(key => !previousKeys.has(key)));
      }
      this.snapshot = packet;
      if (!samePublication(previous, packet)) {
        // Keep rows and their actual observation times while updating expanded
        // groups, but exclude old relationship evidence from the new publication.
        this.members = Object.fromEntries(Object.entries(this.members)
          .filter(([symbol]) => packet.groups?.some(group => group.symbol === symbol))
          .map(([symbol, entry]) => [symbol, { ...entry, stale: true, refreshing: false, loading: false, error: null, relations: [] }]));
        rt.memberTasks.clear();
      }
      rt.chartRequest++; this.chartLoading = false; this.chartError = null;
      if (this.chartVisible && packet.chart?.requiresFetch) this.loadChart();
    },
    markAdditions(keys) {
      if (!keys.length) return;
      const rt = runtime(this); clearTimeout(rt.arrivalTimer);
      this.addedKeys = [...new Set([...this.addedKeys, ...keys])].slice(-80);
      rt.arrivalTimer = setTimeout(() => { this.addedKeys = []; rt.arrivalTimer = null; }, 2000);
    },
    applyQuotePacket(packet) {
      // Mutate quote fields in place: no publication commit, resort, chart
      // reload or relationship replacement is caused by a price tick.
      let changed = false;
      const apply = row => {
        const primary = applyVisibleQuote(row, packet);
        const exchange = applyVisibleExchangeQuote(row, packet);
        return primary || exchange;
      };
      for (const group of this.snapshot?.groups ?? []) if (group.representative)
        changed = apply(group.representative) || changed;
      for (const entry of Object.values(this.members)) {
        for (const row of entry.rows ?? []) changed = apply(row) || changed;
      }
      return changed;
    },
    setChartVisible(visible) {
      this.chartVisible = !!visible;
      if (this.chartVisible) this.loadChart();
      else { runtime(this).chartRequest++; this.chartLoading = false; }
    },
    refresh({ force = true } = {}) {
      if (!this.query) return Promise.resolve(null);
      const rt = runtime(this);
      if (rt.pending && rt.pendingKey === rt.key) return rt.pending;
      const request = ++rt.request, query = { ...this.query }, key = rt.key;
      rt.dirty = false; rt.lastRefreshAt = Date.now(); rt.pendingKey = key;
      this.loading = true; this.error = null;
      const task = (async () => {
        try {
          const packet = await getMemeDirectory(query, { force });
          if (request !== rt.request || key !== rt.key) return null;
          const plan = planMemeDirectoryRefresh(this.snapshot, packet);
          if (plan.ignored) return null;
          this.commitPacket(plan.snapshot);
          if (rt.active && !rt.stream) this.connect();
          return packet;
        } catch (error) {
          if (request === rt.request && key === rt.key) this.error = String(error?.message ?? error);
          return null;
        } finally {
          if (request === rt.request && key === rt.key) { this.loading = false; rt.pending = null; rt.pendingKey = null; if (rt.dirty) this.queueRefresh(); }
        }
      })();
      rt.pending = task;
      return task;
    },
    async loadChart() {
      if (!this.chartVisible || this.chartLoading || !this.query || this.query.view === 'pool' || !this.snapshot?.chart?.requiresFetch) return;
      const rt = runtime(this), request = ++rt.chartRequest, key = rt.key, base = this.snapshot, query = { ...this.query };
      this.chartLoading = true; this.chartError = null;
      try {
        const packet = await getMemeChart(query, { revision: base.realtime?.revision, force: true });
        if (key !== rt.key || request !== rt.chartRequest || this.snapshot !== base) return;
        if (!samePublication(base, packet)) throw new Error('HTTP 409');
        this.snapshot = { ...base, chart: packet.chart };
      } catch (error) {
        if (key !== rt.key || request !== rt.chartRequest) return;
        if (revisionChanged(error)) { this.queueRefresh(); return; }
        this.chartError = String(error?.message ?? error);
      } finally { if (key === rt.key && request === rt.chartRequest) this.chartLoading = false; }
    },
    async loadGroup(symbol, { more = false, focus } = {}) {
      if (!this.query || this.query.view === 'pool' || !this.snapshot) return null;
      const rt = runtime(this), key = rt.key, base = this.snapshot, revision = base.realtime?.revision;
      const existing = this.members[symbol];
      if (existing?.loading && rt.memberTasks.has(symbol)) {
        await rt.memberTasks.get(symbol);
        if (key !== rt.key) return null;
        if (focus && !this.members[symbol]?.rows.some(row=>identity(row)===focus)) return this.loadGroup(symbol,{focus});
        return this.members[symbol] ?? null;
      }
      if (existing && !more && !focus && !existing.error && !existing.stale) return existing;
      const offset = more && !existing?.stale ? (existing?.nextOffset ?? 0) : 0;
      let entry = existing ?? { rows: [], relations: [], total: 0, nextOffset: 0, segments: {}, loading: false, error: null, filterKey: memeFilterKey(this.query), revision };
      const replacesPublication = !!entry.stale;
      const force = !!entry.error || replacesPublication;
      entry.loading = true; entry.refreshing = replacesPublication; entry.error = null; this.members[symbol] = entry; entry = this.members[symbol];
      const task = (async () => {
        try {
          const packet = await getMemeGroup({ ...this.query }, symbol, { offset, focus, revision, force });
          if (key !== rt.key || !samePublication(this.snapshot, packet) || this.members[symbol] !== entry) return null;
          const packets = [packet];
          // Refresh all opened segments together so the updated group retains
          // its expanded contracts instead of collapsing to the first segment.
          if (replacesPublication) {
            const firstOffset = Number(packet.group?.offset ?? offset);
            const offsets = Object.keys(entry.segments).map(Number)
              .filter(start => start !== firstOffset && start < Number(packet.group?.memberCount ?? 0))
              .sort((a, b) => a - b);
            for (let start = 0; start < offsets.length; start += 3) {
              const batch = await Promise.all(offsets.slice(start, start + 3).map(segment =>
                getMemeGroup({ ...this.query }, symbol, { offset: segment, revision, force: true })));
              if (key !== rt.key || this.members[symbol] !== entry || batch.some(part => !samePublication(this.snapshot, part))) return null;
              packets.push(...batch);
            }
          }
          if (key !== rt.key || !samePublication(this.snapshot, packet) || this.members[symbol] !== entry) return null;
          const rows = new Map(!replacesPublication && (more || focus) ? entry.rows.map(row => [identity(row), row]) : []);
          const segments = replacesPublication ? {} : { ...entry.segments };
          const relations = replacesPublication ? [] : [...entry.relations];
          for (const part of packets) {
            const segmentOffset = Number(part.group?.offset ?? offset);
            for (const [index, row] of (part.rows ?? []).entries()) {
              const previousRow = entry.rows.find(previous => identity(previous) === identity(row));
              rows.set(identity(row), { ...mergeDirectoryAsset(previousRow, row), _directoryMemberIndex: segmentOffset + index });
            }
            segments[segmentOffset] = part.rows?.length ?? 0;
            relations.push(...(part.unified?.relations ?? []));
          }
          const nextRows = [...rows.values()].sort((a, b) => a._directoryMemberIndex - b._directoryMemberIndex);
          const nextRelations = [...new Map(relations.map(row => [`${row.chainId}:${row.id}`, row])).values()];
          let nextOffset = 0;
          while (segments[nextOffset] > 0) nextOffset += segments[nextOffset];
          const total = packet.group?.memberCount ?? nextRows.length;
          const next = { rows: nextRows, relations: nextRelations, segments, revision, total, nextOffset,
            hasMore: nextOffset < total, loadedSegmentSize: MEME_GROUP_SEGMENT_SIZE };
          commitMembers(this, entry, next);
          return entry;
        } catch (error) {
          if (key !== rt.key || this.members[symbol] !== entry) return null;
          if (revisionChanged(error)) this.queueRefresh();
          else entry.error = String(error?.message ?? error);
          return null;
        } finally {
          if (this.members[symbol] === entry) { entry.loading = false; entry.refreshing = false; }
          if (rt.memberTasks.get(symbol) === task) rt.memberTasks.delete(symbol);
        }
      })();
      rt.memberTasks.set(symbol, task);
      return task;
    },
    queueRefresh() {
      const rt = runtime(this);
      rt.dirty = true;
      if (!rt.active || rt.invalidateTimer || rt.pending || (typeof document !== 'undefined' && document.hidden)) return;
      const delay = Math.max(300, 5000 - (Date.now() - rt.lastRefreshAt));
      rt.invalidateTimer = setTimeout(() => { rt.invalidateTimer = null; if (rt.active && rt.dirty) this.refresh(); }, delay);
    },
    connect() {
      const rt = runtime(this);
      if (!rt.active || rt.stream || (typeof document !== 'undefined' && document.hidden)) return;
      rt.stream = createMemeDirectoryStream({ apiBase: API_BASE, getCursor: () => this.snapshot?.realtime?.cursor,
        onInvalidate: () => this.queueRefresh(), onStatus: patch => Object.assign(this.stream, patch) });
      rt.stream.start();
    },
    start() {
      const rt = runtime(this);
      if (rt.active) return;
      rt.active = true;
      if (this.snapshot) this.connect();
      rt.timer = setInterval(() => { if (typeof document === 'undefined' || !document.hidden) { this.refresh(); this.connect(); } }, 30000);
      rt.visibility = () => {
        if (document.hidden) { rt.stream?.stop(); rt.stream = null; }
        else { this.refresh(); this.connect(); }
      };
      if (typeof document !== 'undefined') document.addEventListener('visibilitychange', rt.visibility);
    },
    stop() {
      const rt = runtime(this); rt.active = false; rt.request++; rt.chartRequest++;
      clearInterval(rt.timer); clearTimeout(rt.arrivalTimer); this.addedKeys = []; clearTimeout(rt.invalidateTimer); rt.invalidateTimer = null; rt.dirty = false;
      rt.stream?.stop(); rt.stream = null; rt.memberTasks.clear();
      if (typeof document !== 'undefined' && rt.visibility) document.removeEventListener('visibilitychange', rt.visibility);
      this.loading = false; this.chartLoading = false; this.chartVisible = false; rt.pending = null; rt.pendingKey = null; rt.key = null;
    },
    diffCells(key, text) {
      const cells = runtime(this).cells, previous = cells.get(key); cells.delete(key); cells.set(key, text);
      while (cells.size > 2000) cells.delete(cells.keys().next().value);
      return previous !== undefined && previous !== text;
    },
  },
});
