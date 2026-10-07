import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import { renderToString } from '@vue/server-renderer';
import * as signal from '../src/utils/signal-presentation.js';
import * as risk from '../src/utils/risk-presentation.js';

const now = 1_800_000_000_000;
const pool = '0x' + '1'.repeat(40), stock = '0x' + '2'.repeat(40);

async function render(name, props, language = 'zh') {
  const descriptor = parse(readFileSync(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8')).descriptor;
  const source = compileScript(descriptor, { id: name, inlineTemplate: true }).content
    .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm, (_, bindings, path) => `const {${bindings.replace(/\s+as\s+/g, ':')}}=imports[${JSON.stringify(path)}];`)
    .replace('export default', 'return');
  const imports = { vue, '../i18n': { tr: (zh, en) => language === 'en' ? en : zh, useI18n: () => ({ lang: { lang: language } }) }, '../utils/format': { date: value => 'time:' + value, age: () => 'age', usd: value => '$' + value }, '../composables/useMinuteClock': { useMinuteClock: () => vue.ref(now) }, '../utils/signal-presentation': signal, '../utils/risk-presentation': risk };
  const component = new Function('imports', source)(imports);
  return renderToString(vue.createSSRApp(component, props));
}

test('live status has only source clocks and refresh; no reading or apply gate in either language', async () => {
  const quoteAt = Date.now() - 3600000, snapshotAt = Date.now() - 7200000;
  for (const language of ['zh','en']) {
    const html = await render('LiveDataStatus', { stream:{connected:true,state:'connected'}, quoteAt, snapshotAt }, language);
    assert.match(html, new RegExp('time:' + quoteAt));
    assert.match(html, new RegExp('time:' + snapshotAt));
    assert.match(html, language==='zh' ? /最近一条报价.*1小时前/ : /Latest row quote.*1h ago/);
    assert.match(html, language==='zh' ? /列表更新.*2小时前/ : /List updated.*2h ago/);
    assert.equal((html.match(/<button/g)??[]).length,1);
    assert.match(html, language==='zh' ? /刷新/ : /Refresh/);
    assert.doesNotMatch(html,/正在阅读|恢复列表更新|名单暂不重排|Reading;|Resume list|List updates · View/);
  }
});
