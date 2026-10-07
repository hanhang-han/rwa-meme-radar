import test from 'node:test';
import assert from 'node:assert/strict';
import { themeChartSeries } from '../src/utils/theme-chart-model.js';
import { normalizeProductPreferences, readProductPreferences, setProductPreference } from '../src/utils/product-preferences.js';
import { currentBuyRatio, currentVolumeLiquidityRatio, memeChangeObservation } from '../src/utils/meme-row-metrics.js';

test('theme chart retains observed zero, omits missing volume and limits creation markers to the window',()=>{
  const result=themeChartSeries({prices:[{t:10,value:1},{t:20,value:2}],volumes:[{t:10,volumeUsd:null},{t:20,volumeUsd:0}],events:[{t:5,pool:'before'},{t:12,pool:'inside'},{t:21,pool:'after'}]});
  assert.deepEqual(result.volumes,[{t:20,volumeUsd:0}]);
  assert.deepEqual(result.events,[{t:12,pool:'inside'}]);
  assert.equal(themeChartSeries({prices:[],volumes:[]}).start,null);
});

test('preferences preserve the other field and invalid input falls back to the system palette',()=>{
  const map=new Map(),storage={getItem:key=>map.get(key),setItem:(key,value)=>map.set(key,value)};
  assert.deepEqual(readProductPreferences(storage),{theme:'system',gainColor:'green'});
  setProductPreference('theme','dark',storage);setProductPreference('gainColor','red',storage);
  assert.deepEqual(readProductPreferences(storage),{theme:'dark',gainColor:'red'});
  assert.deepEqual(normalizeProductPreferences({theme:'invalid',gainColor:'invalid'}),{theme:'system',gainColor:'green'});
});

test('published metric evidence overrides fallback fields and stale observations stay unknown',()=>{
  const now=2_000_000_000;
  const asset={productMetrics:{buyShare24h:{value:75,at:now,status:'current'},volumeLiquidityRatio:{value:3,at:now,status:'current'},changes:{m5:{value:2,at:now,source:'direct-pool',status:'current'}}}};
  assert.equal(currentBuyRatio(asset,now),.75);
  assert.equal(currentVolumeLiquidityRatio(asset,now),3);
  assert.equal(memeChangeObservation(asset,'change5m').value,2);
  assert.equal(currentBuyRatio(asset,now+900001),null);
  asset.productMetrics.volumeLiquidityRatio.status='unknown';
  assert.equal(currentVolumeLiquidityRatio(asset,now),null);
  assert.equal(currentBuyRatio({buys24h:0,sells24h:0,fieldTimes:{buys24h:now,sells24h:now}},now),null);
  assert.equal(currentBuyRatio({buys24h:3,sells24h:1,fieldTimes:{buys24h:now,sells24h:now-61000}},now),null);
});
