<template>
  <section class="holder-composition" :class="{'without-share':!hasShare}" aria-labelledby="holder-chart-title">
    <div class="holder-heading" :title="distribution.exclusionsApplied ? tr('排除已识别特殊地址后，前十大地址在来源覆盖样本中的持仓占比。','Top 10 holding share in the source sample after known special addresses are excluded.') : tr('来源原始样本内前十大地址持仓占比，可能包含池和合约地址。','Top 10 holding share in the raw source sample, which may include pools and contracts.')"><h3 id="holder-chart-title">{{ distribution.exclusionsApplied ? tr('调整后前十大持仓', 'Adjusted top 10 share') : tr('原始前十大持仓', 'Raw top 10 share') }}</h3><strong v-if="hasShare">{{ share.toFixed(1) }}%</strong></div>
    <template v-if="hasShare">
      <div class="holder-bar" role="img" :aria-label="tr(`前十大 ${share.toFixed(1)}%，其余 ${(100-share).toFixed(1)}%`, `Top 10 ${share.toFixed(1)}%, rest ${(100-share).toFixed(1)}%`)"><span class="holder-top" :style="{width:`${share}%`}"></span><span class="holder-rest" :style="{width:`${100-share}%`}"></span></div>
      <div class="holder-legend"><span><i class="top"></i>{{ tr('前十大', 'Top 10') }} {{ share.toFixed(1) }}%</span><span><i class="rest"></i>{{ tr('其余', 'Rest') }} {{ (100-share).toFixed(1) }}%</span></div>
      <p class="holder-source">{{ isHistorical ? tr('历史观测', 'Historical observation') : tr('观测时间', 'Observed') }} · {{ date(distribution.checkedAt) }} · {{ distribution.provider || '—' }}</p>
    </template>
    <p v-else class="holder-empty" role="status">{{ tr('前十大持仓未能识别', 'Top 10 share is unavailable') }}</p>
    <div class="holder-count" :title="tr('来源记录的持币地址数量；一个人可拥有多个地址，缺值不代表零。','Holder address count reported by the source; one person may own several addresses, and missing data does not mean zero.')"><span>{{ tr('持币地址数', 'Holder address count') }}</span><strong>{{ num(count) }}</strong><small>{{ tr('单独观测', 'Observed separately') }} · {{ date(countAt) }} · {{ countSource || '—' }}</small></div>
    <details class="holder-note"><summary>{{ tr('持仓口径', 'Holdings scope') }}</summary><p>{{ distribution.exclusionsApplied ? tr('已排除已识别的交易池、销毁及交易所地址；仍以来源实际覆盖的地址样本为准。', 'Known pools, burn and exchange addresses are excluded; coverage follows the source holder sample.') : tr('原始持仓可能包含交易池和合约地址，不能用作调整后集中度。', 'Raw holdings may include pools and contracts and do not establish adjusted concentration.') }}</p><p v-if="distribution.excludedAddresses?.length">{{ tr('排除地址','Excluded addresses') }}: {{ distribution.excludedAddresses.join(' · ') }}</p></details>
  </section>
</template>
<script setup>
import {computed} from 'vue';
import {tr} from '../i18n';
import {date,num} from '../utils/format';
const props=defineProps({distribution:{type:Object,default:()=>({})},count:{type:[Number,String],default:null},countAt:{type:[Number,String],default:null},countSource:{type:String,default:''}});
const distribution=computed(()=>props.distribution??{});
const share=computed(()=>Number(distribution.value.top10Percent));
const observedAt=computed(()=>new Date(distribution.value.checkedAt).getTime());
const hasShare=computed(()=>distribution.value.top10Percent!=null && Number.isFinite(share.value) && share.value>=0 && share.value<=100 && Number.isFinite(observedAt.value) && observedAt.value>0);
const isHistorical=computed(()=>hasShare.value && Date.now()-observedAt.value>21_600_000);
</script>
<style scoped>
.holder-composition{min-width:0;padding:12px 0 0;background:transparent}
.holder-heading{display:flex;align-items:baseline;justify-content:space-between;gap:16px}
.holder-heading h3{margin:0;font-family:var(--display-font,Georgia,serif);font-size:18px;font-weight:600}
.holder-heading strong{font-family:var(--number-font,sans-serif);font-size:36px;font-weight:400;font-variant-numeric:tabular-nums}
.holder-bar{display:flex;overflow:hidden;height:20px;margin:21px 0 10px;background:var(--border)}
.holder-top{background:var(--accent)}
.holder-rest{background:var(--border)}
.holder-legend{display:flex;gap:22px;flex-wrap:wrap;font-size:12px}
.holder-legend span{display:flex;align-items:center;gap:7px}
.holder-legend i{display:inline-block;width:8px;height:8px;border-radius:50%}
.holder-legend .top{background:var(--accent)}
.holder-legend .rest{background:var(--border)}
.holder-source,.holder-note{margin:0;color:var(--muted);font-size:12px;line-height:1.6}
.holder-source{margin-top:14px}
.holder-count{display:flex;align-items:baseline;flex-wrap:wrap;gap:9px;margin-top:21px;padding-top:15px;border-top:1px solid var(--border)}
.holder-count span,.holder-count small{color:var(--muted);font-size:12px}
.holder-count strong{font-size:20px;font-variant-numeric:tabular-nums}
.holder-note{margin-top:14px}
.holder-note summary{cursor:pointer}
.holder-note p{margin:7px 0 0}
.holder-empty{margin:13px 0 0;padding:11px 0;border-top:1px solid var(--border);color:var(--muted);font-size:12px;line-height:1.5}
.without-share .holder-count{margin-top:12px;padding-top:12px}
</style>
