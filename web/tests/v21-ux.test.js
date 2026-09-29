import test from 'node:test';
import assert from 'node:assert/strict';
import { price, usd, money } from '../src/utils/format.js';
import { chainScope, inChainScope } from '../src/utils/chain-scope.js';
import { discoveryTime, filteredTrades, metricsForScope, topStockCards, tradeDisplayAmount } from '../src/utils/home-model.js';
import { relationLevel, relationStockIdentityStatus, volumeLiquidityRatio, riskFlags, preferredOfficialStock } from '../src/utils/product-labels.js';

test('price and USD amount formatting keep distinct units and missing values', () => {
  assert.equal(price(0.1), '$0.1000');
  assert.equal(price(0.0951), '$0.09510');
  assert.equal(price(6.09), '$6.090');
  assert.equal(price(0.0001412), '$0.0₃1412');
  assert.equal(price(1234.5), '$1,234');
  assert.equal(usd(182702504.9), '$182.70M');
  assert.equal(usd(43600), '$43.6K');
  assert.equal(usd(21.2), '$21.20');
  assert.equal(usd(null), '—');
  assert.equal(usd(0), '$0.00');
  assert.equal(money(125,'USDT'), '125 USDT');
});

test('URL chain scope is stable and does not treat all as X Layer', () => {
  assert.equal(chainScope({chain:'all'}),'all');
  assert.equal(chainScope({chain:'xlayer'}),'196');
  assert.equal(inChainScope({chainId:'56'},'all'),true);
  assert.equal(inChainScope({chainId:'56'},'196'),false);
});

test('single-pool liquidity never becomes total liquidity or risk denominator', () => {
  const asset={chainId:'196',kind:'candidate',totalLiquidityUsd:null,liquidity:9000,volume24h:400000,volumeCurrency:'USD'};
  assert.equal(volumeLiquidityRatio(asset),null);
  const scoped=metricsForScope({assets:[asset],relations:[]},'196');
  assert.equal(scoped.active,0);
  assert.equal(scoped.liquidity,0);
  assert.equal(scoped.pools,0);
  const now=Date.now();
  const comparable={...asset,totalLiquidityUsd:1000,totalLiquidityAt:now-1000,totalLiquidityStatus:'current',totalLiquidityCoverage:{scope:'token-aggregate',coverage:'provider-indexed-pools',provider:'CoinGecko'},volumeScope:'token-aggregate',fieldSources:{volume24h:'CoinGecko'},fieldTimes:{volume24h:now-2000}};
  assert.equal(volumeLiquidityRatio(comparable),400);
  assert.equal(volumeLiquidityRatio({...comparable,fieldSources:{volume24h:'OKX'}}),null);
  assert.equal(volumeLiquidityRatio({...comparable,totalLiquidityAt:now-360000}),null);
});

test('home metrics use published by-chain values when available', () => {
  const unified={metrics:{activeMemeCount:10,pairCount:5,pairLiquidityTotal:10000},metricsByChain:{'196':{activeMemeCount:2,pairCount:1,pairLiquidityTotal:1000}}};
  assert.equal(metricsForScope(unified,'all').active,10);
  assert.equal(metricsForScope(unified,'196').active,2);
  assert.equal(metricsForScope(unified,'196').liquidity,1000);
  assert.equal(metricsForScope(unified,'196').newPairs,null);
});

test('live feed filters by real comparable quote amount and selected chain', () => {
  const trades=[
    {id:'a',chainId:'56',quoteQuantity:120,volumeCurrency:'USDT',type:'buy'},
    {id:'b',chainId:'196',quoteQuantity:130,volumeCurrency:'USDT',type:'sell'},
    {id:'c',chainId:'196',quoteQuantity:1000,volumeCurrency:'WETH',type:'buy'},
    {id:'d',chainId:'196',quoteQuantity:9,volumeCurrency:'USD',type:'buy'},
  ];
  assert.equal(tradeDisplayAmount(trades[2]),null);
  assert.deepEqual(filteredTrades(trades,'196',100).map(t=>t.id),['b']);
  assert.deepEqual(filteredTrades(trades,'all',100,'buy').map(t=>t.id),['a']);
});

test('new feed distinguishes confirmed pool creation from first indexing', () => {
  const indexed = {poolCreatedAt:100,discoveredAt:200,t:300,creationTx:'0xabc'};
  assert.deepEqual(discoveryTime(indexed),{at:200,kind:'indexed'});
  const confirmed = {...indexed,creationBlock:42,confirmationStatus:'confirmed'};
  assert.deepEqual(discoveryTime(confirmed),{at:100,kind:'created'});
  assert.deepEqual(discoveryTime({...confirmed,confirmationStatus:'orphaned'}),{at:200,kind:'indexed'});
  assert.deepEqual(discoveryTime({t:300}),{at:300,kind:'indexed'});
});

test('unverified stock-side identity is not an A relation, and unknown risk is not clear', () => {
  assert.equal(relationLevel({status:'verified',issuerIdentity:{verificationStatus:'unverified'}}),null);
  assert.equal(relationLevel({status:'verified',level:null}),null);
  assert.equal(relationLevel({level:'A',status:'verified'}),'A');
  assert.deepEqual(riskFlags({riskStatus:'unknown'}),[]);
  assert.deepEqual(riskFlags({riskFlags:['wash_suspect','nonexistent']}),['wash_suspect']);
});

test('detail stock identity requires current qualified evidence on both sides', () => {
  const official = {verificationStatus:'official',eligibleForPair:true};
  const qualified = {level:'A',evidenceStatus:'qualified',sideIdentity:official,stockIdentity:official};
  assert.equal(relationStockIdentityStatus(qualified),'official');
  assert.equal(relationStockIdentityStatus({...qualified,evidenceStatus:'liquidity-stale'}),'unverified');
  assert.equal(relationStockIdentityStatus({...qualified,sideIdentity:{verificationStatus:'unverified',eligibleForPair:false}}),'unverified');
  assert.equal(relationStockIdentityStatus({...qualified,stockIdentity:null}),'unverified');
  assert.equal(relationStockIdentityStatus({...qualified,sideIdentity:{verificationStatus:'legacy',eligibleForPair:false}}),'legacy');
  assert.equal(relationStockIdentityStatus({status:'verified',issuerIdentity:official}),'unverified');
});

test('stock headline and home card use the same current official deployment', () => {
  const versions = [
    {chainId:'196',tokenContractAddress:'0xstock196',stockCode:'AAPL',price:335.3,priceCurrency:'USD',quoteAt:100,issuerIdentity:{verificationStatus:'official',eligibleForPair:true}},
    {chainId:'56',tokenContractAddress:'0xstock56',stockCode:'AAPL',price:336.1,priceCurrency:'USD',quoteAt:200,issuerIdentity:{verificationStatus:'official',eligibleForPair:true}},
    {chainId:'56',tokenContractAddress:'0xunverified',stockCode:'AAPL',price:900,priceCurrency:'USD',quoteAt:300,issuerIdentity:{verificationStatus:'unverified',eligibleForPair:false}},
    {chainId:'196',tokenContractAddress:'0xlegacy',stockCode:'AAPL',price:400,priceCurrency:'USD',quoteAt:400,issuerIdentity:{verificationStatus:'legacy',eligibleForPair:false}},
  ];
  const relations=[{chainId:'196',token:'0xmeme',ticker:'AAPL',level:'A'}];
  const assets=[{chainId:'196',token:'0xmeme',kind:'candidate',volume24h:100,volumeCurrency:'USD'}];
  assert.equal(preferredOfficialStock(versions)?.price,336.1);
  assert.equal(topStockCards(versions,relations,assets,'all')[0].stock.price,336.1);
  assert.equal(topStockCards(versions,relations,assets,'196')[0].stock.price,335.3);
  assert.equal(topStockCards(versions.filter(row=>row.issuerIdentity.verificationStatus==='unverified'),relations,assets,'all').length,0);
});

test('home stock Meme volume distinguishes missing or other currency from a real zero', () => {
  const tickers=['AAPL','AMD','MSFT'];
  const stocks=tickers.map(ticker=>({stockCode:ticker,chainId:'196',issuerIdentity:{verificationStatus:'official',eligibleForPair:true}}));
  const relations=tickers.map((ticker,index)=>({chainId:'196',token:`0xmeme${index}`,ticker,level:'A'}));
  const assets=[
    {chainId:'196',token:'0xmeme0',volume24h:null,volumeCurrency:'USD'},
    {chainId:'196',token:'0xmeme1',volume24h:0,volumeCurrency:'USD'},
    {chainId:'196',token:'0xmeme2',volume24h:500,volumeCurrency:'USDT'},
  ];
  const cards=topStockCards(stocks,relations,assets,'all');
  assert.deepEqual(cards.map(card=>[card.ticker,card.volume]),[['AMD',0],['AAPL',null],['MSFT',null]]);
  assert.equal(usd(cards[0].volume),'$0.00');
  assert.equal(usd(cards[1].volume),'—');
  assert.equal(usd(cards[2].volume),'—');
  const complete=topStockCards(stocks,[...relations,{chainId:'196',token:'0xextra',ticker:'AMD',level:'A'}],[...assets,{chainId:'196',token:'0xextra',volume24h:12.5,volumeCurrency:'USD'}],'all');
  assert.equal(complete.find(card=>card.ticker==='AMD').volume,12.5);
});
