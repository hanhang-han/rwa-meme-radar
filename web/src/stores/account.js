import { defineStore } from 'pinia';
import { developerRequest, getDeveloperSession, logoutDeveloper } from '../api/developer.js';
import { THEME_WATCH_KEY, readThemeWatches, writeThemeWatches } from '../utils/theme-map-model.js';

let pendingAdd = new Set(), pendingRemove = new Set(), inFlight = null, epoch = 0, editVersion = 0;
let initTask, listener, storageListener, refreshTimer, loadingEpoch = null, bootVersion = 0;
const ACCOUNT_KEY = 'cliperx-watch-account';
const valid = key => /^(stock:[A-Z0-9.^-]{1,24}|[1-9]\d{0,9}:0x[a-f0-9]{40})$/.test(key);
const local = (key, value) => { try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key,value); } catch {} };
const stored = key => { try { return localStorage.getItem(key); } catch { return null; } };
const list = key => { try { const value=JSON.parse(stored(key)||'[]'); return Array.isArray(value)?value.filter(valid):[]; } catch { return []; } };
const outboxKey = id => `cliperx-watch-pending:${id}`;
function persistedChanges(id) {
  try { const value=JSON.parse(stored(outboxKey(id))||'{}');
    return {add:Array.isArray(value.add)?value.add.filter(valid):[],remove:Array.isArray(value.remove)?value.remove.filter(valid):[]};
  } catch { return {add:[],remove:[]}; }
}
function applyChanges(items, add=pendingAdd, remove=pendingRemove) {
  const next=new Set(items);for(const key of add)next.add(key);for(const key of remove)next.delete(key);return next;
}
function persist(id) {
  if(!id)return;
  // An interrupted HTTP request may or may not have reached the server.
  // Replaying its intent is safe because each server mutation is idempotent.
  const changes=new Map();
  if(inFlight?.userId===id){for(const key of inFlight.add)changes.set(key,true);for(const key of inFlight.remove)changes.set(key,false);}
  for(const key of pendingAdd)changes.set(key,true);for(const key of pendingRemove)changes.set(key,false);
  local(outboxKey(id),JSON.stringify({add:[...changes].filter(([,add])=>add).map(([key])=>key),remove:[...changes].filter(([,add])=>!add).map(([key])=>key)}));
}

export const useAccountStore = defineStore('account', {
  state: () => ({ session:null, ready:false, syncState:'local', error:null, watches:[...readThemeWatches()] }),
  actions: {
    async start(force=false) {
      if (!listener) {
        listener=e=>this.onChange(e);
        storageListener=e=>{
          this.watches=[...readThemeWatches()];
          if(e.key===ACCOUNT_KEY&&stored(ACCOUNT_KEY)!==(this.session?.user.id??null))this.start(true);
        };
        window.addEventListener('theme-watch-change',listener);window.addEventListener('storage',storageListener);
        refreshTimer=setInterval(()=>{if(this.session&&!document.hidden&&!inFlight&&this.error!=='limit')this.refresh();},60000);
      }
      if(initTask&&!force)return initTask;
      // Route mounts must not clear unsent edits or repeat the session handshake.
      if(this.ready&&!force)return this.session;
      const current=epoch,ticket=++bootVersion;
      const task=(async()=>{
        try { const session=await getDeveloperSession();if(ticket!==bootVersion||current!==epoch)return;
          await this.useSession(session);
        } catch(e) {
          if(ticket!==bootVersion||current!==epoch)return;
          if(e.status===401){
            persist(this.session?.user.id);++epoch;pendingAdd.clear();pendingRemove.clear();inFlight=null;loadingEpoch=null;
            this.session=null;local(ACCOUNT_KEY,null);this.syncState='local';this.error=null;
            writeThemeWatches(readThemeWatches(),true);this.watches=[...readThemeWatches()];
          } else {this.error='unavailable';this.syncState='error';}
        } finally {if(ticket===bootVersion)this.ready=true;if(initTask===task)initTask=null;}
      })();initTask=task;return task;
    },
    async useSession(session) {
      if(!session?.user?.id||!session.csrfToken)throw new Error('invalid-session');
      if(this.session?.user.id===session.user.id){this.session=session;this.ready=true;local(ACCOUNT_KEY,session.user.id);return this.refresh();}
      persist(this.session?.user.id);
      const current=++epoch;pendingAdd.clear();pendingRemove.clear();inFlight=null;loadingEpoch=current;
      this.session=session;this.error=null;this.syncState='loading';this.ready=true;
      local(ACCOUNT_KEY,session.user.id);
      const saved=persistedChanges(session.user.id);pendingAdd=new Set(saved.add);pendingRemove=new Set(saved.remove);
      this.watches=[...readThemeWatches()];
      try {
        const result=await developerRequest('watches');
        if(current!==epoch)return;
        if(result.userId!==session.user.id){loadingEpoch=null;return this.start(true);}
        const remote=new Set(result.items),guest=list(THEME_WATCH_KEY);
        const next=applyChanges(remote);
        for(const key of guest)if(!pendingRemove.has(key)&&!next.has(key)&&next.size<200){pendingAdd.add(key);next.add(key);}
        // Only discard guest entries whose presence on this account is known.
        local(THEME_WATCH_KEY,JSON.stringify(guest.filter(key=>!remote.has(key))));
        persist(session.user.id);writeThemeWatches(next,true);this.watches=[...next];
        loadingEpoch=null;this.syncState='synced';await this.flush();
      }catch(e){if(current===epoch){loadingEpoch=null;this.syncState='error';this.error='unavailable';}throw e;}
    },
    onChange(e) {
      this.watches=[...readThemeWatches()];
      if(!this.session||e.detail?.silent)return;
      if(stored(ACCOUNT_KEY)!==this.session.user.id){this.start(true);return;}
      ++editVersion;
      const before=new Set(e.detail?.before??[]),after=new Set(e.detail?.after??[]);
      for(const key of after)if(!before.has(key)&&valid(key)){pendingRemove.delete(key);pendingAdd.add(key);}
      for(const key of before)if(!after.has(key)&&valid(key)){pendingAdd.delete(key);pendingRemove.add(key);}
      persist(this.session.user.id);this.flush();
    },
    async flush() {
      if(inFlight||loadingEpoch===epoch||!this.session||(!pendingAdd.size&&!pendingRemove.size))return;
      if(stored(ACCOUNT_KEY)!==this.session.user.id)return this.start(true);
      const current=epoch,session=this.session,flight={userId:session.user.id,add:[...pendingAdd],remove:[...pendingRemove]};
      pendingAdd.clear();pendingRemove.clear();inFlight=flight;persist(flight.userId);this.syncState='saving';
      try {
        const result=await developerRequest('watches',{method:'PATCH',csrfToken:session.csrfToken,body:{add:flight.add,remove:flight.remove}});
        if(current!==epoch)return;
        if(result.userId!==session.user.id){this.syncState='error';this.error='unavailable';return this.start(true);}
        inFlight=null;persist(flight.userId);
        const merged=applyChanges(result.items);writeThemeWatches(merged,true);this.watches=[...merged];
        local(THEME_WATCH_KEY,JSON.stringify(list(THEME_WATCH_KEY).filter(key=>!result.items.includes(key))));
        this.syncState='synced';this.error=null;
      }catch(e){
        if(current!==epoch)return;
        for(const key of flight.add)if(!pendingRemove.has(key))pendingAdd.add(key);
        for(const key of flight.remove)if(!pendingAdd.has(key))pendingRemove.add(key);
        inFlight=null;persist(flight.userId);this.syncState='error';this.error=e.status===409?'limit':'unavailable';
        if(e.status===401)await this.start(true);
      }finally{
        if(inFlight===flight)inFlight=null;
        if(current===epoch&&this.syncState!=='error')this.flush();
      }
    },
    async refresh() {
      if(!this.session)return;
      if(stored(ACCOUNT_KEY)!==this.session.user.id)return this.start(true);
      if(inFlight||loadingEpoch===epoch)return;
      if(pendingAdd.size||pendingRemove.size)return this.flush();
      const current=epoch,version=editVersion,id=this.session.user.id;
      try {
        const data=await developerRequest('watches');
        if(current!==epoch||version!==editVersion||inFlight)return;
        if(data.userId!==id)return this.start(true);
        const merged=applyChanges(data.items);writeThemeWatches(merged,true);this.watches=[...merged];this.syncState='synced';this.error=null;
      }catch(e){if(current===epoch){this.syncState='error';this.error='unavailable';if(e.status===401)await this.start(true);}}
    },
    toggle(key) {if(!valid(key))return;const next=readThemeWatches();next.has(key)?next.delete(key):next.add(key);writeThemeWatches(next);},
    async logout() {
      if(!this.session)return;
      const current=epoch;await logoutDeveloper(this.session.csrfToken);if(current!==epoch)return;
      persist(this.session.user.id);++epoch;++bootVersion;pendingAdd.clear();pendingRemove.clear();inFlight=null;loadingEpoch=null;this.session=null;
      local(ACCOUNT_KEY,null);this.watches=[...readThemeWatches()];writeThemeWatches(new Set(this.watches),true);this.syncState='local';this.error=null;
    },
    stop() {
      persist(this.session?.user.id);++epoch;++bootVersion;pendingAdd.clear();pendingRemove.clear();inFlight=null;loadingEpoch=null;initTask=null;
      if(listener)window.removeEventListener('theme-watch-change',listener);
      if(storageListener)window.removeEventListener('storage',storageListener);
      clearInterval(refreshTimer);listener=null;storageListener=null;refreshTimer=null;
    },
  },
});
