import test from 'node:test';
import assert from 'node:assert/strict';
import { assetLogoIdentity, createLogoLoader } from '../src/api/asset-logos.js';
const A='0x'+'a'.repeat(40), B='0x'+'b'.repeat(40), URL='https://cdn.example.com/a.png';
const key=i=>`56:0x${i.toString(16).padStart(40,'0')}`;

test('logo identity uses full contract and chain; never ticker or name',()=>{
  assert.equal(assetLogoIdentity({chainId:56,token:A.toUpperCase()}),`56:${A}`);
  assert.equal(assetLogoIdentity({chainId:'196',tokenContractAddress:B}),`196:${B}`);
  for(const asset of [{stockCode:'NVDA'},{chainId:'1',token:A},{chainId:56,token:'0x1234'},{chainId:56,name:'NVDA'}])assert.equal(assetLogoIdentity(asset),'');
});

test('many components share batches of at most 50 and duplicate requests share one promise',async()=>{
  const jobs=[], calls=[];
  const load=createLogoLoader(async path=>{const ids=new URLSearchParams(path.split('?')[1]).get('ids').split(',');calls.push(ids);return {logos:Object.fromEntries(ids.map(id=>[id,URL]))};},{schedule:job=>jobs.push(job)});
  const requests=Array.from({length:115},(_,i)=>load(key(i)));
  assert.equal(load(key(1)),requests[1]);
  while(jobs.length)await jobs.shift()();
  assert.ok((await Promise.all(requests)).every(url=>url===URL));
  assert.deepEqual(calls.map(ids=>ids.length),[50,50,15]);
  await load(key(1));assert.equal(calls.length,3);
});

test('failed and absent metadata retry after short TTL, unsafe URLs never become images',async()=>{
  let clock=100, calls=0, fail=true, value='data:image/png,x';
  const load=createLogoLoader(async()=>{calls++;if(fail)throw Error('offline');return {logos:{[`56:${A}`]:value}};},{now:()=>clock});
  assert.equal(await load(`56:${A}`),'');fail=false;value=URL;
  assert.equal(await load(`56:${A}`),'');assert.equal(calls,1);
  clock+=15001;assert.equal(await load(`56:${A}`),URL);assert.equal(calls,2);
  clock+=3600001;value='javascript:alert(1)';assert.equal(await load(`56:${A}`),'');
  value=URL;clock+=60001;assert.equal(await load(`56:${A}`),URL);
  assert.equal(await load('NVDA'),'');assert.equal(calls,4);
});

test('same-named assets on separate chains cannot borrow metadata',async()=>{
  const load=createLogoLoader(async()=>({logos:{[`56:${A}`]:URL,[`196:${A}`]:'https://cdn.example.com/other.png'}}));
  assert.deepEqual(await Promise.all([load(`56:${A}`),load(`196:${A}`)]),[URL,'https://cdn.example.com/other.png']);
});


test('explicit recovery bypasses failed cache while still combining duplicates',async()=>{
  let calls=0,fail=true;
  const load=createLogoLoader(async()=>{calls++;if(fail)throw Error('offline');return {logos:{[`56:${A}`]:URL}};});
  assert.equal(await load(`56:${A}`),'');fail=false;
  const pending=load(`56:${A}`,{refresh:true});assert.equal(load(`56:${A}`,{refresh:true}),pending);
  assert.equal(await pending,URL);assert.equal(calls,2);
});
