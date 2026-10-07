<template>
  <details class="natural-filter" @toggle="expanded=$event.target.open">
    <summary>{{ tr('用一句话筛选','Filter in one sentence') }}</summary>
    <form @submit.prevent="parse">
      <label :for="inputId">{{ tr('筛选条件','Filter conditions') }}</label>
      <div class="sentence-row"><input :id="inputId" v-model="text" maxlength="600" :placeholder="tr('如：腾讯相关，BNB 链，流动性大于 1 万美元','e.g. Tencent theme, BNB chain, liquidity > 10k')"><button :disabled="busy || !text.trim()">{{ busy?tr('正在处理…','Processing…'):tr('识别条件','Read conditions') }}</button></div>
    </form>
    <template v-if="parsed">
      <p class="filter-note">{{ parsed.source==='model'?tr('已识别条件，请核对后执行。','Conditions parsed. Review before applying.'):tr('按规则识别，请核对条件。','Read with rules. Review the conditions.') }}</p>
      <div class="condition-list">
        <div v-for="(condition,index) in conditions" :key="index" class="condition-item"><label>{{ fieldLabel(condition.field) }}<select v-model="condition.op" :disabled="['stock','chainId'].includes(condition.field)"><option v-for="op in ['=','>','>=','<','<=']" :key="op">{{ op }}</option></select><input v-model="condition.value" :aria-label="fieldLabel(condition.field)" :type="['stock','chainId'].includes(condition.field)?'text':'number'" step="any"></label><button type="button" :aria-label="tr('删除条件','Remove condition')" @click="conditions.splice(index,1);result=null">×</button></div>
      </div>
      <p v-if="parsed.ignored?.length" class="filter-note" role="status">{{ tr('没有理解这些条件，请使用条件选择器：','Could not read these conditions. Use the selectors:') }} {{ parsed.ignored.join('；') }}</p>
      <p v-if="conditions.some(c=>c.field==='exitImpact1k')" class="filter-note">{{ tr('冲击使用小数：0.05 表示 5%；仅计算支持且数据完整的池。','Impact uses decimals: 0.05 means 5%. Only supported pools with complete data are included.') }}</p>
      <button v-if="conditions.length" type="button" class="apply-filter" :disabled="busy" @click="execute">{{ tr('确认并筛选','Confirm and filter') }}</button>
      <p v-else class="filter-note">{{ tr('没有可执行的条件。','No executable conditions.') }}</p>
    </template>
    <p v-if="error" role="alert" class="filter-error">{{ error }}</p>
    <div v-if="result" class="sentence-results"><p class="filter-note">{{ tr('符合条件','Matching') }} {{ result.total }} · {{ tr('缺值与延迟字段不参与筛选','Missing and delayed fields are excluded') }}</p><RouterLink v-for="item in result.items" :key="item.chainId+item.token" :to="`/asset/${item.chainId}/${item.token}`"><strong>{{ item.symbol || item.name || short(item.token) }}</strong><span>{{ item.name }}</span><span>{{ tr('流动性','Liquidity') }} {{ usd(item.liquidityUsd) }}</span><span>{{ tr('成交 24h','Volume 24h') }} {{ usd(item.volume24hUsd) }}</span><span>→</span></RouterLink><p v-if="!result.items.length">{{ tr('当前没有满足全部条件的资产。','No assets currently meet every condition.') }}</p><small v-if="result.total>result.items.length">{{ tr('显示前','Showing first') }} {{ result.items.length }} / {{ result.total }}</small></div>
  </details>
</template>
<script setup>
import { ref, useId, watch } from 'vue';
import { tr } from '../i18n';
import { productRequest } from '../api/developer';
import { short, usd } from '../utils/format';
const emit=defineEmits(['results']);
const inputId=useId();
const text=ref(''),conditions=ref([]),parsed=ref(null),result=ref(null),busy=ref(false),error=ref(''),expanded=ref(false);
const labels={stock:['股票主题','Stock theme'],chainId:['链 ID','Chain ID'],liquidityUsd:['流动性 USD','Liquidity USD'],volume24hUsd:['成交 24h USD','Volume 24h USD'],change24h:['涨跌 24h %','Change 24h %'],riskCount:['风险项数','Risk item count'],poolAgeHours:['池龄 小时','Pool age hours'],exitImpact1k:['卖出 $1K 冲击','Sell $1K impact']};
watch(conditions,()=>{result.value=null;},{deep:true});
watch(text,()=>{parsed.value=null;conditions.value=[];result.value=null;});
const fieldLabel=field=>tr(...(labels[field]||[field,field]));
function failure(e){return e.status===429?tr('操作较频繁，请稍后再试。','Please wait before trying again.'):e.message==='invalid-filter-schema'?tr('请检查条件的字段、符号和数值。','Check each field, operator and value.'):tr('暂时无法读取，请重试。','Could not load. Please retry.');}
async function parse(){busy.value=true;error.value='';result.value=null;const sentence=text.value;try{const value=await productRequest('filters/parse',{method:'POST',body:{text:sentence}});if(text.value!==sentence)return;parsed.value=value;conditions.value=value.filters.map(c=>({...c}));}catch(e){error.value=failure(e);}finally{busy.value=false;}}
async function execute(){busy.value=true;error.value='';try{if(conditions.value.some(c=>String(c.value??'').trim()===''))throw new Error('invalid-filter-schema');const filters=conditions.value.map(c=>({...c,value:['stock','chainId'].includes(c.field)?String(c.value):Number(c.value)}));result.value=await productRequest('filters/execute',{method:'POST',body:{filters,confirmed:true,limit:50}});emit('results',result.value);}catch(e){error.value=failure(e);}finally{busy.value=false;}}
</script>
<style scoped>
.natural-filter{padding:14px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border);font-size:13px}.natural-filter summary{width:max-content;cursor:pointer;text-decoration:underline;text-underline-offset:4px}.natural-filter form{margin-top:16px}.natural-filter label{display:grid;gap:8px}.sentence-row{display:flex;gap:8px;margin-top:8px}.sentence-row input{flex:1;min-width:0}.natural-filter input,.natural-filter select{padding:9px 10px;border:1px solid var(--border);background:var(--bg);color:var(--text);font:inherit}.natural-filter button{padding:9px 14px;border:1px solid var(--border);background:transparent;color:var(--text);font:inherit;cursor:pointer}.natural-filter button:disabled{opacity:.6;cursor:wait}.filter-note{color:var(--muted);font-size:12px;line-height:1.7}.condition-list{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.condition-item{display:flex;border:1px solid var(--border)}.condition-item label{display:flex;align-items:center;gap:8px;padding-left:10px}.condition-item input{width:110px;border:0;background:transparent}.condition-item select{padding:8px;border:0;background:transparent}.condition-item button{padding:8px;border:0;border-left:1px solid var(--border)}.apply-filter{background:var(--text)!important;color:var(--bg)!important}.filter-error{color:var(--loss)}.sentence-results{margin-top:18px}.sentence-results>a{display:flex;align-items:center;flex-wrap:wrap;gap:10px 20px;padding:13px 0;border-top:1px solid var(--border);color:var(--text)}.sentence-results>a>span:last-child{margin-left:auto}.sentence-results small{font-size:12px;color:var(--muted)}@media(max-width:600px){.sentence-row{flex-direction:column}.condition-list{display:grid}.condition-item label{min-width:0;flex:1}.condition-item input{flex:1;min-width:0}.condition-item select{flex-shrink:0}.sentence-results>a{gap:8px 14px}}
</style>
