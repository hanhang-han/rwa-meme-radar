<template>
  <p v-if="reg && reg.configured && hit" class="v2-registry">
    ⛓ {{ tr('链上登记记录', 'On-chain registry record') }} ·
    <a :href="reg.explorer" target="_blank" rel="noopener">{{ shortAddr(reg.contract) }} ↗</a>
  </p>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { getRegistry } from '../api/client';
import { tr } from '../i18n';

const props = defineProps({
  relation: { type: Object, required: true },
});

const reg = ref(null);
const hit = computed(() =>
  reg.value?.configured && String(props.relation.chainId ?? '196') === String(reg.value.chainId ?? '196') && props.relation.level === 'A' && props.relation.evidenceStatus === 'qualified'
    ? (reg.value.pairs ?? []).find(
        (p) => p.meme === String(props.relation.token ?? '').toLowerCase() && p.stock === String(props.relation.stock ?? '').toLowerCase(),
      )
    : null,
);

function shortAddr(a) {
  return a ? a.slice(0, 10) + '…' + a.slice(-6) : '';
}

let loading=false;
async function load() {
  if(loading)return;loading=true;
  try {
    reg.value = await getRegistry();
  } catch {
    /* Registry is optional; retain the prior observation. */
  }finally{loading=false;}
}
function onResourceChange(event){if(['registry','all'].includes(event.detail?.kind))load();}
watch(()=>[props.relation.chainId,props.relation.id],load);
onMounted(()=>{load();window.addEventListener('resource-change',onResourceChange);});
onUnmounted(()=>window.removeEventListener('resource-change',onResourceChange));
</script>
