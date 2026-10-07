import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as format from '../src/utils/format.js';
import * as stock from '../src/utils/stock-theme-model.js';
import * as chain from '../src/utils/chain-scope.js';
import * as query from '../src/utils/stock-directory-query.js';
import * as presentation from '../src/utils/stock-directory-presentation.js';
import * as signals from '../src/utils/stock-signal-presentation.js';
import * as refresh from '../src/utils/stock-directory-refresh.js';
import * as navigation from '../src/utils/navigation-context.js';
import * as labels from '../src/utils/theme-labels.js';
import * as visibleQuotes from '../src/utils/visible-quotes.js';

const now = Date.now(), token = text => `0x${text.repeat(40)}`;
const row = (ticker, letter, price = 10) => ({ ticker, stock: { chainId: '196', tokenContractAddress: token(letter),
  tokenSymbol: `${ticker}x`, price, priceCurrency: 'USD', priceScope: 'dex', change24h: 1, provider: 'OKX', quoteAt: now - 1000,
  fieldTimes: { price: now - 1000, change24h: now - 1000 } },
  theme: { pairedCount: 1, nameCount: 0, volume: { value: null, known: 0, total: 1 } } });
const packet = (rows = [row('700', 'a'), row('1024', 'b')]) => ({ now,
  directory: { total: rows.length, totalThemes: rows.length, topThemes: [] },
  unified: { stockThemes: rows, sectors: [] } });
const hasClass = (node, name) => String(node.props.class ?? '').split(' ').includes(name);
const quote = (price, extra = {}) => ({ chainId: '196', token: token('a'), price, priceCurrency: 'USD', priceScope: 'dex', provider: 'OKX',
  at: now, fieldTimes: { price: now, change24h: now }, ...extra });
const flush = async () => { for (let i = 0; i < 6; i++) { await Promise.resolve(); await vue.nextTick(); } };
const compiled = compileScript(parse(readFileSync(new URL('../src/views/StockView.vue', import.meta.url), 'utf8')).descriptor,
  { id: 'stock-directory-live', inlineTemplate: true }).content
  .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,
    (_, bindings, path) => `const {${bindings.replace(/\s+as\s+/g, ':')}}=imports[${JSON.stringify(path)}];`)
  .replace(/^import\s+(\w+)\s+from\s+['"]([^'"]+)['"];?\s*$/gm,
    (_, name, path) => `const ${name}=imports[${JSON.stringify(path)}];`)
  .replace('export default', 'return');

function mount(initial = packet(), initialQuery = {}) {
  const saved = Object.fromEntries(['Document', 'ShadowRoot', 'document', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'].map(key => [key, globalThis[key]]));
  const listeners = new Map(), intervals = new Map(), timers = new Map(); let timerId = 0;
  globalThis.Document = class {}; globalThis.ShadowRoot = class {};
  globalThis.document = { hidden: false, addEventListener: (event, fn) => listeners.set(event, fn), removeEventListener: event => listeners.delete(event) };
  globalThis.setTimeout = (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; };
  globalThis.clearTimeout = id => timers.delete(id);
  globalThis.setInterval = (fn, ms) => { intervals.set(++timerId, { fn, ms }); return timerId; };
  globalThis.clearInterval = id => intervals.delete(id);
  const language = vue.reactive({ lang: 'zh' }), route = vue.reactive({ query: { chain: 'all', ...initialQuery }, path: '/stock',
    get fullPath() { return '/stock?' + new URLSearchParams(this.query); } });
  let response = initial, deferred = false, quoteOptions;
  const calls = [], requests = [], replacements = [];
  const stub = (name, props = []) => ({ props, setup: (props, { slots }) => () => vue.h(name, { ...props }, slots.default?.()) });
  const liveNumber = { props: ['value', 'currency', 'format'], setup: props => () => vue.h('live-number', { ...props }, props.value == null ? '—' : String(props.value)) };
  const imports = { vue, 'vue-router': { useRoute: () => route, useRouter: () => ({
    push(location) { route.query = { ...location.query }; return Promise.resolve(); },
    replace(location) { replacements.push(location); route.query = { ...location.query }; return Promise.resolve(); },
  }) }, '../composables/useMinuteClock': { useMinuteClock: () => vue.ref(now) },
  '../composables/useVisibleQuotes': { useVisibleQuotes: options => { quoteOptions = options; return { status: vue.ref({ connected: true, state: 'live' }) }; } },
  '../i18n': { tr: (zh, en) => language.lang === 'en' ? en : zh, useI18n: () => ({ lang: language }) },
  '../utils/format': format, '../utils/chain-scope': chain, '../utils/stock-theme-model': stock,
  '../utils/theme-labels': labels, '../utils/stock-directory-query': query, '../utils/navigation-context': navigation,
  '../utils/stock-directory-presentation': presentation, '../utils/stock-signal-presentation': signals, '../utils/stock-directory-refresh': refresh,
  '../utils/visible-quotes': visibleQuotes,
  '../api/product': { peekStockDirectory: () => null, getStockDirectory(chain, options) {
    calls.push({ chain, options });
    return deferred ? new Promise((resolve, reject) => requests.push({ resolve, reject })) : Promise.resolve(structuredClone(response));
  } },
  '../components/LiveNumber.vue': liveNumber, '../components/LiveDataStatus.vue': stub('live-data-status', ['stream', 'quoteAt', 'snapshotAt']),
  '../components/SignalLegend.vue': stub('signal-legend'), '../components/StockDirectoryFilters.vue': stub('stock-filters', ['query', 'busy']), '../components/ThemeSparkline.vue': stub('sparkline', ['points']) };
  imports['../components/AssetAvatar.vue']={render:()=>null};
  const component = new Function('imports', compiled)(imports);
  const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null });
  const detach = child => { if (child.parent) { const at = child.parent.children.indexOf(child); if (at >= 0) child.parent.children.splice(at, 1); child.parent = null; } };
  const renderer = vue.createRenderer({ createElement: tag => node(tag), createText: text => node('text', text), createComment: () => node('comment'),
    insert: (child, parent, anchor) => { detach(child); child.parent = parent; const at = anchor ? parent.children.indexOf(anchor) : -1; if (at < 0) parent.children.push(child); else parent.children.splice(at, 0, child); },
    remove: detach, setText: (target, text) => target.text = text, setElementText: (target, text) => { target.text = text; target.children = []; },
    patchProp: (target, key, _old, value) => target.props[key] = value,
    parentNode: target => target.parent, nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] ?? null });
  const app = renderer.createApp(component);
  app.component('RouterLink', { props: ['to'], setup: (props, { attrs, slots }) => () => vue.h('a', { ...attrs, to: props.to }, slots.default?.()) });
  const root = node('root'); app.mount(root);
  const all = () => { const result = []; const walk = target => { result.push(target); target.children.forEach(walk); }; walk(root); return result; };
  const text = node => node.text + node.children.map(text).join('');
  const results = () => all().find(node => hasClass(node, 'stock-directory-results'));
  return { route, language, all, text, calls, requests, replacements,
    cards: () => all().filter(node => hasClass(node, 'stock-atlas-tile')),
    status: () => all().find(node => node.type === 'live-data-status'),
    result: value => response = value, defer: () => deferred = true,
    refresh: () => intervals.values().find(item => item.ms === 30000)?.fn(),
    quote: packet => quoteOptions.onQuote(packet), quoteRows: () => quoteOptions.rows.value,
    quoteEnabled: () => quoteOptions.enabled.value,
    interact: (event, payload = {}) => results().props[event]?.(payload),
    hidden: value => { document.hidden = value; listeners.get('visibilitychange')(); },
    unmount() { app.unmount(); for (const [key, value] of Object.entries(saved)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; } },
  };
}

test('visible token quote updates numbers using its actual time without fabricating equity or pool amounts', async () => {
  const env = mount(); try { await flush();
    assert.equal(env.quoteRows().length, 2); assert.equal(env.status().props.quoteAt, now - 1000);
    env.quote(quote(12, { change24h: 2 })); await flush();
    assert.match(env.text(env.cards()[0]), /12/); assert.match(env.text(env.cards()[0]), /2/);
    assert.equal(env.status().props.quoteAt, now); assert.equal(env.status().props.snapshotAt, now);
    assert.equal(env.all().some(node => hasClass(node, 'stock-atlas-equity')), false);
    assert.match(env.text(env.cards()[0]), /待获取/);
    assert.ok(env.all().some(node => node.type === 'time' && /秒前/.test(env.text(node))));
  } finally { env.unmount(); }
});

test('visible refresh immediately applies membership, ranking and amounts during pointer and keyboard interaction', async () => {
  const env = mount(); try { await flush();
    env.interact('onPointerenter', { pointerType: 'mouse' }); env.interact('onFocusin');
    const result = packet([row('1024', 'b', 14), row('9992', 'c')]);
    result.unified.stockThemes[0].theme.volume = { value: 900, known: 1, total: 1 };
    result.directory.topThemes = [...result.unified.stockThemes];
    result.directory.total = 80; result.directory.totalThemes = 100;
    env.result(result); env.refresh(); await flush();
    assert.equal(env.cards().length, 2); assert.match(env.text(env.cards()[0]), /快手/);
    assert.match(env.text(env.cards()[0]), /14/); assert.match(env.text(env.cards()[0]), /900/);
    assert.match(env.text(env.cards()[1]), /泡泡/);
    const ranking=env.all().filter(node=>hasClass(node,'stock-volume-row'));
    assert.equal(ranking.length,1); assert.match(env.text(ranking[0]),/快手/);
    assert.match(env.text(env.all().find(node=>hasClass(node,'stock-directory-meta'))),/筛选结果 80/);
    assert.equal(env.status().props.onApply, undefined); assert.equal(env.status().props.onResume, undefined);
    assert.equal(env.status().props.paused, undefined); assert.equal(env.status().props.pending, undefined);
    assert.equal(env.quoteRows()[1].tokenContractAddress, token('c'));
    const destination = env.cards()[1].props.to;
    assert.equal(destination.query.back, env.route.fullPath); assert.equal(destination.query.from, 'stock');
  } finally { env.unmount(); }
});

test('refresh applies a genuine arrival and highlights the new card', async () => {
  const env = mount(); try { await flush(); env.result(packet([row('9992', 'c'), row('700', 'a')])); env.refresh(); await flush();
    assert.match(env.text(env.cards()[0]), /泡泡/);
    assert.ok(String(env.cards()[0].props.class).includes('is-arriving'));
  } finally { env.unmount(); }
});

test('late responses from a former filter cannot replace the currently selected page', async () => {
  const env = mount(); try { await flush(); env.defer(); env.route.query.q = 'old'; await flush();
    env.route.query.q = 'new'; await flush(); assert.equal(env.requests.length, 2);
    env.requests[1].resolve(packet([row('9992', 'c')])); await flush();
    env.requests[0].resolve(packet([row('700', 'a')])); await flush();
    assert.equal(env.cards().length, 1); assert.match(env.text(env.cards()[0]), /泡泡/);
    assert.equal(env.quoteRows()[0].tokenContractAddress, token('c'));
  } finally { env.unmount(); }
});

test('background tabs stop subscriptions and polls, discard an in-flight publication and reload on return', async () => {
  const env = mount(); try { await flush(); env.defer(); env.refresh(); const calls = env.calls.length;
    env.hidden(true); await flush(); assert.equal(env.quoteEnabled(), false);
    env.requests[0].resolve(packet([row('9992', 'c')])); await flush(); env.refresh();
    assert.equal(env.calls.length, calls); assert.match(env.text(env.cards()[0]), /腾讯/);
    env.hidden(false); await flush(); assert.equal(env.quoteEnabled(), true); assert.equal(env.calls.length, calls + 1);
    env.requests[1].resolve(packet([row('9992', 'c')])); await flush(); assert.match(env.text(env.cards()[0]), /泡泡/);
  } finally { env.unmount(); }
});

test('a quote received while the directory is loading survives its older HTTP observation', async () => {
  const env = mount(); try { await flush(); env.defer(); env.refresh();
    env.quote(quote(20)); await flush();
    env.requests[0].resolve(packet()); await flush();
    assert.equal(env.quoteRows()[0].price, 20); assert.equal(env.status().props.quoteAt, now);
    env.language.lang = 'en'; await flush(); assert.ok(env.all().some(node => node.type === 'time' && /s ago/.test(env.text(node))));
  } finally { env.unmount(); }
});

test('touch and wheel interaction do not delay list arrivals or live quotes',async()=>{
 const env=mount();try{await flush();
  env.interact('onTouchstartPassive');env.interact('onTouchmovePassive');env.interact('onWheelPassive');
  env.result(packet([row('1024','b',14),row('9992','c')]));env.refresh();await flush();
  assert.match(env.text(env.cards()[0]),/快手/);assert.match(env.text(env.cards()[1]),/泡泡/);
  assert.equal(env.quote(quote(12,{token:token('b')})),true);await flush();assert.match(env.text(env.cards()[0]),/12/);
  assert.equal(env.status().props.onApply,undefined);assert.equal(env.status().props.onResume,undefined);
 }finally{env.unmount();}
});

test('cards keep the same comparison rows, separate name clues and expose the legend outside links', async () => {
  const result=packet();result.unified.stockThemes[0].theme.nameCount=3;
  result.unified.stockThemes[0].equity={stockPrice:90,referenceCurrency:'HKD',referenceProvider:'Exchange',referenceAt:now};
  const env=mount(result);try{await flush();
    const coverage=env.cards().map(card=>card.children.find(node=>hasClass(node,'stock-atlas-coverage')));
    assert.deepEqual(coverage.map(group=>group.children.filter(node=>node.type==='div').map(row=>env.text(row.children.find(node=>node.type==='dt')))),
      [['同池 Meme','配对池成交 24h USD','成交池覆盖'],['同池 Meme','配对池成交 24h USD','成交池覆盖']]);
    assert.match(env.text(env.cards()[0]),/名称线索 3 · 未确认同池/);assert.match(env.text(env.cards()[0]),/正股参考价90/);
    const legend=env.all().find(node=>node.type==='signal-legend');assert.ok(legend);
    for(let ancestor=legend.parent;ancestor;ancestor=ancestor.parent)assert.notEqual(ancestor.type,'a');
    env.language.lang='en';await flush();assert.match(env.text(env.cards()[0]),/Name clues 3 · Shared pool unconfirmed/);assert.match(env.text(env.cards()[0]),/Underlying reference90/);
  }finally{env.unmount();}
});

test('unknown directory fields stay unavailable in cards and comparison mode, while recorded stale pools retain their evidence', async () => {
  const missing=row('700','a');missing.stock.price=0;missing.stock.change24h='';
  missing.theme={nameCount:2,volume:{},poolCoverage:{recorded:1,stale:1}};
  const env=mount(packet([missing]));try{await flush();
    const card=env.cards()[0];assert.match(env.text(card),/报价待更新/);assert.match(env.text(card),/同池 Meme—/);assert.match(env.text(card),/成交池覆盖— \/ —/);
    assert.match(env.text(card),/池已记录 1/);assert.match(env.text(card),/估值待更新/);assert.doesNotMatch(env.text(card),/关联失效|安全/);
    const count=env.calls.length;env.route.query.mode='compare';await flush();assert.equal(env.calls.length,count);
    const table=env.all().find(node=>hasClass(node,'stock-index-table'));assert.equal(table.props.tabindex,'0');
    const body=env.all().find(node=>node.type==='tbody');const values=body.children.find(node=>node.type==='tr').children.filter(node=>node.type==='td');
    assert.match(env.text(values[1]),/^—/);assert.match(env.text(values[2]),/^—/);assert.match(env.text(values[3]),/^—/);
    assert.match(env.text(values[6]),/— \/ —/);assert.match(env.text(values[7]),/尚未取得有效报价/);
    env.language.lang='en';await flush();assert.match(env.text(values[6]),/Pools recorded 1 · Valuation update pending/);
  }finally{env.unmount();}
});

test('stale or unknown directory statistics retain their counts and say snapshot pairs even after a fresh streamed quote', async () => {
  const snapshot=packet([row('700','a')]);snapshot.now=now-43*60000;
  snapshot.directory.pairedThemeCount=27;snapshot.unified.stockThemes[0].theme.pairedCount=32;
  const env=mount(snapshot);try{await flush();
    const summary=()=>env.all().find(node=>hasClass(node,'stock-paired-count'));
    const observed=()=>env.all().find(node=>hasClass(node,'stock-statistics-time'));
    assert.match(env.text(summary()),/快照同池 27 个主题/);assert.ok(env.text(observed()).includes(format.date(snapshot.now)));
    assert.equal(observed().children.find(node=>node.type==='time').props.datetime,new Date(snapshot.now).toISOString());
    env.quote(quote(20));await flush();assert.match(env.text(summary()),/快照同池 27 个主题/);assert.equal(env.status().props.quoteAt,now);
    const calls=env.calls.length;env.route.query.mode='compare';await flush();assert.equal(env.calls.length,calls);
    const mobile=()=>env.all().find(node=>hasClass(node,'stock-index-card'));
    assert.match(env.text(mobile()),/快照同池 32/);assert.doesNotMatch(env.text(mobile()),/当前同池/);
    env.language.lang='en';await flush();assert.match(env.text(summary()),/Snapshot pairs 27 themes/);assert.match(env.text(mobile()),/Snapshot pairs 32/);
    assert.doesNotMatch(env.text(mobile()),/Paired now/);
    env.result({...snapshot,now:null});env.refresh();await flush();assert.match(env.text(summary()),/Snapshot pairs 27 themes/);assert.match(env.text(observed()),/Time unconfirmed/);
    env.result({...snapshot,now});env.refresh();await flush();assert.match(env.text(summary()),/Paired now 27 themes/);assert.match(env.text(mobile()),/Paired now 32/);
  }finally{env.unmount();}
});
