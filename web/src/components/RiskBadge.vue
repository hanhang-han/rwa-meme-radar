<template>
  <details class="risk-badge" :class="flags.length ? 'is-flagged' : 'is-unchecked'">
    <summary :aria-label="tr('风险检测', 'Risk checks')">{{ flags.length ? '!' : checked ? tr('未触发','No flag') : tr('未检测','Not checked') }}</summary>
    <div v-if="flags.length"><p v-for="flag in flags" :key="flag">{{ label(flag) }}</p></div>
    <div v-else>{{ checked ? tr('已检测，未触发当前规则；不代表无风险。','Checked: no current rules triggered; this is not a safety guarantee.') : tr('未检测或数据不足。','Not checked or insufficient data.') }}</div>
  </details>
</template>
<script setup>
import { computed } from 'vue';
import { tr } from '../i18n';
import { RISK_LABELS, riskFlags } from '../utils/product-labels';
const props=defineProps({ asset:{type:Object,required:true} });
const flags=computed(()=>riskFlags(props.asset));
const checked=computed(()=>props.asset.riskStatus==='clear');
const label=(flag)=>{const base=tr(...RISK_LABELS[flag]);const evidence=props.asset.riskAssessment?.checks?.[flag]?.evidence;if(flag==='wash_suspect'&&Number.isFinite(Number(evidence?.volumeLiquidityRatio)))return `${base} · ${Math.round(evidence.volumeLiquidityRatio)}x`;return base;};
</script>
