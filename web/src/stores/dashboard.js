// Canonical list entities retain identity across snapshots and delta batches.
// The stream is authoritative; full snapshots are bootstrap/recovery/checks.
import { defineStore } from 'pinia';
import { getDashboard } from '../api/client.js';
import { applyQuote, assetKey, mergeEntity, normalizeAddress, quoteKey } from '../utils/realtime.js';
const pollTasks = new Map();
const rowKey = (group, row) => row.projectionKey ?? (group === 'assets' ? assetKey(row.chainId, row.token)
  : group === 'stockTokens' ? assetKey(row.chainId, row.tokenContractAddress)
  : group === 'relations' ? `${row.chainId ?? '196'}:${row.id}`
  : group === 'sectors' ? `${row.chainId ?? '196'}:${row.sector}`
  : String(row.id ?? row.symbol ?? row.key));
const GROUPS = ['assets','stockTokens','relations','sectors'];
export const projectionRevision = data => Number(data?.revision ?? data?.realtime?.revision ?? data?.realtime?.cursor) || 0;

export const useDashboardStore = defineStore('dashboard', {
  state: () => ({snapshot:null, updatedAt:0, lastSnapshotAt:0, error:null, revision:0, cursor:null, recentDeltas:[],bufferFloorRevision:0,
    lastCellVals:new Map(), lastKpiVals:new Map(), lastSse:new Map(), discoveries:new Map(), liveRelations:new Map(),
    resources:new Map(), stream:{connected:false,state:'connecting',lastAt:0,lastMessageAt:0,reconnects:0,projection:false},_timer:null}),
  getters: {
    feed:s=>s.snapshot?.unified ?? {}, assets:s=>s.snapshot?.unified?.assets ?? [],
    relations:s=>s.snapshot?.unified?.relations ?? [], stockTokens:s=>s.snapshot?.unified?.stockTokens ?? [],
    groups:s=>s.snapshot?.unified?.groups ?? [], metrics:s=>s.snapshot?.unified?.metrics ?? {}, sources:s=>s.snapshot?.unified?.sources ?? [],
    assetIndex:s=>new Map((s.snapshot?.unified?.assets ?? []).map(r=>[assetKey(r.chainId,r.token),r])),
    stockIndex:s=>new Map((s.snapshot?.unified?.stockTokens ?? []).map(r=>[assetKey(r.chainId,r.tokenContractAddress),r])),
    hasProjectionStream:s=>s.stream.connected && s.stream.projection,
  },
  actions: {
    acceptSnapshot(data, view='full') {
      if (!data?.unified || !Array.isArray(data.unified.assets) || !Array.isArray(data.unified.stockTokens)) throw new Error('invalid-dashboard');
      if (view === 'overview' && this.snapshot && this.snapshot.unified?.snapshotScope !== 'overview') return;
      const revision = projectionRevision(data);
      if(revision && revision<this.bufferFloorRevision)throw new Error('snapshot-behind-replay-window');
      const buffered = this.recentDeltas.filter(d=>projectionRevision(d)>revision);
      const before = this.snapshot?.unified;
      for (const group of GROUPS) {
        const known = new Map((before?.[group] ?? []).map(row=>[rowKey(group,row),row]));
        if (Array.isArray(data.unified[group])) data.unified[group] = data.unified[group].map(row=>mergeEntity(known.get(rowKey(group,row)),row,revision,true));
      }
      this.snapshot=data; this.revision=revision; this.cursor=data.realtime?.cursor ?? this.cursor;
      for (const delta of buffered) this.applyProjection(delta, false);
      this.updatedAt=Date.now(); this.lastSnapshotAt=Date.now(); this.error=null; this._restoreSse();
    },
    async poll(options={}) {
      const view=options.view ?? 'full';
      if (pollTasks.has(view)) return pollTasks.get(view);
      const task=(async()=>{
        try { const data=await getDashboard(view); this.acceptSnapshot(data,view); return data; }
        catch(e){this.error=String(e?.message ?? e);return null;}
        finally{pollTasks.delete(view);}
      })(); pollTasks.set(view,task);return task;
    },
    start() {
      if(this._timer)return;
      this.poll({view:'overview'}).finally(()=>this.poll());
      this._timer=setInterval(()=>{
        if(typeof document!=='undefined'&&document.hidden)return;
        // Legacy/failed streams retain the fallback. A healthy delta stream
        // needs only a five-minute reconciliation, never a 20s catalogue.
        if(!this.hasProjectionStream || Date.now()-this.lastSnapshotAt>300000)this.poll();
      },20000);
    },
    stop(){clearInterval(this._timer);this._timer=null;},
    applyProjection(delta, remember=true) {
      if(Number(delta?.schema ?? 1)!==1)return false;
      const revision=projectionRevision(delta);
      if(remember){this.recentDeltas.push(delta);if(this.recentDeltas.length>120){const removed=this.recentDeltas.splice(0,this.recentDeltas.length-120);this.bufferFloorRevision=Math.max(this.bufferFloorRevision,...removed.map(projectionRevision));}}
      if(!this.snapshot?.unified)return true;
      if(revision&&revision<=this.revision)return true;
      const unified=this.snapshot.unified;
      for(const group of GROUPS){
        const removes=new Set((delta.removes?.[group]??[]).map(id=>typeof id==='object'?rowKey(group,id):String(id)));
        const rows=unified[group]??(unified[group]=[]);
        if(removes.size){
          for(let i=rows.length-1;i>=0;i--)if(removes.has(rowKey(group,rows[i])))rows.splice(i,1);
          if(group==='assets')for(const key of removes)this.discoveries.delete(key);
          if(group==='relations')for(const key of removes)this.liveRelations.delete(key);
        }
        const index=new Map(rows.map(row=>[rowKey(group,row),row]));
        for(const row of delta.upserts?.[group]??[]){
          const key=rowKey(group,row),old=index.get(key);
          if(old)mergeEntity(old,row,revision,true);
          else{const incoming=mergeEntity(null,row,revision);rows.push(incoming);index.set(key,incoming);}
        }
      }
      for(const [key,value] of Object.entries(delta.meta??{}))if(!GROUPS.includes(key))unified[key]=value;
      this.revision=Math.max(this.revision,revision); this.cursor=delta.cursor??this.cursor;
      this.stream.projection=true;this.error=null;this.updatedAt=Date.now();
      return true;
    },
    assetByToken(token,chainId='196'){return this.assetIndex.get(assetKey(chainId,token));},
    _key:assetKey,
    _restoreSse(){
      if(this.snapshot?.realtime?.schema===1)return;
      for(const frame of this.discoveries.values())this.upsertDiscovery(frame);
      for(const rel of this.liveRelations.values())this.upsertRelation(rel);
      for(const live of this.lastSse.values()){
        if(live.kind==='stock')this.applyStockQuote(live.data);
        else this.applyPrice(live.data.chainId,live.data.token,live.data.price,live.at,live.data);
      }
    },
    applyPrice(chainId,token,price,at,fields={}) {
      const data={...fields,chainId:String(chainId),token:normalizeAddress(token),price,at,
        marketAt:fields.timeKind==='received'?null:(fields.marketAt??at),venue:fields.venue??'dex',quoteType:fields.quoteType??'dex',priceScope:fields.priceScope??'dex'};
      const key=quoteKey(data),prev=this.lastSse.get(key);
      if(!prev || (prev.at??0)<=(at??0))this.lastSse.set(key,{kind:'asset',assetKey:assetKey(chainId,token),at,data});
      let applied=false;
      const a=this.assetIndex.get(assetKey(chainId,token)),st=this.stockIndex.get(assetKey(chainId,token));
      if(a)applied=applyQuote(a,data)||applied;
      // A DEX stock has a live path even though it is absent from candidate
      // assets. Exchange-selected rows retain their explicitly selected market.
      if(st&&!(data.priceScope==='dex'&&st.priceScope==='exchange'))applied=applyQuote(st,data)||applied;
      return applied;
    },
    applyStockQuote(d){
      const data={...d,token:normalizeAddress(d.token),provider:d.provider??d.venue,priceScope:d.priceScope??'exchange',quoteType:d.quoteType??'exchange'};
      const key=quoteKey(data),prev=this.lastSse.get(key),at=d.marketAt??d.at??d.receivedAt;
      if(!prev||(prev.at??0)<=(at??0))this.lastSse.set(key,{kind:'stock',at,data});
      return applyQuote(this.stockIndex.get(assetKey(d.chainId,d.token)),data);
    },
    upsertDiscovery(frame){
      const asset=frame?.asset;if(!frame?.id||!asset?.token||!frame.chainId||!frame.t)return false;
      const identity=assetKey(frame.chainId,asset.token);this.discoveries.set(identity,frame);
      while(this.discoveries.size>500)this.discoveries.delete(this.discoveries.keys().next().value);
      if(!this.snapshot?.unified)return true;
      const rows=this.snapshot.unified.assets??(this.snapshot.unified.assets=[]),existing=this.assetIndex.get(identity);
      const incoming={...asset,chainId:String(frame.chainId),token:normalizeAddress(asset.token)};
      if(!existing)rows.unshift(incoming);else if(!existing._revision)mergeEntity(existing,incoming);
      const signals=this.snapshot.unified.signals??(this.snapshot.unified.signals=[]);
      if(!signals.some(e=>String(e.id)===String(frame.id))){signals.unshift({...frame,asset:incoming.token,symbol:incoming.symbol,qualified:false});signals.sort((a,b)=>b.t-a.t);signals.splice(60);}
      return true;
    },
    upsertRelation(relation){
      if(!relation?.id)return false;
      this.liveRelations.set(String(relation.id),relation);
      while(this.liveRelations.size>500)this.liveRelations.delete(this.liveRelations.keys().next().value);
      if(!this.snapshot?.unified)return true;
      const rows=this.snapshot.unified.relations??(this.snapshot.unified.relations=[]),old=rows.find(r=>String(r.id)===String(relation.id));
      if(old){if(!old._revision||Number(relation.checkedAt)>Number(old.checkedAt))mergeEntity(old,relation);}
      else rows.unshift(relation);return true;
    },
    invalidate(resource){const key=[resource.kind,resource.chainId??'',resource.token??'',resource.lang??''].join(':');this.resources.set(key,{...resource,at:Date.now()});},
    setStreamStatus(patch){Object.assign(this.stream,patch);},
    diffCells(key,text){const prev=this.lastCellVals.get(key);this.lastCellVals.set(key,text);return prev!==undefined&&prev!==text;},
    diffKpi(key,text){const prev=this.lastKpiVals.get(key);this.lastKpiVals.set(key,text);return prev!==undefined&&prev!==text;},
  },
});
