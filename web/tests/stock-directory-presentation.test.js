import test from 'node:test';
import assert from 'node:assert/strict';
import { directoryCardMarket } from '../src/utils/stock-directory-presentation.js';

const now=1800000000000;
const card=()=>({stock:{price:3.76,priceCurrency:'USD',provider:'OKX',quoteAt:now-1000},
  theme:{pairedCount:0,volume:{value:null,total:0,known:0}}});

test('an existing token quote remains visible when no eligible paired pool exists',()=>{
 const row=card(),before=structuredClone(row),result=directoryCardMarket(row,now);
 assert.equal(result.usable,true);assert.equal(result.state,'current');
 assert.equal(result.poolState,'no-eligible-pools');assert.equal(result.poolValue,null);
 assert.deepEqual(row,before);
});

test('recorded pools awaiting valuations are distinct from undiscovered pools',()=>{
 const row=card();row.theme.poolCoverage={recorded:9,unknown:5,stale:4};
 const result=directoryCardMarket(row,now);
 assert.equal(result.poolState,'recorded-pending');assert.equal(result.poolRecorded,9);
 assert.deepEqual(result.pending,[{reason:'unknown',count:5},{reason:'stale',count:4}]);
 assert.equal(result.poolTotal,0);assert.equal(result.poolValue,null);
});

test('equity and token prices preserve their independent currencies and timestamps',()=>{
 const row=card();row.equity={stockPrice:29.58,referenceCurrency:'HKD',referenceProvider:'Tencent Finance',referenceAt:now-86400000,referenceRealtime:false};
 const result=directoryCardMarket(row,now);
 assert.equal(row.stock.price,3.76);assert.equal(result.equity.stockPrice,29.58);
 assert.equal(result.equity.referenceCurrency,'HKD');assert.equal(result.equity.referenceAt,now-86400000);
 assert.equal(result.state,'current');
});

test('actual zero pool turnover differs from unknown and partially covered turnover',()=>{
 const row=card();row.theme.volume={value:0,total:1,known:1};
 assert.equal(directoryCardMarket(row,now).poolState,'available');
 assert.equal(directoryCardMarket(row,now).poolValue,0);
 row.theme.volume={value:null,total:1,known:0};assert.equal(directoryCardMarket(row,now).poolState,'unavailable');
 row.theme.volume={value:20,total:3,known:1};assert.equal(directoryCardMarket(row,now).poolState,'partial');
});

test('missing price, currency and quote time never masquerade as fresh market data',()=>{
 for(const change of [{price:null},{price:0},{price:NaN},{priceCurrency:''}])assert.equal(directoryCardMarket({...card(),stock:{...card().stock,...change}},now).state,'missing');
 assert.equal(directoryCardMarket({...card(),stock:{...card().stock,quoteAt:null}},now).state,'unconfirmed');
 assert.equal(directoryCardMarket({...card(),stock:{...card().stock,quoteAt:now-900001}},now).state,'historical');
 assert.equal(directoryCardMarket({...card(),stock:{...card().stock,quoteAt:now+5000}},now).state,'unconfirmed');
 const row=card();row.equity={stockPrice:29.58,referenceAt:now,referenceCurrency:'HKD'};
 assert.equal(directoryCardMarket(row,now).equity,null);
});
