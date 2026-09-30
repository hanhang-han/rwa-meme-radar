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
      const task=getDetail(chain,address,{section:'summary'}).then(data=>{
        data.marketTrades=(data.marketTrades??[]).filter(t=>!this.removedTrades.has(`${id}:${t.venue}:${t.marketId}:${t.id}`));
        const entry=this.cache.get(id);const current=entry?.data;
        const revision=Number(data.revision??data.realtime?.revision)||0;
        if(current){
          const asset=mergeEntity(current.asset??{},data.asset??{},revision);
          const stock=data.stock?mergeEntity(current.stock??{},data.stock,revision):data.stock;
          if(entry.sections?.relations)delete data.relations;
          if(revision&&Number(current.revision??0)>revision){delete data.relations;delete data.relationCount;data.revision=current.revision;data.realtime=current.realtime;data.snapshotAt=current.snapshotAt;}
          const trades=new Map((data.trades??[]).map(t=>[t.id,t]));
          for(const t of current.trades??[])if(!trades.has(t.id))trades.set(t.id,t);
          const market=new Map((data.marketTrades??[]).map(t=>[`${t.venue}:${t.marketId}:${t.id}`,t]));
          for(const t of current.marketTrades??[])if(!market.has(`${t.venue}:${t.marketId}:${t.id}`))market.set(`${t.venue}:${t.marketId}:${t.id}`,t);
          Object.assign(current,data,{asset,stock,marketTrades:[...market.values()].filter(t=>!this.removedTrades.has(`${id}:${t.venue}:${t.marketId}:${t.id}`)).sort((a,b)=>b.t-a.t).slice(0,100),trades:[...trades.values()].sort((a,b)=>b.t-a.t).slice(0,150)});
          data=current;
        }
        if(entry){entry.data=data;entry.at=Date.now();}
        else{data.asset=mergeEntity(null,data.asset??{},revision);if(data.stock)data.stock=mergeEntity(null,data.stock,revision);this.cache.set(id,{data,at:Date.now(),sections:{}});}
        this.syncProjection();
        while(this.cache.size>60){const oldest=[...this.cache.keys()].find(key=>!this.active.has(key)&&key!==id);if(!oldest)break;this.cache.delete(oldest);}
        return data;
      }).finally(()=>this.pending.delete(id));
      this.pending.set(id,task);return task;
    },
    async fetchSection(chain,address,section,options={}){
      const id=assetKey(chain,address),key=`${id}:${section}:${options.offset??0}`;
      const hit=this.cache.get(id);
      if(!hit)return;
      if(!options.force&&!options.offset&&Date.now()-(hit.sections?.[section]??0)<20000)return hit.data;
      if(this.pending.has(key))return this.pending.get(key);
      const task=getDetail(chain,address,{section,...options}).then(packet=>{
        if(this.cache.get(id)!==hit)return packet;
        // Section payloads never replace the canonical quote or identity.
        const {asset,stock,revision,realtime,snapshotAt,next,...sectionData}=packet;
        if(asset&&section==='holders'&&(!revision||Number(revision)>=Number(hit.data.asset?._revision??0)))for(const field of ['holders','risk','riskFlags','riskStatus','riskAssessment'])if(asset[field]!==undefined)hit.data.asset[field]=asset[field];
        if(asset&&section==='markets')for(const field of ['exchangeMarkets','poolMarkets'])if(Array.isArray(asset[field]))hit.data.asset[field]=asset[field];
        for(const [field,value]of Object.entries(sectionData)){
          if(Array.isArray(value)&&options.offset){
            const rows=new Map((hit.data[field]??[]).map(r=>[`${r.venue??''}:${r.marketId??''}:${r.id??r.pool??r.address}`,r]));
            for(const r of value)rows.set(`${r.venue??''}:${r.marketId??''}:${r.id??r.pool??r.address}`,r);hit.data[field]=[...rows.values()];
          }else if(['marketTrades','trades'].includes(field)&&Array.isArray(value)){
            const rows=new Map(value.map(r=>[`${r.venue??''}:${r.marketId??''}:${r.id}`,r]));
            for(const r of hit.data[field]??[])if(!rows.has(`${r.venue??''}:${r.marketId??''}:${r.id}`))rows.set(`${r.venue??''}:${r.marketId??''}:${r.id}`,r);
            hit.data[field]=[...rows.values()].filter(t=>field!=='marketTrades'||!this.removedTrades.has(`${id}:${t.venue}:${t.marketId}:${t.id}`)).sort((a,b)=>b.t-a.t).slice(0,150);
          }else hit.data[field]=value;
        }
        hit.sections??={};hit.sections[section]=Date.now();hit.data.sectionNext??={};hit.data.sectionNext[section]=next??null;
        return hit.data;
      }).finally(()=>this.pending.delete(key));this.pending.set(key,task);return task;
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
        if(dash.snapshot.unified?.snapshotScope!=='overview'){
          const previous=new Map((data.relations??[]).map(r=>[r.id,r]));
          data.relations=dash.relations.filter(r=>String(r.chainId??'196')===chain&&(String(r.token).toLowerCase()===address||String(r.stock).toLowerCase()===address)).map(r=>({...previous.get(r.id),...r}));data.relationCount=data.relations.length;
        }
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
