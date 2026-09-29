import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  enrichAsset, enrichRelation, enrichStock, enrichmentState, GECKO_REQUESTS_PER_MINUTE, normalizeBlockscout,
  normalizeCoinGeckoInfo, normalizeCoinGeckoPools, normalizeCoinGeckoTokens, normalizeDexScreener,
  normalizeEodQuotes, normalizeEodSearch, refreshCoinGeckoEnrichment, retryAfterMs, enrichmentSources,
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

test('Dex pair endpoint object preserves native market evidence and unknown one-sided transaction totals',()=>{
  const quote='0x'+'d'.repeat(40);
  const result=normalizeDexScreener('56',{pairs:[{pairAddress:pool,baseToken:{address:token,symbol:'MEME'},quoteToken:{address:quote,symbol:'USDT'},priceNative:'2.5',priceUsd:'2.49',txns:{h24:{buys:0}}}]},at);
  assert.equal(result.pools[`56:${pool}`].priceNative,2.5);
  assert.equal(result.pools[`56:${pool}`].quoteToken?.address,quote);
  assert.equal(result.pools[`56:${pool}`].marketAt,null);
  assert.equal(result.assets[`56:${token}`].fieldTimes?.price,at);
  assert.equal(result.assets[`56:${token}`].txs24h,null);
  assert.equal(result.assets[`56:${token}`].priceProvenance?.dependenciesComplete,false);
});

test('newer Dex single-pool liquidity stays separate from source-reported token aggregate',()=>{
  const asset='0x'+'e'.repeat(40),id=`56:${asset}`,now=Date.now();
  const gecko=normalizeCoinGeckoTokens('56',{data:[{attributes:{address:asset,total_reserve_in_usd:'800',volume_usd:{h24:'16000'}}}]},now-60_000).assets[id];
  const dex=normalizeDexScreener('56',[{pairAddress:pool,baseToken:{address:asset},liquidity:{usd:'20'},volume:{h24:'900000'}}],now).assets[id];
  enrichmentState.assets[id]={CoinGecko:gecko,DexScreener:dex};
  const row=enrichAsset({chainId:'56',token:asset,liquidity:20,fieldTimes:{liquidity:now},fieldScopes:{liquidity:`pool:${pool}`}});
  assert.equal(row.totalLiquidityUsd,800);
  assert.equal(row.totalLiquidityStatus,'current');
  assert.equal(row.totalLiquidityCoverage.coverage,'provider-indexed-pools');
  assert.equal(row.liquidity,800);
  assert.equal(row.fieldScopes.liquidity,'token-aggregate');
});

test('legacy foreign ticker caches cannot survive identity quarantine',async()=>{
  const {quarantineEodCache}=await import('../src/lib/market-enrichment');
  const invalid={id:'SLV',symbol:'SLV.WAR',code:'SLV',exchange:'WAR',currency:'PLN',name:'Selvita',status:'exact' as const,updatedAt:at};
  enrichmentState.resolutions.SLV=invalid;
  assert.equal(normalizeEodSearch('SLV','SLV',[{Code:'SLV',Exchange:'WAR',Currency:'PLN',Type:'Common Stock',Name:'Selvita'}]),null);
  assert.equal(normalizeEodSearch('QQQ','QQQ',[{Code:'QQQ',Exchange:'BA',Currency:'ARS',Type:'ETF',Name:'Invesco'}]),null);
  assert.equal(normalizeEodSearch('UNKNOWN','UNKNOWN',[{Code:'UNKNOWN',Exchange:'US',Currency:'USD',Type:'Common Stock'}]),null);
  quarantineEodCache(at+1);
  assert.equal(enrichmentState.resolutions.SLV,undefined);
  assert.equal(enrichmentState.quarantined.SLV.resolution?.symbol,'SLV.WAR');
});

test('holder-only updates and out-of-order prices never advance or regress price field time',async()=>{
  const {putAsset}=await import('../src/lib/market-enrichment');
  const id=`196:${token}`;
  const original=normalizeCoinGeckoTokens('196',{data:[{attributes:{address:token,price_usd:'3'}}]},at).assets[id];
  putAsset('196',token,original);
  const holder=normalizeCoinGeckoInfo({data:{attributes:{holders:{count:25}}}},at+10000)!;
  putAsset('196',token,holder);
  assert.equal(enrichmentState.assets[id].CoinGecko.fieldTimes?.price,at);
  assert.equal(enrichmentState.assets[id].CoinGecko.fieldTimes?.holders,at+10000);
  putAsset('196',token,normalizeCoinGeckoTokens('196',{data:[{attributes:{address:token,price_usd:'1'}}]},at-100).assets[id]);
  assert.equal(enrichmentState.assets[id].CoinGecko.price,3);
  assert.equal(enrichmentState.assets[id].CoinGecko.fieldTimes?.price,at);
  const legacyId=`4663:${token}`;
  enrichmentState.assets[legacyId]={CoinGecko:{...original,fieldTimes:undefined}};
  putAsset('4663',token,holder);
  const row=enrichAsset({chainId:'4663',token,price:null});
  assert.equal(row.price,3);
  assert.equal(row.fieldTimes.price,null);
  assert.equal(row.fieldTimeKinds.price,'unknown');
});

test('EOD adapter uses actual free account quota without making paid intraday calls',async()=>{
  const {refreshEodhd}=await import('../src/lib/market-enrichment');
  const oldToken=process.env.EODHD_API_TOKEN,oldLimit=process.env.EODHD_DAILY_SYMBOL_LIMIT,oldFetch=globalThis.fetch;
  enrichmentState.eodEntitlement=undefined;
  enrichmentState.eodUsage={day:new Date().toISOString().slice(0,10),symbols:0};
  process.env.EODHD_API_TOKEN='test-not-a-real-key';process.env.EODHD_DAILY_SYMBOL_LIMIT='100';
  const calls:string[]=[];
  globalThis.fetch=(async(input:any)=>{calls.push(String(input).split('?')[0]);return new Response(JSON.stringify({subscriptionType:'free',dailyRateLimit:20,apiRequests:20,apiRequestsDate:new Date().toISOString().slice(0,10),email:'must-not-be-saved@example.com'}),{headers:{'content-type':'application/json'}});}) as any;
  try{await refreshEodhd([{stockCode:'SLV',tokenSymbol:'SLVx'}]);
    assert.deepEqual(calls,['https://eodhd.com/api/user']);
    assert.equal(enrichmentState.providers.EODHD.status,'quota-exhausted');
    assert.equal(enrichmentState.eodEntitlement?.dailyRateLimit,20);
    assert.equal(JSON.stringify(enrichmentState.eodEntitlement).includes('example.com'),false);
    enrichmentState.eodUsage.symbols=0;
    await refreshEodhd([{stockCode:'SLV'}]);
    assert.equal(enrichmentState.providers.EODHD.status,'entitlement-required');
    assert.equal(calls.length,1);
  }finally{globalThis.fetch=oldFetch;if(oldToken===undefined)delete process.env.EODHD_API_TOKEN;else process.env.EODHD_API_TOKEN=oldToken;if(oldLimit===undefined)delete process.env.EODHD_DAILY_SYMBOL_LIMIT;else process.env.EODHD_DAILY_SYMBOL_LIMIT=oldLimit;}
});

test('EOD entitlement check caches by observation day while retaining provider billing day',async()=>{
  const {refreshEodEntitlement}=await import('../src/lib/market-enrichment');
  const oldFetch=globalThis.fetch,oldEntitlement=enrichmentState.eodEntitlement,oldUsage=enrichmentState.eodUsage;
  const checkedAt=Date.parse('2026-09-25T11:00:00Z'),calls:string[]=[];
  enrichmentState.eodEntitlement=undefined;
  enrichmentState.eodUsage={day:'2026-09-25',symbols:3};
  globalThis.fetch=(async(input:any)=>{calls.push(String(input).split('?')[0]);return new Response(JSON.stringify({subscriptionType:'free',dailyRateLimit:20,apiRequests:20,apiRequestsDate:'2026-09-24'}),{headers:{'content-type':'application/json'}});}) as any;
  try{
    const first=await refreshEodEntitlement('test-not-a-real-key',checkedAt);
    const second=await refreshEodEntitlement('test-not-a-real-key',checkedAt+300000);
    assert.equal(second,first);
    assert.equal(calls.length,1);
    assert.equal(first.day,'2026-09-24');
    assert.equal(first.checkedAt,checkedAt);
    assert.equal(first.realtimeAllowed,false);
    assert.equal(enrichmentState.eodUsage.symbols,3);
    const next=await refreshEodEntitlement('test-not-a-real-key',Date.parse('2026-09-26T00:00:00Z'));
    assert.equal(calls.length,2);
    assert.equal(next.day,'2026-09-24');
    assert.equal(next.checkedAt,Date.parse('2026-09-26T00:00:00Z'));
    assert.equal(enrichmentState.eodUsage.symbols,3);
    assert.ok(calls.every(url=>url==='https://eodhd.com/api/user'));
  }finally{globalThis.fetch=oldFetch;enrichmentState.eodEntitlement=oldEntitlement;enrichmentState.eodUsage=oldUsage;}
});

test('public GeckoTerminal round keeps token and pool calls ahead of holder info',async()=>{
  const previousKey=process.env.COINGECKO_API_KEY,previousFetch=globalThis.fetch;
  delete process.env.COINGECKO_API_KEY;
  const calls:string[]=[];
  const chains=['196','56','4663'];
  const unsupported='0x'+'d'.repeat(40);
  const assets=[...chains.map(chain=>({chainId:chain,token})),{chainId:'196',token:unsupported}];
  const relations=chains.map(chain=>({chainId:chain,pool,status:'verified'}));
  globalThis.fetch=(async(input:any)=>{
    const url=String(input);calls.push(url);
    const data=url.includes('/tokens/multi/')?[{attributes:{address:token,price_usd:url.includes('/x-layer/')?null:'1',total_reserve_in_usd:'1001'}}]:[{attributes:{address:pool,reserve_in_usd:'1001'}}];
    return new Response(JSON.stringify({data}),{headers:{'content-type':'application/json'}});
  }) as any;
  try{
    await refreshCoinGeckoEnrichment({assets,relations});
    assert.equal(calls.filter(url=>url.includes('/tokens/multi/')).length,3);
    assert.equal(calls.filter(url=>url.includes('/pools/multi/')).length,3);
    assert.equal(calls.filter(url=>url.includes('/info')).length,0);
    assert.equal(enrichmentState.providers.CoinGecko.status,'partial');
    assert.equal(enrichmentState.attempts[`CoinGecko:asset:196:${token}`]?.reason,null,
      'liquidity without a price still needs its normal refresh turn');
    assert.ok((enrichmentState.attempts[`CoinGecko:asset:196:${token}`]?.nextRetryAt??0)-Date.now()<=300_000);
    assert.equal(enrichmentState.attempts[`CoinGecko:asset:196:${unsupported}`]?.reason,'no-verified-market-or-liquidity');
    assert.ok((enrichmentState.attempts[`CoinGecko:asset:196:${unsupported}`]?.nextRetryAt??0)-Date.now()>23*60*60_000);
  }finally{
    globalThis.fetch=previousFetch;
    if(previousKey===undefined)delete process.env.COINGECKO_API_KEY;else process.env.COINGECKO_API_KEY=previousKey;
  }
});

test('CoinGecko rotates a full token batch on the next minute without repeating recently sampled assets',async()=>{
  const oldNow=Date.now,oldFetch=globalThis.fetch,oldKey=process.env.COINGECKO_API_KEY,oldInfo=process.env.COINGECKO_INFO_ROUND_LIMIT;
  let now=2_100_000_000_000;
  const addr=(n:number)=>'0x'+n.toString(16).padStart(40,'0');
  const assets=['196','56','4663'].flatMap((chain,index)=>Array.from({length:31},(_,n)=>({chainId:chain,token:addr(30000+index*100+n)})));
  const called:string[]=[];
  Date.now=()=>now;
  process.env.COINGECKO_API_KEY='test-not-a-real-key';process.env.COINGECKO_INFO_ROUND_LIMIT='1';
  globalThis.fetch=(async(input:any)=>{
    const url=String(input);called.push(url);
    const addresses=url.split('/tokens/multi/')[1]?.split('?')[0]?.split(',')??[];
    return new Response(JSON.stringify({data:addresses.map(address=>({attributes:{address,price_usd:'1',total_reserve_in_usd:'1001'}}))}),
      {headers:{'content-type':'application/json'}});
  }) as any;
  try{
    await refreshCoinGeckoEnrichment({assets,relations:[]});
    const first=called.filter(url=>url.includes('/tokens/multi/'));
    assert.equal(first.length,3);
    assert.ok(first.every(url=>url.split('/tokens/multi/')[1]?.split('?')[0]?.split(',').length===30));
    now+=60_000;
    await refreshCoinGeckoEnrichment({assets,relations:[]});
    const second=called.filter(url=>url.includes('/tokens/multi/')).slice(3);
    assert.equal(second.length,3);
    assert.ok(second.every(url=>url.split('/tokens/multi/')[1]?.split('?')[0]?.split(',').length===1));
    assert.equal(new Set(called.filter(url=>url.includes('/tokens/multi/')).flatMap(url=>url.split('/tokens/multi/')[1]?.split('?')[0]?.split(',')??[])).size,93);
    assert.ok(called.length<=2*GECKO_REQUESTS_PER_MINUTE);
    assert.equal(enrichmentState.assets[`4663:${addr(30230)}`]?.CoinGecko?.fieldTimes?.liquidity,now);
  }finally{
    Date.now=oldNow;globalThis.fetch=oldFetch;
    if(oldKey===undefined)delete process.env.COINGECKO_API_KEY;else process.env.COINGECKO_API_KEY=oldKey;
    if(oldInfo===undefined)delete process.env.COINGECKO_INFO_ROUND_LIMIT;else process.env.COINGECKO_INFO_ROUND_LIMIT=oldInfo;
  }
});

test('CoinGecko source reports expired liquidity and a stalled round instead of old partial health',()=>{
  const oldNow=Date.now,oldAssets=enrichmentState.assets,oldProvider={...enrichmentState.providers.CoinGecko};
  const now=2_200_000_000_000,old=now-3_600_000,id=`56:${'0x'+'f'.repeat(40)}`;
  Date.now=()=>now;
  enrichmentState.assets={[id]:{CoinGecko:normalizeCoinGeckoTokens('56',
    {data:[{attributes:{address:id.split(':')[1],total_reserve_in_usd:'1001'}}]},old).assets[id]}};
  Object.assign(enrichmentState.providers.CoinGecko,{status:'partial',updatedAt:old,error:null,
    lastAttemptAt:old,lastCompletedAt:old});
  try{
    const expired=enrichmentSources().find(row=>row.provider==='CoinGecko')!;
    assert.equal(expired.status,'stale');
    assert.equal(expired.reason,'last-success-expired');
    assert.deepEqual(expired.liquidityCoverage?.status,'stale');
    assert.equal(expired.liquidityCoverage?.known,1);
    assert.equal(expired.liquidityCoverage?.fresh,0);
    Object.assign(enrichmentState.providers.CoinGecko,{status:'partial',updatedAt:now-60_000,
      lastAttemptAt:now-11*60_000,lastCompletedAt:now-12*60_000});
    const stalled=enrichmentSources().find(row=>row.provider==='CoinGecko')!;
    assert.equal(stalled.status,'stale');
    assert.equal(stalled.reason,'collector-round-stalled');
  }finally{
    Date.now=oldNow;enrichmentState.assets=oldAssets;
    Object.assign(enrichmentState.providers.CoinGecko,oldProvider);
  }
});

test('CoinGecko serves all three chains fairly and defers work on 429 or its shared budget',async()=>{
  assert.equal(retryAfterMs('120'),120_000);
  assert.equal(retryAfterMs('Wed, 21 Oct 2026 07:28:00 GMT',Date.parse('2026-10-21T07:27:00Z')),60_000);

  const oldNow=Date.now,oldFetch=globalThis.fetch,oldKey=process.env.COINGECKO_API_KEY,oldInfo=process.env.COINGECKO_INFO_ROUND_LIMIT;
  let now=2_000_000_000_000,rateLimited=true,simulateResponseLag=false;
  const calls:string[]=[];
  const addr=(n:number)=>'0x'+n.toString(16).padStart(40,'0');
  const assets=['196','56','4663'].flatMap((chain,index)=>Array.from({length:700},(_,n)=>({chainId:chain,token:addr(index*1000+n+1)})));
  const relations=['196','56','4663'].flatMap((chain,index)=>Array.from({length:30},(_,n)=>({chainId:chain,pool:addr(5000+index*100+n),status:'verified'})));
  const nextAssets=['196','56','4663'].flatMap((chain,index)=>Array.from({length:30},(_,n)=>({chainId:chain,token:addr(10000+index*100+n)})));
  const nextRelations=['196','56','4663'].flatMap((chain,index)=>Array.from({length:30},(_,n)=>({chainId:chain,pool:addr(15000+index*100+n),status:'verified'})));
  Date.now=()=>now;
  process.env.COINGECKO_API_KEY='test-not-a-real-key';
  process.env.COINGECKO_INFO_ROUND_LIMIT='1';
  globalThis.fetch=(async(input:any)=>{
    const url=String(input);calls.push(url);
    if(rateLimited)return new Response('{}',{status:429,headers:{'retry-after':'120','content-type':'application/json'}});
    const tokenPart=url.split('/tokens/multi/')[1]?.split('?')[0];
    if(tokenPart){if(simulateResponseLag)now+=4_000;return new Response(JSON.stringify({data:tokenPart.split(',').map(address=>({attributes:{address,price_usd:'1',total_reserve_in_usd:'1001'}}))}),{headers:{'content-type':'application/json'}});}
    const poolPart=url.split('/pools/multi/')[1]?.split('?')[0];
    if(poolPart)return new Response(JSON.stringify({data:poolPart.split(',').map(address=>({attributes:{address,reserve_in_usd:'1001'}}))}),{headers:{'content-type':'application/json'}});
    return new Response('{}',{headers:{'content-type':'application/json'}});
  }) as any;
  try{
    await refreshCoinGeckoEnrichment({assets,relations});
    assert.equal(calls.length,1);
    assert.equal(enrichmentState.providers.CoinGecko.status,'quota-exhausted');
    assert.equal(enrichmentState.attempts[`CoinGecko:asset:196:${addr(1)}`],undefined,'429 must not blacklist selected assets');
    rateLimited=false;
    now+=119_000;
    await refreshCoinGeckoEnrichment({assets,relations});
    assert.equal(calls.length,1,'Retry-After must be honored');
    now+=2_000;
    await refreshCoinGeckoEnrichment({assets,relations});
    const firstRound=calls.slice(1);
    assert.equal(firstRound.length,7,'one token batch and one pool batch per chain, plus one holder');
    assert.equal(firstRound.filter(url=>url.includes('/tokens/multi/')).length,3);
    assert.equal(firstRound.filter(url=>url.includes('/pools/multi/')).length,3);
    assert.equal(firstRound.filter(url=>url.includes('/tokens/')&&url.includes('/info')).length,1);
    await refreshCoinGeckoEnrichment({assets:nextAssets,relations:nextRelations});
    assert.equal(calls.length,1+GECKO_REQUESTS_PER_MINUTE,'all CoinGecko endpoints share the hard budget');
    const secondRound=calls.slice(1+firstRound.length);
    assert.equal(secondRound.filter(url=>url.includes('/tokens/multi/')).length,3);
    assert.equal(secondRound.filter(url=>url.includes('/pools/multi/')).length,2);
    assert.equal(enrichmentState.attempts[`CoinGecko:pool:4663:${addr(15200)}`],undefined,'local budget must not blacklist the unfinished pool');
    assert.equal(enrichmentState.assets[`56:${addr(1001)}`].CoinGecko.fieldTimeKinds?.price,'observed');
    now+=61_000;simulateResponseLag=true;
    const nextToken=addr(9000),requestAt=now;
    await refreshCoinGeckoEnrichment({assets:[{chainId:'196',token:nextToken}],relations:[]});
    assert.equal(enrichmentState.assets[`196:${nextToken}`].CoinGecko.fieldTimes?.price,requestAt+4_000,
      'quote time is response observation, not request start or an invented market event');
  }finally{
    Date.now=oldNow;globalThis.fetch=oldFetch;
    if(oldKey===undefined)delete process.env.COINGECKO_API_KEY;else process.env.COINGECKO_API_KEY=oldKey;
    if(oldInfo===undefined)delete process.env.COINGECKO_INFO_ROUND_LIMIT;else process.env.COINGECKO_INFO_ROUND_LIMIT=oldInfo;
  }
});
