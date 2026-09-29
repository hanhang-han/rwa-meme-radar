<template>
  <section class="panel">
    <div class="panel-head">
      <h2>{{ tr('发现记录', 'Discovery log') }}</h2>
      <button id="v2NewEvents" :disabled="busy" @click="loadFresh">{{ tr('获取最新记录', 'Load latest records') }}</button>
    </div>
    <p>{{ tr('历史持续保存，重复轮询不会生成重复线索。时间表示本站发现时间。', 'History persists; repeated polling does not duplicate leads. Times represent detection by this product.') }}</p>
    <div v-if="error" class="x-empty">{{ tr('部分或全部网络读取失败，已保留现有数据；可重试。', 'Some or all networks failed; existing data was kept. Retry when ready.') }}</div>
    <div v-if="busy && !items.length" class="x-empty">{{ tr('正在读取历史…', 'Loading history…') }}</div>
    <div v-else-if="!busy && !items.length && !error" class="x-empty">{{ tr('暂无发现记录。', 'No discovery records yet.') }}</div>
    <EventRow v-for="item in items" :key="eventKey(item)" :ev="item" />
    <button v-if="hasMore && items.length" :disabled="busy" id="v2MoreEvents" @click="loadMore">{{ tr('加载更早记录', 'Load older records') }}</button>
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import EventRow from '../components/EventRow.vue';
import { getEvents } from '../api/client';
import { tr } from '../i18n';
import { useFeedStore } from '../stores/feed';
import { applyEventPage, eventFromRelationship, eventKey, mergeEventItems } from '../utils/event-records';

const CHAINS = ['196', '56', '4663'];
const items = ref([]);
const cursors = ref({});
const hasMore = computed(() => CHAINS.some((c) => cursors.value[c] !== null));
const busy = ref(false);
const error = ref(false);
const feed = useFeedStore();

async function fetchRound(reset) {
  if (busy.value) return;
  busy.value = true;
  error.value = false;
  try {
    const requests = CHAINS.map(async (c) => {
      const cursor = reset ? null : cursors.value[c];
      if (!reset && cursor === null) return null;
      const data = await getEvents(c, cursor);
      return { chain: c, data };
    });
    const results = await Promise.allSettled(requests);
    let failed = false;
    for (const result of results) {
      if (result.status === 'rejected') {
        failed = true;
        continue;
      }
      if (!result.value) continue;
      const { chain, data } = result.value;
      const updated = applyEventPage(items.value, cursors.value, chain, data);
      items.value = updated.items;
      cursors.value = updated.cursors;
    }
    error.value = failed;
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
watch(() => feed.relationships, (rows) => {
  const incoming = rows.map(eventFromRelationship).filter(Boolean);
  if (incoming.length) items.value = mergeEventItems(items.value, incoming);
}, { deep: true, immediate: true });
</script>
