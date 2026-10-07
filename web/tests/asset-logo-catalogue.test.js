import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { STOCK_LOGOS } from '../src/utils/stock-logo-catalogue.js';
import { TOKEN_LOGOS } from '../src/utils/token-logo-catalogue.js';
import { stockThemeLogo, localTokenLogo } from '../src/utils/asset-logo-catalogue.js';

test('common company themes have packaged images, without assigning them to same-named memes',()=>{
  for(const code of ['AAPL','NVDA','TSLA','MSFT','700','1024']) assert.match(stockThemeLogo('stock:'+code), /^\/dashboard\/assets\/asset-logos\/stock-/);
  for(const identity of ['NVDA','asset:NVDA','stock:__proto__','stock:SPCX']) assert.equal(stockThemeLogo(identity),'');
  assert.equal(localTokenLogo({chainId:'56',token:'0x'+'a'.repeat(40),symbol:'NVDA'}),'');
});

test('common token icons require exact chain and contract, regardless of symbol or case',()=>{
  const asset={chainId:'56',token:'0x55d398326f99059fF775485246999027B3197955',symbol:'USDT'};
  assert.match(localTokenLogo(asset),/token-56-/);
  assert.equal(localTokenLogo({...asset,chainId:'196'}),'');
  assert.equal(localTokenLogo({chainId:'56',symbol:'USDT'}),'');
  assert.equal(localTokenLogo({...asset,token:'0x'+'a'.repeat(40)}),'');
  assert.match(localTokenLogo({chainId:'196',token:'0x779ded0c9e1022225f8e0630b35a9b54be713736'}),/\.svg$/);
});

test('catalogue files actually exist as image assets, with no arbitrary paths',()=>{
  for(const filename of [...Object.values(STOCK_LOGOS),...Object.values(TOKEN_LOGOS)]) {
    assert.match(filename,/^[a-zA-Z0-9.-]+\.(png|svg)$/);
    const bytes=readFileSync(new URL('../public/assets/asset-logos/'+filename,import.meta.url));
    assert.ok(bytes.length>100&&bytes.length<256000);
    assert.ok(filename.endsWith('.png')?bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])):bytes.toString().includes('<svg'));
  }
});
