import test from 'node:test';
import assert from 'node:assert/strict';
import {detailMetricObservation,detailRelationState,detailTriggeredRisks,hasDetailExtraMetrics} from '../src/utils/detail-disclosure-presentation.js';

const now=1_800_000_000_000,pool='0x'+'a'.repeat(40),stock='0x'+'b'.repeat(40);
test('known historical pool liquidity stays dated; missing/incompatible aggregates do not become zero',()=>{
  const stale={totalLiquidityUsd:123, totalLiquidityStatus:'stale',totalLiquidityAt:now-2_000_000,
    fieldAvailability:{totalLiquidityUsd:{value:null,historicalValue:123,at:now-2_000_000,status:'stale',source:'actual-pools',scope:'token-aggregate'}}};
  assert.deepEqual(detailMetricObservation(stale,'totalLiquidityUsd',now),{value:123,at:now-2_000_000,source:'actual-pools',status:'historical',currency:'USD',scope:'token-aggregate'});
  assert.equal(detailMetricObservation({totalLiquidityUsd:0,totalLiquidityAt:now,totalLiquidityStatus:'unknown'},'totalLiquidityUsd',now).value,null);
  assert.equal(detailMetricObservation({totalLiquidityUsd:0,totalLiquidityAt:now,totalLiquidityStatus:'current',totalLiquidityCoverage:{complete:false}},'totalLiquidityUsd',now).value,null);
  assert.equal(detailMetricObservation({totalLiquidityUsd:0,totalLiquidityAt:now-2_000_000,totalLiquidityStatus:'stale',totalLiquidityCoverage:{complete:false,provider:'actual-pools'}},'totalLiquidityUsd',now).value,null);
  assert.equal(detailMetricObservation({totalLiquidityUsd:0,totalLiquidityAt:now-2_000_000,totalLiquidityStatus:'stale',fieldAvailability:{totalLiquidityUsd:{value:null,historicalValue:0,status:'stale',at:now-2_000_000,reason:'indexed-coverage-incomplete',source:'actual-pools'}}},'totalLiquidityUsd',now).value,null);
  assert.equal(detailMetricObservation({...stale,totalLiquidityAt:undefined,fieldAvailability:{}},'totalLiquidityUsd',now).value,null);
  assert.equal(detailMetricObservation({volume24h:100,fieldAvailability:{volume24h:{value:null,historicalValue:100,at:now,status:'unknown',reason:'scope-conflict'}}},'volume24h',now).value,null);
});
test('an observed zero and unknown trade volume currency keep their actual meaning',()=>{
  const asset={volume24h:0,holders:0,totalLiquidityUsd:0,totalLiquidityAt:now,totalLiquidityStatus:'current',totalLiquidityCoverage:{complete:true},fieldTimes:{volume24h:now,holders:now}};
  assert.equal(detailMetricObservation(asset,'volume24h',now).value,0);
  assert.equal(detailMetricObservation(asset,'volume24h',now).currency,null);
  assert.equal(detailMetricObservation(asset,'holders',now).value,0);
  assert.equal(detailMetricObservation(asset,'totalLiquidityUsd',now).value,0);
  const history={volumeCurrency:'USD',fieldAvailability:{volume24h:{value:null,historicalValue:5,status:'stale',at:now-2_000_000,source:'native-record',historicalCurrency:'WBNB'}}};
  assert.equal(detailMetricObservation(history,'volume24h',now).currency,'WBNB');
});
test('professional panel only appears for usable measurements, not unsupported estimates or stale FDV',()=>{
  assert.equal(hasDetailExtraMetrics({exitImpact1k:{status:'unsupported',valuePercent:null},fdvUsd:{status:'unknown',at:now,value:null}},now),false);
  assert.equal(hasDetailExtraMetrics({fdvUsd:{status:'current',at:now-2_000_000,value:100}},now),false);
  assert.equal(hasDetailExtraMetrics({fdvUsd:{status:'current',at:now,value:0}},now),true);
  assert.equal(hasDetailExtraMetrics({poolAgeHours:{status:'current',at:now-86_400_000}},now),true);
  assert.equal(hasDetailExtraMetrics({exitImpact1k:{status:'current',at:now,valuePercent:1,validUntil:now-1}},now),false);
});
test('risk summaries keep actual market flags and triggered historic safety checks visible',()=>{
  const rows=detailTriggeredRisks({riskFlags:['wash_suspect','unknown-invented'],riskAssessment:{checks:{wash_suspect:{status:'triggered',checkedAt:now,provider:'actual-scan'}},safety:{tax:{status:'triggered',checkedAt:now-30_000_000,provider:'GoPlus'},permissions:{status:'unknown'}}}},now);
  assert.deepEqual(rows.map(row=>row.key),['wash_suspect','safety:tax']);
  assert.equal(rows[0].provider,'actual-scan');assert.equal(rows[0].historical,false);
  assert.equal(rows[1].historical,true);assert.equal(rows[1].label[1],'Taxes / trading limits');
  assert.equal(detailTriggeredRisks({},now).length,0);
});
test('relationship status separates observed pairing from missing valuation, time and expired values',()=>{
  const relation={level:'A',status:'verified',pool,stock,liquidityUsd:2000,liquidityAt:now};
  assert.equal(detailRelationState(relation,now).key,'current');
  assert.equal(detailRelationState({...relation,level:'C',liquidityUsd:null},now).key,'liquidity-unknown');
  assert.equal(detailRelationState({...relation,level:'C',liquidityAt:null},now).key,'liquidity-time-unknown');
  assert.equal(detailRelationState({...relation,liquidityAt:now-900001},now).key,'liquidity-expired');
  assert.equal(detailRelationState({level:'B',ticker:'700'},now).key,'name-only');
  assert.equal(detailRelationState({...relation,level:'C',evidenceStatus:'issuer-deployment-unverified'},now).key,'issuer-unverified');
});
