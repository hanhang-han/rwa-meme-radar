import { createApp, watch } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import router from './router';
import { useDashboardStore } from './stores/dashboard';
import { startStream, stopStream } from './composables/useStream';
import './styles/main.css';

const app = createApp(App);
app.use(createPinia());
app.use(router);

const dash = useDashboardStore();
router.isReady().then(() => {
  watch(() => router.currentRoute.value.meta.marketData !== false, (marketData) => {
    if (marketData) { dash.start(); startStream(); }
    else { stopStream(); dash.stop(); }
  }, { immediate: true });
});

app.mount('#app');
