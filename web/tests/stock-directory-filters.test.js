import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';

const flush = async () => { await vue.nextTick(); await vue.nextTick(); };

function mountFilters(initial = {}) {
  const saved = { Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot };
  globalThis.Document ??= class Document {};
  globalThis.ShadowRoot ??= class ShadowRoot {};
  const language = vue.ref('zh'), emissions = [];
  const imports = { vue, '../i18n': { tr: (zh, en) => language.value === 'en' ? en : zh } };
  const compiled = compileScript(parse(readFileSync(new URL('../src/components/StockDirectoryFilters.vue', import.meta.url), 'utf8')).descriptor, { id: 'StockDirectoryFilters', inlineTemplate: true }).content
    .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (_, bindings, path) => `const {${bindings.replace(/\s+as\s+/g, ':')}}=imports[${JSON.stringify(path)}];`)
    .replace('export default', 'return');
  const component = new Function('imports', compiled)(imports);
  const props = vue.reactive({ query: {}, busy: false, ...initial });
  const node = (type, text = '') => ({ type, text, props: {}, style: {}, children: [], parent: null, listeners: new Map(), value: '', selected: false,
    addEventListener(key, callback) { this.listeners.set(key, callback); }, removeEventListener(key) { this.listeners.delete(key); },
    getRootNode() { return {}; },
    get options() { return this.children.filter(child => child.type === 'option'); } });
  const detach = child => { if (child.parent) { const children = child.parent.children, at = children.indexOf(child); if (at >= 0) children.splice(at, 1); child.parent = null; } };
  const renderer = vue.createRenderer({ createElement: tag => node(tag), createText: text => node('text', text), createComment: () => node('comment'),
    insert: (child, parent, anchor) => { detach(child); child.parent = parent; const at = anchor ? parent.children.indexOf(anchor) : -1; if (at < 0) parent.children.push(child); else parent.children.splice(at, 0, child); },
    remove: detach, setText: (target, text) => target.text = text, setElementText: (target, text) => { target.text = text; target.children = []; },
    patchProp: (target, key, _old, value) => { target.props[key] = value; if (key === 'value') { target.value = value; target._value = value; } },
    parentNode: target => target.parent, nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] ?? null });
  const app = renderer.createApp({ render: () => vue.h(component, { ...props, onApply: patch => emissions.push(patch) }) });
  const root = node('root'); app.mount(root);
  const all = () => { const items = []; const visit = target => { items.push(target); target.children.forEach(visit); }; visit(root); return items; };
  const find = (type, name) => all().find(item => item.type === type && (!name || item.props.name === name));
  const textOf = target => target.text + target.children.map(textOf).join('');
  const update = (name, value) => {
    const target = find(name === 'q' ? 'input' : 'select', name);
    if (name === 'q') { target.value = value; target.listeners.get('input')({ target }); }
    else {
      target.options.forEach((option, index) => { option.selected = option._value === value; if (option.selected) target.selectedIndex = index; });
      target.listeners.get('change')({ target });
    }
  };
  const submit = () => find('form').props.onSubmit({ preventDefault() {} });
  const reset = () => all().find(item => item.type === 'button' && item.props.type === 'button').props.onClick();
  const selectValue = name => { const select = find('select', name); return select.options[select.selectedIndex]?._value; };
  return { props, language, emissions, all, find, textOf, update, submit, reset, selectValue, unmount: () => {
    app.unmount();
    for (const key of ['Document', 'ShadowRoot']) { if (saved[key] === undefined) delete globalThis[key]; else globalThis[key] = saved[key]; }
  } };
}

test('stock filters edit a draft and submit one patch for the main directory', async () => {
  const env = mountFilters({ query: { q: 'AAPL', catalog: 'all', sort: 'volume24h', chain: '56', mode: 'compare', page: '3' } });
  try {
    env.update('q', '  腾讯控股  '); env.update('catalog', 'paired'); env.update('sort', 'related'); await flush();
    assert.equal(env.emissions.length, 0);
    env.submit();
    assert.deepEqual(env.emissions, [{ q: '腾讯控股', catalog: 'paired', sort: 'related', page: undefined }]);
    assert.equal(env.props.query.q, 'AAPL');
    assert.equal(Object.hasOwn(env.emissions[0], 'chain'), false);
    assert.equal(Object.hasOwn(env.emissions[0], 'mode'), false);
  } finally { env.unmount(); }
});

test('all themes stay all and empty searches clear only supported filters', async () => {
  const env = mountFilters({ query: { catalog: 'paired', q: '700' } });
  try {
    env.update('catalog', 'all'); env.update('q', '  '); await flush(); env.submit();
    assert.deepEqual(env.emissions[0], { q: undefined, catalog: 'all', sort: 'related', page: undefined });
    env.reset(); await flush();
    assert.deepEqual(env.emissions[1], { q: undefined, catalog: undefined, sort: undefined, page: undefined });
    assert.equal(env.find('input', 'q').value, '');
    assert.equal(env.selectValue('catalog'), 'all');
    assert.equal(env.selectValue('sort'), 'related');
  } finally { env.unmount(); }
});

test('external route changes refresh the draft and invalid sort defaults match the directory', async () => {
  const env = mountFilters({ query: { q: 'old', sort: 'unknown', catalog: 'unknown' } });
  try {
    assert.equal(env.selectValue('sort'), 'related');
    env.update('q', 'not applied'); await flush();
    env.props.query = { q: '1024', catalog: 'paired', sort: 'ticker', chain: '196' }; await flush();
    assert.equal(env.find('input', 'q').value, '1024');
    assert.equal(env.selectValue('catalog'), 'paired');
    assert.equal(env.selectValue('sort'), 'ticker');
    env.props.query.q = 'MSFT'; await flush(); assert.equal(env.find('input', 'q').value, 'MSFT');
    assert.equal(env.emissions.length, 0);
  } finally { env.unmount(); }
});

test('loading has feedback, prevents duplicate submits, and keeps the draft editable', async () => {
  const env = mountFilters({ busy: true });
  try {
    env.update('q', 'TSLA'); await flush(); env.submit(); env.reset();
    assert.equal(env.emissions.length, 0);
    assert.equal(env.find('form').props['aria-busy'], true);
    assert.ok(env.all().some(item => item.type === 'button' && item.props.disabled && env.textOf(item) === '查询中…'));
    assert.notEqual(env.find('input', 'q').props.disabled, true);
    env.props.busy = false; await flush(); env.submit(); assert.equal(env.emissions[0].q, 'TSLA');
  } finally { env.unmount(); }
});

test('mode, page and chain navigation preserve unsubmitted filter drafts', async () => {
  const env = mountFilters({ query: { q: 'AAPL', catalog: 'all', sort: 'volume24h', mode: 'browse', page: '0', chain: 'all' } });
  try {
    env.update('q', '腾讯控股'); env.update('catalog', 'paired'); env.update('sort', 'related'); await flush();
    for (const patch of [{ mode: 'compare' }, { page: '2' }, { chain: '196' }]) {
      env.props.query = { ...env.props.query, ...patch }; await flush();
      assert.equal(env.find('input', 'q').value, '腾讯控股');
      assert.equal(env.selectValue('catalog'), 'paired');
      assert.equal(env.selectValue('sort'), 'related');
    }
    env.props.query.page = '3'; await flush();
    assert.equal(env.find('input', 'q').value, '腾讯控股');
    assert.equal(env.emissions.length, 0);
    env.submit(); assert.deepEqual(env.emissions[0], { q: '腾讯控股', catalog: 'paired', sort: 'related', page: undefined });
  } finally { env.unmount(); }
});

test('overlong searches from the route are visible, preserved and never submitted', async () => {
  const overlong = '股'.repeat(81), env = mountFilters({ query: { q: overlong } });
  try {
    const input = env.find('input', 'q');
    assert.equal(input.props.maxlength, '80');
    assert.equal(input.value, overlong);
    assert.equal(input.props['aria-invalid'], true);
    const alert = env.all().find(item => item.props.role === 'alert');
    assert.equal(input.props['aria-describedby'], alert.props.id);
    assert.equal(env.textOf(alert), '搜索内容最多 80 个字符，请缩短后再查询。');
    env.submit(); assert.equal(env.emissions.length, 0);
    env.language.value = 'en'; await flush();
    assert.equal(env.textOf(env.all().find(item => item.props.role === 'alert')), 'Use 80 characters or fewer for the search.');
    env.update('q', '股'.repeat(80)); await flush(); env.submit();
    assert.equal(env.emissions[0].q, '股'.repeat(80));
    assert.equal(env.all().some(item => item.props.role === 'alert'), false);
    assert.equal(env.find('input', 'q').props['aria-invalid'], undefined);
  } finally { env.unmount(); }
});

test('filter labels, options and actions switch language with associated form labels', async () => {
  const env = mountFilters();
  try {
    assert.ok(env.all().some(item => item.type === 'option' && env.textOf(item) === '有同池 Meme'));
    const labels = env.all().filter(item => item.type === 'label');
    for (const label of labels) assert.ok(env.all().some(item => ['input', 'select'].includes(item.type) && item.props.id === label.props.for));
    env.language.value = 'en'; await flush();
    assert.equal(env.find('form').props['aria-label'], 'Stock theme filters');
    assert.equal(env.find('input', 'q').props.placeholder, 'Company name or stock code');
    assert.ok(env.all().some(item => item.type === 'option' && env.textOf(item) === 'With paired Memes'));
    assert.ok(env.all().some(item => item.type === 'button' && env.textOf(item) === 'Apply filters'));
    assert.ok(env.all().some(item => item.type === 'button' && env.textOf(item) === 'Reset'));
  } finally { env.unmount(); }
});
