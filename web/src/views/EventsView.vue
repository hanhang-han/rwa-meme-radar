<template>
  <div class="events-page">
    <div class="section-page-heading"><h2>{{ tr('发现记录','Discovery log') }}</h2><span>{{ tr('当前网络','Current scope') }} · {{ scopeLabel }}</span></div>
    <section class="panel events-panel">
      <div class="panel-head"><h2>{{ tr('时间线','Timeline') }}</h2><button id="v2NewEvents" type="button" :disabled="busy" @click="loadFresh">{{ busy?tr('读取中…','Loading…'):tr('刷新','Refresh') }}</button></div>
      <p class="events-count" :title="tr('当前网络已加载的原始发现记录数；相同事件可能折叠成一条显示','Loaded raw discovery records in this chain scope; matching events may be folded into one displayed entry')">{{ tr('已加载','Loaded') }} {{ items.length }} {{ tr('条记录','records') }} · {{ tr('按记录时间排序','by record time') }}</p>
      <details class="events-scope-note" :open="noteOpen" @toggle="noteOpen=$event.target.open"><summary>{{ tr('记录说明','About records') }}</summary><p>{{ tr('首次收录、同池关系与关系观测分别标记。相同资产、类型和交易池的同日记录会折叠；池创建时间在详情中单独显示。','First indexing, pool relations and observations are labeled separately. Same-day records for the same asset, type and pool are collapsed. Pool creation time appears separately in details.') }}</p></details>
      <p v-if="error" class="events-error" role="status">{{ tr('部分记录加载失败，已显示可用记录。','Some records could not be loaded; available records are shown.') }} <button type="button" @click="loadFresh">{{ tr('重试','Retry') }}</button></p>
      <div v-if="busy && !items.length" class="x-empty" role="status">{{ tr('正在读取发现记录…','Loading discovery records…') }}</div>
      <div v-else-if="!items.length && !error" class="x-empty">{{ tr('当前范围暂无发现记录。','No discovery records in this scope.') }}</div>
      <section v-for="group in dayGroups" :key="group.key" class="events-day" :aria-label="group.label">
        <h3><span>{{ group.label }}</span><small :title="tr('该日期的原始记录数，包含已折叠的重复观测','Raw records for this date, including folded repeated observations')">{{ group.rawCount }} {{ tr('条','records') }}</small></h3>
        <div class="events-timeline"><EventRow v-for="entry in group.entries" :key="entry.key" :ev="entry.latest" :records="entry.records" :scope="scope" :back="route.fullPath" :navigation-anchor="'event:'+entry.key" :expanded="expandedKeys.includes(entry.key)" @update:expanded="setExpanded(entry.key,$event)" /></div>
      </section>
      <button v-if="hasMore && items.length" id="v2MoreEvents" class="events-more" type="button" :disabled="busy" @click="loadMore">{{ busy?tr('读取中…','Loading…'):tr('加载更早记录','Load older records') }}</button>
    </section>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import EventRow from '../components/EventRow.vue';
import { getEvents } from '../api/client';
import { tr, useI18n } from '../i18n';
import { chainName } from '../utils/format';
import { chainScope, CHAINS } from '../utils/chain-scope';
import { applyEventPage, eventFromRelationship, mergeEventItems } from '../utils/event-records';
import { useFeedStore } from '../stores/feed';
import { navigationSource } from '../utils/navigation-context';
import { readPageState, writePageState } from '../utils/page-navigation-state';

const route=useRoute(),feed=useFeedStore();
const {lang}=useI18n();
const scope=computed(()=>chainScope(route.query));
const scopeLabel=computed(()=>scope.value==='all'?tr('全部链','All chains'):chainName({chainId:scope.value}));
const activeChains=computed(()=>scope.value==='all'?CHAINS:[scope.value]);
const apiItems=ref([]),cursors=ref({}),busy=ref(false),error=ref(false),expandedKeys=ref([]),noteOpen=ref(false);
const stateKey=()=>`events:${navigationSource(route)}:${scope.value}:${String(route.query.back??'')}`;
let activeContextKey=null;
function persistPage(){if(activeContextKey)writePageState(activeContextKey,{apiItems:apiItems.value,cursors:cursors.value,expandedKeys:expandedKeys.value,noteOpen:noteOpen.value});}
function setExpanded(key,open){expandedKeys.value=open?[...new Set([...expandedKeys.value,key])]:expandedKeys.value.filter(value=>value!==key);}
watch([apiItems,cursors,expandedKeys,noteOpen],persistPage);
let requestId=0;
const items=computed(()=>mergeEventItems(apiItems.value,feed.relationships.map(row=>{
  const event=eventFromRelationship(row),relation=row.relation??{};
  return event?{...event,pool:row.pool??relation.pool,poolCreatedAt:row.poolCreatedAt??relation.poolCreatedAt,discoveredAt:row.discoveredAt??relation.discoveredAt}:null;
}).filter(Boolean)).filter(item=>scope.value==='all'||String(item.chainId)===scope.value));
const hasMore=computed(()=>activeChains.value.some(chain=>cursors.value[chain]!==null));
const dayGroups=computed(()=>{
  const days=new Map();
  for(const item of items.value){
    const at=Number(item.t),valid=Number.isFinite(at)&&at>0;
    const d=valid?new Date(at):null;
    const key=d&&!Number.isNaN(d.getTime())?`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`:'unknown';
    if(!days.has(key))days.set(key,{key,date:d,rawCount:0,entries:new Map()});
    const day=days.get(key);day.rawCount++;
    const groupKey=`${key}:${item.chainId}:${item.asset}:${item.kind}:${String(item.pool??item.relation?.pool??'').toLowerCase()}:${String(item.stock??item.relation?.stock??item.ticker??'').toLowerCase()}`;
    if(!day.entries.has(groupKey))day.entries.set(groupKey,{key:groupKey,latest:item,records:[]});
    day.entries.get(groupKey).records.push(item);
  }
  const now=new Date(),today=dayKey(now),yesterday=dayKey(new Date(now.getFullYear(),now.getMonth(),now.getDate()-1));
  return [...days.values()].map(day=>({key:day.key,rawCount:day.rawCount,label:day.key===today?tr('今天','Today'):day.key===yesterday?tr('昨天','Yesterday'):day.date?day.date.toLocaleDateString(lang.lang==='en'?'en-US':'zh-CN',{year:'numeric',month:'long',day:'numeric'}):tr('时间未能识别','Time unavailable'),entries:[...day.entries.values()]}));
});
function dayKey(value){return `${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,'0')}-${String(value.getDate()).padStart(2,'0')}`;}
async function fetchRound(reset){
  const id=++requestId,chains=activeChains.value;
  busy.value=true;error.value=false;
  const results=await Promise.allSettled(chains.map(async chain=>{
    const cursor=reset?null:cursors.value[chain];
    if(!reset&&cursor===null)return null;
    return {chain,data:await getEvents(chain,cursor)};
  }));
  if(id!==requestId)return;
  let failed=false,nextItems=apiItems.value,nextCursors={...cursors.value};
  for(const result of results){
    if(result.status==='rejected'){failed=true;continue;}
    if(!result.value)continue;
    const updated=applyEventPage(nextItems,nextCursors,result.value.chain,result.value.data);
    nextItems=updated.items;
    // Refresh the head without rewinding the older-page cursor already read.
    if(!reset||!Object.hasOwn(nextCursors,result.value.chain))nextCursors=updated.cursors;
  }
  apiItems.value=nextItems;cursors.value=nextCursors;error.value=failed;busy.value=false;
}
function loadFresh(){fetchRound(true);}
function loadMore(){fetchRound(false);}
watch(()=>route.path==='/events'?stateKey():null,key=>{
  if(!key||key===activeContextKey)return;
  persistPage();activeContextKey=key;
  ++requestId;const saved=readPageState(key)??{};
  apiItems.value=Array.isArray(saved.apiItems)?saved.apiItems:[];
  cursors.value=saved.cursors&&typeof saved.cursors==='object'?saved.cursors:{};
  expandedKeys.value=Array.isArray(saved.expandedKeys)?saved.expandedKeys:[];noteOpen.value=saved.noteOpen===true;
  feed.load(scope.value==='all'?undefined:scope.value);fetchRound(true);
},{immediate:true});
onUnmounted(()=>{persistPage();++requestId;});
</script>

<style scoped>
.events-page { display:grid; gap:18px; align-content:start; max-width:980px; margin:0 auto; }
.events-page section.panel.events-panel { margin:0; padding:20px 0 40px; border:0; border-top:1px solid var(--border); border-radius:0; background:transparent; overflow:visible; }
.events-panel .panel-head { margin-bottom:5px; }
.events-panel .panel-head button { padding:7px 17px; border:1px solid var(--text); border-radius:100px; background:transparent; color:var(--text); cursor:pointer; }
.events-panel .panel-head button:disabled { opacity:.55; cursor:wait; }
.events-count { margin:2px 0 12px; color:var(--muted); font-size:12px; line-height:1.6; }
.events-scope-note { color:var(--muted); font-size:12px; }
.events-scope-note summary { width:max-content; cursor:pointer; }
.events-scope-note p { max-width:680px; margin:6px 0 0; line-height:1.6; }
.events-error { padding:10px 0; border-top:1px solid var(--warning); color:var(--warning); font-size:12px; }
.events-error button { border:0; background:none; color:var(--accent); cursor:pointer; }
.events-day { display:grid; grid-template-columns:150px minmax(0,1fr); gap:20px; margin-top:34px; }
.events-day h3 { display:flex; flex-direction:column; gap:5px; margin:0; padding:4px 0; color:var(--text); font-family:var(--display-font); font-size:21px; font-weight:400; }
.events-day h3 small { font-family:inherit; font-size:12px; color:var(--muted); font-weight:400; }
.events-timeline { min-width:0; margin-left:5px; padding-left:22px; border-left:1px solid var(--border); }
.events-more { display:block; width:min(100%,360px); margin:30px auto 0; padding:10px 22px; border:1px solid var(--text); border-radius:100px; background:transparent; color:var(--text); cursor:pointer; }
.events-more:disabled { opacity:.6; cursor:wait; }
@media(max-width:700px) { .events-day { grid-template-columns:1fr; gap:10px; margin-top:28px; } .events-day h3 { flex-direction:row; align-items:baseline; font-size:18px; } .events-timeline { margin-left:10px; } }
</style>
