import { defineStore } from 'pinia';
import { getFeed } from '../api/client.js';
const feedLoadTasks = new Map();
export const tradeKey = t => `${t.chainId ?? '196'}:${String(t.token ?? '').toLowerCase()}:${String(t.venue ?? 'dex').toLowerCase()}:${String(t.marketId ?? '').toLowerCase()}:${t.id}`;
export const relKey = r => r?.id
  ? String(r.id)
  : `${r?.chainId ?? '196'}:relation:${r?.relation?.id ?? ''}:${r?.kind ?? ''}`;
function relationshipTime(row) {
  const value = Number(row?.relation?.checkedAt ?? row?.t ?? row?.at);
  return Number.isFinite(value) && value > 0 ? value : null;
}
function acceptsSnapshot(existing, incoming) {
  if (!existing) return true;
  const before = relationshipTime(existing), after = relationshipTime(incoming);
  return before !== null && after !== null && after > before;
}
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
    load(chainId, { fresh = false } = {}) {
      const loadKey = (chainId || 'all') + (fresh ? ':fresh' : '');
      if (feedLoadTasks.has(loadKey)) return feedLoadTasks.get(loadKey);
      const task = (async () => {
        this._loading = true;
        try {
          const d = await getFeed(chainId, { fresh });
          if (this.loaded) {
            this.appendTrades(d.trades ?? []);
            for (const r of d.relationships ?? []) this.appendRelationship(r, { snapshot: true });
          } else {
            this.trades = merge((d.trades ?? []).filter(t=>!this.removedTrades.has(tradeKey(t))), this.trades, tradeKey, 300);
            const accepted = (d.relationships ?? []).filter(r => acceptsSnapshot(
              [...this.pendingRelationships, ...this.relationships].find(x => relKey(x) === relKey(r)), r));
            this.relationships = merge(this.relationships, accepted, relKey, 60);
          }
          if (Number.isFinite(d.at)) this.lastAt = Math.max(this.lastAt, d.at);
          this.loaded = true; this.error = null;
          return d;
        } catch(e) { this.error = String(e?.message ?? e); if (fresh) throw e; }
        finally { feedLoadTasks.delete(loadKey); this._loading = feedLoadTasks.size > 0; }
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
    appendRelationship(r, { snapshot = false } = {}) {
      if (!(r?.relation?.id ?? r?.id)) return false;
      const existing = [...this.pendingRelationships, ...this.relationships].find(x => relKey(x) === relKey(r));
      if (snapshot && !acceptsSnapshot(existing, r)) return false;
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
