<template>
  <section class="panel important-changes" aria-labelledby="important-changes-title">
    <div class="important-heading">
      <div>
        <span class="important-eyebrow">{{ tr('核验事实与站内观察分开看', 'Verified facts and site observations, separately') }}</span>
        <h2 id="important-changes-title">{{ tr('重要变化', 'Important changes') }}</h2>
        <p>{{ tr('先呈现新核验的股票配对关系；活动放量等指标在采样完整后再加入。', 'Showing newly verified stock pairs. Activity spikes will follow once complete sampling is available.') }}</p>
      </div>
      <div class="important-filter" role="group" :aria-label="tr('变化筛选', 'Change filter')">
        <button type="button" :class="{active:filter==='all'}" :aria-pressed="filter==='all'" @click="filter='all'">{{ tr('全部变化', 'All changes') }}</button>
        <button type="button" :class="{active:filter==='watched'}" :aria-pressed="filter==='watched'" @click="filter='watched'">{{ tr('我关注的', 'Following') }}</button>
      </div>
    </div>

    <div class="important-tab-switch" role="group" :aria-label="tr('记录类型', 'Record type')">
      <button type="button" :class="{active:viewMode==='verified'}" :aria-pressed="viewMode==='verified'" @click="chooseFeed('verified')">{{ tr('已核验变化 · 24h', 'Verified changes · 24h') }} <b>{{ events.length }}</b></button>
      <button type="button" :class="{active:viewMode==='observed'}" :aria-pressed="viewMode==='observed'" @click="chooseFeed('observed')">{{ tr('最近观察 · 未核验', 'Recent observations · unverified') }} <b>{{ recentRows.length }}</b></button>
    </div>

    <p v-if="viewMode==='verified' && changes?.version==='compat-full-snapshot'" class="important-compat">
      {{ tr('当前使用完整快照与已有事件构建变化流；没有明确核验时间时，只显示本站收录时间。', 'This feed uses the full snapshot and existing events. If verification time is unavailable, only the site discovery time is shown.') }}
    </p>
    <p v-if="viewMode==='observed'" class="important-observed-note">{{ tr('下面是本站已保存的发现与池观察，按记录时间排序。它们不构成 A 级股票配对，时间也不表示新池刚创建。', 'These saved discoveries and pool observations are ordered by record time. They are not grade-A stock pairs, and their timestamps do not imply a newly created pool.') }}</p>
    <div v-if="loading" class="important-empty" role="status">{{ tr('正在读取变化记录…', 'Loading change records…') }}</div>
    <div v-else-if="!displayed.length" class="important-empty" role="status">
      {{ viewMode==='observed'
        ? (filter==='watched' ? tr('关注的资产尚无可展示的观察记录。', 'No observation records for followed assets.') : tr('暂无可展示的发现或池观察。', 'No discovery or pool observations are available.'))
        : (filter==='watched' ? tr('最近 24 小时，你关注的资产没有已核验变化。', 'No verified changes for followed assets in the last 24 hours.') : tr('最近 24 小时暂无可核验的新变化。', 'No new verifiable changes in the last 24 hours.')) }}
    </div>
    <div v-else-if="viewMode==='verified'" class="important-list" aria-live="polite">
      <article v-for="item in displayed" :key="item.id" class="important-item" :class="{'is-recent':recent(item)}">
        <div class="important-time">
          <span class="important-time-dot" aria-hidden="true"></span>
          <time :datetime="iso(eventTime(item))" :title="date(eventTime(item))">{{ item.type==='relation-verified' ? tr('核验', 'Verified') : tr('收录', 'Indexed') }} {{ relativeTime(item) }}</time>
        </div>
        <div class="important-body">
          <div class="important-identity">
            <span class="important-theme">{{ item.ticker }} {{ tr('主题', 'theme') }}</span>
            <span>{{ chainName(item) }}</span>
            <span class="important-contract">{{ short(item.token) }}</span>
          </div>
          <RouterLink :to="detailPath(item)" class="important-name">{{ assetName(item) }}</RouterLink>
          <p class="important-fact">{{ item.type==='relation-verified'
            ? tr('本站核验了股票代币与该 Meme 的链上配对池。', 'The site verified an onchain pool pairing a stock token with this meme.')
            : tr('本站收录了一条当前仍有效的已核验股票配对关系。', 'The site indexed a stock-pair relation that is still verified.') }}</p>
          <div class="important-times">
            <span>{{ tr('池创建记录', 'Pool creation record') }} <b>{{ date(item.occurredAt) }}</b></span>
            <span>{{ tr('本站收录', 'Site indexed') }} <b>{{ date(item.discoveredAt) }}</b></span>
            <span>{{ tr('本站核验', 'Site verified') }} <b>{{ date(item.verifiedAt) }}</b></span>
          </div>
          <div class="important-actions">
            <RouterLink v-if="item.relation?.stock && item.relation?.pool" :to="pairPath(item)">{{ tr('查看配对证据', 'View pair evidence') }} →</RouterLink>
            <a v-if="item.relation?.pool" :href="explorer(item.relation.pool,'address',String(item.chainId))" target="_blank" rel="noopener">{{ tr('链上池', 'Onchain pool') }} ↗</a>
            <button type="button" @click="toggleWatch(item)">{{ watched.has(itemKey(item)) ? tr('★ 已关注', '★ Following') : tr('☆ 关注', '☆ Follow') }}</button>
          </div>
        </div>
      </article>
    </div>
    <div v-else class="important-list" aria-live="polite">
      <article v-for="item in displayed" :key="item.id" class="important-item observation-item" :class="{'is-recent':recent(item)}">
        <div class="important-time">
          <span class="important-time-dot" aria-hidden="true"></span>
          <time :datetime="iso(item.at)" :title="date(item.at)">{{ item.kind==='pair-observed' ? tr('池观察', 'Pool observed') : tr('发现', 'Discovered') }} {{ relativeTime(item) }}</time>
        </div>
        <div class="important-body">
          <div class="important-identity">
            <span class="observation-label">{{ item.kind==='pair-observed' ? tr('池观察 · 未核验', 'Pool observed · unverified') : tr('本站发现 · 待核验', 'Site discovery · unverified') }}</span>
            <span v-if="item.ticker">{{ item.ticker }} {{ tr('线索', 'clue') }}</span>
            <span>{{ chainName(item) }}</span>
            <span class="important-contract">{{ short(item.token) }}</span>
          </div>
          <RouterLink :to="detailPath(item)" class="important-name">{{ assetName(item) }}</RouterLink>
          <p class="important-fact">{{ observationFact(item) }}</p>
          <div class="important-times">
            <span>{{ tr('本站记录', 'Site recorded') }} <b>{{ date(item.at) }}</b></span>
            <span v-if="item.poolCreatedAt">{{ tr('来源记录的池创建', 'Reported pool creation') }} <b>{{ date(item.poolCreatedAt) }}</b></span>
            <span v-if="item.discoveredAt">{{ tr('本站发现池', 'Site discovered pool') }} <b>{{ date(item.discoveredAt) }}</b></span>
          </div>
          <div class="important-actions">
            <RouterLink :to="detailPath(item)">{{ tr('查看资产详情', 'View asset details') }} →</RouterLink>
            <a v-if="item.pool" :href="explorer(item.pool,'address',String(item.chainId))" target="_blank" rel="noopener">{{ tr('查看池地址', 'View pool address') }} ↗</a>
            <button type="button" @click="toggleWatch(item)">{{ watched.has(itemKey(item)) ? tr('★ 已关注', '★ Following') : tr('☆ 关注', '☆ Follow') }}</button>
          </div>
        </div>
      </article>
    </div>
    <p class="important-note">{{ viewMode==='verified'
      ? tr('核验表示本站确认池中配对关系，不代表上市公司授权，也不代表 Meme 价格与原股有关。', 'Verification confirms the pool pairing on this site; it does not imply issuer endorsement or that meme prices follow the stock.')
      : tr('名称命中、发现记录和池观察都不是投资信号，也不会计入已核验股票配对。', 'Name matches, discovery records and pool observations are not investment signals or verified stock pairs.') }}</p>
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
  if (item.kind === 'pair-observed') return item.currentlyVerified
    ? tr('记录当时只是池观察；当前关系已升级为 A 级，请打开详情核对最新证据。', 'This was only a pool observation when recorded. The relation is now grade A; open details for current evidence.')
    : tr('本站记录了一个链上池，但股票身份或流动性尚未满足 A 级核验条件。', 'The site recorded an onchain pool, but stock identity or liquidity has not met grade-A verification conditions.');
  if (item.keyword) return tr(`本站发现名称或代号命中“${item.keyword}”；它只是线索，股票关系待核验。`,
    `The name or symbol matched “${item.keyword}”; this is a clue only, and the stock relationship is unverified.`);
  return tr('本站发现一个新候选资产，股票关系待核验。', 'The site discovered a new candidate; any stock relationship remains unverified.');
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
.important-changes{padding:22px 24px;background:linear-gradient(145deg,#152129,#111a22 75%);border-color:#2e3c44}
.important-heading{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:16px}
.important-eyebrow{color:#b9fa6a;font-size:10px;letter-spacing:.12em;text-transform:uppercase}
.important-heading h2{color:#edf3f4;font-size:23px;margin:4px 0 3px}
.important-heading p{color:#9aadb7;font-size:12px;line-height:1.5}
.important-filter{display:inline-flex;flex:none;gap:3px;padding:3px;background:#101920;border:1px solid #314049;border-radius:9px}
.important-filter button{border:0;border-radius:6px;padding:7px 11px;background:transparent;color:#a8b7be;font:inherit;font-size:11px;cursor:pointer}
.important-filter button.active{background:#30443d;color:#ebf9ef}
.important-tab-switch{display:inline-flex;flex-wrap:wrap;gap:5px;padding:3px;margin-bottom:14px;border:1px solid #314049;border-radius:9px;background:#101920}
.important-tab-switch button{display:flex;align-items:center;gap:8px;border:0;border-radius:6px;padding:8px 11px;background:transparent;color:#a8b7be;font:inherit;font-size:11px;cursor:pointer}
.important-tab-switch button.active{background:#30443d;color:#ebf9ef}.important-tab-switch b{color:#b9fa6a;font-size:10px}
.important-compat{color:#d9cfaa;background:#574c2229;border:1px solid #6c6b3d;border-radius:8px;padding:9px 12px;margin-bottom:13px;font-size:11px;line-height:1.5}
.important-observed-note{color:#dccda7;background:#4c40252b;border:1px solid #685a3d;border-radius:8px;padding:9px 12px;margin-bottom:13px;font-size:11px;line-height:1.5}
.important-empty{padding:32px 12px;color:#aab9c6;text-align:center;font-size:12px}
.important-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.important-item{display:grid;grid-template-columns:90px minmax(0,1fr);min-width:0;gap:11px;border:1px solid #293b44;border-radius:12px;background:#16232c;padding:14px}
.important-item.is-recent{border-color:#91d29c;box-shadow:inset 2px 0 #b9fa6a}
.observation-item.is-recent{border-color:#ab9366;box-shadow:inset 2px 0 #e3c878}.observation-item .important-time-dot{background:#e3c878}.observation-label{color:#e3c878;font-weight:700}
.important-time{display:flex;align-items:flex-start;gap:6px;color:#a5b5bd;font-size:10px;line-height:1.4;padding-top:2px}
.important-time-dot{width:6px;height:6px;flex:none;margin-top:4px;border-radius:50%;background:#b9fa6a}
.important-body{min-width:0}.important-identity{display:flex;flex-wrap:wrap;gap:3px 8px;align-items:center;color:#90a3af;font-size:10px}
.important-theme{color:#b9fa6a;font-weight:700}.important-contract{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.important-name{display:inline-block;color:#edf5f2;font-weight:700;font-size:15px;margin:6px 0 3px;text-decoration:none;overflow-wrap:anywhere}
.important-fact{color:#c0cdd0;font-size:12px;line-height:1.55;margin:3px 0 8px}
.important-times{display:flex;flex-wrap:wrap;gap:5px 12px;padding-top:7px;border-top:1px solid #2b3b43;color:#91a5ae;font-size:10px;line-height:1.5}
.important-times b{font-weight:500;color:#c1d0d2}.important-actions{display:flex;align-items:center;flex-wrap:wrap;gap:7px 15px;margin-top:10px}
.important-actions a,.important-actions button{color:#b9fa6a;font:inherit;font-size:11px;text-decoration:none}.important-actions button{border:0;background:none;padding:0;cursor:pointer}
.important-note{color:#91a4ad;font-size:10px;line-height:1.5;margin:12px 0 0}
.important-changes button:focus-visible,.important-changes a:focus-visible{outline:2px solid #b9fa6a;outline-offset:3px}
@media(max-width:900px){.important-list{grid-template-columns:1fr}}
@media(max-width:600px){.important-changes{padding:16px}.important-heading{align-items:flex-start;flex-direction:column;gap:10px}.important-item{grid-template-columns:72px minmax(0,1fr);gap:7px;padding:12px}.important-times{flex-direction:column;gap:2px}.important-tab-switch{display:flex}.important-tab-switch button{flex:1;justify-content:center;padding:8px 5px}}
</style>
