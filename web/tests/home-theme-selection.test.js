import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse, compileScript } from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as format from '../src/utils/format.js';
import * as stock from '../src/utils/stock-theme-model.js';
import * as theme from '../src/utils/theme-map-model.js';
import * as navigation from '../src/utils/navigation-context.js';
import * as pageState from '../src/utils/page-navigation-state.js';

const now=Date.now();
const metric=value=>({value,status:'current',observedAt:now,currency:'USD',source:'verified-fixture'});
const bubble=(ticker,address,name)=>({chainId:'56',token:address,primaryTicker:ticker,symbol:name,name,
  volume24h:metric(1000),price:metric(1),change24h:metric(2),relation:{pool:`pool-${ticker}`}});
const first=bubble('700','0x'+'a'.repeat(40),'腾讯关联资产');
const second=bubble('1024','0x'+'b'.repeat(40),'快手关联资产');
const map=(bubbles=[first,second])=>({themes:[{ticker:'700',nameZh:'腾讯控股'},{ticker:'1024',nameZh:'快手'}],bubbles});
const flush=async()=>{for(let i=0;i<5;i++){await Promise.resolve();await vue.nextTick();}};

const descriptor=parse(readFileSync(new URL('../src/components/ThemeMarketMap.vue',import.meta.url),'utf8')).descriptor;
const compiled=compileScript(descriptor,{id:'home-theme-selection',inlineTemplate:true}).content
  .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,bindings,path)=>`const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`)
  .replace('export default','return');

function mountMap({controlled=true,initial='',autoUpdate=true,restore=false}={}){
  if(!restore)pageState.clearPageState('home-theme-map:all');
  const saved={window:globalThis.window,localStorage:globalThis.localStorage};
  globalThis.window={innerWidth:1200,addEventListener(){},removeEventListener(){}};
  globalThis.localStorage={getItem:()=>null,setItem(){}};
  const imports={vue,'vue-router':{useRoute:()=>({path:'/live',fullPath:'/live?chain=all',query:{chain:'all'}})},'../utils/navigation-context':navigation,'../utils/page-navigation-state':pageState,'../i18n':{tr:zh=>zh,useI18n:()=>({lang:vue.reactive({lang:'zh'})})},
    '../utils/format':format,'../utils/stock-theme-model':stock,'../utils/theme-map-model':theme,
    '../composables/useMinuteClock':{useMinuteClock:()=>vue.ref(now)}};
  const component=new Function('imports',compiled)(imports);
  const selection=vue.ref(initial),props=vue.reactive({themeMap:map(),scope:'all'}),updates=[];
  const node=(type,text='')=>({type,text,props:{},children:[],parent:null});
  const detach=child=>{if(child.parent){const children=child.parent.children;const index=children.indexOf(child);if(index>=0)children.splice(index,1);child.parent=null;}};
  const renderer=vue.createRenderer({createElement:tag=>node(tag),createText:text=>node('text',text),createComment:()=>node('comment'),
    insert:(child,parent,anchor)=>{detach(child);child.parent=parent;const at=anchor?parent.children.indexOf(anchor):-1;if(at<0)parent.children.push(child);else parent.children.splice(at,0,child);},
    remove:detach,setText:(target,text)=>target.text=text,setElementText:(target,text)=>{target.text=text;target.children=[];},
    patchProp:(target,key,_previous,value)=>target.props[key]=value,parentNode:target=>target.parent,
    nextSibling:target=>target.parent?.children[target.parent.children.indexOf(target)+1]??null});
  const app=renderer.createApp({render:()=>vue.h('main',{},[
    vue.h('div',{'data-parent-leader-theme':selection.value}),
    vue.h(component,{...props,...(controlled?{modelValue:selection.value}:{}),'onUpdate:modelValue':value=>{updates.push(value);if(controlled&&autoUpdate)selection.value=value;}}),
  ])});
  app.component('RouterLink',{inheritAttrs:false,props:['to'],setup:(props,{attrs,slots})=>()=>vue.h('a',{...attrs,to:props.to},slots.default?.())});
  const root=node('root');app.mount(root);
  const all=()=>{const rows=[];const walk=target=>{rows.push(target);for(const child of target.children)walk(child);};walk(root);return rows;};
  const textOf=node=>node.text+node.children.map(textOf).join('');
  const hasClass=(node,name)=>String(node.props.class??'').split(' ').includes(name);
  const themeButton=name=>all().find(node=>node.type==='button'&&hasClass(node,'theme-select')&&textOf(node).startsWith(name));
  const assetButton=name=>all().find(node=>node.type==='button'&&hasClass(node,'theme-bubble')&&textOf(node).startsWith(name));
  const clearButton=()=>all().find(node=>node.type==='button'&&/^\d+ ×$/.test(textOf(node)));
  const tableRows=()=>all().filter(node=>node.type==='tbody').flatMap(node=>node.children.filter(child=>child.type==='tr'));
  const parentTheme=()=>all().find(node=>Object.hasOwn(node.props,'data-parent-leader-theme')).props['data-parent-leader-theme'];
  return {selection,props,updates,all,textOf,themeButton,assetButton,clearButton,tableRows,parentTheme,
    unmount:()=>{app.unmount();globalThis.window=saved.window;globalThis.localStorage=saved.localStorage;}};
}

test('controlled map theme and asset clicks update the parent theme used by the leaderboard',async()=>{
  const env=mountMap();
  try{
    await flush();assert.equal(env.tableRows().length,2);
    env.themeButton('腾讯控股').props.onClick();await flush();
    assert.deepEqual(env.updates,['700']);assert.equal(env.selection.value,'700');assert.equal(env.parentTheme(),'700');
    assert.equal(env.themeButton('腾讯控股').props['aria-pressed'],true);
    assert.equal(env.tableRows().length,1);assert.match(env.textOf(env.tableRows()[0]),/腾讯关联资产/);
    env.assetButton('快手关联资产').props.onClick();await flush();
    assert.deepEqual(env.updates,['700','1024']);assert.equal(env.parentTheme(),'1024');
    assert.equal(env.tableRows().length,1);assert.match(env.textOf(env.tableRows()[0]),/快手关联资产/);
    env.clearButton().props.onClick();await flush();
    assert.deepEqual(env.updates,['700','1024','']);assert.equal(env.selection.value,'');assert.equal(env.parentTheme(),'');
    assert.equal(env.tableRows().length,2);assert.equal(env.clearButton(),undefined);
  }finally{env.unmount();}
});

test('external controlled changes synchronize normalized ticker without emitting a feedback loop',async()=>{
  const env=mountMap({initial:'700'});
  try{
    await flush();assert.equal(env.themeButton('腾讯控股').props['aria-pressed'],true);
    env.selection.value='01024';await flush();
    assert.equal(env.themeButton('快手').props['aria-pressed'],true);assert.equal(env.tableRows().length,1);
    assert.match(env.textOf(env.tableRows()[0]),/快手关联资产/);assert.deepEqual(env.updates,[]);
    env.selection.value='';await flush();assert.equal(env.tableRows().length,2);assert.deepEqual(env.updates,[]);
  }finally{env.unmount();}
});

test('a controlled selected theme absent from a partial map remains empty, never returns to all markets',async()=>{
  const env=mountMap({initial:'1024'});
  try{
    await flush();env.props.themeMap=map([first]);await flush();
    assert.equal(env.parentTheme(),'1024');assert.equal(env.tableRows().length,0);
    assert.equal(env.textOf(env.clearButton()),'1024 ×');assert.deepEqual(env.updates,[]);
    env.selection.value='9999';env.props.themeMap=map();await flush();
    assert.equal(env.parentTheme(),'9999');assert.equal(env.tableRows().length,0);assert.equal(env.textOf(env.clearButton()),'9999 ×');
    env.props.themeMap=map([first]);await flush();assert.equal(env.tableRows().length,0);assert.deepEqual(env.updates,[]);
  }finally{env.unmount();}
});

test('uncontrolled map preserves local selection and clears a vanished theme as before',async()=>{
  const env=mountMap({controlled:false});
  try{
    await flush();env.assetButton('快手关联资产').props.onClick();await flush();
    assert.equal(env.tableRows().length,2);assert.equal(env.clearButton(),undefined);assert.deepEqual(env.updates,[]);
    env.themeButton('快手').props.onClick();await flush();assert.equal(env.tableRows().length,1);
    env.props.themeMap=map([first]);await flush();assert.equal(env.tableRows().length,1);
    assert.match(env.textOf(env.tableRows()[0]),/腾讯关联资产/);assert.equal(env.clearButton(),undefined);
    env.themeButton('腾讯控股').props.onClick();await flush();assert.ok(env.clearButton());
    env.clearButton().props.onClick();await flush();assert.equal(env.clearButton(),undefined);
  }finally{env.unmount();}
});

test('map child navigation keeps homepage context and display mode on return',async()=>{
  let env=mountMap();
  await flush();env.all().find(node=>node.type==='button'&&env.textOf(node)==='列表').props.onClick();await flush();env.unmount();
  env=mountMap({restore:true});
  try{await flush();assert.equal(env.all().find(node=>node.type==='button'&&env.textOf(node)==='列表').props['aria-pressed'],true);
    const link=env.all().find(node=>node.type==='a'&&node.props.to?.path.startsWith('/asset/')).props.to;assert.equal(link.query.tab,'overview');assert.equal(link.query.from,'live');assert.equal(link.query.back,'/live?chain=all');
  }finally{env.unmount();}
});
