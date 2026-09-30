<template>
  <details class="relation-badge" :class="'is-' + (level || 'unknown')">
    <summary :aria-label="label + ' · ' + tr('查看关系依据', 'View relationship evidence')">{{ label }}</summary>
    <div>{{ explanation }}</div>
  </details>
</template>
<script setup>
import { computed } from 'vue';
import { tr } from '../i18n';
import { relationLevel } from '../utils/product-labels';
const props=defineProps({ relation:{type:Object,required:true} });
const level=computed(()=>relationLevel(props.relation));
const label=computed(()=>({ A:tr('池子配对','Pool pairing'), B:tr('名称匹配','Name match'), C:tr('社区确认','Community') })[level.value] ?? tr('暂无关联','No relationship'));
const explanation=computed(()=>({ A:tr('该资产与股票代币存在链上配对池，流动性达到收录标准。','An onchain pool pairs this asset with a stock token and meets the liquidity threshold.'), B:tr('名称与股票相关，目前没有符合标准的配对池。','The name matches a stock; no qualifying pair pool is recorded.'), C:tr('社区投票达到门槛，仅代表社区意见。','Community votes meet the threshold; this is community opinion.') })[level.value] ?? tr('暂无关系记录。','No relationship record yet.'));
</script>
