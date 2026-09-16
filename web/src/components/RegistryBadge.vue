<template>
  <p v-if="reg && reg.configured && hit" class="v2-registry">
    ⛓ {{ tr('链上登记', 'On-chain registry') }} ✓ ·
    <a :href="reg.explorer" target="_blank" rel="noopener">{{ shortAddr(reg.contract) }} ↗</a>
  </p>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { getRegistry } from '../api/client';
import { tr } from '../i18n';

const props = defineProps({
  relation: { type: Object, required: true },
});

const reg = ref(null);
const hit = computed(() =>
  reg.value?.configured
    ? (reg.value.pairs ?? []).find(
        (p) => p.meme === String(props.relation.token ?? '').toLowerCase() && p.stock === String(props.relation.stock ?? '').toLowerCase(),
      )
    : null,
);

function shortAddr(a) {
  return a ? a.slice(0, 10) + '…' + a.slice(-6) : '';
}

onMounted(async () => {
  try {
    reg.value = await getRegistry();
  } catch {
    /* registry optional */
  }
});
</script>
