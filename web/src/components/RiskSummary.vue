<template>
  <section class="risk-summary">
    <div class="risk-checks">
      <details v-for="item in rows" :key="item.key" :class="'risk-'+item.status">
        <summary :title="sourceTitle(item)"><span>{{ item.label }}</span><strong>{{ riskStateText(item.status,language) }}</strong></summary>
        <p>{{ riskReasonText(item.reason,language) }}</p>
        <dl v-if="evidenceRows(item).length"><div v-for="row in evidenceRows(item)" :key="row.key"><dt>{{ row.label }}</dt><dd :title="row.title">{{ row.value }}</dd></div></dl>
        <small>{{ sourceTitle(item) }}</small>
      </details>
    </div>
    <div v-if="flags.length" class="risk-trigger-list">
      <details v-for="flag in flags" :key="flag"><summary :title="sourceTitle(checks[flag])">{{ riskLabel(flag) }}</summary><dl><div v-for="row in evidenceRows(checks[flag])" :key="row.key"><dt>{{ row.label }}</dt><dd :title="row.title">{{ row.value }}</dd></div></dl><small>{{ sourceTitle(checks[flag]) }}</small></details>
    </div>
  </section>
</template>
<script setup>
import {computed} from 'vue';
import {tr,useI18n} from '../i18n';
import {date} from '../utils/format';
import {RISK_LABELS,riskFlags} from '../utils/product-labels';
import {riskEvidenceRows,riskProvenance,riskReasonText,riskStateText} from '../utils/risk-presentation';
const props=defineProps({asset:{type:Object,required:true}}),{lang}=useI18n();
const language=computed(()=>lang.lang);
const labels={tax:['买卖税 / 交易限制','Taxes / trading limits'],permissions:['合约权限','Contract permissions'],concentration:['持仓集中度','Holder concentration'],liquidityLock:['流动性锁定','Liquidity lock']};
const rows=computed(()=>Object.entries(labels).map(([key,text])=>({key,label:tr(...text),status:'unknown',...props.asset.riskAssessment?.safety?.[key]})));
const checks=computed(()=>props.asset.riskAssessment?.checks??{}),flags=computed(()=>riskFlags(props.asset));
const riskLabel=flag=>RISK_LABELS[flag]?tr(...RISK_LABELS[flag]):tr('风险指标变化','Risk indicator changed');
const evidenceRows=item=>riskEvidenceRows(item?.evidence,language.value);
function sourceTitle(item){const source=riskProvenance(item);return `${source.provider??tr('暂无检测来源','No check source')} · ${date(source.at)}`;}
</script>
<style scoped>
.risk-checks{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.risk-checks details{border:1px solid var(--border);border-radius:12px;padding:12px;background:var(--surface-raised);min-width:0}summary{cursor:pointer;list-style:none;display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between;font-size:13px}summary strong{font-size:12px;color:var(--muted)}.risk-triggered summary strong{color:var(--warning)}.risk-clear summary strong{color:var(--up)}p,small,dl{font-size:12px;color:var(--muted)}dl>div{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;padding:5px 0}dd{margin:0;overflow-wrap:anywhere;text-align:right;font-variant-numeric:tabular-nums}.risk-trigger-list{display:flex;flex-wrap:wrap;gap:12px;margin-top:12px}.risk-trigger-list details{min-width:200px;max-width:100%}.risk-trigger-list summary{color:var(--warning)}@media(max-width:700px){.risk-checks{grid-template-columns:repeat(2,minmax(0,1fr))}.risk-checks details[open]{grid-column:1/-1}}
</style>
