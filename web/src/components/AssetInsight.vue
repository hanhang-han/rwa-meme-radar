<template>
  <section class="panel asset-insight" aria-labelledby="asset-insight-title">
    <div class="panel-head">
      <div>
        <h2 id="asset-insight-title">{{ tr('资产简报', 'Asset brief') }}</h2>
        <small v-if="brief?.dataAsOf" :title="date(brief.dataAsOf)">{{ tr('基于', 'Based on') }} {{ briefTime }} {{ tr('的数据', 'data') }}</small>
      </div>
      <span v-if="brief?.stale || expired" class="insight-stale">{{ tr('已过期', 'Stale') }}</span>
    </div>
    <div v-if="loading && !brief" class="insight-skeleton loading-skeleton" role="status" :aria-label="tr('加载资产简报', 'Loading asset brief')">
      <div v-for="n in 4" :key="n" class="skeleton-line"></div>
      <p v-if="slow" class="insight-slow">{{ tr('加载较慢。', 'Loading slowly.') }} <button type="button" @click="load">{{ tr('重试', 'Retry') }}</button></p>
    </div>
    <ol v-else-if="lines.length === 4" class="insight-lines">
      <li v-for="(line,index) in lines" :key="index">{{ line }}</li>
    </ol>
    <div v-else class="insight-empty">
      <span>{{ brief?.status === 'no_verified_pool'
        ? tr('暂无可核实的池子配对简报。', 'No brief for a recorded pool pair yet.')
        : error
          ? tr('简报暂时无法读取。', 'The brief could not be loaded.')
          : tr('暂无资产简报。', 'No asset brief yet.') }}</span>
      <button v-if="error" type="button" @click="load">{{ tr('重试', 'Retry') }}</button>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { getInsight } from '../api/client';
import { tr, useI18n } from '../i18n';
import { date } from '../utils/format';

const props = defineProps({ chain: { type:String, required:true }, address: { type:String, required:true } });
const { lang } = useI18n();
const brief = ref(null);
const loading = ref(false);
const error = ref(false);
const slow = ref(false);
const clock = ref(Date.now());
let requestId = 0;
let timer;
let slowTimer;

const lines = computed(() => {
  const source = Array.isArray(brief.value?.lines) ? brief.value.lines : String(brief.value?.text ?? '').split('\n');
  const rows = source.map(row => String(row).trim()).filter(Boolean);
  return rows.length === 4 ? rows : [];
});
const expired = computed(() => {
  const at = Number(brief.value?.dataAsOf);
  return !!at && clock.value - at > 180_000;
});
const briefTime = computed(() => new Date(Number(brief.value?.dataAsOf)).toLocaleTimeString(lang.lang === 'en' ? 'en-US' : 'zh-CN', { hour:'2-digit', minute:'2-digit', hour12:false }));

async function load() {
  const id = ++requestId;
  loading.value = !brief.value;
  error.value = false;
  slow.value = false;
  clearTimeout(slowTimer);
  slowTimer = setTimeout(() => { if (id === requestId) slow.value = true; }, 8000);
  try {
    const response = await getInsight(props.chain, props.address, lang.lang);
    if (id !== requestId) return;
    brief.value = response;
  } catch {
    if (id === requestId) error.value = true;
  } finally {
    if (id === requestId) { loading.value = false; clearTimeout(slowTimer); }
  }
}
function onResourceChange(event) {
  const { kind, chainId, token } = event.detail ?? {};
  if (kind !== 'insight' && kind !== 'all') return;
  if (chainId && String(chainId) !== props.chain) return;
  if (token && String(token).toLowerCase() !== props.address.toLowerCase()) return;
  load();
}
watch(() => [props.chain, props.address, lang.lang], () => { brief.value = null; load(); }, { immediate:true });
onMounted(() => {
  window.addEventListener('resource-change', onResourceChange);
  timer = setInterval(() => { clock.value = Date.now(); if (!document.hidden) load(); }, 60_000);
});
onUnmounted(() => { requestId++; clearInterval(timer); clearTimeout(slowTimer); window.removeEventListener('resource-change', onResourceChange); });
</script>

<style scoped>
.asset-insight .panel-head { align-items:flex-start; }
.asset-insight .panel-head small { display:block;margin-top:4px;color:var(--muted);font-size:12px; }
.insight-stale { color:var(--down);font-size:12px; }
.insight-lines { display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 20px;margin:0;padding:0;list-style:none; }
.insight-lines li { min-width:0;padding:13px 0;border-top:1px solid var(--border);font-size:13px;line-height:1.55;overflow-wrap:anywhere; }
.insight-skeleton { min-height:0;grid-template-columns:repeat(2,minmax(0,1fr)); }
.insight-empty { display:flex;align-items:center;justify-content:space-between;gap:12px;color:var(--muted);font-size:13px; }
.insight-empty button { padding:6px 10px;border:1px solid var(--border);border-radius:8px;background:var(--surface-raised);color:var(--text);cursor:pointer; }
.insight-slow {grid-column:1/-1;color:var(--muted);font-size:12px;}
.insight-slow button {border:0;background:none;color:var(--accent);cursor:pointer;}
@media(max-width:700px) { .insight-lines,.insight-skeleton { grid-template-columns:1fr; } }
</style>
