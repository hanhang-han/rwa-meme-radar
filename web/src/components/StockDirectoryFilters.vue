<template>
  <form class="stock-directory-filters" role="search" :aria-label="tr('股票主题筛选', 'Stock theme filters')" :aria-busy="busy" @submit.prevent="apply">
    <label class="stock-filter-field stock-filter-search" :for="searchId">
      <span>{{ tr('搜索主题', 'Search themes') }}</span>
      <input :id="searchId" v-model="draft.q" type="search" name="q" maxlength="80" :placeholder="tr('公司名称或股票代码', 'Company name or stock code')" :aria-invalid="searchTooLong || undefined" :aria-describedby="searchTooLong ? errorId : undefined" autocomplete="off">
    </label>
    <label class="stock-filter-field" :for="catalogId">
      <span>{{ tr('关联范围', 'Relationship') }}</span>
      <select :id="catalogId" v-model="draft.catalog" name="catalog">
        <option value="all">{{ tr('全部主题', 'All themes') }}</option>
        <option value="paired">{{ tr('有同池 Meme', 'With paired Memes') }}</option>
      </select>
    </label>
    <label class="stock-filter-field" :for="sortId">
      <span>{{ tr('排序', 'Sort by') }}</span>
      <select :id="sortId" v-model="draft.sort" name="sort">
        <option value="related">{{ tr('配对池优先', 'Paired pools first') }}</option>
        <option value="volume24h">{{ tr('股票配对池 24h 成交', 'Stock-paired pool 24h volume') }}</option>
        <option value="ticker">{{ tr('股票代码', 'Stock code') }}</option>
        <option value="change24h">{{ tr('股票代币 24h 涨跌', 'Stock-token 24h change') }}</option>
      </select>
    </label>
    <div class="stock-filter-actions">
      <button type="submit" class="stock-filter-apply" :disabled="busy">{{ busy ? tr('查询中…', 'Loading…') : tr('查询', 'Apply filters') }}</button>
      <button type="button" class="stock-filter-reset" :disabled="busy" @click="reset">{{ tr('重置', 'Reset') }}</button>
    </div>
    <p v-if="searchTooLong" :id="errorId" class="stock-filter-error" role="alert">{{ tr('搜索内容最多 80 个字符，请缩短后再查询。', 'Use 80 characters or fewer for the search.') }}</p>
  </form>
</template>

<script setup>
import { computed, reactive, useId, watch } from 'vue';
import { tr } from '../i18n';

const props = defineProps({
  query: { type: Object, default: () => ({}) },
  busy: { type: Boolean, default: false },
});
const emit = defineEmits(['apply']);
const id = useId();
const searchId = `${id}-search`, catalogId = `${id}-catalog`, sortId = `${id}-sort`;
const errorId = `${id}-search-error`;
const sorts = ['volume24h', 'related', 'ticker', 'change24h'];
const draft = reactive({ q: '', catalog: 'all', sort: 'related' });
const searchTooLong = computed(() => [...draft.q.trim()].length > 80);

function syncQuery(query = {}) {
  draft.q = String(query.q ?? '');
  draft.catalog = query.catalog === 'paired' ? 'paired' : 'all';
  draft.sort = sorts.includes(query.sort) ? query.sort : 'related';
}
watch([
  () => String(props.query.q ?? ''),
  () => props.query.catalog === 'paired' ? 'paired' : 'all',
  () => sorts.includes(props.query.sort) ? props.query.sort : 'related',
], ([q, catalog, sort]) => Object.assign(draft, { q, catalog, sort }), { immediate: true });

function apply() {
  if (props.busy || searchTooLong.value) return;
  emit('apply', { q: draft.q.trim() || undefined, catalog: draft.catalog, sort: draft.sort, page: undefined });
}
function reset() {
  if (props.busy) return;
  syncQuery();
  emit('apply', { q: undefined, catalog: undefined, sort: undefined, page: undefined });
}
</script>

<style scoped>
.stock-directory-filters{display:grid;grid-template-columns:minmax(200px,1.4fr) minmax(165px,.8fr) minmax(210px,1fr) auto;align-items:end;gap:12px;padding:16px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border)}
.stock-filter-field{display:grid;gap:7px;min-width:0;color:var(--muted);font-size:12px;line-height:1.4}
.stock-filter-field input,.stock-filter-field select{box-sizing:border-box;width:100%;min-width:0;height:38px;padding:8px 11px;border:1px solid var(--border);border-radius:6px;background:var(--panel,var(--bg));color:var(--text);font:inherit;font-size:13px}
.stock-filter-field input::placeholder{color:var(--muted)}
.stock-filter-field input:focus-visible,.stock-filter-field select:focus-visible,.stock-filter-actions button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.stock-filter-actions{display:flex;gap:8px}
.stock-filter-actions button{min-height:38px;padding:8px 14px;border:1px solid var(--border);border-radius:6px;background:var(--panel,var(--bg));color:var(--text);font:inherit;font-size:13px;white-space:nowrap;cursor:pointer}
.stock-filter-actions .stock-filter-apply{border-color:var(--accent);background:var(--accent);color:var(--accent-text,#fff)}
.stock-filter-reset:hover:not(:disabled){background:var(--bg)}
.stock-filter-actions button:disabled{opacity:.6;cursor:wait}
.stock-filter-error{grid-column:1/-1;margin:0;color:var(--loss);font-size:12px;line-height:1.6}
@media(max-width:1050px){.stock-directory-filters{grid-template-columns:minmax(200px,1.4fr) minmax(165px,1fr)}.stock-filter-actions{align-self:end}}
@media(max-width:600px){.stock-directory-filters{grid-template-columns:1fr;gap:12px;padding:14px 0}.stock-filter-field input,.stock-filter-field select{height:42px;font-size:16px}.stock-filter-actions{display:grid;grid-template-columns:1fr 1fr}.stock-filter-actions button{min-height:42px}}
</style>
