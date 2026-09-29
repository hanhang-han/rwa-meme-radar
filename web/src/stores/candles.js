import {defineStore} from 'pinia';
import {candleKey,mergeCandleRows,candlePacketMatches} from '../utils/candles.js';
export const useCandleStore=defineStore('candles',{
  state:()=>({series:new Map(),version:0,resets:new Map(),resetVersion:0,subscriptions:new Map()}),
  getters:{
    subscriptionScope:s=>!s.subscriptions.size ? 'none' : [...s.subscriptions.values()].some(p=>!p.marketId&&!p.poolId) ? 'all' : [...new Set([...s.subscriptions.values()].map(candleKey))].sort().join(','),
  },
  actions:{
    subscribe(owner,selection){this.subscriptions.set(owner,{...selection});},
    unsubscribe(owner){this.subscriptions.delete(owner);},
    receive(packet){
      if(!packet?.chainId||!packet.token||!packet.venue||!packet.bar||!packet.row)return false;
      const key=candleKey(packet),old=this.series.get(key);
      const row={...packet.row,sourceEventAt:packet.sourceEventAt,receivedAt:packet.receivedAt,revision:packet.revision??packet.row.revision};
      const rows=mergeCandleRows(old?.rows??[],[row],1000);if(!rows.length)return false;
      this.series.set(key,{...old,...packet,rows,at:Date.now()});this.version++;
      while(this.series.size>100)this.series.delete(this.series.keys().next().value);
      return true;
    },
    reset(packet){
      const key=candleKey(packet);this.series.delete(key);this.resetVersion++;
      this.resets.set(key,{...packet,at:Date.now(),resetVersion:this.resetVersion});this.version++;
      while(this.resets.size>100)this.resets.delete(this.resets.keys().next().value);
      return true;
    },
    matching(selection){return [...this.series.values()].filter(p=>candlePacketMatches(p,selection)).sort((a,b)=>b.at-a.at)[0]??null;},
  },
});
