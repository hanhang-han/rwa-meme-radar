import {test} from 'node:test';
import assert from 'node:assert/strict';
import {correlation,heat,basketIndex,captureRate} from '../src/lib/analytics';
const now=Date.now();
const series=(fn:(i:number)=>number)=>Array.from({length:20},(_,i)=>({t:now-(20-i)*300000,v:fn(i)}));
test('correlation aligns timestamps, rejects sparse and constant samples',()=>{
  assert.equal(correlation(series(i=>i+1),series(i=>2*i+10),now).value,1);
  assert.equal(correlation(series(i=>i+1),series(i=>40-i),now).value,-1);
  assert.equal(correlation(series(()=>1),series(i=>i+1),now).value,null);
  assert.equal(correlation(series(i=>i+1).slice(0,5),series(i=>i+1),now).value,null);
  assert.equal(correlation(series(i=>i+1),series(i=>i+1).map(p=>({...p,t:p.t-86400000})),now).value,null);
});
test('missing heat component is not silently treated as zero',()=>{
  assert.equal(heat(1,null,1000000).value,null);
  assert.equal(heat(1,0.2,1000000).value,100);
  assert.equal(heat(-1,-1,0).value,0);
});
test('basket uses fixed base capitalization weights',()=>{
  assert.equal(basketIndex([{baseCap:75,basePrice:10,price:20},{baseCap:25,basePrice:10,price:10}]).value,175);
  assert.equal(basketIndex([]).value,null);
  assert.equal(basketIndex([{baseCap:1,basePrice:0,price:1}]).value,null);
});
test('capture rate requires complete and consistent denominator',()=>{
  assert.equal(captureRate(38,100).value,38);
  assert.equal(captureRate(38,null).value,null);
  assert.equal(captureRate(101,100).value,null);
  assert.equal(captureRate(0,100).value,0);
});
