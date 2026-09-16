<template>
  <section class="panel">
    <h2>{{ tr('发现记录', 'Discovery log') }}</h2>
    <p>{{ tr('历史持续保存，重复轮询不会生成重复线索。时间表示本站发现时间。', 'History persists; repeated polling does not duplicate leads. Times represent detection by this product.') }}</p>
    <button @click="loadFresh">{{ tr('获取最新记录', 'Load latest records') }}</button>
    <div v-if="error" class="x-empty">{{ tr('历史读取失败，请重试。', 'Could not load history; retry.') }}</div>
    <div v-if="!items.length && !error" class="x-empty">{{ tr('正在读取历史…', 'Loading history…') }}</div>
    <div v-for="item in items" :key="item.id" class="x-event v2-event">
      <span class="x-event-dot"></span>
      <div>
        <span class="v2-pair-line"><b>{{ item.symbol ?? short(item.asset) }}</b><i>↔</i><b>{{ item.ticker }}</b></span>
        <p>{{ item.label }}</p>
        <small>{{ date(item.t) }} · {{ chainName({ chainId: item.chainId ?? '196' }) }} · {{ short(item.pool) }}</small>
      </div>
      <RouterLink :to="detailLink({ token: item.asset, chainId: item.chainId ?? '196' })">{{ tr('查看证据', 'View evidence') }} ↗</RouterLink>
    </div>
    <button v-if="next" :disabled="busy" @click="loadMore">{{ tr('加载更早记录', 'Load older records') }}</button>
  </section>
</template>

<script setup>
import { onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { getEvents } from '../api/client';
import { tr } from '../i18n';
import { chainName, date, detailLink, short } from '../utils/format';

const route = useRoute();
const items = ref([]);
const next = ref(null);
const busy = ref(false);
const error = ref(false);

async function fetchPage(cursor) {
  busy.value = true;
  try {
    const chain = String(route.query.chain ?? '196');
    const data = await getEvents(chain, cursor);
    if (!cursor) items.value = data.items ?? [];
    else items.value = items.value.concat(data.items ?? []);
    next.value = data.next;
    error.value = false;
  } catch {
    error.value = true;
  } finally {
    busy.value = false;
  }
}

function loadFresh() {
  fetchPage(null);
}

function loadMore() {
  fetchPage(next.value);
}

onMounted(() => fetchPage(null));
</script>
