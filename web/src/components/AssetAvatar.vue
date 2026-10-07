<template>
  <span class="asset-avatar" :style="style" aria-hidden="true">
    <img v-if="imageUrl && imageUrl!==failedUrl" :key="imageUrl+':'+imageVersion" :src="imageUrl" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" @error="imageFailed" />
    <span v-else class="asset-avatar-initials">{{ initials }}</span>
  </span>
</template>
<script setup>
import { computed, ref, watch } from 'vue';
import { assetLogoIdentity, loadAssetLogo } from '../api/asset-logos.js';
import { stockThemeLogo, localTokenLogo } from '../utils/asset-logo-catalogue.js';
import { avatarUrl, avatarLabel, avatarColors } from '../utils/asset-avatar.js';
const props = defineProps({
  asset: { type: Object, default: null },
  name: { type: String, default: '' },
  symbol: { type: String, default: '' },
  identity: { type: String, default: '' },
  size: { type: Number, default: 32 },
});
const failedUrl = ref('');
const metadataUrl = ref('');
const imageVersion = ref(0);
const directUrl = computed(() => stockThemeLogo(props.identity) || localTokenLogo(props.asset) || avatarUrl(props.asset));
const imageUrl = computed(() => metadataUrl.value || directUrl.value);
let recover = () => {};
watch([() => assetLogoIdentity(props.asset), directUrl], ([key, direct], _previous, onCleanup) => {
  let active = true, timer = null, attempts = 0;
  metadataUrl.value = '';
  failedUrl.value = '';
  onCleanup(() => { active = false; clearTimeout(timer); recover = () => {}; });
  function scheduleRecovery() {
    if (!active || timer !== null || attempts >= 3) return;
    const delay = [2500, 15000, 45000][attempts++];
    timer = setTimeout(async () => {
      timer = null;
      if (!active) return;
      // Local catalogue images do not require the metadata service. Retry
      // failed external metadata in a shared, short batching window instead.
      if (key && !direct.startsWith('/')) {
        const url = await loadAssetLogo(key, { refresh: true });
        if (!active) return;
        if (url) metadataUrl.value = url;
      }
      failedUrl.value = '';
      imageVersion.value++;
      if (!imageUrl.value) scheduleRecovery();
    }, delay);
  }
  recover = scheduleRecovery;
  if (!direct && key) {
    loadAssetLogo(key).then(url => {
      if (!active) return;
      metadataUrl.value = url;
      if (!url) scheduleRecovery();
    });
  }
}, { immediate: true });
const initials = computed(() => avatarLabel(props.asset, props));
const style = computed(() => ({
  ...avatarColors(props.asset, props.identity),
  '--avatar-size': `${Math.max(20, Math.min(64, Number(props.size) || 32))}px`,
}));
function imageFailed(event) {
  const source = event.currentTarget?.getAttribute('src');
  if (source === imageUrl.value) { failedUrl.value = source; recover(); }
}
</script>
<style scoped>
.asset-avatar{display:inline-flex;align-items:center;justify-content:center;flex:none;width:var(--avatar-size);height:var(--avatar-size);border-radius:50%;overflow:hidden;vertical-align:middle;box-shadow:inset 0 0 0 1px rgba(20,40,60,.05);user-select:none}
.asset-avatar img{display:block;width:100%;height:100%;object-fit:contain;background:var(--panel,#fff)}
.asset-avatar-initials{font:650 calc(var(--avatar-size)*.36)/1 var(--font,sans-serif);letter-spacing:-.02em}
</style>
