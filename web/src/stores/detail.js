// Detail store: per (chain,address) cache with a 20s TTL and in-flight
// dedupe, porting cachedDetail; also tracks the currently watched asset so
// SSE price/trade events can update the page live.
import { defineStore } from 'pinia';
import { getDetail } from '../api/client';

export const useDetailStore = defineStore('detail', {
  state: () => ({
    cache: new Map(),
    pending: new Map(),
    current: null, // { chain, address }
  }),
  getters: {
    currentData: (s) => {
      if (!s.current) return null;
      return s.cache.get(`${s.current.chain}:${s.current.address}`)?.data ?? null;
    },
  },
  actions: {
    async fetch(chain, address) {
      const id = `${chain}:${address}`;
      const hit = this.cache.get(id);
      if (hit && Date.now() - hit.at < 20000) return hit.data;
      if (this.pending.has(id)) return this.pending.get(id);
      const task = getDetail(chain, address)
        .then((data) => {
          this.cache.set(id, { data, at: Date.now() });
          return data;
        })
        .finally(() => this.pending.delete(id));
      this.pending.set(id, task);
      return task;
    },
    watch(chain, address) {
      this.current = { chain, address };
    },
    unwatch() {
      this.current = null;
    },
  },
});
