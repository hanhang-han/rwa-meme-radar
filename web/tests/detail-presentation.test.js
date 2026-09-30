import test from 'node:test';
import assert from 'node:assert/strict';
import {dexAction,detailAsset,detailTrades,pairRelationMatches} from '../src/utils/detail-presentation.js';
import {riskEvidenceRows,riskEvidenceValue,riskFlagSuffix,riskProvenance,riskStateText} from '../src/utils/risk-presentation.js';

const token='0x'+'a'.repeat(40),pool='0x'+'b'.repeat(40);
test('missing summary fields preserve route identity and never invent a price or holders',()=>{
 const row=detailAsset(null,'56',token);
 assert.equal(row.chainId,'56');assert.equal(row.token,token);
 assert.equal(row.price,undefined);assert.equal(row.holders,undefined);
 assert.equal(detailAsset({chainId:'196',token:'wrong',price:null},'56',token).price,null);
});
test('DEX actions include the exact token and supported chain without amount or unsupported fallback',()=>{
 const bnb=new URL(dexAction('56',token).url);
 assert.equal(bnb.origin,'https://pancakeswap.finance');assert.equal(bnb.searchParams.get('chain'),'bsc');assert.equal(bnb.searchParams.get('outputCurrency'),token);assert.equal(bnb.searchParams.has('exactAmount'),false);
 assert.equal(dexAction('196',token).url,'https://web3.okx.com/token/x-layer/'+token);
 assert.equal(dexAction('4663',token),null);assert.equal(dexAction('56','javascript:alert(1)'),null);
});
test('detail selected markets filter mini and full trades consistently while missing amounts remain null',()=>{
 const a={id:'same',chainId:'56',token,venue:'dex',pool,marketId:pool,t:10,volume:null};
 const b={id:'other',chainId:'56',token,venue:'binance',marketId:'AUSDT',t:20,volume:90};
 const data={marketTrades:[a,b],trades:[{...a,wallet:'wallet'}]};
 const rows=detailTrades(data,{kind:'pool',pool:{poolId:pool.toUpperCase()}});
 assert.equal(rows.length,1);assert.equal(rows[0].volume,null);assert.equal(rows[0].wallet,'wallet');
 assert.deepEqual(detailTrades(data,{kind:'exchange',exchange:{venue:'binance',marketId:'AUSDT'}}),[b]);
});
test('old pair links match chain, stock and pool rather than a shared name or other chain',()=>{
 const row={chainId:'56',stock:token,pool};
 assert.equal(pairRelationMatches(row,'56',token.toUpperCase(),pool.toUpperCase()),true);
 assert.equal(pairRelationMatches(row,'196',token,pool),false);
 assert.equal(pairRelationMatches(row,'56',pool,pool),false);
});
test('risk unknown and missing ratio never become safe or a fabricated zero',()=>{
 assert.equal(riskStateText(undefined),'未能识别');assert.equal(riskStateText('unknown','en'),'Unknown');
 assert.equal(riskFlagSuffix('wash_suspect',{volumeLiquidityRatio:null}),'');
 assert.equal(riskFlagSuffix('wash_suspect',{volumeLiquidityRatio:0}),'0x');
 assert.equal(riskEvidenceValue('sellTaxPct',null),'未能识别');
 assert.equal(riskEvidenceValue('sellTaxPct',0),'0%');
});
test('actual GoPlus evidence shows translated fields, percentage bounds, dates and address strings',()=>{
 const evidence={top10RawPercent:90,exclusionsApplied:false,lockedPercentMin:50,burnedPercent:45,unlockedPercentMin:5,coveredPercent:100,nextUnlockAt:Date.UTC(2026,9,2),ownerAddress:token,triggers:['sellTaxPct'],threshold:{change24hPct:1000,totalLiquidityUsd:100000}};
 const rows=riskEvidenceRows(evidence);
 assert.equal(rows.find(row=>row.key===':top10RawPercent').label,'前十大原始持仓占比');
 assert.equal(rows.find(row=>row.key===':lockedPercentMin').value,'50%');
 assert.equal(rows.find(row=>row.key===':nextUnlockAt').value.includes('2026'),true);
 assert.equal(rows.find(row=>row.key===':ownerAddress').value,token);
 assert.equal(rows.find(row=>row.key===':triggers').value,'卖出税率');
 assert.equal(rows.some(row=>row.value==='$100,000'),true);
 assert.equal(rows.some(row=>row.value.startsWith('{')),false);
 assert.deepEqual(riskProvenance({evidence:{provider:'GoPlus',checkedAt:123}}),{provider:'GoPlus',at:123});
});
