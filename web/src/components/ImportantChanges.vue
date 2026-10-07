<template>
  <section class="important-changes" :class="{'panel':!compact,'is-compact':compact}" :aria-label="compact ? tr('发现记录','Discovery records') : undefined" :aria-labelledby="compact ? undefined : 'important-changes-title'">
    <div v-if="!compact" class="important-heading">
      <h2 id="important-changes-title">{{ tr('最近动态', 'Recent activity') }}</h2>
      <div class="important-filter" role="group" :aria-label="tr('动态筛选', 'Activity filter')">
        <button type="button" :class="{active:filter==='all'}" :aria-pressed="filter==='all'" @click="filter='all'">{{ tr('全部', 'All') }}</button>
        <button type="button" :class="{active:filter==='watched'}" :aria-pressed="filter==='watched'" @click="filter='watched'">{{ tr('已关注', 'Following') }}</button>
      </div>
    </div>
    <div v-if="!compact" class="important-tab-switch" role="group" :aria-label="tr('记录类型', 'Record type')">
      <button type="button" :class="{active:viewMode==='verified'}" :aria-pressed="viewMode==='verified'" @click="chooseFeed('verified')">{{ tr('同池记录 · 24h', 'Pool pairs · 24h') }} <b :title="tr('当前范围近24h可显示的同池及关系记录数量','Displayable pair and relation records in the last 24h for this scope')">{{ events.length }}</b></button>
      <button type="button" :class="{active:viewMode==='observed'}" :aria-pressed="viewMode==='observed'" @click="chooseFeed('observed')">{{ compact ? tr('收录与观测','Indexed / observed') : tr('新收录与池记录', 'Discoveries and pool records') }} <b :title="tr('当前范围已加载的收录与观测记录数量','Loaded indexing and observation records for this scope')">{{ recentRows.length }}</b></button>
    </div>
    <div v-if="loading" class="important-empty" role="status">{{ tr('正在加载动态…', 'Loading activity…') }}</div>
    <div v-else-if="!displayed.length" class="important-empty" role="status">
      {{ compact ? tr('当前范围暂无发现记录。','No discovery records in this scope.') : viewMode==='observed'
        ? (activeFilter==='watched' ? tr('暂无关注对象的收录或观测记录。', 'No indexing or observation records for followed assets.') : tr('当前范围暂无收录或观测记录。', 'No indexing or observation records in this scope.'))
        : (activeFilter==='watched' ? tr('关注对象近 24h 无同池记录。', 'No pool-pair records for followed assets in 24h.') : tr('当前范围近 24h 无同池记录。', 'No pool-pair records in this scope in 24h.')) }}
    </div>
    <div v-else class="important-list" :aria-live="compact ? 'off' : 'polite'">
      <article v-for="item in displayed" :key="item.id" class="important-item" :class="{'is-recent':recent(item),'observation-item':viewMode==='observed','is-added':highlightedKeys.has(item.id)}">
        <template v-if="compact">
          <RouterLink :to="detailPath(item)" class="important-compact-link" :title="compactTitle(item)">
            <div class="important-compact-top"><strong>{{ assetName(item) }}</strong><span class="important-status" :class="{'is-unverified':!isVerified(item)}">{{ statusLabel(item) }}</span><time :datetime="iso(eventTime(item))" :title="date(eventTime(item))">{{ relativeTime(item) }}</time></div>
            <div class="important-compact-fact"><span>{{ chainName(item) }}</span><span v-if="item.ticker" class="important-theme">{{ item.ticker }}</span><span v-if="compactFact(item)">{{ compactFact(item) }}</span><small v-if="item.recordCount>1">{{ item.recordCount }} {{ tr('条','records') }}</small></div>
          </RouterLink>
          <button type="button" class="important-compact-watch" :class="{'is-following':watched.has(itemKey(item))}" :aria-pressed="watched.has(itemKey(item))" :aria-label="(watched.has(itemKey(item)) ? tr('取消关注 ','Unfollow ') : tr('关注 ','Follow '))+assetName(item)" @click="toggleWatch(item)">{{ watched.has(itemKey(item)) ? '★' : '☆' }}</button>
        </template>
        <template v-else>
        <div class="important-time">
          <span class="important-time-dot" aria-hidden="true"></span>
          <time :datetime="iso(eventTime(item))" :title="date(eventTime(item))">{{ relativeTime(item) }}</time>
        </div>
        <div class="important-body">
          <div class="important-asset-line">
            <RouterLink :to="detailPath(item)" class="important-name">{{ assetName(item) }}</RouterLink>
            <RouterLink v-if="item.ticker" class="important-theme" :to="themeNavigationLink(item.ticker,{route,scope})">{{ item.ticker }}</RouterLink>
            <span class="important-status" :class="{'is-unverified':!isVerified(item)}">{{ statusLabel(item) }}</span><small v-if="item.recordCount>1" class="important-record-count" :title="tr('此显示条目包含的原始记录数','Raw records folded into this displayed entry')">{{ item.recordCount }} {{ tr('条','records') }}</small>
          </div>
          <div class="important-identity"><span>{{ chainName(item) }}</span><span class="important-contract">{{ short(item.token) }}</span></div>
          <p v-if="eventFact(item)" class="important-fact" :title="tr('配对池流动性以USD计；记录时间与池创建时间分别显示','Pair-pool liquidity is in USD; record time and pool creation time are displayed separately')">{{ eventFact(item) }}</p>
          <details class="important-record">
            <summary>{{ tr('时间', 'Times') }}</summary>
            <dl v-if="viewMode==='verified'">
              <div v-if="item.occurredAt"><dt>{{ tr('池创建', 'Pool created') }}</dt><dd>{{ date(item.occurredAt) }}</dd></div>
              <div v-if="item.discoveredAt && item.discoveredAt!==item.verifiedAt"><dt>{{ tr('首次收录', 'First indexed') }}</dt><dd>{{ date(item.discoveredAt) }}</dd></div>
              <div v-if="item.verifiedAt"><dt>{{ tr('配对记录', 'Pair recorded') }}</dt><dd>{{ date(item.verifiedAt) }}</dd></div>
            </dl>
            <dl v-else>
              <div><dt>{{ item.kind==='pair-observed' ? tr('池关系观测', 'Pool relation observed') : tr('首次收录', 'First indexed') }}</dt><dd>{{ date(item.at) }}</dd></div>
              <div v-if="item.poolCreatedAt"><dt>{{ tr('池创建', 'Pool created') }}</dt><dd>{{ date(item.poolCreatedAt) }}</dd></div>
              <div v-if="item.discoveredAt"><dt>{{ tr('首次发现池', 'Pool first indexed') }}</dt><dd>{{ date(item.discoveredAt) }}</dd></div>
            </dl>
          </details>
        </div>
        <div class="important-actions">
          <RouterLink :to="detailPath(item)">{{ tr('详情', 'Details') }} <span aria-hidden="true">↗</span></RouterLink>
          <RouterLink v-if="item.pool || item.relation?.pool" :to="pairPath(item)">{{ tr('配对依据', 'Pair evidence') }} <span aria-hidden="true">↗</span></RouterLink>
          <a v-if="item.relation?.pool || item.pool" :href="explorer(item.relation?.pool || item.pool,'address',String(item.chainId))" target="_blank" rel="noopener">{{ tr('链上', 'Onchain') }} ↗</a>
          <button type="button" :aria-pressed="watched.has(itemKey(item))" @click="toggleWatch(item)">{{ watched.has(itemKey(item)) ? tr('★ 已关注', '★ Following') : tr('☆ 关注', '☆ Follow') }}</button>
        </div>
        </template>
      </article>
    </div>
    <div v-if="!compact" class="important-footer">
      <details class="important-explainer"><summary>{{ tr('时间口径', 'About timestamps') }}</summary><p>{{ tr('首次收录与池关系观测均为本站记录时间；池创建时间单独列出。', 'First indexing and pool observations use this site’s record times; pool creation time is separate.') }}</p></details>
      <RouterLink :to="pageNavigationLink('/events',{route,scope})">{{ tr('发现记录', 'Discovery records') }} →</RouterLink>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { assetNavigationLink, themeNavigationLink, pageNavigationLink } from '../utils/navigation-context';
import { tr } from '../i18n';
import { age, chainName, date, explorer, short, usd } from '../utils/format';
import { groupActivityItems } from '../utils/home-model';
import { useMinuteClock } from '../composables/useMinuteClock';
import { readThemeWatches, themeEventTime, visibleRecentObservations, visibleThemeEvents, writeThemeWatches } from '../utils/theme-map-model';
import { filterHomeThemeRecords } from '../utils/home-live-model';

const props = defineProps({
  changes: { type:Object, default:null },
  observations: { type:Array, default:() => [] },
  unified: { type:Object, default:null },
  assets: { type:Array, default:() => [] },
  scope: { type:String, default:'all' },
  ticker: { type:String, default:'' },
  compact: Boolean,
  externalFilter: { type:String, default:null },
  rows: { type:Array, default:null },
  highlightedKeys: { type:Set, default:() => new Set() },
  loading: Boolean,
});
const emit = defineEmits(['records']);
const route=useRoute();
const now = useMinuteClock();
const filter = ref('all');
const viewMode = ref('verified');
const manuallySelected = ref(false);
const watched = ref(readThemeWatches());
const eventTime = item => item?.at ?? themeEventTime(item);
const activeFilter = computed(() => props.externalFilter ?? filter.value);
const events = computed(() => filterHomeThemeRecords(groupActivityItems(visibleThemeEvents(props.changes, props.scope, now.value, Infinity)), props.ticker, props.unified?.relations ?? [], props.assets));
const recentRows = computed(() => filterHomeThemeRecords(groupActivityItems(visibleRecentObservations(props.observations,props.unified,props.scope,now.value,Infinity)), props.ticker, props.unified?.relations ?? [], props.assets));
const compactRecords = computed(() => {
  const grouped = new Map();
  for (const item of [...events.value,...recentRows.value]) {
    const pool = item.pool ?? item.relation?.pool;
    const key = pool ? `${itemKey(item)}:${String(pool).toLowerCase()}` : `${itemKey(item)}:${item.kind ?? item.type}`;
    const previous = grouped.get(key);
    if (!previous) grouped.set(key,item);
    else {
      // One observed pool can arrive through both projections. Preserve the
      // verified record instead of displaying the same pool as two discoveries.
      const priority = row => row.type==='relation-verified' ? 3 : isVerified(row) ? 2 : row.pool||row.relation?.pool ? 1 : 0;
      const preferred = priority(item)-priority(previous) || eventTime(item)-eventTime(previous);
      grouped.set(key,{...(preferred>0?item:previous),recordCount:Math.max(Number(item.recordCount)||1,Number(previous.recordCount)||1)});
    }
  }
  return [...grouped.values()].sort((a,b)=>eventTime(b)-eventTime(a));
});
const selectedRows = computed(() => {
  const records = props.compact ? compactRecords.value : viewMode.value === 'verified' ? events.value : recentRows.value;
  return activeFilter.value==='watched' ? records.filter(item=>watched.value.has(itemKey(item)) || watched.value.has(`stock:${String(item.ticker??'').toUpperCase()}`)) : records;
});
const displayed = computed(() => (props.rows ?? selectedRows.value).slice(0, props.compact ? 5 : 6));
watch([selectedRows,viewMode], ([records,mode]) => emit('records', records, props.compact?'compact':mode), {immediate:true});
const assetIndex = computed(() => new Map(props.assets.map(asset => [itemKey(asset),asset])));
watch([events,recentRows], ([verified,observed]) => {
  if (!manuallySelected.value) viewMode.value = verified.length ? 'verified' : observed.length ? 'observed' : 'verified';
}, {immediate:true});

function chooseFeed(mode) { manuallySelected.value = true; viewMode.value = mode; }
function isVerified(item) { return item?.type === 'relation-verified' || item?.currentlyVerified === true; }
function statusLabel(item) {
  if (['pair-observed','relation-observed'].includes(item?.type ?? item?.kind)) return item.currentlyVerified ? tr('同池观测','Verified pool observed') : tr('池关系观测','Pool observed');
  if (isVerified(item)) return tr('同池记录','Pool pair');
  if (['relation-observed','pair-observed','pool-created'].includes(item?.type ?? item?.kind)) return tr('池关系观测','Pool observed');
  return item?.keyword ? tr('名称匹配','Name match') : tr('首次收录','First indexed');
}
function compactFact(item) {
  const relation = item.relation ?? {};
  const matched = props.unified?.relations?.find(row=>String(row.chainId)===String(item.chainId) && String(row.pool).toLowerCase()===String(relation.pool??item.pool).toLowerCase());
  const liquidity = relation.liquidityUsd ?? matched?.liquidityUsd;
  const at = Number(relation.liquidityAt ?? matched?.liquidityAt);
  const current = at>0&&now.value-at>=0&&now.value-at<=900000;
  if (item.keyword) return tr(`匹配“${item.keyword}”`,`matches “${item.keyword}”`);
  if (liquidity != null) return (current?tr('池流动性 ','Pool liquidity '):tr('历史池流动性 ','Historical pool liquidity '))+usd(liquidity);
  return item.recordCount > 1 ? tr('合并同池重复记录','Repeated pool records grouped') : '';
}
function compactTitle(item) { return [date(eventTime(item)),short(item.token),eventFact(item),tr('打开资产详情','Open asset details')].filter(Boolean).join(' · '); }

function itemKey(item) { return `${String(item.chainId ?? '')}:${String(item.token ?? '').toLowerCase()}`; }
function assetName(item) {
  const asset = assetIndex.value.get(itemKey(item));
  return asset?.name || asset?.symbol || item.symbol || short(item.token);
}
function eventFact(item) {
  const relation = item.relation ?? {};
  if(item.type==='relation-verified' || item.currentlyVerified || item.kind==='pair-observed' || item.kind==='pool-created'){
    const matched=props.unified?.relations?.find(row=>String(row.chainId)===String(item.chainId) && String(row.pool).toLowerCase()===String(relation.pool??item.pool).toLowerCase());
    const protocol=relation.protocol??item.dex??matched?.protocol;
    const stock=relation.stockSymbol??matched?.stockSymbol??item.ticker;
    const liquidity=relation.liquidityUsd??matched?.liquidityUsd;
    const at=relation.liquidityAt??matched?.liquidityAt;
    const current=at&&now.value-Number(at)>=0&&now.value-Number(at)<=900000;
    return [protocol,stock?tr('与 '+stock+' 配对','paired with '+stock):null,liquidity!=null?tr((current?'池流动性 ':'历史池流动性 ')+usd(liquidity),(current?'Pool liquidity ':'Historical pool liquidity ')+usd(liquidity)):null,item.pool||relation.pool?short(item.pool||relation.pool):null].filter(Boolean).join(' · ');
  }
  return item.keyword ? tr(`名称匹配“${item.keyword}”`, `Name match: “${item.keyword}”`) : '';
}
function iso(value) { return value ? new Date(value).toISOString() : undefined; }
function relativeTime(item) {
  const at = eventTime(item);
  const elapsed = now.value - at;
  if (!at || elapsed < -60_000) return '—';
  if (elapsed < 60_000) return tr('刚刚', 'just now');
  if (elapsed < 3_600_000) return tr(`${Math.floor(elapsed / 60_000)} 分钟前`, `${Math.floor(elapsed / 60_000)}m ago`);
  return age(at);
}
function recent(item) { const at = eventTime(item); return at && now.value - at >= 0 && now.value - at <= 300_000; }
function detailPath(item) {
  return assetNavigationLink(item,{route,scope:props.scope});
}
function pairPath(item) {
  return assetNavigationLink(item,{route,scope:props.scope,tab:'relation',pool:item.pool??item.relation?.pool});
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
.important-changes{min-width:0}.important-heading{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:13px}.important-heading h2{font-family:inherit;color:var(--text);font-size:17px;letter-spacing:-.01em;font-weight:650;margin:0}.important-filter{display:inline-flex;gap:3px;padding:3px;background:var(--bg);border:1px solid var(--border);border-radius:5px}.important-filter button,.important-tab-switch button{border:0;background:transparent;color:var(--muted);font:inherit;font-size:11px;cursor:pointer}.important-filter button{border-radius:3px;padding:4px 8px}.important-filter button.active{background:var(--panel);color:var(--accent)}.important-tab-switch{display:flex;gap:16px;border-bottom:1px solid var(--border)}.important-tab-switch button{display:flex;align-items:center;gap:6px;padding:7px 0 10px;border-bottom:2px solid transparent}.important-tab-switch button.active{border-color:var(--accent);color:var(--text)}.important-tab-switch b{color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums;font-weight:500}.important-empty{padding:28px 10px;color:var(--muted);text-align:center;font-size:12px}.important-list{display:flex;flex-direction:column}.important-item{display:grid;grid-template-columns:12px minmax(0,1fr);gap:5px 10px;position:relative;padding:14px 0;border-bottom:1px solid var(--border);min-width:0}.important-item:last-child{border-bottom:0}.important-time{display:flex;align-items:center;color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}.important-time time{position:absolute;top:16px;right:0}.important-time-dot{width:6px;height:6px;align-self:flex-start;margin-top:5px;border-radius:50%;background:var(--muted);opacity:.65}.important-item.is-recent .important-time-dot{background:var(--accent);opacity:1}.important-body{min-width:0}.important-asset-line{display:flex;flex-wrap:wrap;align-items:center;gap:5px 8px;padding-right:58px}.important-name{color:var(--text);font-size:13px;font-weight:600;text-decoration:none;overflow-wrap:anywhere}.important-name:hover{color:var(--accent)}.important-theme{color:var(--accent);font-size:11px;font-weight:600}.important-status{color:var(--accent);background:var(--accent-soft);border-radius:3px;padding:2px 5px;font-size:11px}.important-status.is-unverified{color:var(--warning)}.important-record-count{color:var(--muted);font-size:11px}.important-identity{display:flex;gap:8px;flex-wrap:wrap;margin-top:5px;color:var(--muted);font-size:11px}.important-contract{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.important-fact{color:var(--muted);font-size:11px;line-height:1.6;margin:5px 0 0}.important-record{margin-top:4px;color:var(--muted);font-size:11px}.important-record summary,.important-explainer summary{cursor:pointer;width:fit-content}.important-record dl{display:flex;flex-wrap:wrap;gap:8px 14px;padding-top:7px}.important-record dt{color:var(--muted)}.important-record dd{color:var(--text);margin:2px 0 0;font-variant-numeric:tabular-nums}.important-actions{grid-column:2;display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding-top:2px}.important-actions a,.important-actions button{color:var(--accent);font:inherit;font-size:11px;text-decoration:none;white-space:nowrap}.important-actions a:hover{text-decoration:underline}.important-actions a:nth-child(2),.important-actions button{color:var(--muted)}.important-actions button{border:0;background:none;padding:0;cursor:pointer}.important-actions button[aria-pressed=true]{color:var(--accent)}.important-footer{display:flex;align-items:start;justify-content:space-between;gap:16px;margin-top:12px;padding-top:10px;border-top:1px solid var(--border);color:var(--muted);font-size:11px}.important-footer>a{color:var(--accent);text-decoration:none;white-space:nowrap}.important-explainer p{margin:8px 0 0;line-height:1.6;font-size:11px}.important-changes :is(button,a,summary):focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@media(max-width:700px){.important-heading h2{font-size:17px}.important-filter button{min-height:36px;padding:5px 10px}.important-tab-switch button{font-size:12px;min-height:40px}.important-name{font-size:14px}.important-time time{font-size:11px}.important-identity,.important-status,.important-fact,.important-record{font-size:11px}.important-actions a,.important-actions button{font-size:12px;min-height:32px;display:inline-flex;align-items:center}.important-item{padding:15px 0}.important-time time{top:17px}.important-record dl{flex-direction:column}}
.is-compact{display:flex;flex-direction:column;min-height:0}.is-compact .important-tab-switch{gap:16px;flex-shrink:0}.is-compact .important-tab-switch button,.is-compact .important-tab-switch b{font-size:12px}.is-compact .important-item{display:flex;align-items:center;gap:8px;padding:7px 0;min-height:50px}.important-compact-link{display:flex;flex-direction:column;gap:4px;min-width:0;flex:1;text-decoration:none;color:var(--text)}.important-compact-top{display:flex;align-items:center;gap:7px;min-width:0}.important-compact-top>strong{font-size:13px;line-height:1.3;max-width:32%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.important-compact-top>time{margin-left:auto;white-space:nowrap;font-size:12px;font-variant-numeric:tabular-nums;color:var(--muted)}.is-compact .important-status{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:40%}.important-compact-fact{display:flex;align-items:center;gap:7px;font-size:12px;line-height:1.25;color:var(--muted);min-width:0;overflow:hidden;white-space:nowrap}.important-compact-fact>span:last-of-type{overflow:hidden;text-overflow:ellipsis}.important-compact-fact .important-theme{font-size:12px;flex-shrink:0}.important-compact-fact small{font-size:12px;flex-shrink:0;margin-left:auto}.important-compact-watch{width:28px;height:32px;padding:0;flex-shrink:0;color:var(--muted);background:none;border:0;cursor:pointer;font-size:18px;border-radius:4px}.important-compact-watch.is-following{color:var(--accent)}.important-compact-link:hover .important-compact-top>strong{color:var(--accent)}.is-compact .important-empty{padding:32px 5px;font-size:13px;line-height:1.7}.is-compact .important-item.is-added{animation:activity-arrival 1.5s ease-out}@keyframes activity-arrival{from{background:var(--accent-soft)}to{background:transparent}}
@media(max-width:700px){.is-compact .important-item:nth-child(n+4){display:none}.is-compact .important-item{min-height:58px;padding:10px 0}.important-compact-top>strong{font-size:14px}.important-compact-watch{width:36px;height:40px}.is-compact .important-tab-switch{gap:13px}.important-compact-top{gap:5px}}
@media(prefers-reduced-motion:reduce){.is-compact .important-item.is-added{animation:none;background:var(--accent-soft)}}
</style>
