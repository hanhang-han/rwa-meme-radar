import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';

const source = readFileSync(new URL('../src/components/CandleChart.vue', import.meta.url),'utf8');
const { descriptor } = parse(source);
// The component's ordinary script exports pure status formatters. Test the
// actual functions without mounting the chart vendor or replacing time data.
const { candleDelayMs, candleDelayLabel } = await import('data:text/javascript;base64,'+Buffer.from(descriptor.script.content).toString('base64'));
const now = Date.UTC(2026,8,30,12);

test('provider delay takes priority over collector timestamps and remains bilingual', () => {
  const info = {providerDelayMs:120_000,delayMs:600_000,lastSuccessfulAt:now-3_600_000};
  assert.equal(candleDelayMs(info,'5m',now),120_000);
  assert.equal(candleDelayLabel(info,'5m',now),'行情延迟 2 分钟');
  assert.equal(candleDelayLabel(info,'5m',now,'en'),'Market data delayed 2 min');
  assert.equal(candleDelayMs({delayMs:180_000},'5m',now),180_000);
});

test('a just-successful request cannot hide old candles or prove freshness without a market timestamp', () => {
  const info = {providerDelayMs:null,delayMs:-1,lastSuccessfulAt:now-1000,lastCandleAt:now-185*60_000};
  assert.equal(candleDelayLabel(info,'5m',now),'行情延迟 180 分钟');
  assert.equal(candleDelayLabel({lastSuccessfulAt:now-1000},'5m',now),'行情暂未更新');
  assert.equal(candleDelayLabel({delayMs:20_000},'5m',now),'行情延迟不足 1 分钟');
});

test('daily and weekly opening age is not reported as source delay', () => {
  assert.equal(candleDelayMs({lastCandleAt:now-12*3_600_000},'1D',now),0);
  assert.equal(candleDelayMs({rows:[{t:now-6*86_400_000}]},'1W',now),0);
  assert.equal(candleDelayLabel({lastCandleAt:now-12*3_600_000},'1D',now),'行情暂未更新');
  assert.equal(candleDelayMs({lastCandleAt:now-17*60_000},'5m',now),12*60_000);
  assert.equal(candleDelayLabel({rows:[{t:now-17*60_000}]},'5m',now),'行情延迟 12 分钟');
});

test('missing, future, invalid, or zero timestamps never invent a numeric delay', () => {
  for(const info of [null,{}, {lastSuccessfulAt:0,lastCandleAt:0}, {lastSuccessfulAt:now+1,lastCandleAt:now+1}, {delayMs:false,lastSuccessfulAt:'bad'}, {providerDelayMs:0}]) {
    assert.equal(candleDelayLabel(info,'5m',now),'行情暂未更新');
    assert.equal(candleDelayLabel(info,'5m',now,'en'),'Market data has not updated yet');
  }
  assert.equal(candleDelayMs({lastCandleAt:now-600_000},'unsupported-bar',now),null);
});

test('the component compiles with shared pure delay helpers and preserves explicit source errors', () => {
  assert.doesNotThrow(() => compileScript(descriptor,{id:'candle-delay-test'}));
  assert.ok(!source.includes('更新受阻，当前为历史 K 线'));
  assert.ok(source.includes("candleInfo.error === 'quota-exhausted'"));
});
