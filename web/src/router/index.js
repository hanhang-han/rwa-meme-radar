import { createRouter, createWebHashHistory } from 'vue-router';
import { chainScope, savedChainScope, saveChainScope } from '../utils/chain-scope';
import { createNavigationScroll } from '../utils/navigation-scroll.js';

const routes = [
  { path: '/live', name: 'live', component: () => import('../views/LiveView.vue') },
  { path: '/meme', alias: '/memes', name: 'meme', component: () => import('../views/MemeView.vue') },
  { path: '/stock', alias: '/stocks', name: 'stock', component: () => import('../views/StockView.vue') },
  { path: '/stock/:ticker', name: 'stockDetail', component: () => import('../views/StockThemeView.vue') },
  { path: '/pair', alias: '/pools', name: 'pairIndex', redirect: to => ({path:'/meme',query:{...to.query,view:'pool'}}) },
  { path: '/pair/:chain/:stock', alias: '/pools/:chain/:stock', name: 'pair', component: () => import('../views/PairView.vue'), props: true },
  { path: '/events', name: 'events', component: () => import('../views/EventsView.vue') },
  { path: '/asset/:chain/:address', name: 'detail', component: () => import('../views/DetailView.vue'), props: true },
  { path: '/detail/:chain/:address', alias: '/token/:chain/:address', redirect: to => ({path:`/asset/${to.params.chain}/${to.params.address}`,query:to.query}) },
  { path: '/watch', name: 'watch', component: () => import('../views/WatchView.vue') },
  { path: '/me', name: 'account', component: () => import('../views/AccountView.vue'), meta: { marketData:false } },
  { path: '/status', name: 'status', component: () => import('../views/StatusView.vue'), meta: { marketData:false,operator:true } },
  { path: '/developer', name: 'developer', component: () => import('../views/DeveloperView.vue'), meta: { marketData: false } },
  { path: '/', redirect: '/live' },
  { path: '/:pathMatch(.*)*', redirect: '/live' },
];

const navigationScroll = createNavigationScroll();
const router = createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior: navigationScroll.scrollBehavior,
});

router.beforeEach(navigationScroll.capture);

router.beforeEach(async (to, from) => {
  if(to.meta.operator){
    const {getDeveloperSession}=await import('../api/developer.js');
    try{const session=await getDeveloperSession();if(!session.operator)return {path:'/me'};}
    catch{return {path:'/me',query:{next:to.fullPath}};}
  }
  if (to.meta.marketData === false) return;
  if (to.query.chain == null) return { path: to.path, query: { ...to.query, chain: chainScope(from.query?.chain ? from.query : { chain: savedChainScope() }) }, hash: to.hash, replace: true };
  saveChainScope(chainScope(to.query));
});

export default router;
