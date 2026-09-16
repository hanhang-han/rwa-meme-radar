import { createApp } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import router from './router';
import { useDashboardStore } from './stores/dashboard';
import { startStream } from './composables/useStream';
import './styles/main.css';

const app = createApp(App);
app.use(createPinia());
app.use(router);

const dash = useDashboardStore();
dash.start();
startStream();

app.mount('#app');
