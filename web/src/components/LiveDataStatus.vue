<template>
  <div class="live-data-status" role="status">
    <span class="live-transport" :class="{'is-connected':stream.connected}"><i aria-hidden="true"></i>{{ transportLabel }}</span>
    <span :title="quoteAt?date(quoteAt):''">{{ quoteLabel }}</span>
    <span v-if="snapshotAt" class="live-list-time" :title="date(snapshotAt)">{{ tr('列表更新','List updated') }} {{ relative(snapshotAt) }}</span>
    <button class="live-refresh" type="button" @click="emit('refresh')">{{ tr('刷新','Refresh') }}</button>
  </div>
</template>
<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { tr } from '../i18n';
import { date } from '../utils/format';
const props = defineProps({ stream:{type:Object,default:()=>({})}, quoteAt:{type:Number,default:0},
  snapshotAt:{type:Number,default:0} });
const emit = defineEmits(['refresh']);
const clock = ref(Date.now()); let timer;
onMounted(() => { timer = setInterval(() => { if (!document.hidden) clock.value = Date.now(); },1000); });
onUnmounted(() => clearInterval(timer));
function relative(at) {
  const elapsed = clock.value-Number(at);
  if (!at || elapsed < -1000) return tr('时间未知','Time unknown');
  const seconds = Math.max(0,Math.floor(elapsed/1000));
  if (seconds<60) return tr(`${seconds}秒前`,`${seconds}s ago`);
  if (seconds<3600) return tr(`${Math.floor(seconds/60)}分钟前`,`${Math.floor(seconds/60)}m ago`);
  return tr(`${Math.floor(seconds/3600)}小时前`,`${Math.floor(seconds/3600)}h ago`);
}
const quoteLabel = computed(() => props.quoteAt
  ? `${tr('最近一条报价','Latest row quote')} ${relative(props.quoteAt)}`
  : tr('报价时间未知','Quote time unknown'));
const transportLabel = computed(() => props.stream.connected ? tr('行情推送已连接','Quote stream connected')
  : props.stream.state==='idle' ? tr('暂无可订阅行情','No quote subscription available')
  : props.stream.state==='paused' ? tr('后台暂停','Paused in background')
  : props.stream.state==='connecting' ? tr('行情连接中','Connecting quotes')
  : tr('定时更新 · 行情重连中','Scheduled updates · Reconnecting quotes'));
</script>
<style scoped>
.live-data-status{display:flex;align-items:center;flex-wrap:wrap;gap:6px 14px;padding:10px 0;font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}.live-transport{display:inline-flex;align-items:center;gap:5px}.live-transport i{width:5px;height:5px;border-radius:50%;background:var(--muted)}.live-transport.is-connected i{background:var(--up)}.live-data-status button{padding:3px 0;border:0;background:none;font:inherit;color:var(--accent);cursor:pointer}.live-refresh{margin-left:auto}.live-data-status button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}@media(max-width:700px){.live-data-status{font-size:10px;gap:6px 10px}.live-list-time{display:none}}
</style>
