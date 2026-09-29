// Build web first. Tests the production HTML when application scripts cannot load.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const html = await readFile(new URL('../dist/index.html', import.meta.url), 'utf8');
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
try {
  const page = await browser.newPage();
  await page.route('https://boot.test/**', route => route.request().resourceType() === 'document'
    ? route.fulfill({ contentType: 'text/html', body: html }) : route.abort());
  await page.goto('https://boot.test/dashboard/#/pair/196/0xstock?pool=0xpool');
  await page.getByText(/页面资源加载失败或连接较慢/).waitFor();
  await page.getByRole('button', { name: '重新加载 / Reload' }).click();
  await page.waitForURL(url => url.searchParams.has('_reload'));
  assert.equal(new URL(page.url()).hash, '#/pair/196/0xstock?pool=0xpool');
  assert.equal(await page.locator('#boot-status').isVisible(), true);
  console.log('PASS: blocked production scripts show recovery UI; retry preserves pair URL');
} finally { await browser.close(); }
