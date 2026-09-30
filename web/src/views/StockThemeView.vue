<template>
  <div class="stock-theme-page">
    <div class="theme-breadcrumb"><RouterLink :to="{path:'/stock',query:{chain:scope}}">← {{ tr('股票','Stocks') }}</RouterLink><span>{{ theme.ticker }}</span></div>
    <section class="panel theme-heading">
      <div><p class="theme-eyebrow">{{ tr('股票主题','Stock theme') }}</p><h2>{{ themeName }} <span>{{ theme.ticker }}</span></h2><p class="theme-subtitle">{{ tr('关联 Meme 与链上交易池','Related memes and on-chain pools') }}</p></div>
      <button type="button" class="theme-follow" :aria-pressed="followed" @click="toggleFollow">{{ followed?tr('★ 已关注','★ Following'):tr('☆ 关注','☆ Follow') }}</button>
    </section>

    <div v-if="loading" class="panel theme-skeleton" role="status" :aria-label="tr('加载股票主题','Loading stock theme')"><p class="theme-note">{{ store.error?tr('主题暂时无法加载。','The theme could not be loaded.'):tr('正在加载股票主题…','Loading the stock theme…') }} <button type="button" @click="store.poll({view:'market'})">{{ tr('重试','Retry') }}</button></p><div v-for="n in 6" :key="n" class="skeleton-line"></div></div>
    <template v-else>
      <div class="theme-metrics">
        <div><span>{{ tr('池子配对 Meme','Paired memes') }}</span><strong class="mono">{{ num(theme.pairedCount) }}</strong></div>
        <div :title="tr('按币统计 24h 成交，含其他池；并非股票配对池成交。','24h token volume includes other pools; it is not stock-pair pool volume.')"><span>{{ tr('关联 Meme 总成交 · 24h','Related meme total volume · 24h') }}</span><strong class="mono">{{ usd(theme.volume.value) }}</strong><small>{{ tr('覆盖','Coverage') }} {{ theme.volume.known }} / {{ theme.volume.total }}<template v-if="!theme.volume.complete"> · {{ tr('部分数据','Partial data') }}</template></small></div>
        <div><span>{{ tr('关联交易池','Related pools') }}</span><strong class="mono">{{ num(theme.pools.length) }}</strong></div>
        <div><span>{{ tr('名称匹配','Name matches') }}</span><strong class="mono">{{ num(theme.nameCount) }}</strong><small>{{ tr('不计入关联成交','Excluded from related volume') }}</small></div>
      </div>

      <div class="theme-analysis-grid">
        <section v-if="chartAsset" class="panel theme-chart">
          <div class="panel-head"><h2>{{ tr('股票代币行情','Stock-token market') }}</h2><label v-if="quoteVersions.length>1" class="theme-version-select"><span class="sr-only">{{ tr('股票代币版本','Stock-token version') }}</span><select :value="versionKey(selectedStock)" @change="selectedVersion=$event.target.value"><option v-for="stock in quoteVersions" :key="versionKey(stock)" :value="versionKey(stock)">{{ stock.tokenSymbol || theme.ticker }} · {{ chainLabel(stock.chainId) }}</option></select></label></div>
          <div class="theme-quote"><strong :title="stockPriceTitle"><LiveNumber :value="selectedStock.price" :currency="selectedStock.priceCurrency" format="price" /></strong><span :class="Number(selectedStock.change24h)>0?'up':Number(selectedStock.change24h)<0?'down':''"><LiveNumber :value="selectedStock.change24h" format="percent" /> · 24h</span></div>
          <p class="theme-chart-caption">{{ selectedStock.tokenSymbol }} · {{ chainLabel(selectedStock.chainId) }}<span v-if="selectedStock.priceScope==='issuer-derived'"> · {{ tr('发行方参考价','Issuer reference price') }}</span></p>
          <QuoteStatus :row="selectedStock" />
          <CandleChart :asset="chartAsset" />
        </section>
        <section class="panel theme-contributions" :class="{'is-wide':!chartAsset}">
          <div class="panel-head"><h2>{{ tr('Meme 成交占比','Meme volume share') }}</h2><span v-if="theme.concentrated" class="concentration-tag">{{ tr('成交集中','Concentrated volume') }}</span></div>
          <p class="theme-note">{{ tr('占已知美元总成交；按币统计，含其他池。','Share of known USD token volume, including other pools.') }} {{ theme.volume.known }} / {{ theme.volume.total }}</p>
          <div v-for="row in contributions" :key="row.key" class="contribution-row">
            <div><RouterLink :to="assetLink(row)">{{ row.asset?.symbol || short(row.token) }}</RouterLink><span class="mono">{{ shareText(row.volumeShare) }}</span></div>
            <div class="contribution-bar" :title="usd(row.volume)"><i :style="{width:(row.volumeShare*100)+'%'}"></i></div>
            <small>{{ usd(row.volume) }} · {{ chainLabel(row.chainId) }}</small>
          </div>
          <p v-if="!contributions.length" class="x-empty">{{ theme.volume.value===0?tr('已知 24h 成交额为零。','Known 24h volume is zero.'):tr('暂无可比较的成交数据。','Comparable volume data is unavailable.') }}</p>
          <p v-if="remainingContributors" class="theme-note">{{ tr('其余','Other') }} {{ remainingContributors }} {{ tr('个资产见下表','assets are listed below') }}</p>
        </section>
      </div>

      <section class="panel">
        <div class="panel-head"><h2>{{ tr('关联 Meme','Related memes') }}</h2><span class="theme-note">{{ tr('显示','Showing') }} {{ theme.rows.length }} / {{ tr('共','of') }} {{ theme.rows.length }}</span></div>
        <div v-if="theme.rows.length" class="scroll">
          <table class="tbl theme-assets-table"><thead><tr><th>Meme</th><th>{{ tr('关系','Relationship') }}</th><th class="numeric">{{ tr('24h 成交','24h volume') }}</th><th class="numeric">{{ tr('已知成交占比','Known volume share') }}</th><th class="numeric">{{ tr('1h 涨跌','1h change') }}</th><th>{{ tr('池创建时间','Pool created') }}</th><th>{{ tr('风险','Risk') }}</th><th>{{ tr('详情','Details') }}</th></tr></thead><tbody>
            <tr v-for="row in theme.rows" :key="row.key"><td><RouterLink :to="assetLink(row)"><strong>{{ row.asset?.symbol || short(row.token) }}</strong></RouterLink><small>{{ chainLabel(row.chainId) }} · {{ short(row.token) }}</small></td>
              <td><span class="theme-relation" :class="{'is-name':row.level==='B'}">{{ row.level==='A'?tr('池子配对','Pool pair'):tr('名称匹配','Name match') }}</span><small v-if="row.pools.length>1">{{ row.pools.length }} {{ tr('个池','pools') }}</small></td>
              <td class="numeric" :title="fieldTitle(row.asset,'volume24h')"><LiveNumber :value="row.asset?.volume24h" :currency="row.asset?.volumeCurrency ?? row.asset?.priceCurrency" /><small v-if="row.asset?.volume24h != null && !fieldCurrent(row.asset,'volume24h')" class="historical">{{ tr('历史值','Historical') }}</small></td>
              <td class="numeric">{{ shareText(row.volumeShare) }}</td>
              <td class="numeric" :class="Number(row.asset?.change1h)>0?'up':Number(row.asset?.change1h)<0?'down':''" :title="fieldTitle(row.asset,'change1h')">{{ pct(row.asset?.change1h) }}<small v-if="row.asset?.change1h != null && !fieldCurrent(row.asset,'change1h')" class="historical">{{ tr('历史值','Historical') }}</small></td>
              <td :title="date(row.createdAt)">{{ row.createdAt?age(row.createdAt):tr('暂无数据','Unavailable') }}</td><td><RiskBadge :asset="row.asset || {}" /></td>
              <td><RouterLink :to="assetLink(row,row.level==='A')">{{ row.level==='A'?tr('关系证据','Relationship'):tr('资产详情','Asset details') }} →</RouterLink></td>
            </tr>
          </tbody></table>
        </div>
        <p v-else class="x-empty">{{ tr('当前范围暂无关联 Meme。','No related memes in this scope.') }} <RouterLink :to="{path:'/meme',query:{chain:scope,rel:'all'}}">{{ tr('浏览 Meme','Browse memes') }} →</RouterLink></p>
      </section>

      <section class="panel theme-events">
        <div class="panel-head"><h2>{{ tr('动态','Activity') }}</h2><RouterLink :to="{path:'/events',query:{chain:scope}}">{{ tr('全部动态','All activity') }} →</RouterLink></div>
        <article v-for="event in theme.events" :key="event.key"><time :title="date(event.at)">{{ age(event.at) }}</time><RouterLink :to="{path:`/asset/${event.chainId}/${event.token}`,query:{chain:scope,from:'stock',ticker:theme.ticker,tab:event.pool?'relation':'overview',pool:event.pool||undefined}}"><strong>{{ event.asset?.symbol || short(event.token) }}</strong> · {{ eventLabel(event.kind) }}</RouterLink><small>{{ chainLabel(event.chainId) }}<template v-if="event.count>1"> · {{ event.count }} {{ tr('次记录','records') }}</template></small></article>
        <p v-if="!theme.events.length" class="x-empty">{{ tr('暂无关联动态。','No related activity yet.') }}</p>
      </section>

      <details v-if="theme.versions.length" class="panel theme-versions"><summary>{{ tr('股票代币','Stock tokens') }} · {{ theme.versions.length }}</summary><div v-for="stock in theme.versions" :key="versionKey(stock)"><span>{{ stock.tokenSymbol }} · {{ chainLabel(stock.chainId) }}</span><RouterLink :to="{path:`/asset/${stock.chainId}/${stock.tokenContractAddress}`,query:{chain:scope,from:'stock',ticker:theme.ticker}}">{{ short(stock.tokenContractAddress) }} →</RouterLink></div></details>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { useDashboardStore } from '../stores/dashboard';
import { useFeedStore } from '../stores/feed';
import { useMinuteClock } from '../composables/useMinuteClock';
import { tr, useI18n } from '../i18n';
import { age, date, num, pct, short, usd } from '../utils/format';
import { chainScope } from '../utils/chain-scope';
import { marketCatalogReady } from '../utils/meme-filter-model';
import { buildStockTheme, isRecentObservation, normalizeTicker, stockThemeName } from '../utils/stock-theme-model';
import { isOfficialStock, preferredOfficialStock } from '../utils/product-labels';
import { readThemeWatches, writeThemeWatches } from '../utils/theme-map-model';
import CandleChart from '../components/CandleChart.vue';
import LiveNumber from '../components/LiveNumber.vue';
import QuoteStatus from '../components/QuoteStatus.vue';
import RiskBadge from '../components/RiskBadge.vue';

const route=useRoute(), store=useDashboardStore(), feed=useFeedStore(), now=useMinuteClock();
const { lang }=useI18n();
const scope=computed(()=>chainScope(route.query));
const ticker=computed(()=>normalizeTicker(route.params.ticker));
const loading=computed(()=>!marketCatalogReady(store.snapshot));
const theme=computed(()=>buildStockTheme({ticker:ticker.value,stockTokens:store.stockTokens,assets:store.assets,relations:store.relations,events:feed.relationships,scope:scope.value,now:now.value}));
const selectedVersion=ref('');
const versionKey=stock=>`${stock?.chainId}:${stock?.tokenContractAddress}`;
const quoteVersions=computed(()=>theme.value.versions.filter(stock=>isOfficialStock(stock)&&stock.tokenContractAddress&&stock.price!=null&&Number.isFinite(Number(stock.price))));
const selectedStock=computed(()=>quoteVersions.value.find(stock=>versionKey(stock)===selectedVersion.value)??preferredOfficialStock(quoteVersions.value));
watch(()=>selectedStock.value,stock=>{if(stock&&!selectedVersion.value)selectedVersion.value=versionKey(stock);},{immediate:true});
watch([ticker,scope],()=>{selectedVersion.value='';feed.load(scope.value==='all'?undefined:scope.value);});
const chartAsset=computed(()=>selectedStock.value?{...selectedStock.value,token:selectedStock.value.tokenContractAddress,kind:'stock'}:null);
const themeName=computed(()=>stockThemeName(selectedStock.value??theme.value.versions[0],theme.value.ticker,lang.lang));
const stockPriceTitle=computed(()=>`${selectedStock.value?.fieldSources?.price??selectedStock.value?.provider??'—'} · ${date(selectedStock.value?.fieldTimes?.price??selectedStock.value?.quoteAt)}`);
const contributions=computed(()=>theme.value.rows.filter(row=>row.volumeShare>0).slice(0,6));
const remainingContributors=computed(()=>Math.max(0,theme.value.rows.filter(row=>row.volumeShare>0).length-6));
const watched=ref(readThemeWatches());
const watchKey=computed(()=>`stock:${theme.value.ticker}`);
const followed=computed(()=>watched.value.has(watchKey.value));
function syncWatches(){watched.value=readThemeWatches();}
function toggleFollow(){const next=new Set(watched.value);next.has(watchKey.value)?next.delete(watchKey.value):next.add(watchKey.value);writeThemeWatches(next);watched.value=next;}
onMounted(()=>{feed.load(scope.value==='all'?undefined:scope.value);window.addEventListener('theme-watch-change',syncWatches);window.addEventListener('storage',syncWatches);});
onUnmounted(()=>{window.removeEventListener('theme-watch-change',syncWatches);window.removeEventListener('storage',syncWatches);});
function assetLink(row,relation=false){return {path:`/asset/${row.chainId}/${row.token}`,query:{chain:scope.value,from:'stock',ticker:theme.value.ticker,...(relation?{tab:'relation',pool:row.pools[0]?.relation.pool}:{})}};}
function shareText(value){return value==null?'—':`${(value*100).toFixed(1)}%`;}
function fieldTitle(asset,field){return `${asset?.fieldSources?.[field]??'—'} · ${date(asset?.fieldTimes?.[field])}`;}
function fieldCurrent(asset,field){return isRecentObservation(asset?.fieldTimes?.[field],900000,now.value);}
function chainLabel(chain){return {'56':'BNB Chain','196':'X Layer','4663':'Robinhood Chain'}[chain]??chain;}
function eventLabel(kind){return kind==='pool-created'?tr('创建交易池','Pool created'):['verified','relation-verified'].includes(kind)?tr('新增池子配对','Pool pair recorded'):kind==='discovered'?tr('首次收录','First indexed'):tr('发现交易池','Pool discovered');}
</script>

<style scoped>
.theme-breadcrumb { display:flex; gap:14px; font-size:12px; margin:0 0 14px; color:var(--muted); }
.theme-breadcrumb a { color:var(--text); }
.theme-heading { display:flex; align-items:center; justify-content:space-between; gap:20px; }
.theme-heading h2 { font-size:24px; font-weight:650; margin:3px 0; }
.theme-heading h2 span { font-size:16px; color:var(--muted); font-weight:400; margin-left:8px; }
.theme-eyebrow,.theme-subtitle,.theme-note,.theme-chart-caption { color:var(--muted); font-size:12px; }
.theme-follow { background:var(--surface-raised); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:8px 14px; cursor:pointer; white-space:nowrap; }
.theme-follow[aria-pressed=true] { color:var(--accent); border-color:var(--accent); }
.theme-metrics { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:14px; }
.theme-metrics > div { padding:16px; background:var(--panel); border:1px solid var(--border); border-radius:8px; display:flex; flex-direction:column; gap:4px; }
.theme-metrics span,.theme-metrics small { font-size:12px; color:var(--muted); }
.theme-metrics strong { font-size:24px; }
.theme-analysis-grid { display:grid; grid-template-columns:minmax(0,2fr) minmax(250px,1fr); gap:14px; }
.theme-analysis-grid .panel { min-width:0; }
.theme-contributions.is-wide { grid-column:1/-1; }
.theme-quote { display:flex; align-items:baseline; gap:14px; font-variant-numeric:tabular-nums; margin:6px 0; }
.theme-quote > strong { font-size:26px; }
.theme-quote > span { font-size:14px; }
.theme-version-select select { max-width:230px; background:var(--bg); color:var(--text); border:1px solid var(--border); padding:6px 8px; border-radius:5px; }
.concentration-tag { font-size:12px; color:var(--warning); }
.contribution-row { margin:16px 0; }
.contribution-row > div:first-child { display:flex; justify-content:space-between; gap:8px; font-size:13px; }
.contribution-row a { color:var(--text); }
.contribution-row small { color:var(--muted); font-size:12px; }
.contribution-bar { background:var(--surface-raised); height:5px; border-radius:2px; overflow:hidden; margin:7px 0; }
.contribution-bar i { display:block; height:100%; background:var(--accent); }
.theme-assets-table th { font-size:12px; white-space:nowrap; }
.theme-assets-table td { padding-top:13px; padding-bottom:13px; }
.theme-assets-table td small { display:block; font-size:12px; color:var(--muted); margin-top:3px; }
.theme-assets-table td.numeric,.theme-assets-table th.numeric { text-align:right; font-family:ui-monospace,monospace; font-variant-numeric:tabular-nums; }
.theme-assets-table a { color:var(--text); text-decoration:underline; text-underline-offset:3px; }
.theme-assets-table .historical { color:var(--warning); }
.theme-relation { display:inline-block; font-size:12px; padding:2px 6px; border-radius:4px; background:var(--accent-soft); color:var(--accent); white-space:nowrap; }
.theme-relation.is-name { background:var(--surface-raised); color:var(--muted); }
.theme-events article { display:grid; grid-template-columns:115px 1fr auto; align-items:center; gap:12px; padding:13px 0; border-bottom:1px solid var(--border); }
.theme-events article:last-child { border-bottom:0; }
.theme-events time,.theme-events small { font-size:12px; color:var(--muted); }
.theme-events a { color:var(--text); }
.theme-versions summary { cursor:pointer; color:var(--muted); font-size:13px; }
.theme-versions > div { display:flex; justify-content:space-between; flex-wrap:wrap; gap:10px; padding-top:14px; font-size:13px; }
.theme-versions a { color:var(--text); }
.sr-only { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); }
@media(max-width:900px) { .theme-analysis-grid { grid-template-columns:1fr; } }
@media(max-width:600px) {
  .theme-heading { align-items:flex-start; gap:12px; }
  .theme-heading h2 { font-size:20px; }
  .theme-heading h2 span { display:block; margin:3px 0; }
  .theme-metrics { grid-template-columns:1fr 1fr; }
  .theme-metrics strong { font-size:21px; }
  .theme-version-select select { max-width:160px; }
  .theme-events article { grid-template-columns:1fr auto; }
  .theme-events time { grid-column:1/-1; }
  .theme-events small { white-space:nowrap; }
  .theme-chart .panel-head { flex-wrap:wrap; gap:10px; }
}
</style>
