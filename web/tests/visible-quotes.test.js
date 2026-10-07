import test from 'node:test';
import assert from 'node:assert/strict';
import { applyVisibleQuote, visibleQuoteMatches, visibleQuoteTokens } from '../src/utils/visible-quotes.js';
import { directoryObservation } from '../src/utils/meme-directory-presentation.js';
import { livePrice, livePercent } from '../src/utils/format.js';

const token='0x'+'a'.repeat(40),pool='0x'+'b'.repeat(40),now=Date.now();
const base=()=>({chainId:'56',token,provider:'OKX',venue:'dex',priceScope:'dex',priceCurrency:'USD',volumeCurrency:'USD',volumeScope:'token',
  price:1,volume24h:50,change24h:2,fieldTimes:{price:now-2000,volume24h:now-2000,change24h:now-2000},
  fieldSources:{price:'OKX',volume24h:'OKX',change24h:'OKX'},fieldScopes:{price:'token',volume24h:'token',change24h:'token'}});
const quote=(fields={})=>({chainId:'56',token,provider:'OKX',venue:'dex',priceScope:'token',priceCurrency:'USD',volumeCurrency:'USD',volumeScope:'token',price:1.01,
  at:now-1000,fieldTimes:{price:now-1000},timeKind:'received',...fields});

test('visible subscriptions deduplicate exact identities and never include malformed or unlimited contracts',()=>{
  const rows=Array.from({length:100},(_,i)=>({chainId:'56',token:'0x'+i.toString(16).padStart(40,'0')}));
  assert.equal(visibleQuoteTokens(rows).length,80);
  assert.deepEqual(visibleQuoteTokens([{chainId:'56',token:token.toUpperCase()},{chainId:'56',token},{token},{chainId:'0',token},{chainId:'56',token:'bad'}]),[`56:${token}`]);
});

test('only the selected chain, currency, market scope, venue and provider may update a quote',()=>{
  for(const overrides of [{chainId:'196'},{token:pool},{priceCurrency:'USDT'},{priceCurrency:null},{priceScope:'pool',poolId:pool},{venue:'binance'},{provider:'Other'},{priceScope:'unknown'}]){
    const row=base();assert.equal(applyVisibleQuote(row,quote(overrides),now),false);assert.equal(row.price,1);
  }
  assert.equal(visibleQuoteMatches(base(),quote()),true);
  const selected={...base(),priceScope:'pool',quoteType:'pool',marketId:pool,fieldScopes:{price:'pool'}};
  assert.equal(applyVisibleQuote(selected,quote({priceScope:'pool',marketId:pool}),now),true);
  assert.equal(applyVisibleQuote(selected,quote({priceScope:'pool',marketId:token}),now),false);
});

test('new price is visible through a previously published availability copy; unrelated fields keep their observations',()=>{
  const row={...base(),fieldAvailability:{price:{value:1,at:now-2000,status:'current'},volume24h:{value:50,at:now-2000,status:'current'}}};
  assert.equal(applyVisibleQuote(row,quote(),now),true);
  assert.equal(directoryObservation(row,'price',now).value,1.01);
  assert.equal(directoryObservation(row,'price',now).at,now-1000);
  assert.equal(row.fieldAvailability.volume24h.value,50);assert.equal(row.fieldTimes.volume24h,now-2000);
  assert.equal(row.priceProvenance.timeKind,'received');assert.equal(row.priceProvenance.marketAt,null);
  assert.equal(row.priceScope,'dex');
});

test('old and future observations do not override live fields, even with a larger packet revision',()=>{
  for(const time of [now-3000,now+5000]){
    const row=base();assert.equal(applyVisibleQuote(row,quote({fieldTimes:{price:time},revision:999999}),now),false);assert.equal(row.price,1);
  }
});

test('fresh price never refreshes absent, untimed, old, differently scoped or differently denominated statistics',()=>{
  for(const overrides of [{volume24h:80},{volume24h:80,statisticsAt:now-3000},{volume24h:80,statisticsAt:now-1000,volumeCurrency:'WBNB'},
    {volume24h:80,statisticsAt:now-1000,volumeScope:'pool'}]){
    const row=base();assert.equal(applyVisibleQuote(row,quote(overrides),now),true);assert.equal(row.volume24h,50);assert.equal(row.fieldTimes.volume24h,now-2000);
  }
  const row=base();assert.equal(applyVisibleQuote(row,quote({volume24h:80,change24h:2.12,statisticsAt:now-1000}),now),true);
  assert.equal(row.volume24h,80);assert.equal(row.change24h,2.12);assert.equal(row.fieldTimes.change24h,now-1000);
});

test('explicit newer null clears a price; missing and invalid values cannot become zero',()=>{
  const row={...base(),fieldAvailability:{price:{value:1,at:now-2000,status:'current'}}};
  assert.equal(applyVisibleQuote(row,quote({price:null}),now),true);
  assert.equal(directoryObservation(row,'price',now).value,null);
  for(const value of ['',NaN,Infinity,-1,0]){
    const unchanged=base();assert.equal(applyVisibleQuote(unchanged,quote({price:value}),now),false);assert.equal(unchanged.price,1);
  }
});

test('live prices retain cents and readable tiny decimals while preserving units and nulls',()=>{
  assert.equal(livePrice(4193.12),'$4,193.12');assert.notEqual(livePrice(4193.12),livePrice(4193.13));
  assert.equal(livePrice(0.000000016066),'$0.000000016066');
  assert.equal(livePrice(0.000002968,'WBNB'),'0.000002968 WBNB');
  assert.equal(livePrice(null),'—');assert.equal(livePercent(.12),'+0.12%');
});
