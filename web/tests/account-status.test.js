import test from 'node:test';
import assert from 'node:assert/strict';
import { telegramDeliveryState } from '../src/utils/account-status.js';
import { updateChartPalette } from '../src/utils/chart-preferences.js';
const now=1_800_000_000_000,ready={telegram:{available:true,linked:true},health:{configured:true,worker:{status:'ready',updatedAt:now},workerAgeMs:0},preferences:{newPool:true,riskChange:true},now,healthReadAt:now};
test('binding, configuration, delivery failure and stale worker evidence remain distinct',()=>{
 assert.equal(telegramDeliveryState({...ready,loading:true}),'loading');assert.equal(telegramDeliveryState({...ready,error:true}),'error');
 assert.equal(telegramDeliveryState({...ready,telegram:{available:false}}),'unavailable');assert.equal(telegramDeliveryState({...ready,telegram:{available:true,linked:false}}),'unlinked');
 assert.equal(telegramDeliveryState({...ready,preferences:null}),'rules-unconfirmed');assert.equal(telegramDeliveryState({...ready,healthError:true}),'health-error');
 assert.equal(telegramDeliveryState({...ready,now:now+61000}),'delayed');assert.equal(telegramDeliveryState({...ready,health:{configured:true,worker:{status:'unavailable',updatedAt:now}}}),'service-unavailable');
 assert.equal(telegramDeliveryState({...ready,preferences:{newPool:false,riskChange:false,largeTrade:true}}),'paused');
 assert.equal(telegramDeliveryState(ready),'ready');
});
test('changing chart colors uses existing chart and series without moving the historical viewport',()=>{
 const calls=[],range={from:10,to:30},rows=[{time:100,value:5,color:'red'}];
 const chart={applyOptions:options=>calls.push(['chart',options]),remove:()=>assert.fail('chart rebuilt'),timeScale:()=>({getVisibleLogicalRange:()=>range,fitContent:()=>assert.fail('history reset'),setVisibleLogicalRange:()=>assert.fail('viewport moved')})};
 const series=name=>({applyOptions:options=>calls.push([name,options])});const colors={up:'red',down:'green',accent:'blue',muted:'gray',surface:'white',border:'silver',grid:'black',areaTop:'pink',areaBottom:'white'};
 updateChartPalette({chart,candleSeries:series('candles'),lineSeries:series('line'),volumeSeries:{setData:data=>calls.push(['volume',data])},priceLine:series('price'),volumeData:rows},colors);
 assert.equal(calls.find(x=>x[0]==='candles')[1].upColor,'red');assert.deepEqual(calls.find(x=>x[0]==='volume')[1],rows);assert.equal(chart.timeScale().getVisibleLogicalRange(),range);
});
