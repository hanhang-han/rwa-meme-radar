// Canonical list entities retain identity across snapshots and delta batches.
// The stream is authoritative; compact market snapshots bootstrap and reconcile it.
import { defineStore } from 'pinia';
import { markRaw, toRaw } from 'vue';
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
export const MAX_REPLAY_BYTES = 8 * 1024 * 1024;
export const MAX_REPLAY_FRAMES = 120;
const encoder = new TextEncoder();
export const projectionByteLength = raw => encoder.encode(typeof raw === 'string' ? raw : JSON.stringify(raw)).byteLength;
const cloneWire = value => value && typeof value === 'object' ? structuredClone(toRaw(value)) : value;

export function applyFieldOperations(value, operations) {
  // Copy only edited branches; neither the live page nor buffered packets may
  // be partially changed if a malformed operation/base requires recovery.
  function edit(node, path, incoming, remove, depth=0) {
    if(depth===path.length)return remove ? undefined : cloneWire(incoming);
    const key=path[depth];
    if(['__proto__','prototype','constructor'].includes(key)||node==null||typeof node!=='object')throw new Error('invalid-projection-path');
    if(Array.isArray(node)&&(!Number.isInteger(key)||key<0||key>=node.length))throw new Error('invalid-projection-index');
    const copy=Array.isArray(node)?node.slice():{...node};
    if(remove&&depth===path.length-1){if(Array.isArray(copy))throw new Error('invalid-projection-delete');delete copy[key];}
    else copy[key]=edit(node[key],path,incoming,remove,depth+1);
    return copy;
  }
  for(const operation of operations??[]){
    if(!Array.isArray(operation)||!Array.isArray(operation[0])||operation.length<1||operation.length>2)throw new Error('invalid-projection-operation');
    value=edit(value,operation[0],operation[1],operation.length===1);
  }
  return value;
}

export const useDashboardStore = defineStore('dashboard', {
  state: () => ({snapshot:null, updatedAt:0, lastSnapshotAt:0, error:null, revision:0, cursor:null,
    recentDeltas:markRaw([]),recentDeltaSizes:markRaw([]),recentDeltaBytes:0,bufferFloorRevision:0,
    lastCellVals:new Map(), lastKpiVals:new Map(), lastSse:new Map(), discoveries:new Map(), liveRelations:new Map(),
    resources:new Map(), stream:{connected:false,state:'connecting',lastAt:0,lastMessageAt:0,reconnects:0,projection:false},_timer:null,requestedView:'overview',viewEpoch:0}),
  getters: {
    feed:s=>s.snapshot?.unified ?? {}, assets:s=>s.snapshot?.unified?.assets ?? [],
    relations:s=>s.snapshot?.unified?.relations ?? [], stockTokens:s=>s.snapshot?.unified?.stockTokens ?? [],
    groups:s=>s.snapshot?.unified?.groups ?? [], metrics:s=>s.snapshot?.unified?.metrics ?? {}, sources:s=>s.snapshot?.unified?.sources ?? [],
    assetIndex:s=>new Map((s.snapshot?.unified?.assets ?? []).map(r=>[assetKey(r.chainId,r.token),r])),
    stockIndex:s=>new Map((s.snapshot?.unified?.stockTokens ?? []).map(r=>[assetKey(r.chainId,r.tokenContractAddress),r])),
    hasProjectionStream:s=>s.stream.connected && s.stream.projection,
  },
  actions: {
    setProjectionScope(scope) {
      if(this.stream.scoped&&this.stream.scope===scope)return;
      this.viewEpoch+=1;
      this.stream.scoped=true;this.stream.scope=scope;this.stream.projection=false;
      this.error=null;
      this.recentDeltas=markRaw([]);this.recentDeltaSizes=markRaw([]);this.recentDeltaBytes=0;this.bufferFloorRevision=0;
    },
    acceptSnapshot(data, view='full') {
      if (!data?.unified || !Array.isArray(data.unified.assets) || !Array.isArray(data.unified.stockTokens)) throw new Error('invalid-dashboard');
      if(this.stream.scoped&&view!==this.stream.scope)return;
      if (!this.stream.scoped&&view === 'overview' && this.snapshot && this.snapshot.unified?.snapshotScope !== 'overview') return;
      data.unified.snapshotScope??=view;
      const revision = projectionRevision(data);
      if(revision && revision<this.bufferFloorRevision)throw new Error('snapshot-behind-replay-window');
      const buffered = this.recentDeltas.filter(d=>projectionRevision(d)>revision&&(!d.scope||d.scope===view));
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
      const view=options.view ?? this.requestedView;
      const epoch=this.viewEpoch,taskKey=`${epoch}:${view}`;
      if (pollTasks.has(taskKey)) return pollTasks.get(taskKey);
      const task=(async()=>{
        try {
          // A slow response can fall behind the bounded replay buffer. Retry
          // once from the current publication instead of showing a snapshot
          // for which we no longer have every intervening projection.
          for(let attempt=0;attempt<2;attempt++){
            const data=await getDashboard(view);
            if(epoch!==this.viewEpoch)return null;
            try { this.acceptSnapshot(data,view); return data; }
            catch(error){if(error?.message!=='snapshot-behind-replay-window'||attempt>0)throw error;}
          }
        }
        catch(e){if(epoch===this.viewEpoch)this.error=String(e?.message ?? e);return null;}
        finally{pollTasks.delete(taskKey);}
      })(); pollTasks.set(taskKey,task);return task;
    },
    start(view='overview') {
      this.requestedView=view;
      if(this._timer){if(view==='market'&&this.snapshot?.unified?.snapshotScope!=='market')this.poll();return;}
      if(view==='market')this.poll();else this.poll({view:'overview'});
      this._timer=setInterval(()=>{
        if(typeof document!=='undefined'&&document.hidden)return;
        // Legacy/failed streams retain the fallback. A healthy delta stream
        // needs only a five-minute reconciliation, never a 20s catalogue.
        if(!this.hasProjectionStream || Date.now()-this.lastSnapshotAt>300000)this.poll();
      },20000);
    },
    stop(){clearInterval(this._timer);this._timer=null;},
    applyProjection(delta, remember=true, serializedBytes) {
      const schema=Number(delta?.schema ?? 1);
      if(![1,2].includes(schema))return false;
      // An older API ignores scope and emits a global schema-1 catalogue.
      // Never expand a page DTO with those rows during a rolling deployment.
      if(this.stream.scoped&&schema===1)return false;
      if(schema===2&&(!['overview','market'].includes(delta.scope)||this.stream.scoped&&delta.scope!==this.stream.scope))return true;
      const revision=projectionRevision(delta);
      if(remember){
        // Replay history is not rendered. Keep raw immutable packets instead
        // of retaining an ever-growing graph of reactive proxies. Count bytes
        // as well as frames: 120 multi-MB deltas can exhaust a browser tab.
        const raw=markRaw(toRaw(delta));
        const bytes=Number.isFinite(serializedBytes)&&serializedBytes>=0?serializedBytes:projectionByteLength(raw);
        this.recentDeltas.push(raw);this.recentDeltaSizes.push(bytes);this.recentDeltaBytes+=bytes;
        while(this.recentDeltas.length>MAX_REPLAY_FRAMES||this.recentDeltaBytes>MAX_REPLAY_BYTES){
          const removed=this.recentDeltas.shift();
          this.recentDeltaBytes-=this.recentDeltaSizes.shift();
          this.bufferFloorRevision=Math.max(this.bufferFloorRevision,projectionRevision(removed));
        }
        // An oversized frame is still applied below, but cannot be replayed.
        // Any older HTTP snapshot must be rejected using the advanced floor.
      }
      if(!this.snapshot?.unified)return true;
      if(revision&&revision<=this.revision)return true;
      if(schema===2){
        if(this.snapshot.unified.snapshotScope!==delta.scope)return true;
        if(Number(delta.baseRevision)!==this.revision)throw new Error('projection-base-mismatch');
        return this.applyPageProjection(delta);
      }
      const unified=this.snapshot.unified;
      for(const group of GROUPS){
        const groupRemoves=delta.removes?.[group]??[];
        const groupUpserts=delta.upserts?.[group]??[];
        if(!groupRemoves.length&&!groupUpserts.length)continue;
        const removes=new Set(groupRemoves.map(id=>typeof id==='object'?rowKey(group,id):String(id)));
        const rows=unified[group]??(unified[group]=[]);
        if(removes.size){
          for(let i=rows.length-1;i>=0;i--)if(removes.has(rowKey(group,rows[i])))rows.splice(i,1);
          if(group==='assets')for(const key of removes)this.discoveries.delete(key);
          if(group==='relations')for(const key of removes)this.liveRelations.delete(key);
        }
        const index=new Map(rows.map(row=>[rowKey(group,row),row]));
        for(const row of groupUpserts){
          const key=rowKey(group,row),old=index.get(key);
          if(old)mergeEntity(old,row,revision,true);
          else{const incoming=mergeEntity(null,row,revision);rows.push(incoming);index.set(key,incoming);}
        }
      }
      for(const [key,value] of Object.entries(delta.meta??{}))if(!GROUPS.includes(key))unified[key]=value;
      if(Number(delta.now)>0)this.snapshot.now=Number(delta.now);
      this.revision=Math.max(this.revision,revision); this.cursor=delta.cursor??this.cursor;
      this.stream.projection=true;this.error=null;this.updatedAt=Date.now();
      return true;
    },
    applyPageProjection(delta) {
      const unified=this.snapshot.unified,revision=projectionRevision(delta),updates={};
      for(const group of GROUPS){
        const current=unified[group]??[],index=new Map(current.map(row=>[rowKey(group,row),row]));
        for(const key of delta.removes?.[group]??[])index.delete(key);
        for(const row of delta.upserts?.[group]??[])index.set(rowKey(group,row),cloneWire(row));
        for(const [key,ops]of delta.patches?.[group]??[]){
          if(!index.has(key))throw new Error('projection-row-missing');
          index.set(key,applyFieldOperations(index.get(key),ops));
        }
        const order=delta.orders?.[group]??[...index.keys()];
        if(order.length!==index.size||new Set(order).size!==index.size||order.some(key=>!index.has(key)))throw new Error('projection-order-mismatch');
        updates[group]=order.map(key=>index.get(key));
      }
      const meta=applyFieldOperations(Object.fromEntries(Object.entries(unified).filter(([key])=>!GROUPS.includes(key))),delta.metaOps);
      // Commit only after every row/path/order has validated. Existing entity
      // identities are retained so cell flashes and table selection survive.
      for(const group of GROUPS){
        const known=new Map((unified[group]??[]).map(row=>[rowKey(group,row),row]));
        unified[group]=updates[group].map(row=>known.get(rowKey(group,row))===row?row:mergeEntity(known.get(rowKey(group,row)),row,revision,true));
      }
      for(const key of Object.keys(unified))if(!GROUPS.includes(key)&&!(key in meta))delete unified[key];
      Object.assign(unified,meta);
      this.snapshot.now=delta.now;this.revision=revision;this.snapshot.realtime={...this.snapshot.realtime,revision};
      this.stream.projection=true;this.error=null;this.updatedAt=Date.now();return true;
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
