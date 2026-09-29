import { defineStore } from 'pinia';
import { getFeed } from '../api/client.js';
const feedLoadTasks = new Map();
export const tradeKey = t => `${t.chainId ?? '196'}:${String(t.token ?? '').toLowerCase()}:${String(t.venue ?? 'dex').toLowerCase()}:${String(t.marketId ?? '').toLowerCase()}:${t.id}`;
export const relKey = r => r?.id
  ? String(r.id)
  : `${r?.chainId ?? '196'}:relation:${r?.relation?.id ?? ''}:${r?.kind ?? ''}`;
function merge(rows, incoming, key, limit) {
  const map = new Map(rows.map(r => [key(r), r]));
  for (const r of incoming) map.set(key(r), r);
  return [...map.values()].sort((a,b) => (b.t ?? b.relation?.checkedAt ?? 0) - (a.t ?? a.relation?.checkedAt ?? 0)).slice(0,limit);
}
export const useFeedStore = defineStore('feed', {
  state: () => ({ removedTrades:new Set(), trades: [], relationships: [], pendingTrades: [], pendingRelationships: [], loaded: false,
    error: null, newCount: 0, paused: false, lastAt: 0, _loading: false }),
  getters: { hasNew: s => s.newCount > 0 },
  actions: {
    load(chainId) {
      const loadKey = chainId || 'all';
      if (feedLoadTasks.has(loadKey)) return feedLoadTasks.get(loadKey);
      const task = (async () => {
        this._loading = true;
        try {
          const d = await getFeed(chainId);
          if (this.loaded) {
            this.appendTrades(d.trades ?? []);
            for (const r of d.relationships ?? []) this.appendRelationship(r);
          } else {
            this.trades = merge((d.trades ?? []).filter(t=>!this.removedTrades.has(tradeKey(t))), this.trades, tradeKey, 300);
            this.relationships = merge(d.relationships ?? [], this.relationships, relKey, 60);
          }
          this.lastAt = d.at ?? Date.now(); this.loaded = true; this.error = null;
        } catch(e) { this.error = String(e?.message ?? e); }
        finally { this._loading = false; feedLoadTasks.delete(loadKey); }
      })();
      feedLoadTasks.set(loadKey, task);
      return task;
    },
    appendTrades(rows) {
      const known = new Set([...this.trades, ...this.pendingTrades].map(tradeKey));
      const fresh = [];
      for (const r of rows) if (r?.id && !known.has(tradeKey(r)) && !this.removedTrades.has(tradeKey(r))) {
        known.add(tradeKey(r)); fresh.push({...r, receivedAt: r.receivedAt ?? Date.now(), browserReceivedAt: Date.now()});
      }
      const field = this.paused ? 'pendingTrades' : 'trades';
      this[field] = merge(this[field], fresh, tradeKey, 300);
      this.newCount = this.pendingTrades.length + this.pendingRelationships.length;
      return fresh.length;
    },
    removeTrades(packet){
      const ids=new Set(packet.ids??[]);const matches=t=>String(t.chainId)===String(packet.chainId)&&t.token?.toLowerCase()===packet.token?.toLowerCase()&&(!packet.venue||t.venue===packet.venue)&&(!packet.marketId||t.marketId===packet.marketId)&&ids.has(t.id);
      for(const id of ids)this.removedTrades.add(tradeKey({...packet,id}));
      this.trades=this.trades.filter(t=>!matches(t));this.pendingTrades=this.pendingTrades.filter(t=>!matches(t));
      this.newCount=this.pendingTrades.length+this.pendingRelationships.length;
      while(this.removedTrades.size>5000)this.removedTrades.delete(this.removedTrades.values().next().value);
    },
    appendRelationship(r) {
      if (!(r?.relation?.id ?? r?.id)) return false;
      const existing = [...this.pendingRelationships, ...this.relationships].find(x => relKey(x) === relKey(r));
      if (existing && JSON.stringify(existing) === JSON.stringify(r)) return true;
      const field = this.paused ? 'pendingRelationships' : 'relationships';
      this[field] = merge(this[field], [r], relKey, 60);
      this.newCount = this.pendingTrades.length + this.pendingRelationships.length;
      return true;
    },
    clearNew() {
      this.trades = merge(this.trades, this.pendingTrades, tradeKey, 300);
      this.relationships = merge(this.relationships, this.pendingRelationships, relKey, 60);
      this.pendingTrades = []; this.pendingRelationships = []; this.newCount = 0;
    },
    setPaused(v) { this.paused = v; if (!v) this.clearNew(); },
  },
});
