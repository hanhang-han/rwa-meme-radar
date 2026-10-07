<template>
  <details ref="disclosure" class="quote-status" :class="['quote-' + signal.state, 'signal-' + signal.tone, { 'quote-stale': signal.stale }]" @toggle="syncSignalDisclosure(disclosure)" @click.stop @keydown.esc.stop.prevent="close">
    <summary :aria-label="text(signal.label) + ' · ' + tr('展开报价依据', 'Expand quote evidence')" :title="stamp">
      <span class="signal-icon" aria-hidden="true">{{ signal.icon }}</span><span>{{ text(signal.label) }}</span>
      <span v-if="signal.at" class="quote-age"> · {{ age(signal.at) }}</span>
      <span v-if="signal.currency"> · {{ signal.currency }}</span>
      <span v-if="showSource && signal.source" class="quote-source"> · {{ signal.source }}</span>
    </summary>
    <div class="signal-panel" popover="manual">
      <div class="signal-panel-heading"><strong>{{ tr('报价依据', 'Quote evidence') }}</strong><button type="button" @click="close" :aria-label="tr('收起报价依据', 'Close quote evidence')">×</button></div>
      <p>{{ text(signal.explanation) }}</p>
      <dl>
        <div><dt>{{ tr('报价来源', 'Quote source') }}</dt><dd>{{ signal.source || tr('来源未提供', 'Source unavailable') }}</dd></div>
        <div><dt>{{ tr('报价单位', 'Quote unit') }}</dt><dd>{{ signal.currency || tr('单位未提供', 'Unit unavailable') }}</dd></div>
        <div><dt>{{ tr('报价观测', 'Quote observed') }}</dt><dd>{{ signal.at ? date(signal.at) : tr('时间未提供或待核实', 'Time unavailable or unverified') }}</dd></div>
        <div><dt>{{ tr('状态', 'Status') }}</dt><dd>{{ text(signal.label) }}</dd></div>
      </dl>
    </div>
  </details>
</template>
<script setup>
import { computed, ref } from 'vue';
import { useMinuteClock } from '../composables/useMinuteClock';
import { tr, useI18n } from '../i18n';
import { age, date } from '../utils/format';
import { quoteSignal, signalText, syncSignalDisclosure } from '../utils/signal-presentation';

const props = defineProps({ row: { type: [Object, null], required: true }, reference: Boolean, showSource: { type: Boolean, default: false } });
const { lang } = useI18n();
const now = useMinuteClock();
const disclosure = ref(null);
const signal = computed(() => quoteSignal(props.row, props.reference, now.value));
const text = pair => signalText(pair, lang.lang);
const stamp = computed(() => `${signal.value.source || tr('来源未提供', 'Source unavailable')} · ${signal.value.at ? date(signal.value.at) : tr('观测时间待核实', 'Observation time unverified')}`);
const close = () => { if (disclosure.value) disclosure.value.open = false; syncSignalDisclosure(disclosure.value); };
</script>
<style scoped>
.quote-status{position:relative;display:inline-block;max-width:100%;font-size:11px;line-height:1.4;color:var(--muted,#67717d);white-space:normal;vertical-align:middle}.quote-status>summary{display:inline-flex;align-items:center;flex-wrap:wrap;gap:3px;max-width:100%;padding:2px 0;list-style:none;cursor:pointer;white-space:normal}summary::-webkit-details-marker{display:none}.signal-icon{flex:none;font-size:12px}.signal-verified .signal-icon{color:#2563a7}.quote-status.signal-pending{color:#8a692f}.quote-source{min-width:0;overflow-wrap:anywhere}.quote-age{font-variant-numeric:tabular-nums}summary:focus-visible,button:focus-visible{outline:2px solid var(--accent,#2563a7);outline-offset:3px}
.quote-status>.signal-panel{position:absolute;top:calc(100% + 6px);left:0;z-index:50;width:min(300px,calc(100vw - 32px));box-sizing:border-box;max-height:360px;overflow:auto;padding:12px;border:1px solid var(--border,#e1e5eb);border-radius:7px;background:var(--panel,#fff);box-shadow:0 8px 24px #233b5726;white-space:normal;text-align:left;color:var(--text,#273443);font-size:12px;line-height:1.55}.signal-panel-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.signal-panel-heading>strong{font-size:12px}.signal-panel-heading>button{flex:none;width:24px;height:24px;padding:0;border:0;background:transparent;color:var(--muted,#67717d);font-size:20px;line-height:1;cursor:pointer}.signal-panel p{margin:8px 0}.signal-panel dl{margin:8px 0 0}.signal-panel dl>div{display:grid;grid-template-columns:82px minmax(0,1fr);gap:8px;padding:5px 0;border-top:1px solid var(--border,#e1e5eb)}dt{color:var(--muted,#67717d)}dd{margin:0;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.quote-status>.signal-panel:popover-open{position:fixed;margin:0;inset:auto}
@media(max-width:600px){.quote-status>.signal-panel,.quote-status>.signal-panel:popover-open{position:fixed;left:12px;right:12px;top:auto;bottom:12px;width:auto;max-height:60vh}}
</style>
