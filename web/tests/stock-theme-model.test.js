import test from 'node:test';
import assert from 'node:assert/strict';
import { buildPoolRows, buildRecordedPoolRows, buildStockTheme, comparableVolume, hasBasicMarketData, isQualifiedMeme, isQualifiedPool, normalizeTicker, stockAtlasCards, stockThemeCodeLabel, stockThemeName } from '../src/utils/stock-theme-model.js';

const NOW = 2_000_000_000;
const asset = (token, overrides = {}) => ({ chainId:'56', token, kind:'candidate', symbol:token, price:1,
  volume24h:100, volumeCurrency:'USD', volumeScope:'token-aggregate', fieldTimes:{ price:NOW, volume24h:NOW },
  totalLiquidityUsd:1000, totalLiquidityAt:NOW, totalLiquidityStatus:'current', ...overrides });
const relation = (token, pool, overrides = {}) => ({ chainId:'56', token, pool, stock:'0xstock', ticker:'700',
  level:'A', status:'verified', liquidityUsd:1000, liquidityAt:NOW, ...overrides });

test('stock themes match complete ticker identities, normalize numeric codes, and retain chain identity', () => {
  assert.equal(normalizeTicker('00700'), '700');
  const result = buildStockTheme({ ticker:'700', now:NOW,
    stockTokens:[{ chainId:'56', stockCode:'700', tokenContractAddress:'0xstock' }, { chainId:'56', stockCode:'1700', tokenContractAddress:'0xother' }],
    assets:[asset('0xa'), asset('0xa', { chainId:'196' }), asset('0xb'), asset('0xname', { match:{ level:'B', evidenceStatus:'name-only', ticker:'00700' } })],
    relations:[relation('0xa','0xp'), relation('0xa','0xp', { chainId:'196' }), relation('0xb','0xother', { stock:'0xother', ticker:'1700' })],
  });
  assert.equal(result.versions.length, 1);
  assert.deepEqual(result.rows.map(row => row.key).sort(), ['196:0xa','56:0xa','56:0xname']);
  assert.equal(result.pairedCount, 2);
  assert.equal(result.nameCount, 1);
});

test('theme volume excludes name matches, stale and pool-level observations, and exposes incomplete coverage', () => {
  const rows = [asset('0xa'), asset('0xb', { volume24h:null }), asset('0xc', { volumeScope:'pool:0xc' }), asset('0xname', { volume24h:999999, match:{ level:'B', evidenceStatus:'name-only', ticker:'700' } })];
  const result = buildStockTheme({ ticker:'700', assets:rows,
    relations:[relation('0xa','0x1'), relation('0xa','0x2'), relation('0xb','0x3'), relation('0xc','0x4')], now:NOW });
  assert.deepEqual(result.volume, { value:100, known:1, total:3, complete:false });
  assert.equal(result.rows.find(row => row.token === '0xa').volumeShare, 1);
  assert.equal(result.rows.find(row => row.token === '0xb').volumeShare, null);
  assert.equal(result.rows.find(row => row.token === '0xname').volumeShare, null);
  assert.equal(result.concentrated, false);
  assert.equal(comparableVolume(asset('0xd', { fieldTimes:{ volume24h:NOW - 900001 } }), NOW), null);
  assert.equal(comparableVolume(asset('0xe', { volumeCurrency:'USDT' }), NOW), null);
});

test('real zero volume remains known while missing or stale volume never becomes zero', () => {
  const result = buildStockTheme({ ticker:'700', assets:[asset('0xa', { volume24h:0 })], relations:[relation('0xa','0xp')], now:NOW });
  assert.deepEqual(result.volume, { value:0, known:1, total:1, complete:true });
  assert.equal(result.rows[0].volumeShare, null);
  assert.equal(comparableVolume(asset('0xa', { volume24h:null }), NOW), null);
});

test('active Meme and pool filters enforce independent exact eligibility windows', () => {
  assert.equal(isQualifiedMeme(asset('0xa'), NOW), true);
  assert.equal(isQualifiedMeme(asset('0xa', { totalLiquidityAt:NOW - 1800000 }), NOW), true);
  assert.equal(isQualifiedMeme(asset('0xa', { totalLiquidityAt:NOW - 1800001 }), NOW), false);
  assert.equal(isQualifiedMeme(asset('0xa', { fieldTimes:{ price:NOW - 900001 } }), NOW), false);
  assert.equal(isQualifiedMeme(asset('0xa', { totalLiquidityUsd:999 }), NOW), false);
  assert.equal(isQualifiedMeme(asset('0xa', { fieldTimes:{ price:NOW + 1 } }), NOW), false);
  assert.equal(isQualifiedPool(relation('0xa','0xp'), NOW), true);
  assert.equal(isQualifiedPool(relation('0xa','0xp', { liquidityAt:NOW - 900001 }), NOW), false);
  assert.equal(isQualifiedPool(relation('0xa','0xp', { status:'unverified' }), NOW), false);
  assert.equal(hasBasicMarketData(asset('0xa', { volume24h:0 })), true);
  assert.equal(hasBasicMarketData(asset('0xa', { totalLiquidityUsd:null, liquidity:1000 })), false);
});

test('pool list deduplicates chain+pool and never substitutes asset volume for pool volume', () => {
  const entries = [relation('0xa','0xp', { liquidityAt:NOW - 10 }),
    relation('0xa','0xp', { liquidityUsd:2000, poolMarket:{ scope:'pool:0xp', volume24h:0, updatedAt:NOW } }),
    relation('0xa','0xp', { chainId:'196', poolMarket:{ scope:'pool:0xwrong', volume24h:99, updatedAt:NOW } })];
  const rows = buildPoolRows(entries, [asset('0xa')], { now:NOW });
  assert.equal(rows.length, 2);
  assert.equal(rows.find(row => row.chainId === '56').liquidity, 2000);
  assert.equal(rows.find(row => row.chainId === '56').volume24h, 0);
  assert.equal(rows.find(row => row.chainId === '196').volume24h, null);
  assert.equal(rows[0].createdAt, null);
  assert.equal(buildPoolRows(entries, [asset('0xa')], { scope:'196', now:NOW }).length, 1);
});

test('stock events merge duplicate observations but keep pool creation separate from discovery', () => {
  const result = buildStockTheme({ ticker:'700', assets:[asset('0xa')], relations:[relation('0xa','0xp')], now:NOW,
    events:[{ chainId:'56', asset:'56:0xa', ticker:'700', kind:'pair-observed', t:NOW - 50, pool:'0xp' },
      { chainId:'56', asset:'0xa', ticker:'700', kind:'pair-observed', t:NOW - 10, pool:'0xp' },
      { chainId:'56', asset:'0xa', ticker:'1700', kind:'verified', t:NOW - 5, pool:'0xp' },
      { chainId:'56', asset:'0xa', ticker:'700', kind:'pool-created', t:NOW - 100, pool:'0xp' }],
  });
  assert.equal(result.events.length, 2);
  assert.equal(result.events[0].count, 2);
  assert.equal(result.events[0].kind, 'pair-observed');
  assert.equal(result.events[1].kind, 'pool-created');
});


test('theme volume shares accept canonical token USD and exclude derivative products', () => {
  const rows=[asset('0xa',{fieldScopes:{volume24h:'token'},volumeCurrency:null,priceCurrency:'USD'}),
    asset('0xb',{assetCategory:'derivative',volume24h:1000}),asset('0xc',{assetCategory:'derivative',match:{level:'B',evidenceStatus:'name-only',ticker:'700'}})];
  const theme=buildStockTheme({ticker:'700',assets:rows,relations:[relation('0xa','0x1'),relation('0xb','0x2')],now:NOW});
  assert.deepEqual(theme.volume,{value:100,known:1,total:1,complete:true});
  assert.equal(theme.rows.length,1);
  assert.equal(comparableVolume(asset('0xd',{fieldScopes:{volume24h:'exchange:binance'}}),NOW),null);
});

test('name matches never acquire a verified event and repeated event IDs are not counted twice', () => {
  const event={id:'a',chainId:'56',asset:'0xa',ticker:'700',kind:'pair-observed',t:NOW-10,pool:'0xp'};
  const theme=buildStockTheme({ticker:'700',assets:[asset('0xa'),asset('0xb',{match:{level:'B',evidenceStatus:'name-only',ticker:'700'}})],relations:[relation('0xa','0xp')],events:[event,event,{id:'b',chainId:'56',asset:'0xb',ticker:'700',kind:'verified',t:NOW-10}],now:NOW});
  assert.equal(theme.events.length,1);
  assert.equal(theme.events[0].count,1);
});


test('stock theme uses the Chinese company name when old snapshots only provide a wrapper name', () => {
  assert.equal(stockThemeName({stockCode:'NVDA',tokenName:'NVDAx'},'NVDA','zh'),'英伟达');
  assert.equal(stockThemeName({tokenName:'TCENTx'},'00700','zh'),'腾讯控股');
  assert.equal(stockThemeName({stockIdentity:{nameZh:'自定义证券名称'},tokenName:'NVDAx'},'NVDA','zh'),'自定义证券名称');
  assert.equal(stockThemeName({stockIdentity:{nameEn:'NVIDIA'},tokenName:'NVDAx'},'NVDA','en'),'NVIDIA');
  assert.equal(stockThemeName(null,'ABCD','zh'),'ABCD');
});

test('Hong Kong wrappers display company names and secondary padded codes without changing lookup keys', () => {
  for (const [ticker,name,english] of [['1','长江和记实业','CK Hutchison'],['700','腾讯控股','Tencent'],['1024','快手','Kuaishou'],['1038','长江基建','CK Infrastructure'],['1088','中国神华','China Shenhua'],['1093','石药集团','CSPC Pharmaceutical']]) {
    const stock={stockCode:ticker,tokenName:'wrapper-name'};
    assert.equal(stockThemeName(stock,ticker,'zh'),name);
    assert.equal(stockThemeName(stock,ticker,'en'),english);
    assert.equal(stockThemeCodeLabel(stock,ticker,'zh'),`港股 · ${ticker.padStart(5,'0')}`);
    assert.equal(stockThemeCodeLabel(stock,ticker,'en'),`HK · ${ticker.padStart(5,'0')}`);
    assert.equal(stock.stockCode,ticker);
    assert.equal(normalizeTicker(ticker.padStart(5,'0')),ticker);
  }
  assert.equal(stockThemeCodeLabel(null,'NVDA','zh'),'NVDA');
  assert.equal(stockThemeCodeLabel(null,'123456','zh'),'123456');
  assert.equal(stockThemeCodeLabel({stockIdentity:{exchange:'SEHK'}},'5678','zh'),'港股 · 05678');
});

test('atlas emphasizes relationships but keeps an unmodified complete directory and an empty-theme fallback', () => {
  const empty={ticker:'1',theme:{pairedCount:0,nameCount:0,volume:{known:0}}};
  const paired={ticker:'700',theme:{pairedCount:1,nameCount:0,volume:{known:1}}};
  const named={ticker:'1024',theme:{pairedCount:0,nameCount:2,volume:{known:0}}};
  const directory=[empty,named,paired];
  assert.deepEqual(stockAtlasCards(directory),[paired]);
  assert.deepEqual(directory,[empty,named,paired]);
  assert.deepEqual(stockAtlasCards([empty,named]),[named]);
  assert.deepEqual(stockAtlasCards([empty]),[empty]);
  assert.deepEqual(stockAtlasCards([]),[]);
});

const evm = digit => '0x'+digit.repeat(40);
test('verified stale pool records remain visible without entering current A/B volume or counts',()=>{
  const currentToken=evm('1'),oldToken=evm('2'),nameToken=evm('3'),stock=evm('4');
  const stale=relation(oldToken,evm('6'),{stock,level:undefined,evidenceStatus:'liquidity-stale',liquidityAt:NOW-900001});
  const result=buildStockTheme({ticker:'700',now:NOW,
    assets:[asset(currentToken),asset(oldToken,{volume24h:999999}),asset(nameToken,{match:{level:'B',evidenceStatus:'name-only',ticker:'700'}})],
    relations:[relation(currentToken,evm('5'),{stock}),stale]});
  assert.equal(result.pairedCount,1);
  assert.equal(result.nameCount,1);
  assert.deepEqual(result.volume,{value:100,known:1,total:1,complete:true});
  assert.equal(result.recordedPools.length,1);
  assert.equal(result.recordedPools[0].relation,stale);
  assert.equal(result.recordedPools[0].detailAvailable,true);
  assert.equal(result.recordedPools[0].volume,undefined);
  assert.equal(result.recordedPools[0].liquidity,undefined);
});

test('recorded pool identity is chain-scoped, deduplicated and never invents a detail destination',()=>{
  const token=evm('1'),pool=evm('2'),stock=evm('3');
  const old=relation(token,pool,{stock,level:undefined,checkedAt:NOW-100});
  const latest={...old,checkedAt:NOW};
  const otherChain={...old,chainId:'196'};
  const rows=buildRecordedPoolRows([old,latest,otherChain,{...old,pool:evm('4'),status:'unverified'},{...old,pool:'invalid'}],[asset(token)]);
  assert.equal(rows.length,2);
  assert.equal(rows.find(row=>row.chainId==='56').relation,latest);
  assert.equal(rows.find(row=>row.chainId==='56').detailAvailable,true);
  assert.equal(rows.find(row=>row.chainId==='196').detailAvailable,false);
  assert.equal(buildRecordedPoolRows([old,otherChain],[],{scope:'196'}).length,1);
  assert.equal(buildRecordedPoolRows([old],[],{activeKeys:['56:'+pool]}).length,0);
});

test('unrelated and derivative historical records do not become theme Meme relationships',()=>{
  const token=evm('1'),stock=evm('2');
  const result=buildStockTheme({ticker:'700',now:NOW,assets:[asset(token,{assetCategory:'derivative'})],
    relations:[relation(token,evm('3'),{stock,level:undefined}),relation(evm('4'),evm('5'),{stock,ticker:'QQQ',level:undefined})]});
  assert.equal(result.recordedPools.length,0);
  assert.equal(result.rows.length,0);
});
