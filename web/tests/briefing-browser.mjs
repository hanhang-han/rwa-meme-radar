import assert from 'node:assert/strict';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({headless:true,channel:'chrome'});
try {
  const page = await browser.newPage();
  const errors=[]; page.on('pageerror', e=>errors.push(e.message));
  await page.route(/\/api\/(dashboard|feed|stream|ai\/briefing)(\?|$)/, async route => {
    const u=new URL(route.request().url());
    if(u.pathname.endsWith('/ai/briefing')) {
      const en=u.searchParams.get('lang')==='en';
      await new Promise(r=>setTimeout(r,en?30:700));
      return route.fulfill({json:{text:en?'English saved report':'已保存的中文简报',at:Date.now(),cached:true,status:'scheduled'}});
    }
    return route.fulfill({json:u.pathname.endsWith('/dashboard')?{unified:{assets:[],relations:[],stockTokens:[],metrics:{}}}:{}});
  });
  await page.goto('http://127.0.0.1:5178/#/live');
  await page.getByText('正在读取简报…',{exact:true}).waitFor();
  assert.equal(await page.getByText('简报生成中…',{exact:true}).count(),0);
  await page.getByRole('button',{name:'EN',exact:true}).click();
  await page.getByText('English saved report',{exact:true}).waitFor();
  await page.waitForTimeout(900);
  assert.equal(await page.getByText('已保存的中文简报',{exact:true}).count(),0);
  assert.deepEqual(errors,[]);
  console.log('PASS: read-only loading copy and late language response ignored');
} finally {await browser.close();}
