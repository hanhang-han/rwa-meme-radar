<template>
  <section class="risk-summary" :class="{ 'is-compact': compact }">
    <component :is="compact ? 'details' : 'div'" :class="compact ? 'risk-compact' : 'risk-expanded'">
      <summary v-if="compact" class="risk-overview"><span :class="['risk-overview-mark', 'signal-' + signal.tone]" aria-hidden="true">{{ signal.icon }}</span><strong>{{ compactTitle }}</strong><span>{{ tr('展开检测依据', 'Expand check evidence') }}</span></summary>
      <div v-if="signal.alerts.length" class="risk-trigger-list">
        <details v-for="item in signal.alerts" :key="item.key" :class="{ 'risk-critical': item.severity === 'critical' }">
          <summary><span aria-hidden="true">▲</span>{{ text(item.label) }}<small v-if="item.historical">{{ tr('历史提示', 'Historical flag') }}</small></summary>
          <p>{{ item.evidenceMissing ? tr('该历史风险提示缺少可复核检测值，当前状态待核实。', 'Check values are unavailable for this retained historical flag; its current state is unverified.') : riskReasonText(item.reason, lang.lang) }}</p>
          <dl v-if="evidenceRows(item).length"><div v-for="row in evidenceRows(item)" :key="row.key"><dt>{{ row.label }}</dt><dd :title="row.title">{{ row.value }}</dd></div></dl>
          <small>{{ sourceTitle(item) }}</small>
        </details>
      </div>
      <p class="risk-coverage"><strong>{{ tr('检测覆盖', 'Check coverage') }} {{ signal.completed }}/{{ signal.total }}</strong><span>{{ coverageLabel }}</span></p>
      <div class="risk-checks">
        <details v-for="item in signal.rows" :key="item.key" :class="['risk-' + item.status, { 'risk-critical': item.status === 'triggered' && item.severity === 'critical' }]">
          <summary :aria-label="text(item.label) + ' · ' + stateLabel(item)"><span>{{ text(item.label) }}</span><strong><span aria-hidden="true">{{ item.status === 'triggered' ? '▲' : item.status === 'clear' ? '✓' : '○' }}</span>{{ stateLabel(item) }}<small v-if="item.historical">{{ tr('历史提示', 'Historical flag') }}</small></strong></summary>
          <p>{{ riskReasonText(item.reason, lang.lang) }}</p>
          <dl v-if="evidenceRows(item).length"><div v-for="row in evidenceRows(item)" :key="row.key"><dt>{{ row.label }}</dt><dd :title="row.title">{{ row.value }}</dd></div></dl>
          <small>{{ sourceTitle(item) }}</small>
        </details>
      </div>
      <p class="risk-unidentified">{{ signal.coverage === 'complete' ? tr('未触发仅说明这些检测未命中当前规则；检测范围有限。', 'No flags means these checks did not match current rules; coverage remains limited.') : tr('检测覆盖有限：灰色项目缺少当前检测结果，未知不能视为安全。', 'Check coverage limits: gray fields lack current check results; unknown does not establish safety.') }}</p>
    </component>
  </section>
</template>
<script setup>
import { computed } from 'vue';
import { tr, useI18n } from '../i18n';
import { date } from '../utils/format';
import { useMinuteClock } from '../composables/useMinuteClock';
import { riskEvidenceRows, riskReasonText } from '../utils/risk-presentation';
import { riskSignal, signalText } from '../utils/signal-presentation';

const props = defineProps({ asset: { type: [Object, null], required: true }, compact: { type: Boolean, default: false } });
const { lang } = useI18n();
const now = useMinuteClock();
const signal = computed(() => riskSignal(props.asset, now.value));
const text = pair => signalText(pair, lang.lang);
const coverageLabel = computed(() => signal.value.coverage === 'complete' ? tr('当前五项已检测', 'Five current checks completed') : signal.value.coverage === 'partial' ? tr('部分检测', 'Partial checks') : signal.value.hasRecords ? tr('检测待更新', 'Check update due') : tr('未检测', 'Not checked'));
const compactTitle = computed(() => signal.value.alerts.length ? `${text(signal.value.label)}${signal.value.alerts.length > 1 ? ' +' + (signal.value.alerts.length - 1) : ''}${signal.value.historical ? ' · ' + tr('历史', 'Past') : ''}` : text(signal.value.label));
const evidenceRows = item => riskEvidenceRows(item?.evidence, lang.lang);
const sourceTitle = item => `${item.provider || tr('来源未提供', 'Source unavailable')} · ${item.at ? date(item.at) : tr('检测时间待核实', 'Check time unverified')}`;
function stateLabel(item) {
  if (item.status === 'triggered') return tr('已触发', 'Flagged');
  if (item.status === 'clear') return tr('未触发', 'No flag');
  return item.reason === 'stale-scan' ? tr('待更新', 'Update due') : item.current ? tr('未能识别', 'Unavailable') : tr('未检测', 'Not checked');
}
</script>
<style scoped>
.risk-summary{min-width:0}.risk-checks{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border-top:1px solid var(--border,#e1e5eb);border-bottom:1px solid var(--border,#e1e5eb)}.risk-checks details{min-width:0;padding:12px;border-right:1px solid var(--border,#e1e5eb)}.risk-checks details:first-child{padding-left:0}.risk-checks details:last-child{border-right:0}.risk-checks details[open]{background:var(--accent-soft,#f0f6fc)}summary{display:flex;align-items:center;flex-wrap:wrap;gap:6px;cursor:pointer;list-style:none;font-size:12px;line-height:1.5}summary::-webkit-details-marker{display:none}summary:focus-visible{outline:2px solid var(--accent,#2563a7);outline-offset:3px}.risk-checks summary{flex-direction:column;align-items:flex-start;gap:7px}.risk-checks summary>span{color:var(--muted,#67717d);font-size:12px}.risk-checks summary strong{display:flex;align-items:center;flex-wrap:wrap;gap:5px;font-size:12px;font-weight:600;color:var(--muted,#67717d)}.risk-triggered summary strong,.risk-trigger-list summary{color:#9b671d}.risk-critical summary strong,.risk-trigger-list .risk-critical summary{color:#ae3535}.risk-checks summary strong small{font-size:11px;color:var(--muted,#67717d);font-weight:400}.risk-checks p{margin:10px 0 7px;line-height:1.5}.risk-summary p,.risk-summary small,.risk-summary dl{font-size:12px;color:var(--muted,#67717d)}dl>div{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px;padding:5px 0;border-top:1px solid var(--border,#e1e5eb)}dd{margin:0;overflow-wrap:anywhere;text-align:right;font-variant-numeric:tabular-nums}.risk-trigger-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 16px;margin-top:12px}.risk-trigger-list details{min-width:0;padding:8px 0;border-bottom:1px solid var(--border,#e1e5eb)}.risk-trigger-list summary{font-weight:600}.risk-trigger-list summary>small{font-size:11px;font-weight:400}.risk-trigger-list p{margin:8px 0}.risk-coverage{display:flex;align-items:baseline;flex-wrap:wrap;gap:9px;margin:13px 0 10px}.risk-coverage>strong{color:var(--text,#273443);font-size:12px}.risk-unidentified{margin:10px 0 0;line-height:1.5}.risk-compact{border-top:1px solid var(--border,#e1e5eb);padding-top:10px;margin-top:10px}.risk-overview{gap:8px}.risk-overview strong{font-size:12px;font-weight:600;color:var(--text,#273443)}.risk-overview>span:last-child{color:var(--muted,#67717d);font-size:11px}.risk-overview-mark{color:var(--muted,#67717d);font-size:10px}.risk-overview-mark.signal-warning{color:#9b671d}.risk-overview-mark.signal-critical{color:#ae3535}.risk-compact .risk-checks{margin-top:10px}
@media(max-width:700px){.risk-checks{grid-template-columns:repeat(2,minmax(0,1fr))}.risk-checks details{border-bottom:1px solid var(--border,#e1e5eb)}.risk-checks details:nth-child(2n){border-right:0}.risk-checks details:last-child{border-bottom:0}.risk-checks details:first-child{padding-left:12px}.risk-trigger-list{grid-template-columns:1fr}}
</style>
