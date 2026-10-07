<template>
  <div class="home-theme-selector">
    <div class="theme-selector-heading"><span>{{ tr('热门主题','Popular themes') }} · {{ cards.length }}</span><div><button v-if="cards.length>4" type="button" :aria-expanded="expanded" @click="expanded=!expanded">{{ expanded?tr('收起','Show less'):tr('更多主题','More themes') }} <span aria-hidden="true">{{ expanded?'−':'+' }}</span></button><button type="button" :aria-pressed="!modelValue" @click="emit('update:modelValue','')">{{ tr('全部市场','All markets') }}</button></div></div>
    <div class="theme-selector-cards" :class="{'single-theme':cards.length===1}" :style="{'--theme-columns':Math.min(4,cards.length)||1}" role="group" :aria-label="tr('选择股票主题','Choose a stock theme')">
      <article v-for="card in cards.slice(0,expanded?8:4)" :key="card.ticker" :class="{'is-selected':modelValue===card.ticker,'has-history':hasHistory(card)}">
        <button type="button" :aria-pressed="modelValue===card.ticker" @click="emit('update:modelValue',card.ticker)">
          <span class="selector-stock"><AssetAvatar :asset="card.stock" :symbol="card.ticker" :identity="'stock:'+card.ticker" :size="28" /><span class="selector-stock-name"><strong :title="stockThemeName(card.stock,card.ticker,lang.lang)">{{ stockThemeName(card.stock,card.ticker,lang.lang) }}</strong><small>{{ stockThemeCodeLabel(card.stock,card.ticker,lang.lang) }}</small></span></span>
          <span class="selector-price" :title="tr('股票侧代币报价，币种见数值；观测时间','Stock-token quote in the displayed currency; observed at')+' '+date(card.stock?.fieldTimes?.price??card.stock?.quoteAt)"><LiveNumber :value="card.stock?.price" :currency="card.stock?.priceCurrency??''" format="price" :label="tr('股票代币价格','Stock-token price')" /><em v-if="quoteStatus(card.stock)">{{ quoteStatus(card.stock) }}</em><small :class="changeClass(card.stock)" :title="tr('股票侧代币24h价格涨跌（%）；缺失不表示零','Stock-token price change over 24h (%); missing does not mean zero')">{{ tr('24h 涨跌','24h change') }} {{ pct(card.stock?.change24h) }}</small></span>
          <ThemeSparkline v-if="hasHistory(card)" :points="card.sparkline??[]" />
          <span class="selector-meta"><span :title="tr('当前主题符合条件的同池Meme数量','Qualifying pool-paired memes in this theme')">{{ num(card.assetCount) }} {{ tr('同池 Meme','paired memes') }}</span><span v-if="ratio(card)!=null" :title="ratioTitle(card)">{{ tr('量比','Volume ratio') }} {{ ratio(card).toFixed(2)+'×' }}</span></span>
        </button>
        <RouterLink :to="themeNavigationLink(card.ticker,{route,scope})">{{ tr('主题详情','Theme details') }} ↗</RouterLink>
      </article>
    </div>
    <p class="selector-note">{{ tr('股票代币行情','Stock-token markets') }}</p>
  </div>
</template>
<script setup>
import AssetAvatar from './AssetAvatar.vue';
import { ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { themeNavigationLink } from '../utils/navigation-context';
import { readPageState, writePageState } from '../utils/page-navigation-state';
import { tr, useI18n } from '../i18n';
import LiveNumber from './LiveNumber.vue';
import { date, num, pct } from '../utils/format';
import { isRecentObservation, stockThemeName, stockThemeCodeLabel } from '../utils/stock-theme-model';
import ThemeSparkline from './ThemeSparkline.vue';
import { useMinuteClock } from '../composables/useMinuteClock';
const route=useRoute();
const props=defineProps({cards:{type:Array,default:()=>[]},modelValue:{type:String,default:''},scope:{type:String,default:'all'}});
const emit=defineEmits(['update:modelValue']);
const {lang}=useI18n();
const stateKey=()=>`home-theme-cards:${props.scope}`;
const expanded=ref(readPageState(stateKey())?.expanded===true);
watch(expanded,value=>writePageState(stateKey(),{expanded:value}));
watch(()=>props.scope,()=>{expanded.value=readPageState(stateKey())?.expanded===true;});
const now=useMinuteClock();
function hasHistory(card){const rows=(card.sparkline??[]).map((point,index)=>typeof point==='number'?{t:index,value:point}:point).filter(point=>point?.value!=null&&point.value!==''&&Number.isFinite(Number(point.value))&&Number.isFinite(Number(point.t)));return rows.length>1&&Math.max(...rows.map(point=>Number(point.t)))>Math.min(...rows.map(point=>Number(point.t)));}
function quoteStatus(stock){const at=stock?.fieldTimes?.price??stock?.quoteAt;return stock?.price==null?'':!at?tr('时间未知','Time unknown'):isRecentObservation(at,900000,now.value)?'':tr('历史','Historical');}
function ratio(card){const value=card.volumeRatio7d?.value??card.volumeRatio7d;return value!=null&&typeof value!=='object'&&Number.isFinite(Number(value))?Number(value):null;}
function ratioTitle(card){return ratio(card)==null?tr('历史样本不足，暂无量比','Insufficient historical samples'):tr('同池当前24h成交 / 前7个完整日UTC同小时同源成交中位数（倍）','Current paired-pool 24h volume / prior 7 complete days’ same-UTC-hour, same-source median (multiple)');}
function changeClass(stock){return isRecentObservation(stock?.fieldTimes?.change24h,900000,now.value)?Number(stock.change24h)>0?'up':Number(stock.change24h)<0?'down':'':'';}
</script>
<style scoped>
.home-theme-selector{min-width:0;max-width:100%}.theme-selector-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:11px;font-size:12px;color:var(--muted)}.theme-selector-heading>div{display:flex;align-items:center;gap:14px}.theme-selector-heading button{font:inherit;border:0;background:transparent;color:var(--muted);padding:4px 0;cursor:pointer}.theme-selector-heading button:hover,.theme-selector-heading button[aria-pressed=true]{color:var(--accent)}.theme-selector-cards{display:grid;grid-template-columns:repeat(var(--theme-columns),minmax(0,1fr));gap:11px}.theme-selector-cards article{min-width:0;border:1px solid var(--border);border-radius:7px;padding:13px 14px;background:var(--panel);transition:background .15s ease,border-color .15s ease}.theme-selector-cards article:hover{border-color:var(--accent)}.theme-selector-cards article.is-selected{border-color:var(--accent);background:var(--accent-soft)}.theme-selector-cards article>button{display:grid;gap:7px;width:100%;padding:0;background:none;color:var(--text);border:0;text-align:left;font:inherit;cursor:pointer}.selector-stock{display:flex;justify-content:space-between;align-items:center;gap:8px;min-width:0}.selector-stock strong{font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.selector-stock>span{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.selector-price{display:flex;align-items:baseline;gap:5px 8px;flex-wrap:wrap;font-family:var(--number-font);font-variant-numeric:tabular-nums;font-size:15px;font-weight:600}.selector-price small{margin-left:auto;font-size:11px;font-weight:400}.selector-price em{color:var(--warning);font-style:normal;font-size:11px;font-weight:400}.selector-meta{display:flex;justify-content:space-between;gap:7px;font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}.theme-selector-cards article>a{display:block;width:max-content;margin-top:8px;font-size:11px;color:var(--accent);text-decoration:none}.theme-selector-cards article>a:hover{text-decoration:underline}.selector-note{color:var(--muted);font-size:11px;margin:9px 0 0}.theme-selector-cards :deep(.theme-sparkline-label){position:absolute;clip-path:inset(50%);width:1px;height:1px;overflow:hidden;white-space:nowrap}.theme-selector-cards :deep(.theme-sparkline){height:30px}.theme-selector-cards :deep(.theme-sparkline-empty){height:30px;font-size:11px}.home-theme-selector :is(button,a):focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(min-width:701px){.theme-selector-cards.single-theme article{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:20px;padding:13px 16px}.theme-selector-cards.single-theme article>button{grid-template-columns:minmax(130px,.8fr) minmax(160px,1fr) minmax(160px,1.1fr) minmax(140px,.8fr);align-items:center;gap:18px}.theme-selector-cards.single-theme article:not(.has-history)>button{grid-template-columns:minmax(130px,1fr) minmax(160px,1.1fr) minmax(140px,.8fr)}.single-theme .selector-stock{display:grid;justify-content:start;gap:3px}.single-theme .selector-price{display:grid;gap:3px;justify-content:start}.single-theme .selector-price small{margin-left:0}.single-theme .selector-meta{display:grid;gap:4px}.theme-selector-cards.single-theme article>a{margin:0;white-space:nowrap}}
@media(min-width:701px) and (max-width:1000px){.theme-selector-cards.single-theme article>button{grid-template-columns:minmax(95px,.8fr) minmax(135px,1fr) minmax(115px,1fr);gap:12px}.single-theme .selector-meta{grid-column:1/-1;display:flex;justify-content:flex-start;gap:18px}.theme-selector-cards.single-theme article{gap:12px}}
@media(max-width:700px){.theme-selector-cards{display:flex;max-width:100%;min-width:0;overflow-x:auto;scroll-snap-type:x proximity;padding-bottom:5px}.theme-selector-cards article{flex:0 0 220px;scroll-snap-align:start;padding:13px}.theme-selector-heading button{min-height:40px}.theme-selector-heading>div{gap:11px}.theme-selector-cards article>a{min-height:28px;display:flex;align-items:center}.selector-stock strong{font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
@media(prefers-reduced-motion:reduce){.theme-selector-cards article{transition:none}}
.selector-stock .selector-stock-name{display:grid;gap:3px;min-width:0;white-space:normal;overflow:visible}.selector-stock-name small{font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.selector-stock,.single-theme .selector-stock{display:flex;justify-content:flex-start;gap:9px}.selector-stock-name strong{color:var(--text)}
</style>
