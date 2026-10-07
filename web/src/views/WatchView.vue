<template>
  <div class="watch-page">
    <div class="section-page-heading watch-heading"><div><h2>{{ tr('我的关注','Watchlist') }} <small>{{ num(keys.length) }}</small></h2><p class="watch-sync" role="status">{{ syncLabel }} <button v-if="account.syncState==='error'" type="button" @click="retrySync">{{ tr('重试同步','Retry sync') }}</button></p></div><RouterLink :to="{path:'/me',query:{chain:scope}}">{{ account.session?tr('账号与提醒','Account and alerts'):tr('登录同步','Sign in to sync') }} →</RouterLink></div>
    <div v-if="undo" class="watch-undo" role="status">{{ tr('已取消关注','Unfollowed') }} {{ undo.name }} <button type="button" @click="undoRemove">{{ tr('撤销','Undo') }}</button></div>
    <section v-if="!keys.length" class="panel watch-empty">
      <h3>{{ tr('把想看的主题和资产放在这里','Keep the themes and assets you follow here') }}</h3><p>{{ tr('关注后，可以一起查看行情和关联动态。','Follow items to see their quotes and related activity together.') }}</p>
      <div><RouterLink :to="{path:'/stock',query:{chain:scope}}">{{ tr('浏览股票主题','Explore stock themes') }} →</RouterLink><RouterLink :to="{path:'/meme',query:{chain:scope}}">{{ tr('浏览 Meme','Explore memes') }} →</RouterLink></div>
    </section>
    <template v-else>
      <form class="watch-toolbar" @submit.prevent="applySearch">
        <label class="watch-search"><span>{{ tr('搜索关注','Search follows') }}</span><input v-model="search" maxlength="80" :placeholder="tr('名称、代码或合约','Name, ticker or contract')"><button type="submit">{{ tr('搜索','Search') }}</button></label>
        <label><span>{{ tr('类型','Type') }}</span><select :value="kind" @change="setQuery({kind:$event.target.value})"><option value="all">{{ tr('全部关注','All follows') }}</option><option value="theme">{{ tr('股票主题','Stock themes') }}</option><option value="asset">{{ tr('精确资产','Exact assets') }}</option></select></label>
        <label><span>{{ tr('排序','Sort') }}</span><select :value="sort" @change="setQuery({sort:$event.target.value})"><option value="followed">{{ tr('关注顺序','Follow order') }}</option><option value="name">{{ tr('名称','Name') }}</option><option value="change24h">{{ tr('24h 涨幅','24h gain') }}</option></select></label>
        <label><span>{{ tr('行情状态','Quote status') }}</span><select :value="filter" @change="setQuery({filter:$event.target.value})"><option value="all">{{ tr('全部状态','All statuses') }}</option><option value="quote">{{ tr('报价待更新','Quotes pending') }}</option><option value="risk">{{ tr('有风险提示','With risk flags') }}</option></select></label>
        <button v-if="q||kind!=='all'||filter!=='all'" type="button" class="watch-clear" @click="setQuery({q:undefined,kind:'all',filter:'all'})">{{ tr('清除筛选','Clear filters') }}</button>
      </form>
      <SignalLegend />
      <LiveDataStatus :stream="quoteStream" :quote-at="latestQuoteAt" :snapshot-at="snapshotAt" @refresh="refreshAll" />
      <div class="watch-content">
        <section class="panel watch-main">
          <div class="panel-head"><h2>{{ tr('关注行情','Followed quotes') }}</h2><span>{{ num(total) }} {{ tr('个对象','items') }}</span></div>
          <p v-if="summaryError" class="watch-error" role="alert">{{ tr('行情摘要暂时无法读取，已保存的关注仍在。','Quote summary could not be loaded; your saved follows are retained.') }} <button type="button" @click="loadSummary()">{{ tr('重试行情','Retry quotes') }}</button></p>
          <p v-else-if="summaryLoading" class="watch-loading" role="status">{{ tr('正在更新行情…','Updating quotes…') }}</p>
          <div v-if="rows.length" class="watch-list">
            <article v-for="row in rows" :key="row.key" class="watch-row" :class="{selected:selectedKey===row.key}" :data-navigation-anchor="row.key">
              <RouterLink class="watch-select" :to="rowLink(row)"><AssetAvatar :asset="row.asset" :name="rowName(row)" :symbol="row.kind==='theme'?row.ticker:''" :identity="row.kind==='theme'?'stock:'+row.ticker:''" :size="30" class="watch-avatar" /><span class="watch-identity"><strong>{{ rowName(row) }}</strong><small>{{ rowSubtitle(row) }}</small></span></RouterLink>
              <div class="watch-row-values">
                <span v-if="row.kind==='theme'" class="watch-value-label">{{ tr('股票代币参考价','Stock-token reference') }}</span>
                <strong class="watch-price"><LiveNumber :value="numeric(row.asset?.price)" :currency="row.asset?.priceCurrency??''" format="price" :label="row.kind==='theme'?tr('股票代币参考价','Stock-token reference'):tr('资产报价','Asset price')" /></strong>
                <span v-if="numeric(row.asset?.change24h)!=null" :class="changeClass(row.asset.change24h)"><LiveNumber :value="numeric(row.asset.change24h)" format="percent" :description="date(row.asset.fieldTimes?.change24h)" /> · 24h <small v-if="oldChange(row.asset)">{{ tr('历史','Historical') }}</small></span>
                <div v-if="row.asset" class="watch-quote-time"><QuoteStatus :row="row.asset" show-source /></div>
                <template v-if="exchange(row.asset)"><small class="watch-exchange-label">{{ tr('交易所','Exchange') }} · {{ exchange(row.asset).venue }}</small><span class="watch-exchange-price"><LiveNumber :value="numeric(exchange(row.asset).price)" :currency="exchange(row.asset).priceCurrency" format="price" /><span v-if="exchange(row.asset).changeCurrent" :class="changeClass(exchange(row.asset).change24h)"><LiveNumber :value="numeric(exchange(row.asset).change24h)" format="percent" /> · 24h</span></span><small :title="exchange(row.asset).marketId+' · '+date(exchange(row.asset).at)">{{ quoteAgeAt(exchange(row.asset).at) }}</small></template>
                <small v-if="row.kind==='theme'&&row.theme" class="watch-theme-summary">{{ num(row.theme?.theme?.pairedCount??0) }} {{ tr('同池 Meme','paired memes') }} · {{ tr('配对池成交','Paired-pool volume') }} {{ usd(row.theme?.theme?.volume?.value) }}</small>
                <RiskBadge v-if="row.asset" :asset="row.asset" compact />
                <small v-if="row.status==='unsupported-chain'">{{ tr('此链行情暂不支持，关注仍保留','Quotes on this chain are unsupported; follow retained') }}</small>
                <small v-else-if="row.status==='not-indexed'">{{ tr('该资产尚未收录，关注仍保留','Asset not indexed yet; follow retained') }}</small>
              </div>
              <div class="watch-actions"><button type="button" :aria-pressed="selectedKey===row.key" @click="selectItem(row.key)">{{ tr('动态','Activity') }}</button><button type="button" class="watch-remove" :aria-label="tr('取消关注','Unfollow')+' '+rowName(row)" @click="remove(row)">☆</button></div>
            </article>
          </div>
          <p v-else-if="!summaryLoading" class="x-empty">{{ tr('当前范围和筛选没有关注对象。','No followed items match this scope and filter.') }}</p>
          <div v-if="total>pageSize" class="watch-pagination"><button type="button" :disabled="page<=1" @click="setPage(page-1)">{{ tr('上一页','Previous') }}</button><span>{{ page }} / {{ Math.max(1,Math.ceil(total/pageSize)) }}</span><button type="button" :disabled="page*pageSize>=total" @click="setPage(page+1)">{{ tr('下一页','Next') }}</button></div>
        </section>
        <section class="panel watch-activity">
          <div class="panel-head"><h2>{{ selectedKey?tr('动态 · ','Activity · ')+selectedName:tr('全部关注动态','All followed activity') }}</h2><button v-if="selectedKey" type="button" @click="selectItem('')">{{ tr('清除对象筛选','Clear item filter') }}</button></div>
          <p v-if="eventError" class="watch-error" role="alert">{{ tr('动态暂时无法读取。','Activity could not be loaded.') }} <button type="button" @click="loadEvents()">{{ tr('重试动态','Retry activity') }}</button></p>
          <div class="watch-event-list" tabindex="0" :aria-label="tr('关注动态记录','Followed activity records')">
            <article v-for="event in events" :key="eventKey(event)" class="watch-event-entry"><RouterLink :to="eventLink(event)"><span><strong>{{ event.symbol||event.relation?.tokenSymbol||short(event.asset??event.token) }}</strong><small>{{ eventLabel(event) }} · {{ event.ticker||chainName({chainId:event.chainId}) }}</small></span><time :title="date(event.t)">{{ age(event.t) }}</time></RouterLink><RouterLink v-if="event.pool||event.relation?.pool" class="watch-event-evidence" :to="eventLink(event,true)">{{ tr('配对依据','Pair evidence') }} →</RouterLink></article>
            <p v-if="!events.length" class="x-empty">{{ eventLoading?tr('正在读取动态…','Loading activity…'):eventError?tr('保留关注，稍后重试。','Your follows are retained. Retry later.'):tr('暂无关联动态。','No related activity yet.') }}</p>
          </div>
          <button v-if="hasMore||gapCursor" type="button" class="watch-history" :disabled="historyLoading" @click="loadHistory">{{ historyLoading?tr('读取中…','Loading…'):tr('读取更多记录','Load more records') }}</button>
          <small class="watch-event-note">{{ tr('按关注身份查询历史记录；记录时间不等于建池时间。','History is queried by followed identity. Record time is not pool creation time.') }}</small>
        </section>
      </div>
    </template>
    <details class="watch-more" @toggle="kolOpen=$event.target.open"><summary>{{ tr('更多观察 · 全站 KOL 提及','More observations · Site-wide KOL mentions') }}</summary><KolTracker v-if="kolOpen" /></details>
  </div>
</template>

<script setup>
import AssetAvatar from '../components/AssetAvatar.vue';
import { computed, ref, onMounted, onUnmounted, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useAccountStore } from '../stores/account';
import { getWatchSummary, getWatchEvents } from '../api/watch';
import { useVisibleQuotes } from '../composables/useVisibleQuotes';
import { tr, useI18n } from '../i18n';
import { age, chainName, date, num, short, usd } from '../utils/format';
import { chainScope, inChainScope } from '../utils/chain-scope';
import { assetNavigationLink, themeNavigationLink } from '../utils/navigation-context';
import { stockThemeName, stockThemeCodeLabel } from '../utils/stock-theme-model';
import { stockQuoteAgeLabel } from '../utils/stock-directory-refresh';
import { eventKey, mergeEventItems } from '../utils/event-records';
import { visibleExchangeObservation } from '../utils/visible-exchange-quote';
import { watchKeys, placeholderWatch, planWatchSummary, applyWatchQuote, latestWatchQuote, filterWatchEvents, planWatchEvents, WATCH_PAGE_SIZE } from '../utils/watch-model';
import LiveNumber from '../components/LiveNumber.vue';
import LiveDataStatus from '../components/LiveDataStatus.vue';
import KolTracker from '../components/KolTracker.vue';
import SignalLegend from '../components/SignalLegend.vue';
import QuoteStatus from '../components/QuoteStatus.vue';
import RiskBadge from '../components/RiskBadge.vue';

const route=useRoute(),router=useRouter(),account=useAccountStore(),{lang}=useI18n();
const scope=computed(()=>chainScope(route.query)),keys=computed(()=>watchKeys(account.watches));
const q=computed(()=>String(route.query.q??'').trim().slice(0,80));
const kind=computed(()=>['theme','asset'].includes(route.query.kind)?route.query.kind:'all');
const sort=computed(()=>['name','change24h'].includes(route.query.sort)?route.query.sort:'followed');
const filter=computed(()=>['quote','risk'].includes(route.query.filter)?route.query.filter:'all');
const page=computed(()=>Math.max(1,Math.min(10,Math.floor(Number(route.query.page)||1)))),pageSize=WATCH_PAGE_SIZE;
const selectedKey=computed(()=>keys.value.includes(route.query.item)?route.query.item:'');
const eventKeys=computed(()=>selectedKey.value?[selectedKey.value]:keys.value);
const search=ref(q.value),rows=ref([]),total=ref(0),snapshotAt=ref(0),realtime=ref({});
const summaryLoading=ref(false),summaryError=ref(false),events=ref([]),eventLoading=ref(false),eventError=ref(false),historyLoading=ref(false),hasMore=ref(false),historyCursor=ref(null),gapQueue=ref([]),kolOpen=ref(false),undo=ref(null);
const now=ref(Date.now()),hidden=ref(typeof document!=='undefined'&&document.hidden);
let summaryVersion=0,eventVersion=0,summaryAbort,eventAbort,historyAbort,clockTimer,summaryTimer,eventTimer,undoTimer,disposed=false;
const context=computed(()=>`${account.session?.user?.id??'device'}:${scope.value}:${keys.value.join(',')}`);
const summaryContext=computed(()=>`${context.value}:${q.value}:${kind.value}:${sort.value}:${filter.value}:${page.value}`);
const eventContext=computed(()=>`${context.value}:${selectedKey.value}`);
const gapCursor=computed(()=>gapQueue.value[0]??null);
const visibleAssets=computed(()=>rows.value.map(row=>row.asset).filter(Boolean));
const latestQuoteAt=computed(()=>latestWatchQuote(rows.value,now.value));
const {status:quoteStream}=useVisibleQuotes({rows:visibleAssets,enabled:computed(()=>!hidden.value&&keys.value.length>0),getCursor:()=>realtime.value.cursor,onReset:()=>loadSummary(),onQuote:packet=>applyWatchQuote(rows.value,packet,now.value)});
const selectedName=computed(()=>{const row=rows.value.find(row=>row.key===selectedKey.value);return row?rowName(row):selectedKey.value.startsWith('stock:')?stockThemeName(null,selectedKey.value.slice(6),lang.lang):short(selectedKey.value);});
const syncLabel=computed(()=>account.syncState==='saving'?tr('正在同步关注…','Syncing follows…'):account.syncState==='loading'?tr('已保存的关注先显示，账号同步中…','Saved follows are shown while account sync loads…'):account.syncState==='error'?tr('账号暂未同步，关注保留在此设备。','Account sync is unavailable; follows are retained on this device.'):!account.ready?tr('已保存的关注先显示，正在检查账号…','Saved follows are shown while checking the account…'):account.session?tr('已同步到账号','Synced to your account'):tr('已保存在此设备','Saved on this device'));
const numeric=value=>value!=null&&value!==''&&Number.isFinite(Number(value))?Number(value):null;
const changeClass=value=>Number(value)>0?'up':Number(value)<0?'down':'';
const exchange=asset=>visibleExchangeObservation(asset,now.value);
const oldChange=asset=>!Number(asset?.fieldTimes?.change24h)||now.value-Number(asset.fieldTimes.change24h)>900000;
const quoteAgeAt=at=>stockQuoteAgeLabel(at,now.value,lang.lang);
function rowName(row){return row.kind==='theme'?(lang.lang==='en'?row.nameEn:row.nameZh)||stockThemeName(row.asset,row.ticker,lang.lang):lang.lang==='en'?row.nameEn||row.symbol||row.asset?.symbol||row.asset?.tokenSymbol||row.asset?.name||short(row.token):row.nameZh||row.asset?.name||row.asset?.tokenName||row.symbol||row.asset?.symbol||row.asset?.tokenSymbol||short(row.token);}
function rowSubtitle(row){return row.kind==='theme'?`${tr('股票主题','Stock theme')} · ${stockThemeCodeLabel(row.asset,row.ticker,lang.lang)}`:`${row.kind==='stock-token'?tr('股票代币','Stock token'):'Meme'} · ${chainName({chainId:row.chainId})==='—'?tr('网络','Network')+' '+row.chainId:chainName({chainId:row.chainId})} · ${short(row.token)}`;}
function rowLink(row){return row.kind==='theme'?themeNavigationLink(row.ticker,{route,scope:scope.value}):assetNavigationLink({chainId:row.chainId,token:row.token},{route,scope:scope.value});}
function eventLink(event,evidence=false){return assetNavigationLink({chainId:event.chainId,token:event.asset??event.token},{route,scope:scope.value,tab:evidence?'relation':'overview',pool:evidence?event.pool??event.relation?.pool:undefined});}
function eventLabel(event){return event.kind==='invalidated'?tr('关系证据变化','Relationship evidence changed'):['verified','relation-verified'].includes(event.kind)?tr('同池关系记录','Pool relation recorded'):tr('关联观测','Relation observed');}
function setQuery(patch){router.replace({query:{...route.query,...patch,page:undefined}});}
function applySearch(){setQuery({q:search.value.trim()||undefined});}
function setPage(value){router.replace({query:{...route.query,page:value===1?undefined:String(value)}});}
function selectItem(key){router.replace({query:{...route.query,item:key||undefined}});}
function placeholders(){const previous=new Map(rows.value.map(row=>[row.key,row]));return keys.value.filter(key=>(kind.value==='all'||(kind.value==='theme')===key.startsWith('stock:'))&&(key.startsWith('stock:')||inChainScope({chainId:key.split(':')[0]},scope.value))).slice((page.value-1)*pageSize,page.value*pageSize).map(key=>previous.get(key)??placeholderWatch(key));}
function resetSummary(){++summaryVersion;summaryAbort?.abort();rows.value=placeholders();total.value=keys.value.length;summaryError.value=false;snapshotAt.value=0;realtime.value={};summaryLoading.value=false;loadSummary();}
async function loadSummary(){
  if(!keys.value.length||hidden.value||disposed)return;
  const ticket=++summaryVersion,identity=summaryContext.value;summaryAbort?.abort();const abort=new AbortController();summaryAbort=abort;summaryLoading.value=true;
  try{const result=await getWatchSummary({keys:keys.value,chain:scope.value,q:q.value,kind:kind.value,sort:sort.value,quoteFilter:filter.value,limit:pageSize,offset:(page.value-1)*pageSize},{signal:abort.signal});
    if(disposed||abort.signal.aborted||ticket!==summaryVersion||identity!==summaryContext.value)return;
    const next=(result.items??[]).filter(row=>keys.value.includes(row.key));
    rows.value=planWatchSummary(rows.value,next).rows;
    total.value=result.directory?.total??next.length;snapshotAt.value=Number(result.snapshotAt??0);realtime.value=result.realtime??{};summaryError.value=false;
    if(page.value>1&&(page.value-1)*pageSize>=total.value)setPage(Math.max(1,Math.ceil(total.value/pageSize)));
  }catch(error){if(!abort.signal.aborted&&ticket===summaryVersion&&identity===summaryContext.value)summaryError.value=true;}
  finally{if(ticket===summaryVersion)summaryLoading.value=false;}
}
function resetEvents(){++eventVersion;eventAbort?.abort();historyAbort?.abort();events.value=[];historyCursor.value=null;gapQueue.value=[];hasMore.value=false;eventError.value=false;historyLoading.value=false;loadEvents();}
async function loadEvents(){
  if(!eventKeys.value.length||hidden.value||disposed)return;
  const ticket=++eventVersion,identity=eventContext.value;eventAbort?.abort();const abort=new AbortController();eventAbort=abort;eventLoading.value=true;
  try{const result=await getWatchEvents({keys:eventKeys.value,chain:scope.value,limit:20,cursor:null},{signal:abort.signal});
    if(disposed||abort.signal.aborted||ticket!==eventVersion||identity!==eventContext.value)return;
    const incoming=filterWatchEvents(result.items,eventKeys.value,scope.value),known=new Set(events.value.map(eventKey));
    if(!known.size){historyCursor.value=result.nextCursor??null;hasMore.value=!!result.hasMore;}
    else if(incoming.length&&!incoming.some(event=>known.has(eventKey(event)))&&result.hasMore&&result.nextCursor&&!gapQueue.value.some(cursor=>JSON.stringify(cursor)===JSON.stringify(result.nextCursor)))gapQueue.value.push(result.nextCursor);
    events.value=planWatchEvents(events.value,[],incoming).items;eventError.value=false;
  }catch(error){if(!abort.signal.aborted&&ticket===eventVersion&&identity===eventContext.value)eventError.value=true;}
  finally{if(ticket===eventVersion)eventLoading.value=false;}
}
async function loadHistory(){
  if(historyLoading.value||hidden.value||!eventKeys.value.length)return;
  const identity=eventContext.value,ticket=eventVersion,oldGap=gapCursor.value,cursor=oldGap??historyCursor.value;if(!cursor)return;
  historyLoading.value=true;historyAbort?.abort();const abort=new AbortController();historyAbort=abort;
  try{const result=await getWatchEvents({keys:eventKeys.value,chain:scope.value,limit:20,cursor},{signal:abort.signal});
    if(disposed||abort.signal.aborted||identity!==eventContext.value||ticket!==eventVersion)return;
    const incoming=filterWatchEvents(result.items,eventKeys.value,scope.value),known=new Set(events.value.map(eventKey));
    events.value=mergeEventItems(events.value,incoming);eventError.value=false;
    if(oldGap){if(incoming.some(event=>known.has(eventKey(event)))||!result.hasMore)gapQueue.value.shift();else gapQueue.value[0]=result.nextCursor;}
    else{historyCursor.value=result.nextCursor??null;hasMore.value=!!result.hasMore;}
  }catch(error){if(!abort.signal.aborted&&identity===eventContext.value)eventError.value=true;}
  finally{if(identity===eventContext.value&&historyAbort===abort)historyLoading.value=false;}
}
function refreshAll(){loadSummary();loadEvents();}
function retrySync(){if(account.session)account.refresh();else account.start(true);}
function remove(row){account.toggle(row.key);clearTimeout(undoTimer);undo.value={key:row.key,name:rowName(row),context:account.session?.user?.id??'device'};undoTimer=setTimeout(()=>undo.value=null,7000);}
function undoRemove(){const entry=undo.value;if(entry&&(account.session?.user?.id??'device')===entry.context&&!keys.value.includes(entry.key))account.toggle(entry.key);undo.value=null;clearTimeout(undoTimer);}
function visibility(){hidden.value=document.hidden;++summaryVersion;++eventVersion;summaryAbort?.abort();eventAbort?.abort();historyAbort?.abort();summaryLoading.value=false;eventLoading.value=false;historyLoading.value=false;if(!hidden.value){now.value=Date.now();refreshAll();}}
watch(q,value=>search.value=value);
watch(summaryContext,resetSummary,{immediate:true});
watch(eventContext,resetEvents,{immediate:true});
watch(()=>account.session?.user?.id,()=>{undo.value=null;});
onMounted(()=>{account.start();document.addEventListener('visibilitychange',visibility);clockTimer=setInterval(()=>{if(!document.hidden)now.value=Date.now();},1000);summaryTimer=setInterval(()=>{if(!summaryLoading.value)loadSummary();},30000);eventTimer=setInterval(()=>{if(!eventLoading.value&&!historyLoading.value)loadEvents();},10000);});
onUnmounted(()=>{disposed=true;++summaryVersion;++eventVersion;summaryAbort?.abort();eventAbort?.abort();historyAbort?.abort();clearInterval(clockTimer);clearInterval(summaryTimer);clearInterval(eventTimer);clearTimeout(undoTimer);document.removeEventListener('visibilitychange',visibility);});
</script>

<style scoped>
.watch-page{display:grid;gap:14px;min-width:0}.watch-heading{align-items:flex-start;margin-bottom:0}.watch-heading h2{margin:0}.watch-heading h2 small{margin-left:8px;color:var(--muted);font:500 13px var(--number-font,inherit)}.watch-heading>a{font-size:12px;color:var(--accent);white-space:nowrap}.watch-sync{margin:8px 0 0;color:var(--muted);font-size:11px}.watch-page button{font:inherit;cursor:pointer}.watch-sync button,.watch-error button,.watch-clear,.watch-undo button{padding:0;border:0;background:none;color:var(--accent)}.watch-page .panel{margin:0;min-width:0;padding:20px;background:var(--panel);border:1px solid var(--border);border-radius:11px}.watch-empty{padding:32px!important}.watch-empty h3{font-size:17px;margin:0 0 10px}.watch-empty p{color:var(--muted);font-size:13px}.watch-empty>div{display:flex;gap:20px;margin-top:22px}.watch-empty a{color:var(--accent);font-size:13px}.watch-undo{padding:10px 14px;border:1px solid var(--border);background:var(--accent-soft);font-size:12px}.watch-undo button{margin-left:14px}.watch-toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}.watch-toolbar label{display:grid;gap:5px;min-width:120px;color:var(--muted);font-size:11px}.watch-toolbar .watch-search{position:relative;flex:1;min-width:200px}.watch-search input{padding-right:60px}.watch-search button{position:absolute;right:7px;bottom:7px;border:0;background:none;color:var(--accent);font-size:12px}.watch-toolbar input,.watch-toolbar select{min-width:0;width:100%;min-height:36px;border:1px solid var(--border);border-radius:6px;background:var(--panel);color:var(--text);padding:7px 10px;font:inherit;font-size:12px}.watch-clear{padding:9px 0;font-size:12px}.watch-content{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(290px,1fr);gap:18px;align-items:start}.watch-content .panel-head{gap:12px;margin-bottom:8px}.panel-head h2{font-size:15px}.panel-head>span{font-size:11px;color:var(--muted)}.panel-head button{padding:0;border:0;background:none;color:var(--accent);font-size:11px}.watch-error{color:var(--warning);font-size:12px;line-height:1.6}.watch-error button{margin-left:5px}.watch-loading{margin:5px 0;font-size:11px;color:var(--muted)}.watch-row{display:grid;grid-template-columns:minmax(120px,1fr) minmax(150px,1fr) auto;gap:12px;align-items:center;padding:15px 0;border-bottom:1px solid var(--border)}.watch-row:last-child{border-bottom:0}.watch-row.selected{background:var(--accent-soft);margin-inline:-8px;padding-inline:8px;border-radius:5px}.watch-select{display:flex;align-items:center;gap:10px;min-width:0;text-decoration:none;color:var(--text)}.watch-avatar{display:grid;place-items:center;width:30px;height:30px;flex:none;border-radius:50%;background:var(--accent-soft);color:var(--accent);font-size:13px;font-weight:650}.watch-identity{display:grid;gap:5px;min-width:0}.watch-identity strong{font-size:13px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.watch-identity small{color:var(--muted);font-size:10px;overflow-wrap:anywhere;line-height:1.5}.watch-row-values{display:flex;flex-wrap:wrap;align-items:baseline;gap:5px 10px;font-size:12px;min-width:0;font-variant-numeric:tabular-nums}.watch-price{font-size:14px;color:var(--text)}.watch-row-values>small,.watch-row-values .watch-value-label{flex-basis:100%;color:var(--muted);font-size:10px;line-height:1.5}.watch-row-values>.watch-quote-time{flex-basis:100%;min-width:0;line-height:1.5}.watch-row-values :deep(.quote-status){font-size:10px;white-space:normal}.watch-row-values .watch-historical{color:var(--warning)}.watch-row-values .watch-risk{color:var(--warning)}.watch-row-values .watch-exchange-label{color:var(--muted);margin-top:3px}.watch-exchange-price{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;color:var(--text)}.watch-exchange-price>span{font-size:11px}.watch-theme-summary{border-top:1px solid var(--border);padding-top:5px;margin-top:2px}.watch-actions{display:flex;gap:9px;align-items:center}.watch-actions button{border:0;padding:5px 0;background:none;color:var(--accent);font-size:11px;white-space:nowrap}.watch-actions button[aria-pressed=true]{text-decoration:underline;text-underline-offset:3px}.watch-actions .watch-remove{color:var(--muted);font-size:19px;padding:4px;min-height:36px}.watch-pagination{display:flex;align-items:center;justify-content:center;gap:16px;margin-top:16px;font-size:11px;color:var(--muted)}.watch-pagination button,.watch-history{border:1px solid var(--border);background:var(--panel);color:var(--text);padding:7px 12px;border-radius:5px;font-size:11px}.watch-pagination button:disabled,.watch-history:disabled{opacity:.45;cursor:default}.watch-event-list{max-height:570px;overflow:auto;scrollbar-width:thin}.watch-event-entry{padding:14px 0;border-bottom:1px solid var(--border)}.watch-event-entry>a{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;color:var(--text);text-decoration:none}.watch-event-entry span{display:grid;gap:5px;min-width:0}.watch-event-entry strong{font-size:12px;overflow-wrap:anywhere}.watch-event-entry small,.watch-event-entry time{color:var(--muted);font-size:10px;line-height:1.5}.watch-event-entry time{white-space:nowrap}.watch-event-entry .watch-event-evidence{display:block;width:max-content;color:var(--accent);font-size:10px;margin-top:8px}.watch-history{display:block;margin:15px auto}.watch-event-note{display:block;color:var(--muted);font-size:10px;line-height:1.6;margin-top:14px}.watch-more{border-top:1px solid var(--border);padding:15px 0;color:var(--muted);font-size:12px}.watch-more summary{cursor:pointer;width:max-content;max-width:100%}.watch-page :is(a,button,input,select,summary,.watch-event-list):focus-visible{outline:2px solid var(--accent);outline-offset:3px}@media(max-width:1050px){.watch-content{grid-template-columns:1fr}.watch-event-list{max-height:390px}}@media(max-width:650px){.watch-heading{gap:12px}.watch-heading>a{font-size:10px}.watch-toolbar label{flex:1;min-width:100px}.watch-toolbar .watch-search{flex-basis:100%}.watch-page .panel{padding:15px}.watch-empty{padding:24px!important}.watch-empty>div{flex-direction:column;gap:14px}.watch-row{grid-template-columns:minmax(0,1fr) auto;gap:7px 10px}.watch-row-values{grid-row:2;grid-column:1/-1;padding-left:40px}.watch-actions{grid-column:2;grid-row:1}.watch-identity strong{font-size:13px}.watch-event-list{max-height:420px}}
</style>
