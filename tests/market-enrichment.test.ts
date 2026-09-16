import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  enrichAsset, enrichRelation, enrichStock, enrichmentState, normalizeBlockscout,
  normalizeCoinGeckoInfo, normalizeCoinGeckoPools, normalizeCoinGeckoTokens, normalizeDexScreener,
  normalizeEodQuotes, normalizeEodSearch,
} from '../src/lib/market-enrichment';

const token='0x'+'a'.repeat(40),pool='0x'+'b'.repeat(40),stock='0x'+'c'.repeat(40),at=2_000_000;

test('DEX and CoinGecko observations keep exact chain identities, zero activity and negative changes',()=>{
  const dex=normalizeDexScreener('56',[{pairAddress:pool,baseToken:{address:token},priceUsd:'2',marketCap:'1000',priceChange:{h24:'-5.2'},volume:{h24:'0'},liquidity:{usd:'500'},txns:{h24:{buys:0,sells:0}}}],at);
  assert.equal(dex.assets[`56:${token}`].price,2);
  assert.equal(dex.assets[`56:${token}`].txs24h,0);
  assert.equal(dex.assets[`56:${token}`].change24h,-5.2);
  assert.equal(dex.pools[`56:${pool}`].liquidityUsd,500);

  const gecko=normalizeCoinGeckoTokens('196',{data:[{id:`x-layer_${token}`,attributes:{address:token,price_usd:'3',market_cap_usd:null,total_reserve_in_usd:'700',volume_usd:{h24:'12'}},relationships:{top_pools:{data:[{id:`x-layer_${pool}`}]}}}],included:[{id:`x-layer_${pool}`,attributes:{address:pool,reserve_in_usd:'1400',volume_usd:{h24:'11'},price_change_percentage:{h24:'-1'},transactions:{h24:{buys:2,sells:3}}}}]},at+1);
  assert.equal(gecko.assets[`196:${token}`].volume24h,12);
  assert.equal(gecko.assets[`196:${token}`].txs24h,5);
  assert.equal(gecko.pools[`196:${pool}`].liquidityUsd,1400);
  const pools=normalizeCoinGeckoPools('4663',{data:[{id:`robinhood_${pool}`,attributes:{reserve_in_usd:'900',transactions:{h24:{buys:1,sells:4}}}}]},at);
  assert.equal(pools[`4663:${pool}`].txs24h,5);
});

test('enrichment fills only newer or missing fields and preserves per-field provenance',()=>{
  enrichmentState.assets[`56:${token}`]={CoinGecko:{provider:'CoinGecko',updatedAt:at,price:2,marketCap:100,volume24h:50,buys24h:null,sells24h:null,txs24h:null,holders:null,liquidity:20,change24h:null},DexScreener:{provider:'DexScreener',updatedAt:at,price:2.1,marketCap:110,volume24h:48,buys24h:4,sells24h:5,txs24h:9,holders:null,liquidity:22,change24h:-2}};
  const row=enrichAsset({chainId:'56',token,provider:'OKX',price:1,volume24h:null,fieldTimes:{price:at+10}});
  assert.equal(row.price,1);
  assert.equal(row.volume24h,50);
  assert.equal(row.fieldSources.volume24h,'CoinGecko');
  assert.equal(row.buys24h,4);
  assert.equal(row.fieldSources.buys24h,'DexScreener');
  assert.deepEqual(new Set(row.providers),new Set(['OKX','CoinGecko','DexScreener']));

  enrichmentState.pools[`56:${pool}`]={CoinGecko:{provider:'CoinGecko',updatedAt:at,liquidityUsd:800,volume24h:4,buys24h:1,sells24h:2,txs24h:3}};
  const relation=enrichRelation({chainId:'56',pool,liquidityUsd:null,liquidityAt:1});
  assert.equal(relation.liquidityUsd,800);assert.equal(relation.liquidityProvider,'CoinGecko');assert.equal(relation.poolMarket.txs24h,3);
});

test('Blockscout and EODHD normalizers do not turn missing values into zero and EOD quotes remain timestamped',()=>{
  assert.deepEqual(normalizeBlockscout({holders_count:'0',transfers_count:'12'},at),{provider:'Blockscout',updatedAt:at,holders:0,transfers:12});
  const holder=normalizeCoinGeckoInfo({data:{attributes:{holders:{count:25,distribution_percentage:{top_10:'61.2'},last_updated:'1970-01-01T00:33:19.000Z'}}}},at)!;
  assert.equal(holder.holders,25);assert.equal(holder.holderTop10,61.2);assert.equal(holder.fieldTimes?.holders,1_999_000);
  const resolution=normalizeEodSearch('AAPL','AAPL',[{Code:'AAPL',Exchange:'BA',Name:'Apple DRC',Type:'Common Stock',isPrimary:false},{Code:'AAPL',Exchange:'US',Name:'Apple Inc.',Type:'Common Stock',Currency:'USD',isPrimary:true}],at)!;
  assert.equal(resolution.symbol,'AAPL.US');
  const quotes=normalizeEodQuotes({code:'AAPL.US',close:'200',timestamp:'1900',volume:'10',change_p:'-1.5'},{AAPL:resolution},at+5);
  assert.equal(quotes.AAPL.marketAt,1_900_000);assert.equal(quotes.AAPL.change24h,-1.5);
  enrichmentState.resolutions.AAPL=resolution;enrichmentState.stocks.AAPL=quotes.AAPL;
  const enriched=enrichStock({assetId:`4663:${stock}`,stockCode:'AAPL',tokenSymbol:'AAPL',stockIdentity:{id:'AAPL',code:'AAPL',market:null,nameEn:null,status:'provider-code'},stockPrice:null});
  assert.equal(enriched.stockPrice,200);assert.equal(enriched.referenceProvider,'EODHD');assert.equal(enriched.stockIdentity.nameEn,'Apple Inc.');
});
