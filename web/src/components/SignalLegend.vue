<template>
  <details class="signal-legend">
    <summary><span aria-hidden="true">≡</span>{{ tr('信号说明', 'Signal guide') }}<span class="legend-summary-note">{{ tr('关系 · 报价 · 风险', 'Relations · quotes · risks') }}</span></summary>
    <div class="legend-content">
      <div class="legend-row"><span class="legend-mark verified">⇄ {{ tr('同池已核实', 'Verified pool') }}</span><p>{{ tr('已核实链上配对池且当前估值达标；不代表公司授权或官方合作。', 'A verified onchain pair whose current valuation qualifies; company authorization or official affiliation is not established.') }}</p></div>
      <div class="legend-row"><span class="legend-mark neutral">≈ {{ tr('仅名称线索', 'Name only') }}</span><p>{{ tr('只反映名称相近，尚未确认同池。', 'Name similarity alone; pool pairing has not been confirmed.') }}</p></div>
      <div class="legend-row"><span class="legend-mark pending">⇄ {{ tr('估值待更新', 'Update due') }}</span><p>{{ tr('配对依据保留，估值过期或时间未知，暂不计入当前达标同池。', 'Pairing evidence is retained. An outdated or unknown-time valuation is excluded from current qualifying pools.') }}</p></div>
      <div class="legend-row"><span class="legend-mark verified">● {{ tr('实时行情', 'Live quote') }}</span><p>{{ tr('来源明确实时属性并有有效报价时间；最新观测、延迟参考与历史报价分别标注。', 'The source establishes realtime data and a valid quote time. Latest observations, delayed references and historical quotes are labeled separately.') }}</p></div>
      <div class="legend-row"><span class="legend-mark critical">▲ {{ tr('风险提示', 'Risk flags') }}</span><p>{{ tr('红色优先显示关键限制，琥珀色表示规则触发的提示；点击标签查看检测值、来源和时间，历史提示保留标记。', 'Red prioritizes critical restrictions; amber identifies triggered rules. Open a label for check values, sources and times. Historical flags remain labeled.') }}</p></div>
      <div class="legend-row"><span class="legend-mark neutral">○ {{ tr('部分 / 未检测', 'Partial / unchecked') }}</span><p>{{ tr('灰色表示检测范围或数据受限，不能当作安全；“未触发”仅指当前已完成的五项检测未触发规则。', 'Gray marks limited coverage or unavailable checks and does not establish safety. “No flags” means only that the five completed current checks did not trigger rules.') }}</p></div>
    </div>
  </details>
</template>
<script setup>
import { tr } from '../i18n';
</script>
<style scoped>
.signal-legend{max-width:100%;color:var(--muted,#67717d);font-size:12px;line-height:1.5}.signal-legend>summary{display:flex;align-items:center;flex-wrap:wrap;gap:6px;width:fit-content;max-width:100%;padding:5px 0;list-style:none;cursor:pointer}.signal-legend>summary::-webkit-details-marker{display:none}.signal-legend>summary:focus-visible{outline:2px solid var(--accent,#2563a7);outline-offset:3px}.legend-summary-note{margin-left:6px;font-size:11px}.legend-content{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 24px;margin-top:5px;padding:12px 14px;border:1px solid var(--border,#e1e5eb);border-radius:6px;background:var(--panel,#fff)}.legend-row{display:grid;grid-template-columns:120px minmax(0,1fr);gap:12px;align-items:start;min-width:0;padding:8px 0}.legend-mark{width:fit-content;max-width:100%;box-sizing:border-box;padding:2px 5px;border:1px solid var(--border,#e1e5eb);border-radius:4px;font-size:11px}.verified{color:#2563a7;background:#f0f6fc;border-color:#b9cfe4}.pending{color:#8a692f;background:#fcf8ef;border-color:#decfae}.critical{color:#ae3535;background:#fff3f3;border-color:#e6babb}.neutral{border-style:dashed}.legend-row p{margin:0;overflow-wrap:anywhere}
@media(max-width:800px){.legend-content{grid-template-columns:1fr}}@media(max-width:600px){.legend-content{padding:9px 11px}.legend-row{grid-template-columns:108px minmax(0,1fr);gap:10px}.legend-summary-note{font-size:11px}}
</style>
