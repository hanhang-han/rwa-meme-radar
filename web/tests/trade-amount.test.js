import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';

const source=readFileSync(new URL('../src/components/TradeAmount.vue',import.meta.url),'utf8');
const {descriptor}=parse(source);
const {describeTradeAmount}=await import('data:text/javascript;base64,'+Buffer.from(descriptor.script.content).toString('base64'));

test('dated USD valuation wins over the separate raw native quote',()=>{
  assert.deepEqual(describeTradeAmount({volume:25.5,volumeCurrency:'USD',quoteQuantity:0.2,priceCurrency:'wNVDAx'}),
    {value:25.5,unit:'USD',usd:true});
});

test('an unvalued on-chain swap shows its native quote from either wire field',()=>{
  const expected={value:0.352,unit:'wNVDAx',usd:false};
  assert.deepEqual(describeTradeAmount({quoteQuantity:0.352,priceCurrency:'wNVDAx',volume:null}),expected);
  assert.deepEqual(describeTradeAmount({quoteVolume:0.352,priceCurrency:'wNVDAx',volumeCurrency:'wNVDAx'}),expected);
  assert.deepEqual(describeTradeAmount({volumeCurrency:'USD',volume:null,quoteQuantity:0.352,priceCurrency:'wNVDAx'}),expected);
});

test('stablecoin or unknown units do not silently become dollars',()=>{
  assert.deepEqual(describeTradeAmount({quoteQuantity:100,priceCurrency:'USDT',volumeCurrency:'USDT'}),
    {value:100,unit:'USDT',usd:false});
  assert.deepEqual(describeTradeAmount({quoteVolume:5}),{value:null,unit:null,usd:false});
  assert.deepEqual(describeTradeAmount({quoteQuantity:0,priceCurrency:'WETH'}),{value:0,unit:'WETH',usd:false});
  assert.deepEqual(describeTradeAmount({quoteQuantity:4,priceCurrency:'USD'}),{value:4,unit:'USD',usd:true});
});

test('the shared amount component compiles and labels unpriced USD clearly',()=>{
  assert.doesNotThrow(()=>compileScript(descriptor,{id:'trade-amount-test'}));
  assert.match(source,/暂无法折算为美元/);
  for(const file of ['components/HomeActivityPanel.vue','views/DetailView.vue'])
    assert.match(readFileSync(new URL(`../src/${file}`,import.meta.url),'utf8'),/<TradeAmount :trade=/);
});
