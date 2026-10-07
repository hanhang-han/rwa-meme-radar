<template>
  <details ref="disclosure" class="relation-badge signal-badge" :class="['is-' + signal.state, 'signal-' + signal.tone]" @toggle="syncSignalDisclosure(disclosure)" @click.stop @keydown.esc.stop.prevent="close">
    <summary :aria-label="text(signal.label) + ' · ' + tr('展开关系依据', 'Expand relationship evidence')">
      <span class="signal-icon" aria-hidden="true">{{ signal.icon }}</span><span>{{ text(signal.label) }}</span>
    </summary>
    <div class="signal-panel" popover="manual">
      <div class="signal-panel-heading"><strong>{{ tr('关系依据', 'Relationship evidence') }}</strong><button type="button" @click="close" :aria-label="tr('收起关系依据', 'Close relationship evidence')">×</button></div>
      <p>{{ text(signal.explanation) }}</p>
      <dl>
        <div v-if="relation?.ticker"><dt>{{ tr('股票主题', 'Stock theme') }}</dt><dd>{{ relation.ticker }}</dd></div>
        <div v-if="signal.recorded"><dt>{{ tr('交易池', 'Pool') }}</dt><dd>{{ relation.pool }}</dd></div>
        <div v-if="signal.recorded"><dt>{{ tr('股票侧代币', 'Stock-side token') }}</dt><dd>{{ relation.stock }}</dd></div>
        <div><dt>{{ tr('核验来源', 'Verification source') }}</dt><dd>{{ relation?.provider || relation?.source || tr('来源未提供', 'Source unavailable') }}</dd></div>
        <div><dt>{{ tr('关系核验', 'Relationship checked') }}</dt><dd>{{ time(relation?.checkedAt) }}</dd></div>
        <template v-if="signal.recorded">
          <div><dt>{{ tr('池流动性', 'Pool liquidity') }}</dt><dd>{{ knownLiquidity ? usd(relation.liquidityUsd) : tr('暂无估值', 'Unavailable') }}<span v-if="knownLiquidity && !signal.current"> · {{ tr('观测值', 'Observed value') }}</span></dd></div>
          <div><dt>{{ tr('估值来源', 'Valuation source') }}</dt><dd>{{ relation.liquidityProvider || relation.liquiditySource || tr('来源未提供', 'Source unavailable') }}</dd></div>
          <div><dt>{{ tr('估值观测', 'Valuation observed') }}</dt><dd>{{ time(relation.liquidityAt) }}</dd></div>
        </template>
      </dl>
      <p class="signal-note">{{ tr('同池或名称线索不代表上市公司授权、官方合作或价格相关。', 'Pool pairing or a name clue does not establish company authorization, official affiliation or price correlation.') }}</p>
    </div>
  </details>
</template>
<script setup>
import { computed, ref } from 'vue';
import { tr, useI18n } from '../i18n';
import { date, usd } from '../utils/format';
import { useMinuteClock } from '../composables/useMinuteClock';
import { relationSignal, signalText, signalTime, syncSignalDisclosure } from '../utils/signal-presentation';

const props = defineProps({ relation: { type: [Object, null], required: true } });
const { lang } = useI18n();
const now = useMinuteClock();
const disclosure = ref(null);
const signal = computed(() => relationSignal(props.relation, now.value));
const knownLiquidity = computed(() => typeof props.relation?.liquidityUsd === 'number' && Number.isFinite(props.relation.liquidityUsd) && props.relation.liquidityUsd >= 0);
const text = pair => signalText(pair, lang.lang);
const time = at => signalTime(at, now.value) == null ? tr('时间未提供或待核实', 'Time unavailable or unverified') : date(signalTime(at, now.value));
const close = () => { if (disclosure.value) disclosure.value.open = false; syncSignalDisclosure(disclosure.value); };
</script>
<style scoped>
.relation-badge.signal-badge{position:relative;display:inline-block;max-width:100%;padding:0;border:0;border-radius:4px;background:transparent;color:var(--muted,#67717d);vertical-align:middle;white-space:normal}
.signal-badge>summary{display:inline-flex;align-items:center;justify-content:flex-start;gap:4px;width:auto;height:auto;max-width:100%;box-sizing:border-box;padding:3px 6px;border:1px solid var(--border,#e1e5eb);border-radius:4px;font-size:11px;line-height:1.35;white-space:normal;list-style:none;cursor:pointer;color:inherit;background:var(--panel,#fff)}
summary::-webkit-details-marker{display:none}.signal-icon{flex:none;font-size:12px;line-height:1}.signal-verified>summary{color:#2563a7;border-color:#b9cfe4;background:#f0f6fc}.signal-neutral>summary{border-style:dashed}.signal-pending>summary{color:#8a692f;border-color:#decfae;background:#fcf8ef}
summary:focus-visible,button:focus-visible{outline:2px solid var(--accent,#2563a7);outline-offset:3px}
.relation-badge>.signal-panel{position:absolute;top:calc(100% + 6px);right:0;left:auto;z-index:50;width:min(310px,calc(100vw - 32px));max-width:calc(100vw - 32px);box-sizing:border-box;max-height:400px;overflow:auto;padding:12px;border:1px solid var(--border,#e1e5eb);border-radius:7px;background:var(--panel,#fff);box-shadow:0 8px 24px #233b5726;white-space:normal;text-align:left;font-size:12px;line-height:1.55;color:var(--text,#273443)}
.signal-panel-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.signal-panel-heading>strong{font-size:12px}.signal-panel-heading>button{flex:none;width:24px;height:24px;padding:0;border:0;background:transparent;font-size:20px;line-height:1;color:var(--muted,#67717d);cursor:pointer}
.signal-panel p{margin:8px 0}.signal-panel dl{margin:8px 0 0}.signal-panel dl>div{display:grid;grid-template-columns:86px minmax(0,1fr);gap:8px;padding:5px 0;border-top:1px solid var(--border,#e1e5eb)}dt,.signal-note{color:var(--muted,#67717d)}dd{margin:0;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.signal-note{padding-top:8px;border-top:1px solid var(--border,#e1e5eb)}
.relation-badge>.signal-panel:popover-open{position:fixed;margin:0;inset:auto}
@media(max-width:600px){.relation-badge>.signal-panel,.relation-badge>.signal-panel:popover-open{position:fixed;left:12px;right:12px;top:auto;bottom:12px;width:auto;max-width:none;max-height:60vh}}
</style>
