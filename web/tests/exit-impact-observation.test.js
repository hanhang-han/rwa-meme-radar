import test from 'node:test';
import assert from 'node:assert/strict';
import { exitImpactObservation } from '../src/utils/exit-impact-observation.js';

const stamp = 1_900_000_000_000;
const estimate = {status:'current', method:'v2-balanced-reserves-estimate',
  at:stamp-60_000, valuePercent:4, source:'V2 reserves', pool:'old-pool',
  feeSourceUrl:'https://example.com/v2-fees'};

test('Quoter value, method, source, pool and verification stay on one packet',()=>{
  const quote={status:'current',method:'v3-v2-quoter',at:stamp,validUntil:stamp+30_000,
    valuePercent:1.2,source:'verified-quoter',pool:'quoted-pool',verificationUrl:'https://example.com/quoter'};
  const result=exitImpactObservation(estimate,quote,stamp+1);
  assert.equal(result.valuePercent,1.2);
  assert.equal(result.packet,quote);
  assert.equal(result.packet.source,'verified-quoter');
  assert.equal(result.packet.feeSourceUrl,undefined);
  assert.equal(result.isQuoter,true);
  assert.equal(result.quotedMethod,'v3-v2-quoter');
});

test('Quoter expires at its deadline without falling back to the V2 estimate',()=>{
  const quote={status:'current',method:'v4-quoter',at:stamp,validUntil:stamp+30_000,valuePercent:1};
  assert.equal(exitImpactObservation(estimate,quote,stamp+29_999).valuePercent,1);
  const result=exitImpactObservation(estimate,quote,stamp+30_000);
  assert.equal(result.valuePercent,null);
  assert.equal(result.expired,true);
  assert.equal(result.packet,quote);
});

test('V2 estimates keep their observed 30-minute lifetime',()=>{
  assert.equal(exitImpactObservation(estimate,null,estimate.at+1_800_000).valuePercent,4);
  assert.equal(exitImpactObservation(estimate,null,estimate.at+1_800_001).valuePercent,null);
});

test('unconfigured, future, missing validity and nonnumeric quotes remain unknown',()=>{
  const unavailable={status:'unavailable',at:stamp,reason:'verified-quoter-unconfigured'};
  assert.equal(exitImpactObservation(estimate,unavailable,stamp).valuePercent,null);
  assert.equal(exitImpactObservation(estimate,unavailable,stamp).packet.reason,'verified-quoter-unconfigured');
  for(const quote of [
    {status:'current',method:'v4-quoter',at:stamp+1,validUntil:stamp+30_000,valuePercent:1},
    {status:'current',method:'v4-quoter',at:stamp,valuePercent:1},
    {status:'current',method:'v4-quoter',at:stamp,validUntil:stamp+30_000,valuePercent:'1'},
    {status:'current',at:stamp,validUntil:stamp+30_000,valuePercent:1},
    {...estimate},
  ]) assert.equal(exitImpactObservation(estimate,quote,stamp).valuePercent,null);
});
