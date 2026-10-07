import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as vueRouter from 'vue-router';
import * as format from '../src/utils/format.js';
import * as realtime from '../src/utils/realtime.js';
import * as presentation from '../src/utils/detail-presentation.js';
import * as themeModel from '../src/utils/stock-theme-model.js';
import * as navigation from '../src/utils/stock-navigation.js';

const token = '0x' + 'a'.repeat(40), stock = '0x' + 'b'.repeat(40), pool = '0x' + 'c'.repeat(40);
const compiled = compileScript(parse(readFileSync(new URL('../src/components/RelationshipPath.vue', import.meta.url), 'utf8')).descriptor,
  { id: 'stock-relationship-navigation', inlineTemplate: true }).content
  .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (_, bindings, path) => `const {${bindings.replace(/\s+as\s+/g, ':')}}=imports[${JSON.stringify(path)}];`)
  .replace(/^import\s+(\w+)\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (_, name, path) => `const ${name}=imports[${JSON.stringify(path)}];`)
  .replace('export default', 'return');
const flush = async () => { for (let i = 0; i < 3; i += 1) { await Promise.resolve(); await vue.nextTick(); } };

async function mountRelationship(query, relationOverrides = {}) {
  const router = vueRouter.createRouter({ history: vueRouter.createMemoryHistory(), routes: [
    '/stock', '/stock/:ticker', '/meme', '/asset/:chain/:address',
  ].map(path => ({ path, component: { render: () => null } })) });
  await router.push({ path: `/asset/196/${token}`, query });
  const language = vue.ref('zh'), relation = vue.reactive({ chainId: '196', ticker: '700', token, stock, pool, level: 'A', status: 'verified', ...relationOverrides });
  const stub = { render: () => vue.h('span') };
  const imports = { vue, 'vue-router': vueRouter,
    '../i18n': { tr: (zh, en) => language.value === 'en' ? en : zh },
    '../utils/format': format, '../utils/detail-presentation': presentation, '../utils/realtime': realtime,
    '../utils/stock-theme-model': themeModel, '../utils/stock-navigation': navigation,
    '../stores/dashboard': { useDashboardStore: () => ({ stockIndex: new Map(), assetIndex: new Map() }) },
    '../composables/useMinuteClock': { useMinuteClock: () => vue.ref(Date.now()) },
    './RelationBadge.vue': stub, './RegistryBadge.vue': stub };
  const component = new Function('imports', compiled)(imports);
  const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null });
  const detach = child => { if (child.parent) { const at = child.parent.children.indexOf(child); if (at >= 0) child.parent.children.splice(at, 1); child.parent = null; } };
  const renderer = vue.createRenderer({ createElement: tag => node(tag), createText: text => node('text', text), createComment: () => node('comment'),
    insert: (child, parent, anchor) => { detach(child); child.parent = parent; const at = anchor ? parent.children.indexOf(anchor) : -1; if (at < 0) parent.children.push(child); else parent.children.splice(at, 0, child); },
    remove: detach, setText: (target, text) => target.text = text, setElementText: (target, text) => { target.text = text; target.children = []; },
    patchProp: (target, key, _old, value) => target.props[key] = value, parentNode: target => target.parent,
    nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] ?? null });
  const app = renderer.createApp({ render: () => vue.h(component, { relation, asset: { token, name: 'MONKEY' },
    stock: { tokenContractAddress: stock, tokenName: 'Tencent token' }, chain: '196', scope: query.chain ?? 'all' }) });
  app.use(router); const root = node('root'); app.mount(root); await flush();
  const textOf = target => target.text + target.children.map(textOf).join('');
  const all = () => { const result = []; const visit = target => { result.push(target); target.children.forEach(visit); }; visit(root); return result; };
  const link = label => all().find(target => target.type === 'a' && textOf(target) === label);
  const click = async label => { const target = link(label); assert.ok(target, `link ${label}`);
    await target.props.onClick({ button: 0, defaultPrevented: false, preventDefault() {} }); await flush(); };
  return { router, language, relation, all, textOf, link, click, unmount: () => app.unmount() };
}

test('real relationship node links preserve theme, directory and pool through both token sides', async () => {
  const directory = '/stock?chain=all&q=Tencent&page=3', theme = `/stock/700?chain=all&back=${encodeURIComponent(directory)}`;
  const env = await mountRelationship({ chain: 'all', from: 'stock', ticker: '700', back: theme });
  try {
    assert.ok(env.link('腾讯控股 700'));
    await env.click('Tencent token');
    assert.equal(env.router.currentRoute.value.params.address, stock);
    assert.equal(env.router.currentRoute.value.query.back, theme);
    assert.equal(env.router.currentRoute.value.query.pool, pool);
    assert.equal(env.router.currentRoute.value.query.tab, 'overview');
    await env.click('MONKEY');
    assert.equal(env.router.currentRoute.value.params.address, token);
    assert.equal(navigation.stockDirectoryLink(env.router.currentRoute.value.query), directory);
    env.language.value = 'en'; await flush(); assert.ok(env.link('Tencent 700'));
    await env.click('Tencent 700'); assert.equal(env.router.currentRoute.value.fullPath, theme);
  } finally { env.unmount(); }
});

test('name-only relationship never adds an unverified pool or invents stock-source breadcrumbs', async () => {
  const env = await mountRelationship({ chain: '56', from: 'meme', back: '/meme?chain=56&page=2' }, { level: 'B', status: 'unverified' });
  try {
    await env.click('Tencent token');
    assert.equal(env.router.currentRoute.value.query.pool, undefined);
    assert.equal(env.router.currentRoute.value.query.from, 'meme');
    assert.equal(env.router.currentRoute.value.query.ticker, undefined);
    assert.equal(env.router.currentRoute.value.query.back, '/meme?chain=56&page=2');
    assert.equal(navigation.detailStockContext(env.router.currentRoute.value.query), null);
  } finally { env.unmount(); }
});

test('opening the current asset from its own relationship replaces the section instead of growing browser history',async()=>{
  const env=await mountRelationship({chain:'all',from:'meme',back:'/meme?chain=all&page=2',tab:'relation'});
  const history=env.router.options.history,changes=[];
  for(const method of ['push','replace']){const original=history[method];history[method]=(...args)=>{changes.push(method);return original.apply(history,args);};}
  try{
    await env.click('MONKEY');
    assert.equal(env.router.currentRoute.value.params.address,token);assert.equal(env.router.currentRoute.value.query.tab,'overview');
    assert.equal(env.router.currentRoute.value.query.pool,pool);assert.equal(env.router.currentRoute.value.query.from,'meme');
    assert.equal(env.router.currentRoute.value.query.back,'/meme?chain=all&page=2');assert.deepEqual(changes,['replace']);
  }finally{env.unmount();}
});
