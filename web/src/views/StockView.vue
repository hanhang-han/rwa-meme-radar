<template>
  <div class="stock-index-page">
    <section class="panel">
      <div class="stock-index-heading"><h2>{{ tr('股票','Stocks') }}</h2><span>{{ tr('显示','Showing') }} {{ cards.length }} / {{ tr('共','of') }} {{ allRows.length }} {{ tr('个主题','themes') }}</span></div>
      <div class="stock-index-toolbar"><p v-if="search">{{ tr('搜索','Search') }}：{{ route.query.q }} <button @click="setQuery({q:undefined})">×</button></p><select :value="sort" :aria-label="tr('排序','Sort')" @change="setQuery({sort:$event.target.value})"><option value="related">{{ tr('关联 Meme 数','Related meme count') }}</option><option value="ticker">{{ tr('股票代码','Ticker') }}</option><option value="change24h">{{ tr('股票代币 24h 涨跌','Stock-token 24h change') }}</option></select></div>
      <div v-if="!cards.length && loading" role="status" :aria-label="tr('加载股票','Loading stocks')"><div v-for="n in 8" :key="n" class="skeleton-line"></div></div>
      <p v-else-if="!cards.length" class="x-empty">{{ tr('暂无匹配股票主题。','No matching stock themes.') }} <button v-if="search" @click="setQuery({q:undefined})">{{ tr('清除搜索','Clear search') }}</button></p>
      <template v-else>
        <div class="stock-index-table scroll"><table class="tbl"><thead><tr><th>{{ tr('股票','Stock') }}</th><th class="numeric">{{ tr('股票代币价格','Stock-token price') }}</th><th class="numeric">24h</th><th class="numeric">{{ tr('配对 Meme','Paired memes') }}</th><th class="numeric">{{ tr('Meme 24h 成交','Meme 24h volume') }}</th><th>{{ tr('数据覆盖','Coverage') }}</th></tr></thead><tbody>
          <tr v-for="card in cards" :key="card.ticker"><td><RouterLink :to="themeLink(card.ticker)"><strong>{{ card.ticker }}</strong><small>{{ companyTitle(card.stock ?? card.list[0]) }}</small></RouterLink></td><td class="numeric" :title="quoteTitle(card.stock)"><LiveNumber :value="card.stock?.price" :currency="card.stock?.priceCurrency ?? ''" format="price" /><small>{{ card.stock?chainLabel(card.stock.chainId):tr('暂无数据','Unavailable') }}</small></td><td class="numeric" :class="Number(card.stock?.change24h)>0?'up':Number(card.stock?.change24h)<0?'down':''"><LiveNumber :value="card.stock?.change24h" format="percent" /><small v-if="card.stock?.change24h != null && !currentChange(card.stock)" class="historical">{{ tr('历史值','Historical') }}</small></td><td class="numeric">{{ card.theme.pairedCount }}<small v-if="card.theme.nameCount">+{{ card.theme.nameCount }} {{ tr('名称匹配','name matches') }}</small></td><td class="numeric">{{ usd(card.theme.volume.value) }}</td><td>{{ card.theme.volume.known }} / {{ card.theme.volume.total }}<small>{{ tr('美元成交数据','USD volume data') }}</small></td></tr>
        </tbody></table></div>
        <div class="stock-index-mobile"><RouterLink v-for="card in cards" :key="card.ticker" :to="themeLink(card.ticker)" class="stock-index-card"><div><strong>{{ card.ticker }} <small>{{ companyTitle(card.stock ?? card.list[0]) }}</small></strong><span>→</span></div><div><span><LiveNumber :value="card.stock?.price" :currency="card.stock?.priceCurrency ?? ''" format="price" /></span><span :class="Number(card.stock?.change24h)>0?'up':Number(card.stock?.change24h)<0?'down':''"><LiveNumber :value="card.stock?.change24h" format="percent" /> · 24h</span></div><small>{{ card.theme.pairedCount }} {{ tr('配对 Meme','paired memes') }} · {{ usd(card.theme.volume.value) }} {{ tr('24h 成交','24h volume') }}</small><small>{{ tr('成交覆盖','Volume coverage') }} {{ card.theme.volume.known }} / {{ card.theme.volume.total }}</small></RouterLink></div>
        <div class="x-pager"><button :disabled="safePage<=0" @click="setQuery({page:safePage-1})">{{ tr('上一页','Previous') }}</button><span>{{ safePage+1 }} / {{ pages }}</span><button :disabled="safePage+1>=pages" @click="setQuery({page:safePage+1})">{{ tr('下一页','Next') }}</button></div>
      </template>
    </section>
    <section v-if="sectors.length" class="panel stock-themes-index"><h2>{{ tr('主题指数','Theme indexes') }}</h2><div class="v2-baskets"><details v-for="sector in sectors" :key="sector.chainId+':'+sector.sector+':'+sector.basketVersion" class="x-basket"><summary class="stock-basket-summary"><strong>{{ themeLabel(sector.sector, lang.lang) }} <small>{{ sector.scopeLabel || chainLabel(sector.chainId) }}</small></strong><span :class="{'is-stale':sector.dataStatus!=='current'}">{{ sector.dataStatus==='current' && sector.value!=null?num(sector.value):'—' }}</span><small>{{ tr('有效行情','Current quotes') }} {{ num(sector.quoteCoverage?.fresh) }} / {{ num(sector.quoteCoverage?.total) }}<br>{{ tr('最后有效','Last valid') }} {{ date(sector.dataStatus==='current'?sector.at:sector.lastAt) }}</small></summary><div class="stock-basket-meta">{{ tr('基期','Base') }} {{ date(sector.baseAt) }} · {{ num(sector.members) }} {{ tr('个成分','constituents') }}</div><p v-for="part in sector.components??[]" :key="part.token"><RouterLink :to="{path:'/asset/'+sector.chainId+'/'+part.token,query:{chain:scope,from:'stock'}}">{{ part.symbol }}</RouterLink> {{ part.weight!=null?pct(part.weight*100):'' }}</p></details></div></section>
  </div>
</template>

<script setup>
import { computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useDashboardStore } from '../stores/dashboard';
import { useMinuteClock } from '../composables/useMinuteClock';
import { tr, useI18n } from '../i18n';
import { date, num, pct, usd } from '../utils/format';
import { chainScope, inChainScope } from '../utils/chain-scope';
import { preferredOfficialStock } from '../utils/product-labels';
import { countThemeRows } from '../utils/theme-presentation';
import { buildStockTheme, isRecentObservation, normalizeTicker, stockTicker } from '../utils/stock-theme-model';
import { themeLabel } from '../utils/theme-labels';
import LiveNumber from '../components/LiveNumber.vue';

const route=useRoute(), router=useRouter(), store=useDashboardStore(), now=useMinuteClock();
const {lang}=useI18n();
const scope=computed(()=>chainScope(route.query));
const search=computed(()=>String(route.query.q??'').trim().toLowerCase());
const sort=computed(()=>['ticker','change24h'].includes(route.query.sort)?route.query.sort:'related');
const loading=computed(()=>!store.snapshot||store.snapshot.unified?.snapshotScope==='overview');
const sectors=computed(()=>(store.snapshot?.unified?.sectors??[]).filter(row=>inChainScope(row,scope.value)));
const allRows=computed(()=>{
  const grouped=new Map(), stockKeys=new Map();
  for(const stock of store.stockTokens){
    const ticker=stockTicker(stock);if(!ticker||!inChainScope(stock,scope.value))continue;
    if(!grouped.has(ticker))grouped.set(ticker,{ticker,list:[],relations:[]});
    grouped.get(ticker).list.push(stock);stockKeys.set(`${stock.chainId}:${String(stock.tokenContractAddress).toLowerCase()}`,ticker);
  }
  for(const relation of store.relations){
    if(!inChainScope(relation,scope.value))continue;
    const ticker=stockKeys.get(`${relation.chainId}:${String(relation.stock).toLowerCase()}`)??normalizeTicker(relation.ticker);
    grouped.get(ticker)?.relations.push(relation);
  }
  const rows=[...grouped.values()].filter(group=>!search.value||[group.ticker,...group.list.flatMap(stock=>[stock.tokenSymbol,stock.tokenName,stock.tokenContractAddress,stock.stockIdentity?.nameZh,stock.stockIdentity?.nameEn])].some(value=>String(value??'').toLowerCase().includes(search.value)))
    .map(group=>({...group,count:countThemeRows(group.relations),stock:preferredOfficialStock(group.list)}));
  return rows.sort((a,b)=>sort.value==='ticker'?a.ticker.localeCompare(b.ticker,undefined,{numeric:true}):sort.value==='change24h'?(Number(b.stock?.change24h??-Infinity)-Number(a.stock?.change24h??-Infinity)||a.ticker.localeCompare(b.ticker)):(b.count-a.count||a.ticker.localeCompare(b.ticker,undefined,{numeric:true})));
});
const PAGE_SIZE=20;
const pages=computed(()=>Math.max(1,Math.ceil(allRows.value.length/PAGE_SIZE)));
const safePage=computed(()=>Math.min(Math.max(0,Number(route.query.page)||0),pages.value-1));
const cards=computed(()=>allRows.value.slice(safePage.value*PAGE_SIZE,(safePage.value+1)*PAGE_SIZE).map(group=>({...group,theme:buildStockTheme({ticker:group.ticker,stockTokens:group.list,relations:group.relations,assets:store.assets,scope:scope.value,now:now.value})})));
const COMPANY={AAPL:'苹果',AMD:'超威半导体',AMZN:'亚马逊',BABA:'阿里巴巴',COIN:'Coinbase',GME:'游戏驿站',GOOGL:'谷歌',HOOD:'Robinhood',INTC:'英特尔',META:'Meta',MSFT:'微软',NVDA:'英伟达',NFLX:'奈飞',QQQ:'纳斯达克100指数ETF',SPY:'标普500指数ETF',TSLA:'特斯拉',TSM:'台积电',MU:'美光科技',PLTR:'帕兰提尔','700':'腾讯控股','1810':'小米集团','9992':'泡泡玛特'};
function companyTitle(stock){const identity=stock?.stockIdentity;return lang.lang==='en'?(identity?.nameEn??stock?.tokenName??''):(identity?.nameZh??COMPANY[stockTicker(stock)]??identity?.nameEn??stock?.tokenName??'');}
function themeLink(ticker){return {path:`/stock/${encodeURIComponent(ticker)}`,query:{chain:scope.value}};}
function setQuery(patch){router.push({query:{...route.query,...patch,page:patch.page||undefined}});}
function chainLabel(chain){return {'56':'BNB Chain','196':'X Layer','4663':'Robinhood Chain'}[chain]??chain;}
function quoteTitle(stock){return `${stock?.fieldSources?.price??stock?.provider??'—'} · ${date(stock?.fieldTimes?.price??stock?.quoteAt)}`;}
function currentChange(stock){return isRecentObservation(stock?.fieldTimes?.change24h,900000,now.value);}
</script>

<style scoped>
.stock-index-heading,.stock-index-toolbar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; margin-bottom:16px; }
.stock-index-heading h2 { font-size:20px; }
.stock-index-heading span,.stock-index-toolbar p { color:var(--muted); font-size:12px; }
.stock-index-toolbar { justify-content:flex-end; }
.stock-index-toolbar p { margin-right:auto; }
.stock-index-toolbar select { padding:7px 10px; color:var(--text); background:var(--bg); border:1px solid var(--border); border-radius:6px; }
.stock-index-table th { font-size:12px; }
.stock-index-table td { padding-top:15px; padding-bottom:15px; }
.stock-index-table small { display:block; font-size:12px; color:var(--muted); margin-top:3px; }
.stock-index-table .historical { color:var(--warning); }
.stock-index-table a { color:var(--text); }
.stock-index-table td.numeric,.stock-index-table th.numeric { text-align:right; font-family:ui-monospace,monospace; font-variant-numeric:tabular-nums; }
.stock-index-mobile { display:none; }
.stock-themes-index h2 { font-size:16px; margin-bottom:14px; }
@media(max-width:760px) {
  .stock-index-table { display:none; }
  .stock-index-mobile { display:grid; gap:10px; }
  .stock-index-card { display:flex; flex-direction:column; gap:10px; padding:14px; border:1px solid var(--border); border-radius:8px; background:var(--bg); color:var(--text); }
  .stock-index-card > div { display:flex; align-items:center; justify-content:space-between; gap:10px; }
  .stock-index-card small { color:var(--muted); font-size:12px; }
  .stock-index-card strong small { display:block; margin-top:3px; }
}
</style>
