<template>
  <span class="relation-badge" :class="'is-' + (level || 'unknown')">
    <span>{{ label }}</span><details><summary :aria-label="tr('关系依据', 'Relationship evidence')">?</summary><div>{{ explanation }}</div></details>
  </span>
</template>
<script setup>
import { computed } from 'vue';
import { tr } from '../i18n';
import { relationLevel } from '../utils/product-labels';
const props=defineProps({ relation:{type:Object,required:true} });
const level=computed(()=>relationLevel(props.relation));
const label=computed(()=>({ A:tr('池配对','Paired'), B:tr('名称相关','Name match'), C:tr('社区确认','Community') })[level.value] ?? tr('待核验','Unverified'));
const explanation=computed(()=>({ A:tr('已核验与官方股票代币的配对池，流动性达到收录标准。','A verified pool pairs this asset with an approved stock token and meets the liquidity threshold.'), B:tr('名称与股票相关，尚无符合标准的配对池。','The name matches a stock; no qualifying pair pool is verified.'), C:tr('社区投票达到门槛，仅代表社区意见。','Community votes meet the threshold; this is community opinion.') })[level.value] ?? tr('配对关系尚未核验。','Pairing has not been verified.'));
</script>
