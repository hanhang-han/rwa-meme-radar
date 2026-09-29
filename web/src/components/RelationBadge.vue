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
const explanation=computed(()=>({ A:tr('官方股票代币与 Meme 有达到门槛的池子证据。','A pool with an approved stock token meets the liquidity floor.'), B:tr('名称命中股票关键词，尚无合格配对池。','The name matches a stock keyword; no qualifying pair pool.'), C:tr('社区投票达到门槛，仅代表社区意见。','Community votes meet the threshold; this is community opinion.') })[level.value] ?? tr('当前没有足够证据判定关系等级。','There is not enough evidence to assign a relation level.'));
</script>
