import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';

const source = readFileSync(new URL('../src/components/CandleChart.vue', import.meta.url),'utf8');
const { descriptor } = parse(source);
// The component's ordinary script exports pure status formatters. Test the
// actual functions without mounting the chart vendor or replacing time data.
const { candleDelayMs, candleDelayLabel, shouldOfferNativePool, candleClosePoints, candleVolumeField, candleVolumeData } = await import('data:text/javascript;base64,'+Buffer.from(descriptor.script.content).toString('base64'));
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

test('a delayed USD chart offers an explicit native-pool switch, never a silent unit change', () => {
  const pool={poolId:'0x'+'a'.repeat(40),priceCurrency:'wNVDAx'};
  const delayed={priceCurrency:'USD',lastCandleAt:now-16*60_000};
  assert.equal(shouldOfferNativePool(delayed,'5m',now,'',pool),true);
  assert.equal(shouldOfferNativePool({...delayed,lastCandleAt:now-14*60_000},'5m',now,'',pool),false);
  assert.equal(shouldOfferNativePool({...delayed,priceCurrency:'wNVDAx'},'5m',now,'',pool),false);
  assert.equal(shouldOfferNativePool(delayed,'5m',now,pool.poolId,pool),false);
  assert.equal(shouldOfferNativePool(delayed,'5m',now,'',null),false);
});

test('line mode can use real candle closes without inventing zero points from missing data', () => {
  assert.deepEqual(candleClosePoints([
    {t:3000,c:3},{t:1000,c:1},{t:2000,c:null},{t:3000,c:4},{t:4000,c:''},
  ]),[{time:1,value:1},{time:3,value:4}]);
  assert.deepEqual(candleClosePoints([{t:1000,c:1}]),[{time:1,value:1}]);
});

test('volume bars retain measured zero, skip missing volume, and use one unit across the chart', () => {
  const colors={upVolume:'green',downVolume:'red'};
  const rows=[{t:1000,o:1,c:2,v:99},{t:2000,o:2,c:1,vu:0},{t:3000,o:1,c:2,vu:null,v:null},{t:4000,o:1,c:2,vu:3}];
  assert.equal(candleVolumeField(rows),'vu');
  assert.deepEqual(candleVolumeData(rows,colors),[
    {time:2,value:0,color:'red'}, {time:4,value:3,color:'green'},
  ]);
  assert.deepEqual(candleVolumeData([{t:1000,vu:null,v:''}],colors),[]);
  assert.deepEqual(candleVolumeData([{t:1000,v:0,o:1,c:1}],colors),[{time:1,value:0,color:'green'}]);
});

test('comparison chart omits missing dots while retaining observed zero', async () => {
  const comparisonSource=readFileSync(new URL('../src/components/ComparisonChart.vue', import.meta.url),'utf8');
  const comparisonScript=parse(comparisonSource).descriptor.script.content;
  const {observedComparisonRows}=await import('data:text/javascript;base64,'+Buffer.from(comparisonScript).toString('base64'));
  const rows=[{at:1000,value:null},{at:2000,value:0},{at:3000,value:2},{at:4000,value:NaN},{at:NaN,value:3}];
  assert.deepEqual(observedComparisonRows(rows,'value'),[rows[1],rows[2]]);
});

test('the component compiles with shared pure delay helpers and preserves explicit source errors', () => {
  assert.doesNotThrow(() => compileScript(descriptor,{id:'candle-delay-test'}));
  assert.ok(!source.includes('更新受阻，当前为历史 K 线'));
  assert.ok(source.includes("candleInfo?.error === 'quota-exhausted'"));
});
