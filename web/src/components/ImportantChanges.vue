<template>
  <section class="panel important-changes" aria-labelledby="important-changes-title">
    <div class="important-heading">
      <h2 id="important-changes-title">{{ tr('最新动态', 'Latest activity') }}</h2>
      <div class="important-filter" role="group" :aria-label="tr('动态筛选', 'Activity filter')">
        <button type="button" :class="{active:filter==='all'}" :aria-pressed="filter==='all'" @click="filter='all'">{{ tr('全部', 'All') }}</button>
        <button type="button" :class="{active:filter==='watched'}" :aria-pressed="filter==='watched'" @click="filter='watched'">{{ tr('已关注', 'Following') }}</button>
      </div>
    </div>
    <div class="important-tab-switch" role="group" :aria-label="tr('记录类型', 'Record type')">
      <button type="button" :class="{active:viewMode==='verified'}" :aria-pressed="viewMode==='verified'" @click="chooseFeed('verified')">{{ tr('配对动态 · 24h', 'Pair activity · 24h') }} <b>{{ events.length }}</b></button>
      <button type="button" :class="{active:viewMode==='observed'}" :aria-pressed="viewMode==='observed'" @click="chooseFeed('observed')">{{ tr('收录记录', 'Discoveries') }} <b>{{ recentRows.length }}</b></button>
    </div>
    <div v-if="loading" class="important-empty" role="status">{{ tr('正在加载动态…', 'Loading activity…') }}</div>
    <div v-else-if="!displayed.length" class="important-empty" role="status">
      {{ viewMode==='observed'
        ? (filter==='watched' ? tr('关注的资产暂无收录记录。', 'No discoveries for followed assets.') : tr('暂无收录记录。', 'No discoveries yet.'))
        : (filter==='watched' ? tr('关注的资产在 24 小时内暂无配对动态。', 'No pair activity for followed assets in 24 hours.') : tr('24 小时内暂无配对动态。', 'No pair activity in 24 hours.')) }}
    </div>
    <div v-else class="important-list" aria-live="polite">
      <article v-for="item in displayed" :key="item.id" class="important-item" :class="{'is-recent':recent(item),'observation-item':viewMode==='observed'}">
        <div class="important-time">
          <span class="important-time-dot" aria-hidden="true"></span>
          <time :datetime="iso(eventTime(item))" :title="date(eventTime(item))">{{ relativeTime(item) }}</time>
          <small>{{ viewMode==='verified' && item.type==='relation-verified' ? tr('核验', 'Verified') : tr('收录', 'Indexed') }}</small>
        </div>
        <div class="important-body">
          <div class="important-asset-line">
            <RouterLink :to="detailPath(item)" class="important-name">{{ assetName(item) }}</RouterLink>
            <span v-if="item.ticker" class="important-theme"><span aria-hidden="true">{{ viewMode==='verified' ? '↔' : '·' }}</span> {{ item.ticker }}</span>
            <span class="important-status" :class="{'is-unverified':viewMode==='observed' && !item.currentlyVerified}" :title="viewMode==='observed' && !item.currentlyVerified ? tr('股票身份或配对条件尚未核实', 'Stock identity or pairing criteria are unverified') : undefined">{{ viewMode==='verified' || item.currentlyVerified ? tr('配对已核验', 'Verified pair') : tr('待核验', 'Unverified') }}</span>
          </div>
          <div class="important-identity"><span>{{ chainName(item) }}</span><span class="important-contract">{{ short(item.token) }}</span></div>
          <p v-if="viewMode==='observed' && observationFact(item)" class="important-fact">{{ observationFact(item) }}</p>
          <details class="important-record">
            <summary>{{ tr('时间记录', 'Record times') }}</summary>
            <dl v-if="viewMode==='verified'">
              <div><dt>{{ tr('池创建', 'Pool created') }}</dt><dd>{{ date(item.occurredAt) }}</dd></div>
              <div><dt>{{ tr('首次收录', 'First indexed') }}</dt><dd>{{ date(item.discoveredAt) }}</dd></div>
              <div><dt>{{ tr('核验', 'Verified') }}</dt><dd>{{ date(item.verifiedAt) }}</dd></div>
            </dl>
            <dl v-else>
              <div><dt>{{ tr('收录', 'Indexed') }}</dt><dd>{{ date(item.at) }}</dd></div>
              <div v-if="item.poolCreatedAt"><dt>{{ tr('池创建', 'Pool created') }}</dt><dd>{{ date(item.poolCreatedAt) }}</dd></div>
              <div v-if="item.discoveredAt"><dt>{{ tr('首次发现池', 'Pool first indexed') }}</dt><dd>{{ date(item.discoveredAt) }}</dd></div>
            </dl>
          </details>
        </div>
        <div class="important-actions">
          <RouterLink v-if="viewMode==='verified' && item.relation?.stock && item.relation?.pool" :to="pairPath(item)">{{ tr('查看配对', 'View pair') }} <span aria-hidden="true">↗</span></RouterLink>
          <RouterLink v-else :to="detailPath(item)">{{ tr('查看资产', 'View asset') }} <span aria-hidden="true">↗</span></RouterLink>
          <a v-if="item.relation?.pool || item.pool" :href="explorer(item.relation?.pool || item.pool,'address',String(item.chainId))" target="_blank" rel="noopener">{{ tr('链上记录', 'Onchain') }} ↗</a>
          <button type="button" :aria-pressed="watched.has(itemKey(item))" @click="toggleWatch(item)">{{ watched.has(itemKey(item)) ? tr('★ 已关注', '★ Following') : tr('☆ 关注', '☆ Follow') }}</button>
        </div>
      </article>
    </div>
    <div class="important-footer">
      <details class="important-explainer"><summary>{{ tr('记录说明', 'About these records') }}</summary><p>{{ tr('核验确认链上池的配对关系，不代表发行方授权或价格联动。收录时间是本站首次记录时间，不是池创建时间。', 'Verification confirms an onchain pool pairing, not issuer endorsement or linked prices. Indexing time is when the site first recorded it, not when the pool was created.') }}</p></details>
      <RouterLink :to="{path:'/events',query:{chain:scope}}">{{ tr('全部记录', 'All records') }} →</RouterLink>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { tr } from '../i18n';
import { chainName, date, explorer, short } from '../utils/format';
import { useMinuteClock } from '../composables/useMinuteClock';
import { readThemeWatches, themeEventTime, visibleRecentObservations, visibleThemeEvents, writeThemeWatches } from '../utils/theme-map-model';

const props = defineProps({
  changes: { type:Object, default:null },
  observations: { type:Array, default:() => [] },
  unified: { type:Object, default:null },
  assets: { type:Array, default:() => [] },
  scope: { type:String, default:'all' },
  loading: Boolean,
});
const now = useMinuteClock();
const filter = ref('all');
const viewMode = ref('verified');
const manuallySelected = ref(false);
const watched = ref(readThemeWatches());
const eventTime = item => item?.at ?? themeEventTime(item);
const events = computed(() => visibleThemeEvents(props.changes, props.scope, now.value, 12));
const recentRows = computed(() => visibleRecentObservations(props.observations,props.unified,props.scope,now.value,30));
const displayed = computed(() => (filter.value === 'watched'
  ? (viewMode.value === 'verified' ? events.value : recentRows.value).filter(item => watched.value.has(itemKey(item)))
  : (viewMode.value === 'verified' ? events.value : recentRows.value)).slice(0, 6));
const assetIndex = computed(() => new Map(props.assets.map(asset => [itemKey(asset),asset])));
watch([events,recentRows], ([verified,observed]) => {
  if (!manuallySelected.value) viewMode.value = verified.length ? 'verified' : observed.length ? 'observed' : 'verified';
}, {immediate:true});

function chooseFeed(mode) { manuallySelected.value = true; viewMode.value = mode; }

function itemKey(item) { return `${String(item.chainId ?? '')}:${String(item.token ?? '').toLowerCase()}`; }
function assetName(item) {
  const asset = assetIndex.value.get(itemKey(item));
  return asset?.name || asset?.symbol || item.symbol || short(item.token);
}
function observationFact(item) {
  if (item.kind === 'pair-observed') return '';
  return item.keyword ? tr(`名称匹配“${item.keyword}”`, `Name match: “${item.keyword}”`) : '';
}
function iso(value) { return value ? new Date(value).toISOString() : undefined; }
function relativeTime(item) {
  const at = eventTime(item);
  const elapsed = now.value - at;
  if (!at || elapsed < 0) return '—';
  if (elapsed < 60_000) return tr('刚刚', 'just now');
  if (elapsed < 3_600_000) return tr(`${Math.floor(elapsed / 60_000)} 分钟前`, `${Math.floor(elapsed / 60_000)}m ago`);
  return date(at);
}
function recent(item) { const at = eventTime(item); return at && now.value - at >= 0 && now.value - at <= 300_000; }
function detailPath(item) {
  return {path:`/detail/${encodeURIComponent(item.chainId)}/${encodeURIComponent(item.token)}`,query:{chain:props.scope}};
}
function pairPath(item) {
  return {path:`/pair/${encodeURIComponent(item.chainId)}/${encodeURIComponent(item.relation.stock)}`,
    query:{chain:props.scope,pool:item.relation.pool}};
}
function toggleWatch(item) {
  const next = new Set(watched.value);
  if (next.has(itemKey(item))) next.delete(itemKey(item));
  else next.add(itemKey(item));
  watched.value = next;
  writeThemeWatches(next);
}
function syncWatch() { watched.value = readThemeWatches(); }
onMounted(() => {
  window.addEventListener('theme-watch-change', syncWatch);
  window.addEventListener('storage', syncWatch);
});
onUnmounted(() => {
  window.removeEventListener('theme-watch-change', syncWatch);
  window.removeEventListener('storage', syncWatch);
});
</script>

<style scoped>
.important-changes{padding:20px 24px;background:var(--panel);border-color:var(--border)}
.important-heading{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:14px}
.important-heading h2{color:var(--text);font-size:18px;letter-spacing:0;font-weight:600}
.important-filter{display:inline-flex;gap:3px;padding:3px;background:var(--bg);border:1px solid var(--border);border-radius:6px}
.important-filter button,.important-tab-switch button{border:0;background:transparent;color:var(--muted);font:inherit;font-size:12px;cursor:pointer}
.important-filter button{border-radius:4px;padding:5px 11px}.important-filter button.active{background:var(--surface-raised);color:var(--text)}
.important-tab-switch{display:flex;gap:20px;border-bottom:1px solid var(--border)}
.important-tab-switch button{display:flex;align-items:center;gap:7px;padding:7px 0 12px;border-bottom:2px solid transparent}
.important-tab-switch button.active{border-color:var(--accent);color:var(--text)}.important-tab-switch b{color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums;font-weight:500}
.important-empty{padding:34px 12px;color:var(--muted);text-align:center;font-size:13px}
.important-list{display:flex;flex-direction:column}
.important-item{display:grid;grid-template-columns:112px minmax(0,1fr) auto;gap:20px;padding:17px 0;border-bottom:1px solid var(--border);min-width:0}
.important-item:last-child{border-bottom:0}
.important-time{display:grid;grid-template-columns:7px minmax(0,1fr);align-content:start;gap:2px 9px;color:var(--muted);font-size:11px;line-height:1.5;padding-top:3px;font-variant-numeric:tabular-nums}
.important-time small{grid-column:2;color:var(--muted);font-size:10px}.important-time-dot{width:5px;height:5px;margin-top:6px;border-radius:50%;background:var(--muted)}
.important-item.is-recent .important-time-dot{background:var(--accent)}
.important-body{min-width:0}.important-asset-line{display:flex;flex-wrap:wrap;align-items:center;gap:7px 12px}
.important-name{color:var(--text);font-size:14px;font-weight:600;text-decoration:none;overflow-wrap:anywhere}.important-name:hover{color:var(--accent)}
.important-theme{color:var(--text);font-size:12px;font-weight:600}.important-theme>span{color:var(--muted);margin-right:5px}
.important-status{color:var(--accent);background:var(--accent-soft);border-radius:4px;padding:2px 6px;font-size:10px}.important-status.is-unverified{color:var(--warning)}
.important-identity{display:flex;gap:10px;flex-wrap:wrap;margin-top:4px;color:var(--muted);font-size:11px}.important-contract{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.important-fact{color:var(--muted);font-size:12px;line-height:1.5;margin-top:7px}
.important-record{margin-top:6px;color:var(--muted);font-size:11px}.important-record summary,.important-explainer summary{cursor:pointer;width:fit-content}
.important-record dl{display:flex;flex-wrap:wrap;gap:8px 20px;padding-top:8px}.important-record dt{color:var(--muted)}.important-record dd{color:var(--text);margin:2px 0 0;font-variant-numeric:tabular-nums}
.important-actions{display:flex;align-items:center;justify-content:flex-end;flex-wrap:wrap;gap:12px;align-content:start;padding-top:3px}
.important-actions a,.important-actions button{color:var(--accent);font:inherit;font-size:11px;text-decoration:none;white-space:nowrap}
.important-actions a:nth-child(2),.important-actions button{color:var(--muted)}.important-actions button{border:0;background:none;padding:0;cursor:pointer}.important-actions button[aria-pressed=true]{color:var(--accent)}
.important-footer{display:flex;align-items:start;justify-content:space-between;gap:24px;margin-top:14px;padding-top:12px;border-top:1px solid var(--border);color:var(--muted);font-size:11px}
.important-footer>a{color:var(--accent);text-decoration:none;white-space:nowrap}.important-explainer p{max-width:640px;margin:8px 0 0;line-height:1.6}
.important-changes :is(button,a,summary):focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(max-width:1000px){.important-item{grid-template-columns:100px minmax(0,1fr);gap:4px 16px}.important-actions{grid-column:2;justify-content:flex-start;padding-top:8px}}
@media(max-width:600px){.important-changes{padding:16px}.important-item{grid-template-columns:76px minmax(0,1fr);gap:4px 12px;padding:16px 0}.important-time{font-size:10px;gap:2px 6px}.important-heading h2{font-size:17px}.important-asset-line{gap:5px 8px}.important-record dl{flex-direction:column}.important-actions{gap:12px}.important-tab-switch{gap:16px}.important-tab-switch button{font-size:11px}}
</style>
