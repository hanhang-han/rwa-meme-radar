import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as avatar from '../src/utils/asset-avatar.js';

test('only usable HTTPS metadata images are accepted', () => {
  assert.equal(avatar.avatarUrl({ logoUrl: 'https://cdn.example.com/coin.png' }), 'https://cdn.example.com/coin.png');
  assert.equal(avatar.avatarUrl({ tokenLogoUrl: 'https://cdn.example.com/token.svg' }), 'https://cdn.example.com/token.svg');
  for (const logoUrl of ['http://example.com/x', '//example.com/x', 'javascript:alert(1)', 'data:image/svg+xml,x', 'https://user:pass@example.com/x', {}, null, 'https://example.com/'+ 'a'.repeat(2048)]) {
    assert.equal(avatar.avatarUrl({ logoUrl }), '');
  }
});

test('fallback uses symbols, intact Chinese characters and emoji, without pretending contract fragments are names', () => {
  assert.equal(avatar.avatarLabel({ symbol: 'pons' }), 'PO');
  assert.equal(avatar.avatarLabel({ tokenSymbol: 'KUAIx' }), 'KU');
  assert.equal(avatar.avatarLabel(null, { name: '腾讯控股' }), '腾讯');
  assert.equal(avatar.avatarLabel({ name: '👩‍🚀 Coin' }), '👩‍🚀 ');
  assert.equal(avatar.avatarLabel({ name: '0xabcdefabcdef' }), '?');
  assert.equal(avatar.avatarLabel({ symbol: 'NVDAB' }, { symbol: 'NVDA' }), 'NV');
});

test('colors remain stable through quote changes, languages and contract casing', () => {
  const asset = { chainId: '56', token: '0xaBcD', name: 'Coin', price: 1 };
  assert.deepEqual(avatar.avatarColors(asset), avatar.avatarColors({ ...asset, token: '0xabcd', name: '币', price: 2 }));
  assert.deepEqual(avatar.avatarColors({ stockCode: 'NVDA' }), avatar.avatarColors(null, 'stock:NVDA'));
});

const source = compileScript(parse(readFileSync(new URL('../src/components/AssetAvatar.vue', import.meta.url), 'utf8')).descriptor, { id: 'avatar-test', inlineTemplate: true }).content
  .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (_, bindings, path) => `const {${bindings.replace(/\s+as\s+/g, ':')}}=imports[${JSON.stringify(path)}];`)
  .replace('export default', 'return');
let lookup = async () => '';
const component = new Function('imports', source)({ vue, '../utils/asset-avatar.js': avatar,
  '../utils/asset-logo-catalogue.js': { stockThemeLogo: identity => identity === 'stock:LOCAL' ? '/dashboard/assets/test.png' : '', localTokenLogo: () => '' },
  '../api/asset-logos.js': { assetLogoIdentity: asset => asset?.token || '', loadAssetLogo: key => lookup(key) } });

function mount(props) {
  const state = vue.reactive(props);
  const node = (type, text = '') => ({ type, text, children: [], parent: null, props: {}, getAttribute(key) { return this.props[key]; } });
  const detach = child => { if (child.parent) { child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null; } };
  const renderer = vue.createRenderer({
    createElement: tag => node(tag), createText: text => node('text', text), createComment: () => node('comment'),
    insert(child, parent, anchor) { detach(child); child.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; if (index < 0) parent.children.push(child); else parent.children.splice(index, 0, child); },
    remove: detach, setText: (target, text) => target.text = text, setElementText: (target, text) => { target.text = text; target.children = []; },
    patchProp: (target, key, _old, value) => target.props[key] = value,
    parentNode: target => target.parent, nextSibling: target => target.parent?.children[target.parent.children.indexOf(target)+1] ?? null,
  });
  const root = node('root'), app = renderer.createApp({ render: () => vue.h(component, state) });
  app.mount(root);
  const nodes = () => { const out = []; const visit = target => { out.push(target); target.children.forEach(visit); }; visit(root); return out; };
  return { state, nodes, image: () => nodes().find(target => target.type === 'img'), unmount: () => app.unmount() };
}

test('images are decorative and lazy; failed images fall back without changing the avatar size', async () => {
  const env = mount({ asset: { symbol: 'CT', logoUrl: 'https://cdn.example.com/ct.png' }, size: 28 });
  try {
    const image = env.image(), wrapper = env.nodes().find(target => target.props.class === 'asset-avatar');
    assert.equal(wrapper.props['aria-hidden'], 'true');
    assert.equal(image.props.alt, '');
    assert.equal(image.props.loading, 'lazy');
    assert.equal(image.props.referrerpolicy, 'no-referrer');
    assert.equal(wrapper.props.style['--avatar-size'], '28px');
    image.props.onError({ currentTarget: image });
    await vue.nextTick();
    assert.equal(env.image(), undefined);
    assert.ok(env.nodes().some(target => target.text === 'CT'));
    assert.equal(wrapper.props.style['--avatar-size'], '28px');
  } finally { env.unmount(); }
});

test('new metadata can recover an image and late errors from previous identities cannot hide it', async () => {
  const env = mount({ asset: { symbol: 'AA', logoUrl: 'https://cdn.example.com/a.png' } });
  try {
    const first = env.image();
    first.props.onError({ currentTarget: first }); await vue.nextTick();
    env.state.asset = { symbol: 'BB', logoUrl: 'https://cdn.example.com/b.png' }; await vue.nextTick();
    assert.equal(env.image().props.src, 'https://cdn.example.com/b.png');
    first.props.onError({ currentTarget: first }); await vue.nextTick();
    assert.equal(env.image().props.src, 'https://cdn.example.com/b.png');
    const second = env.image();
    env.state.asset = { ...env.state.asset, price: 42 }; await vue.nextTick();
    assert.equal(env.image(), second);
  } finally { env.unmount(); }
});


test('metadata responses follow exact identity and quote changes do not request it again', async () => {
  const resolutions = new Map(), calls = [];
  lookup = key => { calls.push(key); return new Promise(done => resolutions.set(key, done)); };
  const env = mount({ asset: { token: 'A', symbol: 'AA' } });
  try {
    env.state.asset = { token: 'B', symbol: 'BB' }; await vue.nextTick();
    resolutions.get('A')('https://cdn.example.com/a.png'); await vue.nextTick(); await vue.nextTick();
    assert.equal(env.image(), undefined);
    resolutions.get('B')('https://cdn.example.com/b.png'); await vue.nextTick(); await vue.nextTick();
    assert.equal(env.image().props.src, 'https://cdn.example.com/b.png');
    const image = env.image();
    env.state.asset = { ...env.state.asset, price: 33 }; await vue.nextTick();
    assert.deepEqual(calls, ['A', 'B']); assert.equal(env.image(), image);
  } finally { env.unmount(); lookup = async () => ''; }
});


test('common local company icons render without a metadata request', async () => {
  let calls=0; lookup=async()=>{calls++;return '';};
  const env=mount({identity:'stock:LOCAL',asset:{token:'A'}});
  try { assert.equal(env.image().props.src,'/dashboard/assets/test.png');assert.equal(calls,0); }
  finally {env.unmount();lookup=async()=>'';}
});

test('metadata failures automatically recover and unmounted avatars cancel delayed retries', async t => {
  t.mock.timers.enable({apis:['setTimeout']});
  let calls=0;lookup=async()=>++calls===1?'':'https://cdn.example.com/recovered.png';
  const env=mount({asset:{token:'A',symbol:'AA'}});
  try {
    await vue.nextTick();await vue.nextTick();assert.equal(env.image(),undefined);
    t.mock.timers.tick(2500);await vue.nextTick();await vue.nextTick();
    assert.equal(env.image().props.src,'https://cdn.example.com/recovered.png');assert.equal(calls,2);
    env.image().props.onError({currentTarget:env.image()});await vue.nextTick();env.unmount();
    t.mock.timers.tick(15000);await vue.nextTick();assert.equal(calls,2);
  } finally {env.unmount();lookup=async()=>'';t.mock.timers.reset();}
});

test('broken external images retry at most three times and then keep the fallback', async t => {
  t.mock.timers.enable({apis:['setTimeout']});lookup=async()=>'';
  const env=mount({asset:{symbol:'AA',logoUrl:'https://cdn.example.com/a.png'}});
  try {
    for(const delay of [2500,15000,45000]) {
      const img=env.image();img.props.onError({currentTarget:img});await vue.nextTick();
      assert.equal(env.image(),undefined);t.mock.timers.tick(delay);await vue.nextTick();await vue.nextTick();assert.ok(env.image());
    }
    env.image().props.onError({currentTarget:env.image()});await vue.nextTick();
    t.mock.timers.tick(600000);await vue.nextTick();assert.equal(env.image(),undefined);
  } finally {env.unmount();lookup=async()=>'';t.mock.timers.reset();}
});
