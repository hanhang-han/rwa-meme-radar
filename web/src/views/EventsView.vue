<template>
  <section class="panel">
    <div class="panel-head">
      <h2>{{ tr('发现记录', 'Discovery log') }}</h2>
      <button id="v2NewEvents" @click="loadFresh">{{ tr('获取最新记录', 'Load latest records') }}</button>
    </div>
    <p>{{ tr('历史持续保存，重复轮询不会生成重复线索。时间表示本站发现时间。', 'History persists; repeated polling does not duplicate leads. Times represent detection by this product.') }}</p>
    <div v-if="error" class="x-empty">{{ tr('历史读取失败，请重试。', 'Could not load history; retry.') }}</div>
    <div v-if="!items.length && !error" class="x-empty">{{ tr('正在读取历史…', 'Loading history…') }}</div>
    <EventRow v-for="item in items" :key="item.id" :ev="item" />
    <button v-if="hasMore" :disabled="busy" id="v2MoreEvents" @click="loadMore">{{ tr('加载更早记录', 'Load older records') }}</button>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import EventRow from '../components/EventRow.vue';
import { getEvents } from '../api/client';
import { tr } from '../i18n';

const CHAINS = ['196', '56', '4663'];
const items = ref([]);
const cursors = ref({});
const hasMore = computed(() => CHAINS.some((c) => cursors.value[c] !== null));
const busy = ref(false);
const error = ref(false);
const seen = new Set();

async function fetchRound(reset) {
  busy.value = true;
  error.value = false;
  try {
    const merged = [];
    for (const c of CHAINS) {
      const cursor = reset ? null : cursors.value[c];
      if (!reset && cursor === null) continue;
      const data = await getEvents(c, cursor);
      for (const item of data.items ?? []) {
        if (!seen.has(item.id)) {
          seen.add(item.id);
          merged.push({ ...item, chainId: item.chainId ?? c });
        }
      }
      if (reset) cursors.value[c] = data.next ?? null;
    }
    merged.sort((a, b) => (b.t ?? 0) - (a.t ?? 0));
    if (reset) items.value = merged;
    else {
      const known = new Set(items.value.map((i) => i.id));
      items.value = [...items.value, ...merged.filter((i) => !known.has(i.id))].sort((a, b) => (b.t ?? 0) - (a.t ?? 0));
    }
  } catch {
    error.value = true;
  } finally {
    busy.value = false;
  }
}

function loadFresh() {
  fetchRound(true);
}

function loadMore() {
  fetchRound(false);
}

onMounted(() => fetchRound(true));
</script>
