<template>
  <div class="home-page">
    <div class="home-heading"><div><h2>{{ tr('市场概览','Market overview') }}</h2><p class="home-deck">{{ tr('股票主题、Meme 行情与链上动态','Stock themes, meme markets and on-chain activity') }}</p></div><span>{{ scope==='all'?tr('全部网络','All chains'):chainName({chainId:scope}) }}</span></div>
    <SignalLegend />
    <div class="home-metrics" :aria-label="tr('市场数据摘要','Market summary')">
      <a :href="kpiHref({qualified:'1',rel:'all'})" :title="tr('报价在15分钟内、流动性至少$1K的候选Meme；不等于全部收录数量','Candidate memes with quotes within 15m and liquidity of at least $1K; separate from all indexed assets')"><span>{{ tr('行情可用 Meme','Quoted memes') }}</span><strong>{{ num(scopeMetrics.active) }}</strong></a>
      <a :href="kpiHref({view:'pool',qualified:'1'})" :title="tr('符合股票身份与关系条件、报价在15分钟内的配对池，按网络和池地址去重','Qualifying stock-paired pools with quotes within 15m, deduplicated by chain and pool')"><span>{{ tr('配对池','Paired pools') }}</span><strong>{{ num(scopeMetrics.pools) }}</strong></a>
      <a :href="kpiHref({view:'pool',qualified:'1'})" :title="tr('符合条件配对池的双边流动性合计（USD），不等于资产全部池流动性','Two-sided liquidity across qualifying pair pools (USD), separate from token-wide liquidity')"><span>{{ tr('池流动性','Pool liquidity') }}</span><strong>{{ usd(scopeMetrics.liquidity) }}</strong></a>
      <a :href="kpiHref({new:'24h',sort:'firstSeen',rel:'all',fresh:'0',showMissing:'1'})" :title="tr('最近24小时首次收录的候选资产；收录时间不等于创建时间','Candidates first indexed in the last 24h; indexing time is not creation time')"><span>{{ tr('24h 新收录','New in 24h') }}</span><strong>{{ num(scopeMetrics.newAssets) }}</strong></a>
    </div>

    <section class="panel home-themes">
      <div class="panel-head"><h2>{{ tr('股票主题','Stock themes') }}</h2><div class="theme-view-controls"><div class="home-view-switch" role="group" :aria-label="tr('主题展现方式','Theme display')"><button type="button" :aria-pressed="themeView==='rank'" @click="selectThemeView('rank')">{{ tr('排行','Ranking') }}</button><button type="button" :aria-pressed="themeView==='map'" @click="selectThemeView('map')">{{ tr('关系图','Map') }}</button></div><RouterLink :to="{path:'/stock',query:{chain:scope}}">{{ tr('全部主题','All themes') }} →</RouterLink></div></div>
      <template v-if="themeView==='rank'"><HomeThemeSelector v-if="hotStocks.length" v-model="selectedTicker" :cards="hotStocks" :scope="scope" /><p v-else class="x-empty" role="status">{{ hotStocksKnown?tr('当前网络暂无已收录的关联主题。','No recorded related themes in this scope.'):tr('正在读取股票主题…','Loading stock themes…') }}</p></template>
      <ThemeMarketMap v-else v-model="selectedTicker" :theme-map="themeMap" :name-clues="nameClues" :name-clues-loading="!expandedDataReady&&!marketError" :stock-tokens="expandedUnified?.stockTokens??store.stockTokens" :scope="scope" :loading="marketLoading||(!themeMapReady&&!marketError)" @load-clues="loadMarket" />
      <p v-if="themeView==='map'&&marketError" class="home-map-error" role="alert">{{ tr('关系图数据暂时无法读取。','Theme map data could not be loaded.') }} <button type="button" @click="loadMarket">{{ tr('重试','Retry') }}</button></p>
      <div v-if="selectedTicker" class="home-selected-theme" role="status"><div><strong>{{ selectedThemeName }}</strong><span>{{ stockThemeCodeLabel(hotStocks.find(card=>card.ticker===selectedTicker)?.stock,selectedTicker,lang.lang) }}</span><span>{{ selectedThemeLoading?tr('加载中…','Loading…'):tr('同池 Meme','Pool-paired memes')+' '+num(selectedTheme?.pairedCount) }}</span></div><div class="selected-theme-actions"><RouterLink :to="themeNavigationLink(selectedTicker,{route,scope})">{{ tr('主题详情','Theme details') }} →</RouterLink><button type="button" @click="selectedTicker=''">{{ tr('清除','Clear') }}</button></div><button v-if="selectedThemeError" type="button" @click="loadSelectedTheme">{{ tr('加载较慢，重试','Loading slowly, retry') }}</button></div>
    </section>

    <div class="home-market-grid">
      <section class="panel home-leaders" :class="{'has-themes':hasLeaderThemes}">
        <div class="panel-head"><h2>{{ tr('Meme 24h 成交榜','Meme 24h volume') }}</h2><RouterLink :to="{path:'/meme',query:{chain:scope,rel:selectedTicker?'A':'all',ticker:selectedTicker||undefined,sort:'volume24h',rank:'volume24h',category:'meme',fresh:'0',showMissing:'1'}}">{{ tr('全部','All') }} →</RouterLink></div>
        <p class="home-subline">{{ selectedTicker?selectedThemeName:tr('全部市场','All markets') }}<template v-if="related.length"> · {{ tr('共','Total') }} {{ num(leaderTotal) }}</template></p>
        <div v-if="related.length" class="home-rank-head"><span>#</span><span>Meme</span><span class="rank-price">{{ tr('价格 / 24h 涨跌','Price / 24h change') }}</span><span v-if="hasLeaderThemes" class="rank-theme-heading">{{ tr('主题','Theme') }}</span><span class="rank-risk-heading">{{ tr('风险','Risk') }}</span><span :title="tr('资产全市场24h美元成交，并非单个交易池成交','Token-wide 24h USD volume, separate from individual pool volume')">{{ tr('24h 成交','24h volume') }} · USD</span></div>
        <div class="home-rank-list">
          <div v-for="(asset,index) in related" :key="asset.chainId+':'+asset.token" class="home-leader-row">
            <span class="rank-position">{{ String(index+1).padStart(2,'0') }}</span><div class="rank-identity"><RouterLink :to="assetPath(asset)" class="rank-asset"><AssetAvatar :asset="asset" :size="28" /><span class="rank-name"><strong>{{ asset.name || asset.symbol || short(asset.token) }}</strong><small>{{ chainName(asset) }}</small></span></RouterLink><div class="rank-mobile-risk"><RiskBadge :asset="asset" compact /></div></div><div class="rank-quote" :title="assetQuoteTitle(asset)"><LiveNumber :value="asset.price" :currency="asset.priceCurrency??''" format="price" :label="tr('资产价格','Asset price')" :description="assetQuoteTitle(asset)" /><small :class="assetChangeClass(asset)">{{ pct(asset.change24h) }}<em v-if="assetQuoteStatus(asset)"> · {{ assetQuoteStatus(asset) }}</em></small></div><span v-if="hasLeaderThemes" class="rank-tickers" :title="leaderThemeLabel(asset)">{{ leaderThemeLabel(asset) || '—' }}</span><span class="rank-risk"><RiskBadge :asset="asset" compact /></span><strong class="rank-amount" :title="assetVolumeTitle(asset)">{{ usd(asset.volume24h) }}<i :style="{width:leaderVolumeWidth(asset.volume24h)}"></i></strong>
          </div>
          <p v-if="!related.length" class="x-empty" role="status">{{ (selectedTicker ? selectedThemeLoading : !publishedLeaders && !fullSnapshotReady) ? tr('正在加载资产行情…','Loading asset markets…') : tr('暂无可比较的近期美元成交。','No comparable recent USD volume.') }} <button v-if="selectedThemeError" type="button" @click="loadSelectedTheme">{{ tr('重试','Retry') }}</button></p>
        </div>
        <p class="home-rank-footer">{{ tr('按美元成交额排序','Ranked by USD volume') }}<span>{{ tr('历史报价保留时间标记','Historical quotes are marked') }}</span></p>
      </section>
      <HomeActivityPanel :changes="importantChanges" :observations="feed.relationships" :trades="feed.trades" :unified="activityUnified" :assets="activityAssets" :scope="scope" :ticker="selectedTicker" :theme-name="selectedThemeName" :theme-loading="selectedThemeLoading" :loading="!importantChangesReady" />
    </div>

    <section v-if="briefingItems.length" class="panel home-activity-summary">
      <div class="panel-head"><h2>{{ tr('成交变化','Volume changes') }}</h2><span class="home-subline"><time>{{ briefing?.at ? clockTime(briefing.at) : '—' }}</time><template v-if="briefing?.stale"> · {{ tr('历史数据','Historical data') }}</template></span></div>
      <div class="briefing-list"><article v-for="item in briefingItems" :key="item.id" class="briefing-item"><span class="activity-warning" aria-hidden="true">!</span><RouterLink v-if="item.asset?.address && item.asset?.chainId" :to="assetPath({chainId:item.asset.chainId,token:item.asset.address})" class="briefing-asset">{{ item.asset.name || item.asset.symbol || short(item.asset.address) }}</RouterLink><strong v-else>{{ item.asset?.name ?? tr('市场动态','Market activity') }}</strong><span>{{ briefingMessageFor(item) }}</span></article></div>
    </section>
    <details v-if="snapshotItems.length || briefingLoading || briefingError" class="home-snapshot"><summary>{{ tr('市场快照','Market snapshot') }}<span>{{ briefing?.at ? clockTime(briefing.at) : '' }}{{ briefing?.stale?' · '+tr('历史','Historical'):'' }}</span></summary><div class="briefing-list"><article v-for="item in snapshotItems" :key="item.id" class="briefing-item"><RouterLink v-if="item.asset?.address && item.asset?.chainId" :to="assetPath({chainId:item.asset.chainId,token:item.asset.address})" class="briefing-asset">{{ item.asset.name || item.asset.symbol || short(item.asset.address) }}</RouterLink><strong v-else>{{ item.asset?.name ?? tr('市场动态','Market activity') }}</strong><span>{{ briefingMessageFor(item) }}</span></article></div><p v-if="briefingLoading && !snapshotItems.length" class="x-empty" role="status">{{ tr('正在读取市场快照…','Loading market snapshot…') }}</p><p v-else-if="briefingError && !snapshotItems.length" class="x-empty" role="alert">{{ tr('暂时无法读取。','Could not be loaded.') }} <button type="button" @click="loadBriefing">{{ tr('重试','Retry') }}</button></p></details>

    <section v-if="distribution.length" class="home-networks"><details><summary><strong>{{ tr('覆盖网络','Network coverage') }}</strong><span>{{ distribution.map(item=>chainName(item)).join(' · ') }}</span><span class="network-expand">{{ tr('分布详情','Distribution') }}</span></summary><div class="network-columns"><span>{{ tr('网络','Chain') }}</span><span>{{ tr('收录 Meme','Indexed memes') }}</span><span>{{ tr('配对池 24h 成交','Pair-pool 24h volume') }}</span><span>{{ tr('池流动性','Pool liquidity') }}</span></div><RouterLink v-for="item in distribution" :key="item.chainId" class="network-row" :to="{path:'/meme',query:{chain:String(item.chainId),rel:'all'}}"><strong>{{ chainName(item) }}</strong><span>{{ num(item.assets) }}<span class="chain-share" aria-hidden="true"><i :style="{width:distributionMax?(Number(item.assets||0)/distributionMax*100)+'%':'0%'}"></i></span></span><span :title="distributionVolumeTitle(item)">{{ usd(item.volume?.value) }}<small>{{ chainStatus(item) }}</small></span><span>{{ usd(item.liquidity?.value) }}</span></RouterLink><p v-if="!distribution.some(item=>String(item.chainId)==='5042')" class="home-network-pending">Arc · {{ arcStatus }}</p></details></section>
  </div>
</template>

<script setup>
import AssetAvatar from '../components/AssetAvatar.vue';
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import RiskBadge from '../components/RiskBadge.vue';
import SignalLegend from '../components/SignalLegend.vue';
import LiveNumber from '../components/LiveNumber.vue';
import ThemeMarketMap from '../components/ThemeMarketMap.vue';
import HomeActivityPanel from '../components/HomeActivityPanel.vue';
import HomeThemeSelector from '../components/HomeThemeSelector.vue';
import { getStockTheme } from '../api/product';
import { useDashboardStore } from '../stores/dashboard';
import { useFeedStore } from '../stores/feed';
import { getBriefing, getDashboard } from '../api/client';
import { tr, useI18n } from '../i18n';
import { chainName, date, num, pct, short, usd } from '../utils/format';
import { chainScope, inChainScope } from '../utils/chain-scope';
import { homeStockCards, metricsForScope } from '../utils/home-model';
import { mergeHomeThemeAssets } from '../utils/home-live-model';
import { useMinuteClock } from '../composables/useMinuteClock';
import { relationMatchesAsset } from '../utils/relations';
import { buildStockTheme, comparableVolume, isRecentObservation, stockThemeName, stockThemeCodeLabel, normalizeTicker } from '../utils/stock-theme-model';
import { buildNameClues, fallbackRelationEvents, fallbackThemeMap } from '../utils/theme-map-model';
import { assetNavigationLink, themeNavigationLink } from '../utils/navigation-context';
import { readPageState, writePageState } from '../utils/page-navigation-state';

const route = useRoute();
const store = useDashboardStore();
const feed = useFeedStore();
const { lang } = useI18n();
const scope = computed(() => chainScope(route.query));
const pageStateKey=()=>`home:${scope.value}`;
let activePageStateKey=pageStateKey();
const restoredPageState=readPageState(activePageStateKey)??{};
const now=useMinuteClock();
const fullSnapshotReady = computed(() => !!store.snapshot?.unified && store.snapshot.unified.snapshotScope !== 'overview');
const marketPayload=ref(null),marketLoading=ref(false),marketError=ref(false);
let marketRequest=0,marketTask;
const expandedUnified=computed(()=>fullSnapshotReady.value?store.snapshot.unified:marketPayload.value?.unified??store.snapshot?.unified);
const expandedDataReady=computed(()=>!!expandedUnified.value && expandedUnified.value.snapshotScope!=='overview');
const themeMapReady = computed(() => !!store.snapshot?.unified?.themeMap || expandedDataReady.value);
const importantChangesReady = computed(() => !!store.snapshot?.unified?.importantChanges || fullSnapshotReady.value);
const themeMap = computed(() => {
  const unified = expandedUnified.value;
  if (!unified) return null;
  return store.snapshot?.unified?.themeMap ?? unified.themeMap ?? fallbackThemeMap(unified);
});
const nameClues = computed(() => expandedDataReady.value ? buildNameClues(expandedUnified.value,scope.value) : []);
const hotStocksKnown = computed(() => fullSnapshotReady.value || Array.isArray(scope.value==='all'?store.snapshot?.unified?.hotStocks:store.snapshot?.unified?.hotStocksByChain?.[scope.value]));
const importantChanges = computed(() => {
  const unified = store.snapshot?.unified;
  if (!unified) return null;
  return unified.importantChanges ?? fallbackRelationEvents(feed.relationships, unified);
});
const scopeMetrics = computed(() => metricsForScope(store.snapshot?.unified, scope.value, now.value));
const hotStocks = computed(() => homeStockCards(store.snapshot?.unified,scope.value,now.value).slice(0,8));
const selectedTicker=ref(normalizeTicker(restoredPageState.selectedTicker??'')),selectedThemePayload=ref(null),selectedThemeLoading=ref(false),selectedThemeError=ref(false);
let selectedRequest=0;
const selectedThemeAssets=computed(()=>mergeHomeThemeAssets(selectedThemePayload.value?.unified?.assets??[],store.assets));
const selectedTheme=computed(()=>selectedThemePayload.value?buildStockTheme({ticker:selectedTicker.value,...selectedThemePayload.value.unified,assets:selectedThemeAssets.value,scope:scope.value,now:now.value}):null);
const selectedThemeName=computed(()=>stockName(hotStocks.value.find(card=>normalizeTicker(card.ticker)===normalizeTicker(selectedTicker.value))?.stock??[...(selectedThemePayload.value?.unified?.stockTokens??[]),...store.stockTokens].find(stock=>normalizeTicker(stock.stockIdentity?.code??stock.stockCode??stock.ticker)===normalizeTicker(selectedTicker.value)),selectedTicker.value));
async function loadSelectedTheme(){
  const request=++selectedRequest;selectedThemeError.value=false;
  if(!selectedTicker.value){selectedThemePayload.value=null;selectedThemeLoading.value=false;return;}
  selectedThemeLoading.value=true;
  try{const result=await getStockTheme(selectedTicker.value,scope.value);if(request===selectedRequest)selectedThemePayload.value=result;}
  catch{if(request===selectedRequest)selectedThemeError.value=true;}
  finally{if(request===selectedRequest)selectedThemeLoading.value=false;}
}
watch([selectedTicker,scope],()=>{if(route.path!=='/live')return;selectedThemePayload.value=null;loadSelectedTheme();},{immediate:true});
const leaderVolumeMax = computed(() => Math.max(0,...related.value.map(asset => Number(asset.volume24h) || 0)));
function leaderVolumeWidth(value) { return value != null && Number.isFinite(Number(value)) && leaderVolumeMax.value > 0 ? `${Math.max(0,Math.min(100,Number(value)/leaderVolumeMax.value*100))}%` : '0%'; }
const themeView = ref(restoredPageState.themeView==='map'?'map':'rank');
function persistHome(){writePageState(activePageStateKey,{selectedTicker:selectedTicker.value,themeView:themeView.value});}
watch([selectedTicker,themeView],persistHome);
watch(()=>route.path==='/live'?pageStateKey():null,key=>{if(!key||key===activePageStateKey)return;persistHome();activePageStateKey=key;const value=readPageState(key)??{};selectedTicker.value=normalizeTicker(value.selectedTicker??'');themeView.value=value.themeView==='map'?'map':'rank';if(themeView.value==='map'&&!themeMapReady.value)loadMarket();});
function selectThemeView(view) { themeView.value = view; if (view === 'map' && !themeMapReady.value) loadMarket(); }
const activityUnified = computed(() => {
  const main = store.snapshot?.unified ?? {}, selected = selectedThemePayload.value?.unified ?? {};
  return {...main, relations:[...(main.relations ?? []),...(selected.relations ?? [])], stockTokens:[...(main.stockTokens ?? []),...(selected.stockTokens ?? [])]};
});
const activityAssets = computed(() => [...store.assets,...selectedThemeAssets.value,...(activityUnified.value.stockTokens ?? [])]);
const distribution = computed(() => [...(store.snapshot?.unified?.distribution ?? [])].sort((a,b)=>(b.volume?.value??-1)-(a.volume?.value??-1)));
const arcStatus=computed(()=>store.snapshot?.unified?.chainStatus?.find?.(row=>String(row.chainId)==='5042')?.status==='connecting'?tr('接入中','Connecting'):tr('暂无数据','No data'));
function chainStatus(item){const status=store.snapshot?.unified?.chainStatus?.find?.(row=>String(row.chainId)===String(item.chainId));if(status?.delayMs>30000)return tr('接收延迟 ','Ingestion delay ')+Math.ceil(status.delayMs/1000)+'s';return tr('成交覆盖 ','Volume coverage ')+(item.volume?.known??'—')+'/'+(item.volume?.total??'—');}
const distributionMax = computed(() => Math.max(0, ...distribution.value.map(row => Number(row.assets) || 0)));
const eligibleRelations = computed(() => store.relations.filter(r => r.level === 'A'));
const publishedLeaders=computed(()=>scope.value==='all'?store.snapshot?.unified?.memeLeaders:store.snapshot?.unified?.memeLeadersByChain?.[scope.value]);
const eligibleLeaders=computed(()=>fullSnapshotReady.value ? store.assets.filter(asset=>asset.kind==='candidate' && asset.assetCategory!=='derivative' && inChainScope(asset,scope.value) && comparableVolume(asset,now.value)!=null).sort((a,b)=>Number(b.volume24h)-Number(a.volume24h)) : []);
const related=computed(()=>(selectedTicker.value?(selectedTheme.value?.rows??[]).filter(row=>row.level==='A'&&row.volume!=null).map(row=>({...row.asset,tickers:[selectedTicker.value]})):(publishedLeaders.value?.items ?? eligibleLeaders.value)).slice(0,6));
const hasLeaderThemes = computed(() => related.value.some(asset => leaderThemeLabel(asset)));
function leaderThemeLabel(asset) { const codes=asset.tickers?.length?asset.tickers:eligibleRelations.value.filter(r=>relationMatchesAsset(r,asset)).map(r=>r.ticker); return [...new Set(codes.filter(Boolean))].slice(0,2).map(ticker=>stockName(store.stockTokens.find(stock=>normalizeTicker(stock.stockIdentity?.code??stock.stockCode??stock.ticker)===normalizeTicker(ticker)),ticker)).join(' · '); }
const leaderTotal=computed(()=>selectedTicker.value?selectedTheme.value?.pairedCount:publishedLeaders.value?.total ?? eligibleLeaders.value.length);
function kpiHref(query){return '#/meme?'+new URLSearchParams({chain:scope.value,...query}).toString();}
function assetPath(asset){return assetNavigationLink(asset,{route,scope:scope.value});}
function distributionVolumeTitle(item){return tr(`配对池24h成交（USD） · 有成交观测的池 ${num(item.volume?.known)} / 符合条件的池 ${num(item.volume?.total)}；缺失不表示零`,`Paired-pool 24h volume (USD) · pools with volume observations ${num(item.volume?.known)} / qualifying pools ${num(item.volume?.total)}; missing does not mean zero`);}
async function loadMarket(){
  if(marketTask)return marketTask;
  const request=++marketRequest;marketLoading.value=true;marketError.value=false;
  marketTask=(async()=>{try{const result=await getDashboard('market');if(request===marketRequest)marketPayload.value=result;}catch{if(request===marketRequest)marketError.value=true;}finally{if(request===marketRequest)marketLoading.value=false;marketTask=null;}})();
  return marketTask;
}

const briefing = ref(null);
const briefingLoading = ref(true);
const briefingError = ref(false);
const briefingStatus = ref('scheduled');
const briefingCache = new Map();
let briefingRequest = 0;
const scopedBriefingItems = computed(() => (briefing.value?.items ?? []).filter(item => scope.value === 'all' || String(item.asset?.chainId) === scope.value));
const briefingItems = computed(() => scopedBriefingItems.value.filter(item => item.type==='wash_suspect'||item.type==='thin_spike').slice(0,3));
const snapshotItems = computed(() => scopedBriefingItems.value.filter(item => item.type!=='wash_suspect'&&item.type!=='thin_spike').slice(0,3));
function briefingMessageFor(item) {
  const f = item.fields ?? {};
  const liquidity = f.totalLiquidityUsd == null ? '' : tr('流动性 ' + usd(f.totalLiquidityUsd), 'liquidity ' + usd(f.totalLiquidityUsd));
  if (item.type === 'wash_suspect') {
    if (f.volumeLiquidityRatio != null) return tr('24h 成交 / 流动性 ' + Math.round(f.volumeLiquidityRatio) + ' 倍',
      '24h volume / liquidity ' + Math.round(f.volumeLiquidityRatio) + 'x');
    if (f.transactionsPerHolder != null) return tr('成交笔数 / 地址数 ' + Math.round(f.transactionsPerHolder),
      'Trades / addresses ' + Math.round(f.transactionsPerHolder));
  }
  if (item.type === 'thin_spike') return [tr('24h 涨幅 ' + pct(f.change24hPercent), '24h change ' + pct(f.change24hPercent)), liquidity].filter(Boolean).join(' · ');
  return [f.volume24hUsd == null ? '' : tr('24h 成交 ' + usd(f.volume24hUsd), '24h volume ' + usd(f.volume24hUsd)), liquidity].filter(Boolean).join(' · ') || '—';
}
function assetQuoteStatus(asset) {
  if (asset.price==null) return '';
  const at=asset.fieldTimes?.price??asset.quoteAt;
  return !at?tr('时间未知','Time unknown'):isRecentObservation(at,900000,now.value)?'':tr('历史','Historical');
}
function assetChangeClass(asset) { return isRecentObservation(asset.fieldTimes?.change24h,900000,now.value) ? Number(asset.change24h)>0?'up':Number(asset.change24h)<0?'down':'' : ''; }
function assetQuoteTitle(asset) { return `${asset.fieldSources?.price??asset.provider??tr('来源未能识别','Unknown source')} · ${tr('报价时间','Quote time')} ${date(asset.fieldTimes?.price??asset.quoteAt)}`; }
function assetVolumeTitle(asset) {
  const at = asset.fieldTimes?.volume24h ?? asset.volumeAt;
  return `${tr('资产24h成交（USD）','Asset volume 24h (USD)')} · ${asset.fieldSources?.volume24h ?? asset.provider ?? tr('来源未能识别', 'Unknown source')} · ${tr('观测时间','Observed at')} ${date(at)}`;
}
function stockName(stock,ticker) { return stockThemeName(stock,ticker,lang.lang); }
function clockTime(value) { return value ? new Date(value).toLocaleTimeString(lang.lang === 'en' ? 'en-US' : 'zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false}) : '—'; }
async function loadBriefing() {
  const request = ++briefingRequest, language = lang.lang;
  briefingLoading.value = !briefing.value;
  try {
    const result = await getBriefing(language);
    if (request !== briefingRequest) return;
    briefingError.value = false;
    briefingStatus.value = result?.status ?? result?.reason ?? 'scheduled';
    if (result?.text || result?.items?.length) { briefing.value = result; briefingCache.set(language,result); }
  } catch { if (request === briefingRequest) briefingError.value = true; }
  finally { if (request === briefingRequest) briefingLoading.value = false; }
}
let feedTimer;
function loadFeed() { return feed.load(scope.value === 'all' ? null : scope.value); }
function onResourceChange(event) {
  const kind = event.detail?.kind;
  if (kind === 'briefing' || kind === 'all') loadBriefing();
  if (kind === 'feed' || kind === 'all') loadFeed();
}
onMounted(() => {
  loadBriefing(); loadFeed();
  if(themeView.value==='map'&&!themeMapReady.value)loadMarket();
  window.addEventListener('resource-change',onResourceChange);
  feedTimer = setInterval(() => { if (!document.hidden && !store.hasProjectionStream) { loadFeed(); loadBriefing(); } },30000);
});
onUnmounted(() => { persistHome();window.removeEventListener('resource-change',onResourceChange); clearInterval(feedTimer); briefingRequest++; selectedRequest++; marketRequest++; });
watch(() => lang.lang, () => { briefing.value = briefingCache.get(lang.lang) ?? null; briefingError.value = false; loadBriefing(); });
watch(scope,loadFeed);
</script>

<style scoped>
.home-page{min-width:0;gap:18px}.home-heading{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px 16px}.home-heading h2{margin:0;font-family:inherit;font-size:27px;line-height:1.25;font-weight:650;letter-spacing:-.025em}.home-deck{margin:5px 0 0;font-size:13px;color:var(--muted)}.home-heading>span,.home-subline{color:var(--muted);font-size:12px}.home-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.home-metrics>a{display:flex;align-items:center;justify-content:space-between;gap:12px;min-width:0;padding:15px 17px;border:1px solid var(--border);background:var(--panel);border-radius:9px;color:var(--text);text-decoration:none;transition:border-color .15s}.home-metrics>a:hover{border-color:var(--accent)}.home-metrics span{font-size:12px;color:var(--muted)}.home-metrics strong{font:600 26px/1.25 var(--number-font,inherit);font-variant-numeric:tabular-nums;letter-spacing:-.02em;overflow-wrap:anywhere}
.home-page .panel{min-width:0}.home-page .panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.home-page .panel-head h2{font-family:inherit;font-size:17px;font-weight:650;letter-spacing:-.01em}.home-page .panel-head>a,.theme-view-controls>a{font-size:12px;white-space:nowrap}.theme-view-controls,.selected-theme-actions{display:flex;align-items:center;gap:16px}.home-view-switch{display:flex;border:1px solid var(--border);border-radius:6px;padding:3px;background:var(--bg)}.home-view-switch button{border:0;border-radius:4px;background:none;font:inherit;font-size:12px;padding:5px 10px;color:var(--muted);cursor:pointer}.home-view-switch button[aria-pressed=true]{background:var(--panel);color:var(--accent)}.home-themes :deep(.theme-market.panel){margin:0;padding:0;border:0;border-radius:0;box-shadow:none}.home-selected-theme{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px 14px;margin-top:12px;padding:9px 12px;border:1px solid var(--border);border-radius:6px;background:var(--accent-soft);font-size:12px}.home-selected-theme>div:first-child{display:flex;align-items:center;flex-wrap:wrap;gap:12px}.home-selected-theme span{color:var(--muted)}.home-selected-theme a,.home-selected-theme button{color:var(--accent)}.home-selected-theme button,.home-rank-list button,.home-snapshot button,.home-map-error button{border:0;background:none;cursor:pointer;font:inherit;color:var(--accent);padding:0}
.home-market-grid{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(320px,1fr);gap:18px;align-items:stretch}.home-market-grid>*{min-width:0;height:480px;box-sizing:border-box;padding:16px 18px}.home-page .home-leaders{display:flex;flex-direction:column;padding:16px 18px}.home-leaders .panel-head{flex:none}.home-leaders .home-subline{margin:0 0 8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.home-rank-head,.home-leader-row{display:grid;grid-template-columns:22px minmax(100px,1.2fr) minmax(103px,1fr) 76px minmax(90px,.85fr);align-items:center;gap:10px}.has-themes .home-rank-head,.has-themes .home-leader-row{grid-template-columns:22px minmax(85px,1.2fr) minmax(95px,.95fr) minmax(58px,.6fr) 76px minmax(82px,.8fr)}.home-rank-head{flex:none;padding:9px 0;border-bottom:1px solid var(--border);font-size:11px;color:var(--muted)}.home-rank-list{flex:1;min-height:0}.home-leader-row{height:50px;border-bottom:1px solid var(--border);font-size:13px}.home-leader-row:last-child{border-bottom:0}.rank-identity{min-width:0}.rank-mobile-risk{display:none}.rank-asset{min-width:0;color:var(--text);text-decoration:none}.rank-asset:hover{color:var(--accent)}.home-leader-row strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.home-leader-row small{display:block;color:var(--muted);font-size:11px;margin-top:3px}.rank-position,.rank-tickers{font-size:11px;color:var(--muted)}.rank-tickers{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rank-risk{min-width:0;width:auto}.rank-risk :deep(summary){white-space:nowrap;font-size:11px}.rank-risk :deep(.risk-badge>div){left:auto;right:0}.rank-quote,.rank-amount,.rank-price,.home-rank-head>span:last-child{text-align:right;font-family:var(--number-font,inherit);font-variant-numeric:tabular-nums}.rank-quote>span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rank-quote em{font:inherit;color:var(--warning)}.rank-amount i{display:block;height:3px;max-width:100%;margin:5px 0 0 auto;border-radius:2px;background:var(--accent);opacity:.5}.home-rank-footer{display:flex;justify-content:space-between;gap:8px;margin:8px 0 0;padding-top:10px;border-top:1px solid var(--border);font-size:11px;color:var(--muted)}
.activity-warning{display:grid;place-items:center;flex:none;width:17px;height:17px;border:1px solid var(--warning);border-radius:50%;color:var(--warning);font-size:11px}.briefing-asset{color:var(--text)}.briefing-list{display:grid;gap:8px}.briefing-item{display:flex;align-items:baseline;gap:12px;font-size:13px;flex-wrap:wrap}.briefing-item>span:last-child{color:var(--muted)}.home-snapshot{padding:0 4px;font-size:12px;color:var(--muted)}.home-snapshot>summary{cursor:pointer;padding:4px 0}.home-snapshot>summary span{margin-left:14px}.home-snapshot .briefing-list{padding:12px 0}.home-networks{border-top:1px solid var(--border);padding-top:12px}.home-networks summary{display:flex;align-items:center;gap:18px;flex-wrap:wrap;cursor:pointer;font-size:12px;padding:4px 0;color:var(--muted)}.home-networks summary strong{font-weight:600;color:var(--text)}.network-expand{margin-left:auto;color:var(--accent)}.network-expand:after{content:' +';font-size:14px}.home-networks details[open] .network-expand:after{content:' −'}.network-columns,.network-row{display:grid;grid-template-columns:1fr .65fr 1fr .7fr;align-items:center;gap:18px;font-size:12px}.network-columns{padding:17px 0 9px;color:var(--muted)}.network-row{padding:13px 0;border-top:1px solid var(--border);color:var(--text);text-decoration:none}.network-row:hover{color:var(--accent)}.network-row small{display:block;font-size:11px;color:var(--muted);margin-top:4px}.network-row>span{font-variant-numeric:tabular-nums}.chain-share{display:block;height:3px;margin-top:5px;border-radius:2px;background:var(--surface-raised);overflow:hidden}.chain-share i{display:block;height:100%;background:var(--accent);opacity:.65}.home-network-pending{font-size:11px;color:var(--muted);margin:12px 0 0}.home-page :is(a,button,summary):focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(max-width:1150px){.home-market-grid{grid-template-columns:minmax(0,1.5fr) minmax(300px,1fr)}.has-themes .home-rank-head,.has-themes .home-leader-row{grid-template-columns:20px minmax(75px,1fr) minmax(94px,.9fr) 76px minmax(78px,.8fr);gap:8px}.rank-theme-heading,.rank-tickers{display:none}.home-metrics>a{padding:14px 12px;gap:7px}.home-metrics strong{font-size:23px}}
@media(max-width:900px){.home-market-grid{grid-template-columns:1fr}.home-market-grid>*{height:auto}.home-rank-list{flex:initial}.home-rank-head,.home-leader-row{grid-template-columns:22px minmax(100px,1.2fr) minmax(100px,1fr) 76px minmax(90px,.8fr)}.has-themes .home-rank-head,.has-themes .home-leader-row{grid-template-columns:22px minmax(90px,1.2fr) minmax(95px,1fr) minmax(58px,.6fr) 76px minmax(82px,.8fr)}.rank-theme-heading,.rank-tickers{display:block}}
@media(max-width:700px){.rank-mobile-risk{display:block;margin-top:4px}.rank-mobile-risk :deep(summary){font-size:10px}.home-leader-row{height:auto;min-height:62px;padding-block:7px}.home-heading h2{font-size:25px}.home-heading>span{font-size:11px}.home-deck{font-size:12px}.home-metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.home-metrics>a{padding:12px}.home-metrics strong{font-size:25px}.theme-view-controls{gap:10px}.theme-view-controls>a{font-size:11px}.home-view-switch button{padding:7px 8px}.home-themes .panel-head{flex-wrap:wrap;gap:10px}.home-rank-head,.home-leader-row,.has-themes .home-rank-head,.has-themes .home-leader-row{grid-template-columns:18px minmax(75px,1.1fr) minmax(88px,1fr) minmax(72px,.8fr);gap:7px}.rank-theme-heading,.rank-tickers,.rank-risk-heading,.rank-risk{display:none}.rank-quote>span,.rank-amount{font-size:12px}.home-leader-row:nth-child(n+6){display:none}.home-rank-footer{font-size:10px}.home-rank-footer span{display:none}.home-selected-theme{align-items:flex-start}.home-networks summary{gap:8px 14px}.home-networks summary>span:nth-child(2){font-size:11px;order:3;width:100%}.network-columns,.network-row{grid-template-columns:minmax(75px,1fr) 50px minmax(85px,1fr) minmax(65px,.8fr);gap:9px;font-size:11px}.network-row small{font-size:10px}}
@media(prefers-reduced-motion:reduce){.home-metrics>a{transition:none}}
.rank-asset{display:flex;align-items:center;gap:8px;min-width:0}.rank-name{display:block;min-width:0}.rank-name strong,.rank-name small{display:block;overflow-wrap:anywhere}
</style>
