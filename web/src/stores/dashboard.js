// The unified snapshot store: 20s polling with ETag, in-memory asset objects
// so SSE price events can mutate values before the next full poll.
import { defineStore } from 'pinia';
import { getDashboard } from '../api/client';

export const useDashboardStore = defineStore('dashboard', {
  state: () => ({
    snapshot: null,
    updatedAt: 0,
    error: null,
    lastCellVals: new Map(),
    lastKpiVals: new Map(),
    _timer: null,
  }),
  getters: {
    feed: (s) => s.snapshot?.unified ?? {},
    assets: (s) => s.snapshot?.unified?.assets ?? [],
    relations: (s) => s.snapshot?.unified?.relations ?? [],
    stockTokens: (s) => s.snapshot?.unified?.stockTokens ?? [],
    groups: (s) => s.snapshot?.unified?.groups ?? [],
    metrics: (s) => s.snapshot?.unified?.metrics ?? {},
  },
  actions: {
    async poll() {
      try {
        const data = await getDashboard();
        this.snapshot = data;
        this.updatedAt = Date.now();
        this.error = null;
      } catch (e) {
        this.error = String(e?.message ?? e);
      }
    },
    start() {
      if (this._timer) return;
      this.poll();
      this._timer = setInterval(() => this.poll(), 20000);
    },
    stop() {
      if (this._timer) clearInterval(this._timer);
      this._timer = null;
    },
    assetByToken(token) {
      return this.assets.find((a) => a.token === token);
    },
    // SSE price event: mutate the in-memory asset; views flash via watch.
    applyPrice(token, price, at) {
      const a = this.assets.find((x) => x.token === token);
      if (!a) return false;
      a.price = price;
      a.fieldTimes = { ...(a.fieldTimes ?? {}), price: at };
      return true;
    },
    // Diff-based cell flashing: returns the list of keys that changed since
    // the previous snapshot so views can flash only those cells.
    diffCells(key, text) {
      const prev = this.lastCellVals.get(key);
      this.lastCellVals.set(key, text);
      return prev !== undefined && prev !== text;
    },
    diffKpi(key, text) {
      const prev = this.lastKpiVals.get(key);
      this.lastKpiVals.set(key, text);
      return prev !== undefined && prev !== text;
    },
  },
});
