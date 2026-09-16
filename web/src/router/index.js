import { createRouter, createWebHashHistory } from 'vue-router';

const routes = [
  { path: '/live', name: 'live', component: () => import('../views/LiveView.vue') },
  { path: '/meme', name: 'meme', component: () => import('../views/MemeView.vue') },
  { path: '/stock', name: 'stock', component: () => import('../views/StockView.vue') },
  { path: '/pair', name: 'pairIndex', component: () => import('../views/PairView.vue') },
  { path: '/pair/:chain/:stock', name: 'pair', component: () => import('../views/PairView.vue'), props: true },
  { path: '/events', name: 'events', component: () => import('../views/EventsView.vue') },
  { path: '/detail/:chain/:address', name: 'detail', component: () => import('../views/DetailView.vue'), props: true },
  { path: '/', redirect: '/live' },
  { path: '/:pathMatch(.*)*', redirect: '/live' },
];

export default createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
});
