import { createApp, watch } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import router from './router';
import { useDashboardStore } from './stores/dashboard';
import { startStream, stopStream } from './composables/useStream';
import './styles/main.css';
import {useAccountStore} from './stores/account';

const app = createApp(App);
app.use(createPinia());
app.use(router);

const dash = useDashboardStore();
router.isReady().then(() => {
  useAccountStore().start();
  watch(() => [router.currentRoute.value.meta.marketData !== false,router.currentRoute.value.name], ([marketData,name]) => {
    if (marketData) { dash.start(['meme','stock','stockDetail','watch','events','pair'].includes(name)?'market':'overview'); startStream(); }
    else { stopStream(); dash.stop(); }
  }, { immediate: true });
});

app.mount('#app');
