<template>
  <details class="risk-badge" :class="flags.length ? 'is-flagged' : 'is-unchecked'">
    <summary :aria-label="tr('风险检测', 'Risk checks')">{{ flags.length ? '!' : checked ? tr('未触发','No flag') : tr('未能识别','Unknown') }}</summary>
    <div v-if="flags.length"><p v-for="flag in flags" :key="flag">{{ label(flag) }}</p></div>
    <div v-else>{{ checked ? tr('已检测项目未触发当前规则。','Tested fields did not trigger current rules.') : tr('暂无完整检测结果。','A complete check result is unavailable.') }}</div>
  </details>
</template>
<script setup>
import { computed } from 'vue';
import { tr } from '../i18n';
import { RISK_LABELS, riskFlags } from '../utils/product-labels';
import { riskFlagSuffix } from '../utils/risk-presentation';
const props=defineProps({ asset:{type:Object,required:true} });
const flags=computed(()=>riskFlags(props.asset));
const checked=computed(()=>props.asset.riskStatus==='clear' && props.asset.riskAssessment?.status==='clear');
const label=(flag)=>{const base=tr(...RISK_LABELS[flag]);const evidence=props.asset.riskAssessment?.checks?.[flag]?.evidence;const suffix=riskFlagSuffix(flag,evidence);return suffix?`${base} · ${suffix}`:base;};
</script>
