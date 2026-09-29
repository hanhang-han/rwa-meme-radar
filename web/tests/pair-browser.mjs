// Run against the Vite dev server with PAIR_TEST_URL and PLAYWRIGHT_MODULE set.
import assert from 'node:assert/strict';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => { errors.push(e.message); console.error(e.message); });
  const stock = '0xstock';
  const relations = ['a', 'b'].map(id => ({ id, chainId: '196', stock, token: `0xmeme${id}`, pool: `0xpool${id}`, ticker: '700', status: 'verified', liquidityUsd: 53, liquidityAt: 1000 }));
  let stockFails = false;
  await page.route(/\/api\/(dashboard|token|stream|feed)(\/|\?|$)/, async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/dashboard')) {
      await new Promise(r => setTimeout(r, 700)); // Detail resolves before dashboard.
      return route.fulfill({ json: { unified: { relations, assets: [], stockTokens: [] } } });
    }
    if (path.includes('/token/')) {
      const token = path.split('/').at(-1);
      if (token === stock && stockFails) return route.fulfill({ status: 503, json: {} });
      return route.fulfill({ json: { asset: { token, symbol: 'TCENTx', price: 53 }, pools: token === stock ? [] : [{ pool: `0xdistribution${token.slice(-1)}`, protocol: 'Uniswap', liquidityUsd: 12 }] } });
    }
    return route.fulfill({ json: {} });
  });
  const url = `${process.env.PAIR_TEST_URL || 'http://127.0.0.1:5178'}/#/pair/196/${stock}?pool=0xpoola`;
  await page.goto(url);
  await page.getByText('流动性池分布', { exact: true }).waitFor({timeout:10000}).catch(async e => { console.error(await page.locator('body').innerText()); throw e; });
  assert.match(await page.locator('.v2-pool').innerText(), /Uniswap/);
  assert.match(await page.locator('.v2-pool a').getAttribute('href'), /xlayer\/address\/0xdistributiona/);
  await page.locator('aside a').nth(1).click();
  await page.waitForFunction(() => document.querySelector('.v2-pool a')?.href.includes('0xdistributionb'));
  assert.match(page.url(), /#\/pair\/196\/0xstock\?pool=0xpoolb/);
  stockFails = true;
  await page.reload();
  await page.getByRole('alert').filter({ hasText: '资产详情暂时无法读取' }).waitFor();
  await page.locator('.x-proof').first().waitFor();
  assert.equal(await page.locator('.x-proof').count(), 2);
  stockFails = false;
  await page.getByRole('alert').getByRole('button', { name: '重试' }).click();
  await page.getByText('TCENTx', { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log('PASS: delayed snapshot, pool rendering, hash navigation, detail failure and retry');
} finally { await browser.close(); }
