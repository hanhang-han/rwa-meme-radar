import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parse,compileScript} from '@vue/compiler-sfc';
import * as vue from 'vue';
import {renderToString} from '@vue/server-renderer';
import * as format from '../src/utils/format.js';
import * as disclosure from '../src/utils/detail-disclosure-presentation.js';
import * as stockModel from '../src/utils/stock-theme-model.js';
import * as navigation from '../src/utils/stock-navigation.js';
import * as signals from '../src/utils/signal-presentation.js';

function compile(file,id){
const source=readFileSync(new URL(file,import.meta.url),'utf8');
return compileScript(parse(source).descriptor,{id,inlineTemplate:true}).content
  .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,bindings,path)=>`const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`)
  .replace(/^import\s+(\w+)\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,name,path)=>`const ${name}=imports[${JSON.stringify(path)}];`)
  .replace('export default','return');
}
const compiled=compile('../src/components/DetailRelationshipRecord.vue','detail-relation-record');
const now=1_800_000_000_000,address=c=>'0x'+c.repeat(40);
async function render(relation,language='zh'){
  const directory='/stock?chain=all&page=3',theme='/stock/700?chain=all&back='+encodeURIComponent(directory);
  const route={path:'/asset/56/'+address('a'),query:{from:'stock',ticker:'700',chain:'all',back:theme}};
  const imports={vue,'vue-router':{useRoute:()=>route},'../i18n':{tr:(zh,en)=>language==='en'?en:zh,useI18n:()=>({lang:vue.reactive({lang:language})})},
    '../utils/format':format,'../utils/signal-presentation':signals,'../utils/detail-disclosure-presentation':disclosure,'../utils/stock-theme-model':stockModel,'../utils/stock-navigation':navigation,
    '../stores/dashboard':{useDashboardStore:()=>({stockIndex:new Map()})},'../composables/useMinuteClock':{useMinuteClock:()=>vue.ref(now)}};
  imports['./RelationBadge.vue']=new Function('imports',compile('../src/components/RelationBadge.vue','relation-badge'))(imports);
  const component=new Function('imports',compiled)(imports);
  const app=vue.createSSRApp(component,{relation,chain:'56',scope:'all'});
  app.component('RouterLink',{props:['to'],setup:(props,{slots})=>()=>vue.h('a',{href:typeof props.to==='string'?props.to:props.to.path+'?'+new URLSearchParams(props.to.query).toString()},slots.default?.())});
  return renderToString(app);
}
const relation=(overrides={})=>({token:address('a'),stock:address('b'),pool:address('c'),ticker:'700',chainId:'56',level:'A',status:'verified',liquidityUsd:2000,liquidityAt:now,provider:'actual-registry',checkedAt:now,...overrides});

test('company summary is visible, full addresses are collapsed, and both asset links retain stock/directory/pool context',async()=>{
  const html=await render(relation());
  assert.match(html,/腾讯控股/);assert.match(html,/同池已核实/);
  assert.match(html,/<details class="detail-relation-evidence">/);assert.doesNotMatch(html,/<details[^>]*\bopen/);
  assert.match(html,new RegExp('/asset/56/'+address('a')));assert.match(html,new RegExp('/asset/56/'+address('b')));
  assert.match(html,new RegExp('pool='+address('c')));assert.match(html,/from=stock/);assert.match(html,/ticker=700/);
  assert.match(html,/stock%3Fchain%3Dall%26page%3D3/);assert.match(html,/actual-registry/);
});
test('missing valuation and time are explained instead of calling every unavailable pool delayed',async()=>{
  const missing=await render(relation({level:'C',liquidityUsd:null,liquidityAt:null}));
  assert.match(missing,/同池 · 暂无估值/);assert.doesNotMatch(missing,/历史估值|观测已过期/);
  const noTime=await render(relation({level:'C',liquidityAt:null}),'en');
  assert.match(noTime,/Tencent/);assert.match(noTime,/Paired · update due/);assert.doesNotMatch(noTime,/Historical valuation/);
  const expired=await render(relation({liquidityAt:now-900001}));
  assert.match(expired,/同池 · 估值待更新/);assert.match(expired,/历史估值/);
});
test('name similarity keeps stock link but cannot create a pool price or a verified same-pool claim',async()=>{
  const html=await render(relation({level:'B',status:'unverified',evidenceStatus:'name-only'}),'en');
  assert.match(html,/Name only/);assert.match(html,/excluded from same-pool volume/);
  assert.doesNotMatch(html,new RegExp('pool='+address('c')));assert.doesNotMatch(html,/Current same pool/);
});
