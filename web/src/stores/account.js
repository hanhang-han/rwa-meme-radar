import { defineStore } from 'pinia';
import { developerRequest, getDeveloperSession, logoutDeveloper } from '../api/developer.js';
import { THEME_WATCH_KEY, readThemeWatches } from '../utils/theme-map-model.js';
import { createWatchOutbox, validWatchKey } from '../utils/watch-outbox.js';

const outbox=createWatchOutbox();
let inFlight=null,epoch=0,editVersion=0,initTask,listener,storageListener,refreshTimer,loadingEpoch=null,bootVersion=0;
const ACCOUNT_KEY='cliperx-watch-account';
const local=(key,value)=>{try{value===null?localStorage.removeItem(key):localStorage.setItem(key,value);}catch{}};
const stored=key=>{try{return localStorage.getItem(key);}catch{return null;}};
const list=key=>{try{const items=JSON.parse(stored(key)||'[]');return Array.isArray(items)?items.filter(validWatchKey):[];}catch{return [];}};
const cacheKey=id=>id?`${THEME_WATCH_KEY}:${id}`:THEME_WATCH_KEY;
const readCache=id=>new Set(list(cacheKey(id)));
function writeCache(id,items,silent=true){const before=[...readCache(id)],after=[...items].filter(validWatchKey).slice(0,200);local(cacheKey(id),JSON.stringify(after));window.dispatchEvent(new CustomEvent('theme-watch-change',{detail:{before,after,silent,owner:id}}));}

export const useAccountStore=defineStore('account',{
  state:()=>({session:null,ready:false,sessionState:'checking',sessionError:null,syncState:'local',error:null,watches:[...readThemeWatches()]}),
  actions:{
    async start(force=false){
      if(!listener){
        listener=e=>this.onChange(e);
        storageListener=e=>{
          const id=this.session?.user.id;
          if(e.key===ACCOUNT_KEY&&stored(ACCOUNT_KEY)!==(id??null)){++editVersion;this.start(true);return;}
          const legacy=id&&e.key===`cliperx-watch-pending:${id}`;
          if(e.key!==THEME_WATCH_KEY&&e.key!==`${THEME_WATCH_KEY}:${id}`&&!outbox.matches(e.key,id)&&!legacy&&e.key!==null)return;
          ++editVersion;
          if(legacy)outbox.migrate(id);
          this.watches=id?[...outbox.apply(id,readThemeWatches())]:[...readThemeWatches()];
          if(id&&(outbox.matches(e.key,id)||legacy)&&!inFlight&&loadingEpoch!==epoch&&this.error!=='limit')this.flush();
        };
        window.addEventListener('theme-watch-change',listener);window.addEventListener('storage',storageListener);
        refreshTimer=setInterval(()=>{if(this.session&&!document.hidden&&!inFlight&&this.error!=='limit')this.refresh();},60000);
      }
      if(initTask&&!force)return initTask;
      if(this.ready&&!force)return this.session;
      const current=epoch,ticket=++bootVersion,pointer=stored(ACCOUNT_KEY);this.sessionState='checking';this.sessionError=null;
      const task=(async()=>{
        try{const session=await getDeveloperSession();if(ticket!==bootVersion||current!==epoch)return;if(stored(ACCOUNT_KEY)!==pointer)return this.start(true);await this.useSession(session);}
        catch(e){
          if(ticket!==bootVersion)return;
          // The authenticated handshake succeeded even if its follow-up read failed.
          if(this.sessionState==='authenticated'&&stored(ACCOUNT_KEY)===this.session?.user.id)return this.session;
          if(current!==epoch)return;if(stored(ACCOUNT_KEY)!==pointer)return this.start(true);
          if(e.status===401){++epoch;inFlight=null;loadingEpoch=null;this.session=null;local(ACCOUNT_KEY,null);this.sessionState='guest';this.sessionError=null;this.syncState='local';this.error=null;this.watches=[...readCache(null)];writeCache(null,readCache(null));}
          else{this.sessionState='error';this.sessionError='unavailable';if(['loading','saving'].includes(this.syncState)){this.syncState='error';this.error='unavailable';}}
        }finally{if(ticket===bootVersion)this.ready=true;if(initTask===task)initTask=null;}
      })();initTask=task;return task;
    },
    async useSession(session){
      if(!session?.user?.id||!session.csrfToken)throw new Error('invalid-session');
      this.sessionState='authenticated';this.sessionError=null;
      if(this.session?.user.id===session.user.id){this.session=session;this.ready=true;local(ACCOUNT_KEY,session.user.id);return this.refresh();}
      const current=++epoch;inFlight=null;loadingEpoch=current;
      this.session=session;this.error=null;this.syncState='loading';this.ready=true;local(ACCOUNT_KEY,session.user.id);outbox.migrate(session.user.id);
      this.watches=[...outbox.apply(session.user.id,readCache(session.user.id))];
      const checkpoint=outbox.checkpoint(session.user.id);
      try{
        const result=await developerRequest('watches');if(current!==epoch)return;if(stored(ACCOUNT_KEY)!==session.user.id){loadingEpoch=null;return this.start(true);}
        if(result.userId!==session.user.id){loadingEpoch=null;return this.start(true);}
        const remote=new Set(result.items),guest=list(THEME_WATCH_KEY),next=outbox.apply(session.user.id,remote,checkpoint);
        for(const key of guest)if(!next.has(key)&&next.size<200&&!outbox.pending(session.user.id).remove.includes(key)){outbox.record(session.user.id,key,true);next.add(key);}
        local(THEME_WATCH_KEY,JSON.stringify(guest.filter(key=>!remote.has(key))));
        writeCache(session.user.id,next);this.watches=[...next];loadingEpoch=null;this.syncState='synced';await this.flush();
      }catch(e){if(current===epoch){loadingEpoch=null;this.syncState='error';this.error='unavailable';}throw e;}
      return session;
    },
    onChange(e){
      if(stored(ACCOUNT_KEY)!==(this.session?.user.id??null)){this.start(true);return;}if(e.detail?.owner!=null&&e.detail.owner!==this.session?.user.id)return;this.watches=[...readCache(this.session?.user.id??null)];if(!this.session||e.detail?.silent)return;
      ++editVersion;const before=new Set(e.detail?.before??[]),after=new Set(e.detail?.after??[]),id=this.session.user.id;
      for(const key of after)if(!before.has(key)&&validWatchKey(key))outbox.record(id,key,true);
      for(const key of before)if(!after.has(key)&&validWatchKey(key))outbox.record(id,key,false);
      this.watches=[...outbox.apply(id,after)];this.flush();
    },
    async flush(){
      if(inFlight||loadingEpoch===epoch||!this.session)return;
      if(stored(ACCOUNT_KEY)!==this.session.user.id)return this.start(true);
      const session=this.session,current=epoch,checkpoint=outbox.checkpoint(session.user.id),changes=outbox.pending(session.user.id);if(!changes.claims.length)return;
      const flight={userId:session.user.id,...changes};inFlight=flight;this.syncState='saving';
      try{
        const result=await developerRequest('watches',{method:'PATCH',csrfToken:session.csrfToken,body:{add:flight.add,remove:flight.remove}});
        if(current!==epoch)return;if(stored(ACCOUNT_KEY)!==session.user.id)return this.start(true);
        if(result.userId!==session.user.id){this.syncState='error';this.error='unavailable';return this.start(true);}
        outbox.acknowledge(flight.userId,flight.claims);inFlight=null;
        const merged=outbox.apply(flight.userId,result.items,checkpoint);writeCache(flight.userId,merged);this.watches=[...merged];
        local(THEME_WATCH_KEY,JSON.stringify(list(THEME_WATCH_KEY).filter(key=>!result.items.includes(key))));this.syncState='synced';this.error=null;
      }catch(e){if(current!==epoch)return;inFlight=null;this.syncState='error';this.error=e.status===409?'limit':'unavailable';if(e.status===401)await this.start(true);}
      finally{if(inFlight===flight)inFlight=null;if(current===epoch&&this.syncState!=='error')this.flush();}
    },
    async refresh(){
      if(!this.session)return;if(stored(ACCOUNT_KEY)!==this.session.user.id)return this.start(true);
      if(inFlight||loadingEpoch===epoch)return;if(outbox.pending(this.session.user.id).claims.length)return this.flush();
      const current=epoch,version=editVersion,id=this.session.user.id,checkpoint=outbox.checkpoint(id);
      try{const data=await developerRequest('watches');if(current!==epoch||version!==editVersion||inFlight)return;if(stored(ACCOUNT_KEY)!==id)return this.start(true);if(data.userId!==id)return this.start(true);const merged=outbox.apply(id,data.items,checkpoint);writeCache(id,merged);this.watches=[...merged];this.syncState='synced';this.error=null;}
      catch(e){if(current===epoch){this.syncState='error';this.error='unavailable';if(e.status===401)await this.start(true);}}
    },
    toggle(key){if(!validWatchKey(key))return;if(stored(ACCOUNT_KEY)!==(this.session?.user.id??null)){this.start(true);return;}const id=this.session?.user.id??null,next=readCache(id);next.has(key)?next.delete(key):next.add(key);writeCache(id,next,false);},
    async logout(){if(!this.session)return;const id=this.session.user.id;if(stored(ACCOUNT_KEY)!==id)return this.start(true);const current=epoch;await logoutDeveloper(this.session.csrfToken);if(current!==epoch)return;if(stored(ACCOUNT_KEY)!==id)return this.start(true);++epoch;++bootVersion;inFlight=null;loadingEpoch=null;this.session=null;this.sessionState='guest';this.sessionError=null;local(ACCOUNT_KEY,null);this.watches=[...readCache(null)];writeCache(null,new Set(this.watches));this.syncState='local';this.error=null;},
    stop(){++epoch;++bootVersion;inFlight=null;loadingEpoch=null;initTask=null;if(listener)window.removeEventListener('theme-watch-change',listener);if(storageListener)window.removeEventListener('storage',storageListener);clearInterval(refreshTimer);listener=null;storageListener=null;refreshTimer=null;},
  },
});
