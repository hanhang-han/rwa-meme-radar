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
    if (marketData && ['meme', 'watch'].includes(name)) {
      // These pages own compact reads and visible-asset subscriptions.
      stopStream();
      dash.stop();
    }
    else if (marketData) {
      // Stock pages refresh their own compact directory/theme endpoints.
      // Keep the overview stream for status and feed signals without loading
      // the complete asset catalogue beside those page-specific responses.
      dash.start(['events','pair'].includes(name)?'market':'overview');
      startStream();
    }
    else { stopStream(); dash.stop(); }
  }, { immediate: true });
});

app.mount('#app');
