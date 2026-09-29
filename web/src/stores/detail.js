import { defineStore } from 'pinia';
import { getDetail } from '../api/client.js';
import { applyQuote, assetKey, mergeEntity } from '../utils/realtime.js';
import { useDashboardStore } from './dashboard.js';
const refreshTimers=new Map();
export const useDetailStore=defineStore('detail',{
  state:()=>({cache:new Map(),pending:new Map(),current:null,active:new Map(),removedTrades:new Set()}),
  getters:{currentData:s=>s.current?s.cache.get(assetKey(s.current.chain,s.current.address))?.data??null:null},
  actions:{
    async fetch(chain,address,options={}){
      address=String(address).toLowerCase();const id=assetKey(chain,address),hit=this.cache.get(id);
      if(!options.force&&hit&&Date.now()-hit.at<20000)return hit.data;
      if(this.pending.has(id))return this.pending.get(id);
      const task=getDetail(chain,address).then(data=>{
        data.marketTrades=(data.marketTrades??[]).filter(t=>!this.removedTrades.has(`${id}:${t.venue}:${t.marketId}:${t.id}`));
        const current=this.cache.get(id)?.data;
        if(current){
          const asset=mergeEntity(current.asset??{},data.asset??{});
          const stock=data.stock?mergeEntity(current.stock??{},data.stock):data.stock;
          const trades=new Map((data.trades??[]).map(t=>[t.id,t]));
          for(const t of current.trades??[])if(!trades.has(t.id))trades.set(t.id,t);
          const market=new Map((data.marketTrades??[]).map(t=>[`${t.venue}:${t.marketId}:${t.id}`,t]));
          for(const t of current.marketTrades??[])if(!market.has(`${t.venue}:${t.marketId}:${t.id}`))market.set(`${t.venue}:${t.marketId}:${t.id}`,t);
          Object.assign(current,data,{asset,stock,marketTrades:[...market.values()].filter(t=>!this.removedTrades.has(`${id}:${t.venue}:${t.marketId}:${t.id}`)).sort((a,b)=>b.t-a.t).slice(0,100),trades:[...trades.values()].sort((a,b)=>b.t-a.t).slice(0,150)});
          data=current;
        }
        this.cache.set(id,{data,at:Date.now()});this.syncProjection();
        while(this.cache.size>60){const oldest=[...this.cache.keys()].find(key=>!this.active.has(key)&&key!==id);if(!oldest)break;this.cache.delete(oldest);}
        return data;
      }).finally(()=>this.pending.delete(id));
      this.pending.set(id,task);return task;
    },
    activate(chain,address){const id=assetKey(chain,address);this.active.set(id,(this.active.get(id)??0)+1);return()=>{const n=(this.active.get(id)??1)-1;if(n>0)this.active.set(id,n);else this.active.delete(id);};},
    watch(chain,address){this.current={chain:String(chain),address:String(address).toLowerCase()};},
    unwatch(){this.current=null;},
    applyQuote(quote){
      const data=this.cache.get(assetKey(quote.chainId,quote.token))?.data;if(!data)return false;
      if(data.asset?.priceScope==='exchange'&&quote.priceScope==='dex')return false;
      let applied=applyQuote(data.asset,quote);
      if(data.stock)applied=applyQuote(data.stock,quote)||applied;
      return applied;
    },
    appendTrades(packet){
      const data=this.cache.get(assetKey(packet.chainId,packet.token))?.data;if(!data)return;
      const rows=new Map((data.trades??[]).map(t=>[t.id,t]));
      for(const t of packet.fresh??[])rows.set(t.id,t);
      data.trades=[...rows.values()].sort((a,b)=>b.t-a.t).slice(0,150);
    },
    removeTrades(packet){
      const key=assetKey(packet.chainId,packet.token),data=this.cache.get(key)?.data,ids=new Set(packet.ids??[]);
      for(const id of ids)this.removedTrades.add(`${key}:${packet.venue}:${packet.marketId}:${id}`);
      if(data)data.marketTrades=(data.marketTrades??[]).filter(t=>!(ids.has(t.id)&&(!packet.venue||packet.venue===t.venue)&&(!packet.marketId||packet.marketId===t.marketId)));
      while(this.removedTrades.size>5000)this.removedTrades.delete(this.removedTrades.values().next().value);
    },
    appendMarketTrade(trade){
      if(this.removedTrades.has(`${assetKey(trade.chainId,trade.token)}:${trade.venue}:${trade.marketId}:${trade.id}`))return;
      const data=this.cache.get(assetKey(trade.chainId,trade.token))?.data;if(!data)return;
      const rows=data.marketTrades??(data.marketTrades=[]);
      if(!rows.some(t=>t.id===trade.id&&t.venue===trade.venue&&t.marketId===trade.marketId))rows.unshift(trade);
      rows.sort((a,b)=>b.t-a.t);rows.splice(100);
    },
    syncProjection(){
      const dash=useDashboardStore();
      for(const [key,{data}]of this.cache){
        const row=dash.assetIndex.get(key),stock=dash.stockIndex.get(key);
        if(row&&data.asset)mergeEntity(data.asset,row,row._revision);
        if(stock){if(data.stock)mergeEntity(data.stock,stock,stock._revision);if(data.asset)mergeEntity(data.asset,{...stock,token:stock.tokenContractAddress},stock._revision);}
        if(!dash.snapshot)continue;
        const [chain,address]=key.split(':');
        data.relations=dash.relations.filter(r=>String(r.chainId??'196')===chain&&(String(r.token).toLowerCase()===address||String(r.stock).toLowerCase()===address));
      }
    },
    invalidate(resource={}){
      for(const [key,hit] of this.cache){
        const [chain,address]=key.split(':');
        if(resource.chainId&&String(resource.chainId)!==chain||resource.token&&String(resource.token).toLowerCase()!==address)continue;
        hit.at=0;
        if(!this.active.has(key)||refreshTimers.has(key))continue;
        refreshTimers.set(key,setTimeout(()=>{refreshTimers.delete(key);if(this.active.has(key))this.fetch(chain,address,{force:true}).catch(()=>{});},150));
      }
    },
  },
});
