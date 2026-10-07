<template>
  <section class="panel asset-insight" aria-labelledby="asset-insight-title">
    <div class="panel-head">
      <div>
        <h2 id="asset-insight-title">{{ tr('要点', 'Key points') }}</h2>
        <small v-if="brief?.dataAsOf" :title="date(brief.dataAsOf)">{{ tr('基于', 'Based on') }} {{ briefTime }}</small>
      </div>
      <span v-if="brief?.stale || expired" class="insight-stale">{{ tr(`延迟 ${Math.max(1,Math.floor((clock-Number(brief?.dataAsOf??clock))/60000))} 分钟`, `Delayed ${Math.max(1,Math.floor((clock-Number(brief?.dataAsOf??clock))/60000))} min`) }}</span>
    </div>
    <div v-if="loading && !brief" class="insight-skeleton loading-skeleton" role="status" :aria-label="tr('加载要点', 'Loading asset brief')">
      <div v-for="n in 4" :key="n" class="skeleton-line"></div>
      <p v-if="slow" class="insight-slow">{{ tr('加载较慢。', 'Loading slowly.') }} <button type="button" @click="load">{{ tr('重试', 'Retry') }}</button></p>
    </div>
    <div v-else-if="lines.length" class="insight-content">
      <p v-for="(line,index) in primaryLines" :key="index" class="insight-primary"><strong v-if="line.label">{{ line.label }}</strong><span>{{ line.text }}</span></p>
      <details v-if="extraLines.length" class="insight-evidence"><summary>{{ tr('数据来源与缺口', 'Sources and gaps') }}</summary><p v-for="(line,index) in extraLines" :key="index">{{ line }}</p></details>
    </div>
    <div v-else class="insight-empty">
      <span>{{ brief?.status === 'no_verified_pool'
        ? tr('当前池数据不足，暂无要点。', 'Current pool data is insufficient for key points.')
        : error
          ? tr('要点暂时无法读取。', 'Key points could not be loaded.')
          : tr('暂无要点。', 'No key points yet.') }}</span>
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
  return rows;
});
const primaryLines = computed(() => lines.value.slice(0,3).map(line => {
  const parts = line.match(/^([^：:]{1,12})[：:]\s*(.+)$/);
  return parts ? { label:parts[1], text:parts[2] } : { label:'', text:line };
}));
const extraLines = computed(() => lines.value.slice(3));
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
.asset-insight .panel-head{align-items:baseline;gap:14px}
.asset-insight .panel-head small{display:block;margin-top:4px;color:var(--muted);font-size:12px}
.insight-stale{color:var(--down);font-size:12px}
.insight-content{border-left:2px solid var(--accent);padding:2px 0 2px 18px}
.insight-primary{display:grid;grid-template-columns:46px minmax(0,1fr);gap:12px;min-width:0;margin:0 0 11px;font-size:13px;line-height:1.75;overflow-wrap:anywhere}
.insight-primary:last-of-type{margin-bottom:0}
.insight-primary strong{color:var(--accent);font-size:12px;font-weight:650;letter-spacing:.05em}
.insight-primary span{min-width:0}
.insight-evidence{margin-top:16px;color:var(--muted);font-size:12px;line-height:1.65}
.insight-evidence summary{cursor:pointer;color:var(--muted)}
.insight-evidence p{margin:8px 0;overflow-wrap:anywhere}
.insight-skeleton{min-height:0;grid-template-columns:repeat(2,minmax(0,1fr))}
.insight-empty{display:flex;align-items:center;justify-content:space-between;gap:12px;border-left:2px solid var(--border);padding-left:18px;color:var(--muted);font-size:13px}
.insight-empty button{padding:6px 11px;border:1px solid var(--border);border-radius:999px;background:transparent;color:var(--text);cursor:pointer}
.insight-slow{grid-column:1/-1;color:var(--muted);font-size:12px}
.insight-slow button{border:0;background:none;color:var(--accent);cursor:pointer}
@media(max-width:700px){.insight-content{padding-left:14px}.insight-primary{grid-template-columns:40px minmax(0,1fr);gap:8px}.insight-skeleton{grid-template-columns:1fr}}
</style>
