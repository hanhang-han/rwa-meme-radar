<template>
  <p v-if="reg && reg.configured && hit" class="v2-registry">
    ⛓ {{ tr('链上登记记录', 'On-chain registry record') }} ·
    <a :href="reg.explorer" target="_blank" rel="noopener">{{ shortAddr(reg.contract) }} ↗</a>
  </p>
</template>

<script>
import { getRegistry } from '../api/client';
// Every badge reads the same registry. Share both the pending request and a
// short result cache; a resource invalidation still fetches a fresh version.
const registryCache = { value: null, loadedAt: 0, pending: null };
function readRegistry(force = false) {
  if (!force && registryCache.loadedAt && Date.now() - registryCache.loadedAt < 30000)
    return Promise.resolve(registryCache.value);
  if (registryCache.pending) return registryCache.pending;
  registryCache.pending = getRegistry().then(value => {
    registryCache.value = value;
    registryCache.loadedAt = Date.now();
    return value;
  }).finally(() => { registryCache.pending = null; });
  return registryCache.pending;
}
</script>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
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

async function load(force = false) {
  try {
    reg.value = await readRegistry(force);
  } catch {
    /* Registry is optional; retain the prior observation. */
  }
}
function onResourceChange(event){if(['registry','all'].includes(event.detail?.kind))load(true);}
watch(()=>[props.relation.chainId,props.relation.id],()=>load());
onMounted(()=>{load();window.addEventListener('resource-change',onResourceChange);});
onUnmounted(()=>window.removeEventListener('resource-change',onResourceChange));
</script>
