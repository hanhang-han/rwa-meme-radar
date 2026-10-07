<template>
  <section class="panel home-activity" :aria-label="tr('市场动态','Market activity')">
    <div class="activity-heading">
      <h2>{{ tr('市场动态','Market activity') }}</h2>
      <div class="activity-following" role="group" :aria-label="tr('动态范围','Activity scope')">
        <button type="button" :class="{active:filter==='all'}" :aria-pressed="filter==='all'" @click="filter='all'">{{ tr('全部','All') }}</button>
        <button type="button" :class="{active:filter==='watched'}" :aria-pressed="filter==='watched'" @click="filter='watched'">{{ tr('已关注','Following') }}</button>
      </div>
    </div>
    <div class="activity-switch-row">
      <div class="activity-tabs" role="group" :aria-label="tr('动态类型','Activity type')">
        <button type="button" :class="{active:mode==='discover'}" :aria-pressed="mode==='discover'" @click="chooseMode('discover')">{{ tr('发现','Discoveries') }}</button>
        <button type="button" :class="{active:mode==='trades'}" :aria-pressed="mode==='trades'" @click="chooseMode('trades')">{{ tr('成交','Trades') }}</button>
      </div>
      <button type="button" class="activity-pause" :aria-pressed="userPaused" :title="tr('仅暂停此列表的自动插入，行情继续更新','Pause automatic insertion in this list; quotes keep updating')" @click="togglePause">{{ userPaused ? tr('继续','Resume') : tr('暂停','Pause') }}</button>
    </div>
    <div class="activity-scope"><span>{{ ticker ? (themeName || ticker) : tr('全部市场','All markets') }}</span><span>{{ scope==='all' ? tr('全部网络','All chains') : chainName({chainId:scope}) }}</span></div>
    <button v-if="userPaused && pendingCount" type="button" class="activity-new" @click="resumeUpdates" role="status">{{ tr('新增 '+pendingCount+' 条 · 查看',''+pendingCount+' new · Show') }}</button>
    <div class="activity-content">
      <ImportantChanges v-show="mode==='discover'" compact :changes="changes" :observations="observations" :unified="unified" :assets="assets" :scope="scope" :ticker="ticker" :loading="loading||themeLoading" :external-filter="filter" :rows="discoveryState.rows" :highlighted-keys="discoveryState.addedKeys" @records="receiveDiscoveries" />
      <div v-show="mode==='trades'" class="activity-trades" aria-live="off">
        <p v-if="themeLoading" class="activity-empty" role="status">{{ tr('正在读取主题成交…','Loading theme trades…') }}</p>
        <p v-else-if="!tradeState.rows.length" class="activity-empty" role="status">{{ filter==='watched' ? tr('当前范围暂无关注资产的链上成交。','No recorded on-chain trades for followed assets in this scope.') : tr('当前范围暂无已收录链上成交。','No recorded on-chain trades in this scope.') }}</p>
        <article v-else v-for="trade in tradeState.rows" :key="tradeKey(trade)" class="activity-trade" :class="{'is-added':tradeState.addedKeys.has(tradeKey(trade))}">
          <RouterLink :to="tradePath(trade)" class="activity-trade-main" :title="tradeTitle(trade)">
            <div class="activity-trade-top"><strong>{{ tradeName(trade) }}</strong><span class="activity-side" :class="trade.type==='buy'?'up':trade.type==='sell'?'down':''">{{ trade.type==='buy'?tr('买入','Buy'):trade.type==='sell'?tr('卖出','Sell'):tr('方向未知','Unknown side') }}</span><time :datetime="iso(tradeTime(trade))" :title="date(tradeTime(trade))">{{ relative(tradeTime(trade)) }}</time></div>
            <div class="activity-trade-fact"><span>{{ chainName(trade) }}</span><b><TradeAmount :trade="trade" /></b><span v-if="!hasAmount(trade)" class="activity-missing">{{ tr('金额待补','Amount unavailable') }}</span><span v-else-if="ingestionDelay(trade)>30000" class="activity-delay" :title="tr('成交发生至服务端接收的延迟','Delay between the trade and server ingestion')">{{ tr('接收延迟 ','Ingest delay ')+Math.ceil(ingestionDelay(trade)/1000)+'s' }}</span></div>
          </RouterLink>
          <a v-if="trade.hash" class="activity-trade-explorer" :href="explorer(trade.hash,'tx',String(trade.chainId))" target="_blank" rel="noopener" :aria-label="tr('核对这笔链上交易','Verify this on-chain transaction')" :title="tr('链上交易','On-chain transaction')">↗</a>
        </article>
      </div>
    </div>
    <div class="activity-footer">
      <template v-if="mode==='discover'"><span>{{ tr('收录时间与池创建时间分开记录','Indexing and pool creation are separate') }}</span><RouterLink :to="discoveryPath">{{ ticker ? tr('主题详情','Theme details') : tr('发现记录','Discovery records') }} →</RouterLink></template>
      <template v-else><span v-if="latestTradeAt" :title="date(latestTradeAt)">{{ tr('最近成交 ','Last trade ')+relative(latestTradeAt) }}</span><span v-else>{{ tr('链上逐笔记录','On-chain trade records') }}</span><span v-if="latestReceivedAt" :title="date(latestReceivedAt)">{{ tr('最近接收 ','Last received ')+relative(latestReceivedAt) }}</span></template>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { assetNavigationLink, themeNavigationLink, pageNavigationLink } from '../utils/navigation-context';
import { readPageState, writePageState } from '../utils/page-navigation-state';
import ImportantChanges from './ImportantChanges.vue';
import TradeAmount, { describeTradeAmount } from './TradeAmount.vue';
import { tr } from '../i18n';
import { age, chainName, date, explorer, short } from '../utils/format';
import { useMinuteClock } from '../composables/useMinuteClock';
import { onchainRecordedTrades } from '../utils/home-model';
import { createReadingBuffer, filterHomeThemeRecords } from '../utils/home-live-model';
import { readThemeWatches } from '../utils/theme-map-model';
import { tradeKey } from '../stores/feed';

const props=defineProps({
  trades:{type:Array,default:()=>[]},changes:{type:Object,default:null},observations:{type:Array,default:()=>[]},
  unified:{type:Object,default:null},assets:{type:Array,default:()=>[]},scope:{type:String,default:'all'},
  ticker:{type:String,default:''},themeName:{type:String,default:''},themeLoading:Boolean,loading:Boolean,
});
const now=useMinuteClock();
const route=useRoute();
const stateKey=()=>`home-activity:${props.scope}`;
const restored=readPageState(stateKey())??{};
const mode=ref(restored.mode==='trades'?'trades':'discover'),filter=ref(restored.filter==='watched'?'watched':'all'),watched=ref(readThemeWatches());
const userPaused=ref(false);
const discoveryRows=ref([]),discoveryMode=ref('verified');
const emptyState=()=>({rows:[],pendingCount:0,addedKeys:new Set()});
const discoveryState=ref(emptyState()),tradeState=ref(emptyState());
const discoveryBuffer=createReadingBuffer({key:item=>String(item.id),limit:5});
const tradeBuffer=createReadingBuffer({key:tradeKey,limit:5});
const context=computed(()=>`${props.scope}:${props.ticker}:${filter.value}`);
const discoveryContext=computed(()=>`${context.value}:${discoveryMode.value}`);
const assetIndex=computed(()=>new Map(props.assets.map(asset=>[assetKey(asset),asset])));
const selectedTrades=computed(()=>{
  const scoped=onchainRecordedTrades(props.trades,null,props.scope,0,'all');
  const themed=filterHomeThemeRecords(scoped,props.ticker,props.unified?.relations??[],props.assets);
  return filter.value==='watched'?themed.filter(item=>watched.value.has(assetKey(item))||(props.ticker&&watched.value.has(`stock:${props.ticker.toUpperCase()}`))):themed;
});
const pendingCount=computed(()=>mode.value==='trades'?tradeState.value.pendingCount:discoveryState.value.pendingCount);
const latestTradeAt=computed(()=>tradeTime(selectedTrades.value[0]));
const latestReceivedAt=computed(()=>Math.max(0,...selectedTrades.value.map(trade=>Number(trade.browserReceivedAt??trade.receivedAt)||0)));
const discoveryPath=computed(()=>props.ticker?themeNavigationLink(props.ticker,{route,scope:props.scope}):pageNavigationLink('/events',{route,scope:props.scope}));
watch([mode,filter],()=>writePageState(stateKey(),{mode:mode.value,filter:filter.value}));
watch(()=>props.scope,()=>{const value=readPageState(stateKey())??{};mode.value=value.mode==='trades'?'trades':'discover';filter.value=value.filter==='watched'?'watched':'all';});
let highlightTimer;

function saveState(target,state){
  const arrived=state.addedKeys.size>0;
  const key=target===tradeState?tradeKey:item=>String(item.id);
  const highlighted=new Set([...state.addedKeys,...state.rows.map(key).filter(id=>target.value.addedKeys.has(id))]);
  target.value={...state,addedKeys:highlighted};
  if(arrived){
    clearTimeout(highlightTimer);
    highlightTimer=setTimeout(()=>{
      tradeState.value={...tradeState.value,addedKeys:new Set()};
      discoveryState.value={...discoveryState.value,addedKeys:new Set()};
    },1700);
  }
}
watch([selectedTrades,context,userPaused,mode],([records,currentContext])=>saveState(tradeState,tradeBuffer.sync(records,{paused:userPaused.value&&mode.value==='trades',context:currentContext})),{immediate:true});
watch([discoveryRows,discoveryContext,userPaused,mode],([records,currentContext])=>saveState(discoveryState,discoveryBuffer.sync(records,{paused:userPaused.value&&mode.value==='discover',context:currentContext})),{immediate:true});
watch(context,()=>{tradeState.value={...tradeState.value,addedKeys:new Set()};discoveryState.value={...discoveryState.value,addedKeys:new Set()};});
function receiveDiscoveries(records,feedMode){discoveryMode.value=feedMode;discoveryRows.value=records;}
function chooseMode(value){mode.value=value;}
function resumeUpdates(){
  userPaused.value=false;
  saveState(tradeState,tradeBuffer.flush());saveState(discoveryState,discoveryBuffer.flush());
}
function togglePause(){if(userPaused.value)resumeUpdates();else userPaused.value=true;}
function assetKey(item){return `${String(item?.chainId??'')}:${String(item?.token??'').toLowerCase()}`;}
function tradeName(trade){const asset=assetIndex.value.get(assetKey(trade));return asset?.name||asset?.symbol||trade.name||trade.symbol||short(trade.token);}
function tradePath(trade){return assetNavigationLink(trade,{route,scope:props.scope,tab:'trades',pool:trade.poolId??trade.pool??trade.poolAddress});}
function tradeTime(trade){return Number(trade?.sourceEventAt??trade?.t)||null;}
function ingestionDelay(trade){const delay=Number(trade?.ingestDelayMs);if(trade?.ingestDelayMs!=null&&Number.isFinite(delay))return Math.max(0,delay);const received=Number(trade?.receivedAt),at=tradeTime(trade);return at&&received>=at?received-at:0;}
function hasAmount(trade){return describeTradeAmount(trade).value!=null;}
function tradeTitle(trade){return [date(tradeTime(trade)),trade.source??trade.provider??trade.venue,trade.hash, tr('查看资产成交','View asset trades')].filter(Boolean).join(' · ');}
function relative(at){if(!at||at>now.value+60000)return '—';if(now.value-at<60000)return tr('刚刚','just now');return age(at);}
function iso(at){return at&&Number.isFinite(at)?new Date(at).toISOString():undefined;}
function syncWatch(){watched.value=readThemeWatches();}
onMounted(()=>{window.addEventListener('theme-watch-change',syncWatch);window.addEventListener('storage',syncWatch);});
onUnmounted(()=>{clearTimeout(highlightTimer);window.removeEventListener('theme-watch-change',syncWatch);window.removeEventListener('storage',syncWatch);});
</script>

<style scoped>
.home-activity{display:flex;flex-direction:column;min-width:0;min-height:0;height:100%}.activity-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:13px}.activity-heading h2{margin:0;font-size:17px;font-weight:650;letter-spacing:-.01em}.activity-following{display:inline-flex;gap:3px;padding:3px;border:1px solid var(--border);background:var(--bg);border-radius:5px}.activity-following button{border:0;background:none;border-radius:3px;color:var(--muted);font:inherit;font-size:12px;padding:4px 8px;cursor:pointer}.activity-following button.active{color:var(--accent);background:var(--panel)}.activity-switch-row{display:flex;align-items:center;justify-content:space-between;gap:10px;border-bottom:1px solid var(--border)}.activity-tabs{display:flex;gap:21px}.activity-tabs button{padding:6px 0 10px;border:0;border-bottom:2px solid transparent;background:none;color:var(--muted);font:inherit;font-size:13px;cursor:pointer}.activity-tabs button.active{color:var(--text);border-bottom-color:var(--accent);font-weight:600}.activity-pause{background:none;border:0;padding:5px 0;color:var(--muted);font:inherit;font-size:12px;cursor:pointer}.activity-pause[aria-pressed=true]{color:var(--accent)}.activity-scope{display:flex;gap:8px;padding:10px 0 5px;color:var(--muted);font-size:12px;min-width:0}.activity-scope span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.activity-scope span+span:before{content:'·';margin-right:8px}.activity-new{display:block;align-self:stretch;margin-top:6px;padding:7px 10px;border:1px solid var(--border);border-radius:4px;background:var(--accent-soft);color:var(--accent);font:inherit;font-size:12px;text-align:center;cursor:pointer}.activity-content{flex:1;min-height:0;overflow:auto}.activity-trade{display:flex;align-items:center;gap:8px;padding:7px 0;min-height:50px;border-bottom:1px solid var(--border)}.activity-trade:last-child{border-bottom:0}.activity-trade-main{display:flex;flex-direction:column;gap:4px;min-width:0;flex:1;color:var(--text);text-decoration:none}.activity-trade-top{display:flex;align-items:center;gap:9px;min-width:0;line-height:1.3}.activity-trade-top>strong{max-width:47%;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.activity-side{font-size:12px;flex-shrink:0}.activity-trade-top>time{margin-left:auto;color:var(--muted);font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums}.activity-trade-main:hover strong{color:var(--accent)}.activity-trade-fact{display:flex;align-items:center;gap:9px;line-height:1.25;color:var(--muted);font-size:12px;min-width:0;overflow:hidden;white-space:nowrap}.activity-trade-fact>b{font-weight:550;color:var(--text);min-width:0;overflow:hidden;text-overflow:ellipsis}.activity-missing,.activity-delay{font-size:12px;overflow:hidden;text-overflow:ellipsis}.activity-delay{margin-left:auto;color:var(--warning)}.activity-trade-explorer{display:flex;align-items:center;justify-content:center;flex-shrink:0;width:28px;height:32px;font-size:16px;color:var(--muted);border-radius:4px;text-decoration:none}.activity-trade-explorer:hover{color:var(--accent);background:var(--accent-soft)}.activity-empty{padding:32px 5px;text-align:center;font-size:13px;line-height:1.7;color:var(--muted)}.activity-footer{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:12px;padding-top:10px;border-top:1px solid var(--border);font-size:12px;color:var(--muted);min-width:0}.activity-footer>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.activity-footer>a{color:var(--accent);text-decoration:none;flex-shrink:0;white-space:nowrap}.home-activity :is(button,a):focus-visible{outline:2px solid var(--accent);outline-offset:3px}.activity-trade.is-added{animation:trade-arrival 1.5s ease-out}@keyframes trade-arrival{from{background:var(--accent-soft)}to{background:transparent}}
@media(max-width:700px){.home-activity{height:auto}.activity-content{overflow:visible}.activity-following button{min-height:32px;padding:5px 9px}.activity-tabs button{min-height:40px}.activity-pause{min-height:36px}.activity-trade:nth-child(n+4){display:none}.activity-trade{min-height:58px;padding:10px 0}.activity-trade-top>strong{font-size:14px}.activity-trade-explorer{width:36px;height:40px}.activity-footer{font-size:12px}.activity-footer>span{max-width:70%}.activity-trade-fact{gap:7px}.activity-trade-top{gap:7px}.activity-new{min-height:36px}}
@media(prefers-reduced-motion:reduce){.activity-trade.is-added{animation:none;background:var(--accent-soft)}}
</style>
