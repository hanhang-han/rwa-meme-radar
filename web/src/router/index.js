import { createRouter, createWebHashHistory } from 'vue-router';
import { chainScope, savedChainScope, saveChainScope } from '../utils/chain-scope';

const routes = [
  { path: '/live', name: 'live', component: () => import('../views/LiveView.vue') },
  { path: '/meme', alias: '/memes', name: 'meme', component: () => import('../views/MemeView.vue') },
  { path: '/stock', alias: '/stocks', name: 'stock', component: () => import('../views/StockView.vue') },
  { path: '/stock/:ticker', name: 'stockDetail', component: () => import('../views/StockView.vue') },
  { path: '/pair', alias: '/pools', name: 'pairIndex', component: () => import('../views/PairView.vue') },
  { path: '/pair/:chain/:stock', alias: '/pools/:chain/:stock', name: 'pair', component: () => import('../views/PairView.vue'), props: true },
  { path: '/events', name: 'events', component: () => import('../views/EventsView.vue') },
  { path: '/detail/:chain/:address', alias: '/token/:chain/:address', name: 'detail', component: () => import('../views/DetailView.vue'), props: true },
  { path: '/status', name: 'status', component: () => import('../views/StatusView.vue') },
  { path: '/developer', name: 'developer', component: () => import('../views/DeveloperView.vue'), meta: { marketData: false } },
  { path: '/', redirect: '/live' },
  { path: '/:pathMatch(.*)*', redirect: '/live' },
];

const router = createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
});

router.beforeEach((to, from) => {
  if (to.meta.marketData === false) return;
  if (to.query.chain == null) return { path: to.path, query: { ...to.query, chain: chainScope(from.query?.chain ? from.query : { chain: savedChainScope() }) }, hash: to.hash, replace: true };
  saveChainScope(chainScope(to.query));
});

export default router;
