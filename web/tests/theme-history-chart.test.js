import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as format from '../src/utils/format.js';
import * as chart from '../src/utils/theme-chart-model.js';

const now=Date.now(),hour=3_600_000;
const payload=()=>({from:now-4*hour,to:now,prices:[{t:now-4*hour,value:3.77,source:'OKX'},{t:now-3*hour,value:3.76,source:'OKX'},{t:now-hour,value:3.78,source:'OKX'}],volumes:[{t:now-hour,volumeUsd:null}],events:[{t:now-hour,chainId:'196',pool:'pool',symbol:'KUAIx'}],coverage:{prices:{source:'OKX'},volume:{known:0,total:5}}});
const compiled=compileScript(parse(readFileSync(new URL('../src/components/ThemeHistoryChart.vue',import.meta.url),'utf8')).descriptor,{id:'theme-history-chart',inlineTemplate:true}).content
 .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,bindings,path)=>`const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`)
 .replace('export default','return');
const flush=async()=>{await vue.nextTick();await vue.nextTick();};

function mountChart(initial={compact:true,data:payload()}){
 const savedObserver=globalThis.ResizeObserver,language=vue.reactive({lang:'zh'}),props=vue.reactive(initial),emitted=[];
 let observer;
 globalThis.ResizeObserver=class{constructor(callback){this.callback=callback;observer=this;}observe(element){this.element=element;}disconnect(){this.disconnected=true;}};
 const imports={vue,'../i18n':{tr:(zh,en)=>language.lang==='en'?en:zh,useI18n:()=>({lang:language})},'../utils/format':format,'../utils/theme-chart-model':chart,'../composables/useMinuteClock':{useMinuteClock:()=>vue.ref(now)}};
 const component=new Function('imports',compiled)(imports);
 const node=(type,text='')=>({type,text,props:{},children:[],parent:null,clientWidth:400});
 const detach=child=>{if(child.parent){const children=child.parent.children,at=children.indexOf(child);if(at>=0)children.splice(at,1);child.parent=null;}};
 const renderer=vue.createRenderer({createElement:tag=>node(tag),createText:text=>node('text',text),createComment:()=>node('comment'),insert:(child,parent,anchor)=>{detach(child);child.parent=parent;const at=anchor?parent.children.indexOf(anchor):-1;if(at<0)parent.children.push(child);else parent.children.splice(at,0,child);},remove:detach,setText:(target,text)=>target.text=text,setElementText:(target,text)=>{target.text=text;target.children=[];},patchProp:(target,key,_old,value)=>target.props[key]=value,parentNode:target=>target.parent,nextSibling:target=>target.parent?.children[target.parent.children.indexOf(target)+1]??null});
 const app=renderer.createApp({setup:()=>()=>vue.h(component,{...props,onRefresh:()=>emitted.push(['refresh']),'onUpdate:range':value=>emitted.push(['range',value])})}),root=node('root');app.mount(root);
 const all=()=>{const rows=[];const walk=target=>{rows.push(target);target.children.forEach(walk);};walk(root);return rows;},textOf=node=>node.text+node.children.map(textOf).join('');
 return {props,language,emitted,all,textOf,observer:()=>observer,resize:width=>observer.callback([{contentRect:{width}}]),unmount:()=>{app.unmount();if(savedObserver===undefined)delete globalThis.ResizeObserver;else globalThis.ResizeObserver=savedObserver;}};
}

test('compact history shows real prices and their source without unavailable volume or pool markers',async()=>{
 const env=mountChart();try{await flush();
  const all=env.all(),svg=all.find(n=>n.type==='svg');assert.equal(svg.props.viewBox,'0 0 400 190');
  assert.match(env.textOf(all[0]),/OKX · 最后行情/);assert.match(env.textOf(all[0]),new RegExp(format.date(now-hour).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
  assert.equal(all.filter(n=>n.type==='rect').length,0);assert.equal(all.filter(n=>n.type==='details').length,0);assert.doesNotMatch(env.textOf(all[0]),/成交时段覆盖|关联池成交暂无数据/);
  const path=all.find(n=>n.type==='path').props.d;assert.equal((path.match(/M/g)||[]).length,2,'the real observation gap is preserved');assert.doesNotMatch(path,/NaN|Infinity/);
 }finally{env.unmount();}
});

test('compact price-only mode does not draw an empty chart for volume-only or unknown price data',async()=>{
 const env=mountChart({compact:true,data:{prices:[{t:now,value:null}],volumes:[{t:now,volumeUsd:0}]}});try{await flush();
  assert.equal(env.all().some(n=>n.type==='svg'),false);assert.match(env.textOf(env.all()[0]),/暂无可用历史价格/);
  env.props.compact=false;await flush();assert.equal(env.all().some(n=>n.type==='svg'),true,'full mode retains the known zero volume observation');
 }finally{env.unmount();}
});

test('compact geometry follows container width and disconnects its observer on removal',async()=>{
 const env=mountChart();let observer;try{await flush();observer=env.observer();
  const original=env.all().find(n=>n.type==='path').props.d;env.resize(320);await flush();
  const svg=env.all().find(n=>n.type==='svg');assert.equal(svg.props.viewBox,'0 0 320 190');assert.notEqual(env.all().find(n=>n.type==='path').props.d,original);
  assert.ok(env.all().some(n=>n.type==='text'&&n.props.x===244),'the price axis is recomputed at the narrow container edge');
 }finally{env.unmount();}assert.equal(observer.disconnected,true);
});

test('refresh failures keep the prior plot and observation time with an explicit error',async()=>{
 const env=mountChart();try{await flush();const svg=env.all().find(n=>n.type==='svg');env.props.error=true;env.props.loading=true;await flush();
  assert.equal(env.all().find(n=>n.type==='svg'),svg);assert.match(env.textOf(env.all().find(n=>n.props.role==='alert')),/更新失败，保留上次结果/);
  const button=env.all().find(n=>n.type==='button');assert.equal(button.props.disabled,true);assert.match(env.textOf(button),/更新中/);
  env.props.loading=false;env.props.error=false;env.language.lang='en';await flush();assert.equal(env.all().some(n=>n.props.role==='alert'),false);
  assert.match(env.textOf(env.all()[0]),/Stock-token price|Last quote/);env.all().find(n=>n.type==='button').props.onClick();env.all().find(n=>n.type==='select').props.onChange({target:{value:'7d'}});assert.deepEqual(env.emitted,[['refresh'],['range','7d']]);
 }finally{env.unmount();}
});

test('one real price observation remains a visible point, and default mode keeps combined analysis',async()=>{
 const env=mountChart({compact:true,data:{prices:[{t:now,value:3.7661,source:'OKX'}]}});try{await flush();assert.equal(env.all().filter(n=>n.type==='circle').length,1);
  env.props.compact=false;env.props.data=payload();await flush();assert.equal(env.all().find(n=>n.type==='svg').props.viewBox,'0 0 760 330');assert.equal(env.all().filter(n=>n.type==='details').length,1);
  assert.match(env.textOf(env.all()[0]),/纵轴：代币价格 · 横轴：本地时间/);assert.equal(env.all().some(n=>n.type==='text'&&n.props.x==='708'),false,'unknown volume has no zero-valued axis');
  env.props.data={...payload(),volumes:[{t:now-hour,volumeUsd:100}]};await flush();assert.match(env.textOf(env.all()[0]),/左轴：代币价格 · 右轴：每小时成交额/);
  const volumeAxis=env.all().filter(n=>n.type==='text'&&n.props.x==='708');assert.equal(volumeAxis.length,2);assert.match(env.textOf(volumeAxis[0]),/\$100/);assert.equal(env.textOf(volumeAxis[1]),'$0');
 }finally{env.unmount();}
});
