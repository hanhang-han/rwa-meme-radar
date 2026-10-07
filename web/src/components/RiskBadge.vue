<template>
  <details ref="disclosure" class="risk-badge signal-badge" :class="['signal-' + signal.tone, { 'is-flagged': signal.alerts.length, 'is-unchecked': !signal.alerts.length, 'is-critical': signal.critical, 'is-compact': compact }]" @toggle="syncSignalDisclosure(disclosure)" @click.stop @keydown.esc.stop.prevent="close">
    <summary :aria-label="summaryLabel" :title="summaryLabel">
      <span class="signal-icon" aria-hidden="true">{{ signal.icon }}</span><span>{{ text(signal.label) }}</span>
      <span v-if="signal.alerts.length > 1" class="signal-count">+{{ signal.alerts.length - 1 }}</span><span v-if="signal.historical" class="signal-history">{{ tr('历史', 'Past') }}</span>
    </summary>
    <div class="signal-panel" popover="manual">
      <div class="signal-panel-heading"><strong>{{ tr('风险与检测依据', 'Risk and check evidence') }}</strong><button type="button" @click="close" :aria-label="tr('收起检测依据', 'Close check evidence')">×</button></div>
      <p class="coverage-note">{{ coverageLabel }} · {{ signal.completed }}/{{ signal.total }}</p>
      <template v-if="signal.alerts.length">
        <section v-for="item in signal.alerts" :key="item.key" class="risk-item" :class="{ 'risk-critical': item.severity === 'critical' }">
          <strong>{{ text(item.label) }}<span v-if="item.historical"> · {{ tr('历史提示', 'Historical flag') }}</span></strong>
          <p>{{ item.evidenceMissing ? tr('保留的风险提示缺少可复核检测值，不能据此判断当前状态。', 'The retained risk flag has no available check evidence; its current state cannot be established.') : riskReasonText(item.reason, lang.lang) }}</p>
          <dl v-if="riskEvidenceRows(item.evidence, lang.lang).length"><div v-for="row in riskEvidenceRows(item.evidence, lang.lang)" :key="row.key"><dt>{{ row.label }}</dt><dd :title="row.title">{{ row.value }}</dd></div></dl>
          <small>{{ provenance(item) }}</small>
        </section>
      </template>
      <p v-else>{{ signal.state === 'clear' ? tr('已完成的检测未触发当前规则；这不是全面安全结论。', 'Completed checks did not trigger current rules; this is not a full safety assessment.') : tr('检测数据不足或已过期，未知项目不能视为安全。', 'Check data is incomplete or outdated; unknown fields do not establish safety.') }}</p>
      <p v-if="signal.coverage !== 'complete'" class="signal-note">{{ tr('检测覆盖有限：部分项目缺少可用检测，详情中列出各项依据。', 'Check coverage limits: some fields lack usable checks. Asset details list the evidence for each field.') }}</p>
    </div>
  </details>
</template>
<script setup>
import { computed, ref } from 'vue';
import { tr, useI18n } from '../i18n';
import { date } from '../utils/format';
import { riskEvidenceRows, riskReasonText } from '../utils/risk-presentation';
import { riskSignal, signalText, syncSignalDisclosure } from '../utils/signal-presentation';
import { useMinuteClock } from '../composables/useMinuteClock';

const props = defineProps({ asset: { type: [Object, null], required: true }, compact: { type: Boolean, default: false } });
const { lang } = useI18n();
const now = useMinuteClock();
const disclosure = ref(null);
const signal = computed(() => riskSignal(props.asset, now.value));
const text = pair => signalText(pair, lang.lang);
const coverageLabel = computed(() => signal.value.coverage === 'complete' ? tr('五项检测已完成', 'Five checks completed') : signal.value.coverage === 'partial' ? tr('部分检测', 'Partial checks') : tr('暂无当前检测', 'No current checks'));
const summaryLabel = computed(() => `${signal.value.alerts.length ? signal.value.alerts.map(item => text(item.label)).join(' · ') : text(signal.value.label)} · ${coverageLabel.value} · ${tr('展开依据', 'Expand evidence')}`);
const provenance = item => `${item.provider || tr('来源未提供', 'Source unavailable')} · ${item.at ? date(item.at) : tr('检测时间待核实', 'Check time unverified')}`;
const close = () => { if (disclosure.value) disclosure.value.open = false; syncSignalDisclosure(disclosure.value); };
</script>
<style scoped>
.risk-badge.signal-badge{position:relative;display:inline-block;max-width:100%;vertical-align:middle;color:var(--muted,#67717d)}.risk-badge.signal-badge>summary{display:inline-flex;align-items:center;justify-content:flex-start;gap:4px;width:auto;height:auto;max-width:100%;box-sizing:border-box;padding:3px 5px;border:1px solid var(--border,#e1e5eb);border-radius:4px;background:var(--panel,#fff);font-size:11px;line-height:1.35;white-space:normal;list-style:none;cursor:pointer}.signal-badge.signal-warning>summary{color:#9b671d;border-color:#e2cb9d;background:#fff9ee}.signal-badge.signal-critical>summary{color:#ae3535;border-color:#e6babb;background:#fff3f3}.signal-badge.signal-neutral>summary{border-style:dashed}.is-compact>summary{padding:3px 4px;font-size:10px}.signal-icon{flex:none;font-size:10px}.signal-count,.signal-history{font-size:10px;color:inherit;opacity:.85}summary::-webkit-details-marker{display:none}summary:focus-visible,button:focus-visible{outline:2px solid var(--accent,#2563a7);outline-offset:3px}
.risk-badge>.signal-panel{position:absolute;top:calc(100% + 6px);left:auto;right:0;z-index:50;width:min(320px,calc(100vw - 32px));max-width:calc(100vw - 32px);box-sizing:border-box;max-height:420px;overflow:auto;padding:12px;border:1px solid var(--border,#e1e5eb);border-radius:7px;background:var(--panel,#fff);box-shadow:0 8px 24px #233b5726;color:var(--text,#273443);white-space:normal;text-align:left;font-size:12px;line-height:1.55}.signal-panel-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.signal-panel-heading>strong{font-size:12px}.signal-panel-heading>button{flex:none;width:24px;height:24px;padding:0;border:0;background:transparent;color:var(--muted,#67717d);font-size:20px;line-height:1;cursor:pointer}.signal-panel .risk-item{padding:9px 0;border-top:1px solid var(--border,#e1e5eb)}.risk-item>strong{font-size:12px;color:#9b671d}.risk-item.risk-critical>strong{color:#ae3535}.risk-item>strong>span{font-size:11px;font-weight:400;color:var(--muted,#67717d)}.signal-panel p{margin:7px 0}.signal-panel small,.coverage-note,.signal-note{color:var(--muted,#67717d)}.signal-note{padding-top:8px;border-top:1px solid var(--border,#e1e5eb)}dl{margin:7px 0}dl>div{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px;padding:4px 0;border-top:1px solid var(--border,#e1e5eb)}dt{color:var(--muted,#67717d)}dd{margin:0;overflow-wrap:anywhere;text-align:right;font-variant-numeric:tabular-nums}
.risk-badge>.signal-panel:popover-open{position:fixed;margin:0;inset:auto}
@media(max-width:600px){.risk-badge>.signal-panel,.risk-badge>.signal-panel:popover-open{position:fixed;left:12px;right:12px;top:auto;bottom:12px;width:auto;max-width:none;max-height:60vh}}
</style>
