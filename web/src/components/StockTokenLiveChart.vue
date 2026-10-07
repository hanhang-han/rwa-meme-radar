<template>
  <div v-if="options.length" class="stock-live-chart">
    <div class="stock-live-head"><strong>{{ tr('链上 K 线','On-chain candles') }}</strong><select v-model="selectedKey" :aria-label="tr('链上交易市场','On-chain trading market')"><option v-for="option in options" :key="key(option)" :value="key(option)">{{ option.symbol }} · {{ chainName(option.chainId) }}</option></select><span v-if="market" class="theme-note">{{ tr('计价','Quote') }} · {{ market.priceCurrency }}</span></div>
    <p class="stock-live-note">{{ tr('使用所选池的真实成交，包装代币与原生代币分别计价。','Uses trades from the selected pool; wrapped and native tokens have separate prices.') }} <a v-if="selected" :href="explorer(selected.poolId,'address',selected.chainId)" target="_blank" rel="noopener">{{ short(selected.poolId) }} ↗</a></p>
    <CandleChart v-if="market" :key="key(selected)" :asset="asset" :pool="selected.poolId" initial-mode="candle" />
    <p v-else class="stock-live-empty" role="status">{{ loading?tr('正在连接交易市场…','Connecting to the market…'):pending?tr('正在补充该池的成交记录，新成交会更新 K 线。','Collecting this pool; new trades will update its candles.'):tr('此池的实时成交暂不可用，已保留上方报价走势。','Live trades for this pool are unavailable; quote history is retained above.') }} <button v-if="!loading" type="button" @click="load">{{ tr('重试','Retry') }}</button></p>
  </div>
</template>
<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { tr } from '../i18n';
import { explorer, short } from '../utils/format';
import { getLiveMarkets } from '../utils/live-market-stream';
import CandleChart from './CandleChart.vue';
const props=defineProps({options:{type:Array,default:()=>[]}}),selectedKey=ref(''),market=ref(null),loading=ref(false),pending=ref(false);
let request=0,timer;
const key=row=>`${row?.chainId}:${row?.token}:${row?.poolId}`;
const selected=computed(()=>props.options.find(row=>key(row)===selectedKey.value)??props.options[0]);
const asset=computed(()=>({chainId:selected.value.chainId,token:selected.value.token,symbol:selected.value.symbol,venue:'dex',priceScope:'pool',priceCurrency:market.value?.priceCurrency}));
const chainName=id=>({'196':'X Layer','56':'BNB Chain','4663':'Robinhood Chain'}[id]??id);
async function load(){if(!selected.value||loading.value)return;const current=++request,choice=selected.value;loading.value=true;try{const response=await getLiveMarkets(choice.chainId,choice.token,{pool:choice.poolId,bar:'5m'});if(current!==request||key(choice)!==key(selected.value))return;market.value=response.markets?.find(row=>row.liveMarket===true&&row.venue==='dex'&&String(row.chainId)===String(choice.chainId)&&row.priceCurrency&&String(row.poolId).toLowerCase()===choice.poolId.toLowerCase()&&String(row.token).toLowerCase()===choice.token.toLowerCase())??null;pending.value=response.selectionStatus==='pending';}catch{if(current===request)pending.value=false;}finally{if(current===request)loading.value=false;}}
watch(()=>key(selected.value),()=>{request++;loading.value=false;market.value=null;pending.value=false;selectedKey.value=selected.value?key(selected.value):'';load();},{immediate:true});
onMounted(()=>timer=setInterval(()=>{if(!document.hidden)load();},15000));
onUnmounted(()=>{request++;clearInterval(timer);});
</script>
<style scoped>
.stock-live-chart{border-top:1px solid var(--border);margin-top:18px;padding-top:18px}.stock-live-head{display:flex;align-items:center;flex-wrap:wrap;gap:12px;font-size:13px}.stock-live-head select{font:inherit;max-width:100%;padding:5px 8px;color:var(--text);background:var(--panel);border:1px solid var(--border);border-radius:5px}.stock-live-note{font-size:11px;color:var(--muted);line-height:1.6;margin:8px 0}.stock-live-note a{color:var(--accent);margin-left:6px}.stock-live-empty{font-size:12px;color:var(--muted);padding:14px 0}.stock-live-empty button{border:0;background:none;color:var(--accent);cursor:pointer;font:inherit}
</style>
