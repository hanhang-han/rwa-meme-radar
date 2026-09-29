import assert from 'node:assert/strict';
import { test } from 'node:test';
import { canonicalStockCode, officialStockIdentity, stockIdentity, stockTokenManifest } from '../src/lib/stock-identity';
import { buildTickerSet, matchStock } from '../src/lib/stocks';

const native='0xc845b2894dbddd03858fd2d643b4ef725fe0849d';
const current='0xa8ddb5cd96b5222afe198316e9a57caa642850d5';

test('official issuer identity is pinned to chain and address, not a matching ticker',()=>{
  assert.equal(stockTokenManifest.entries,4592);
  const a=officialStockIdentity('196',native),b=officialStockIdentity('196',current);
  assert.equal(a.verificationStatus,'official');
  assert.equal(a.tokenKind,'native');
  assert.equal(b.tokenKind,'wrapper-current');
  assert.equal(a.underlyingId,b.underlyingId);
  assert.equal(officialStockIdentity('4663',native,'NVDA').verificationStatus,'unverified');
  assert.equal(officialStockIdentity('196','0x'+'9'.repeat(40),'NVDA').verificationStatus,'unverified');
  const exposed=stockIdentity({chainId:'196',tokenContractAddress:native,stockCode:'WRONG',tokenSymbol:'NVDAx'});
  assert.equal(exposed.code,'NVDA');
  assert.equal(exposed.verificationStatus,'official');
  assert.equal(stockIdentity({chainId:'196',tokenContractAddress:'0x'+'9'.repeat(40),stockCode:'NVDA'}).verificationStatus,'unverified');
});

test('CRDAx catalogue code follows the issuer deployment and preserves the OKX report',()=>{
  const address='0x71eed272e83fba6194ec22af39bb50040f4a528b';
  for(const chainId of ['196','56']){
    const issuer=officialStockIdentity(chainId,address,'CRDA');
    assert.equal(issuer.verificationStatus,'official');
    assert.equal(issuer.underlyingId,'xstocks:fe08dada-99dd-4d75-b580-07c63b1eeabd');
    assert.equal(issuer.ticker,'CRDAL');
    assert.equal(issuer.tokenSymbol,'CRDAx');
    assert.equal(issuer.underlyingIsin,'GB00BJFFLV09');
    assert.deepEqual(canonicalStockCode({chainId,tokenContractAddress:address,
      stockCode:'CRDA',tokenSymbol:'CRDAx'}),{
      stockCode:'CRDAL',reportedStockCode:'CRDA',stockCodeSource:'official-xstocks-manifest',
      stockCodeSourceUrl:issuer.sourceUrl,
    });
  }
  for(const row of [
    {chainId:'4663',tokenContractAddress:address,stockCode:'CRDA',tokenSymbol:'CRDAx'},
    {chainId:'196',tokenContractAddress:'0x'+'9'.repeat(40),stockCode:'CRDA',tokenSymbol:'CRDAx'},
    {chainId:'196',tokenContractAddress:address,stockCode:'CRDA',tokenSymbol:'OTHER'},
    {chainId:'196',tokenContractAddress:address,stockCode:'OTHER',tokenSymbol:'CRDAx'},
  ])assert.equal(canonicalStockCode(row).stockCode,row.stockCode);
});

test('B level name match uses curated complete terms with no crypto/ticker fuzz',()=>{
  const tickers=buildTickerSet(['BTC','MSTR','TSLA']);
  assert.equal(matchStock('TSLA','Tesla rocket',tickers)?.level,'B');
  assert.equal(matchStock('TSLA','Tesla rocket',tickers)?.ticker,'TSLA');
  assert.equal(matchStock('x','拉布布币',tickers)?.ticker,'9992');
  assert.equal(matchStock('TSLA','拉布布币',tickers)?.ticker,'TSLA');
  assert.equal(matchStock('TSLACAT','xTeslaCat',tickers),null);
  assert.equal(matchStock('BTC','Bitcoin',tickers),null);
  assert.equal(matchStock('XBTCX','Crypto',tickers),null);
});
