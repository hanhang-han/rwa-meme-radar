import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parse,compileScript} from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as format from '../src/utils/format.js';

test('live numbers show a cent change, flash only visible changes, and preserve tiny prices and units',async()=>{
 const compiled=compileScript(parse(readFileSync(new URL('../src/components/LiveNumber.vue',import.meta.url),'utf8')).descriptor,{id:'live-number-test',inlineTemplate:true}).content
  .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,bindings,path)=>`const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`).replace('export default','return');
 const component=new Function('imports',compiled)({vue,'../utils/format':format,'../i18n':{tr:zh=>zh}});
 const saved={requestAnimationFrame:globalThis.requestAnimationFrame,cancelAnimationFrame:globalThis.cancelAnimationFrame};
 const frames=new Map();let next=0;
 globalThis.requestAnimationFrame=fn=>{frames.set(++next,fn);return next;};globalThis.cancelAnimationFrame=id=>frames.delete(id);
 const node=type=>({type,props:{},children:[],text:''});
 const renderer=vue.createRenderer({createElement:node,createText:text=>({...node('text'),text}),createComment:()=>node('comment'),
  insert:(child,parent)=>{child.parent=parent;parent.children.push(child);},remove:child=>child.parent.children.splice(child.parent.children.indexOf(child),1),
  setText:(target,text)=>target.text=text,setElementText:(target,text)=>target.text=text,patchProp:(target,key,_old,value)=>target.props[key]=value,
  parentNode:target=>target.parent,nextSibling:()=>null});
 const props=vue.reactive({value:4193.12,format:'price',currency:'USD'}),root=node('root'),app=renderer.createApp({render:()=>vue.h(component,props)});
 try{
  app.mount(root);const span=root.children[0];assert.equal(span.text,'$4,193.12');
  props.value=4193.13;await vue.nextTick();assert.equal(span.text,'$4,193.13');assert.equal(frames.size,1);
  for(const [id,fn] of frames){frames.delete(id);fn();}await vue.nextTick();assert.match(span.props.class,/live-number-up/);
  props.value=4193.13001;await vue.nextTick();assert.equal(frames.size,0);assert.equal(span.text,'$4,193.13');
  props.currency='WBNB';props.value=.000002968;await vue.nextTick();assert.equal(frames.size,0);assert.equal(span.text,'0.000002968 WBNB');
  props.value=null;await vue.nextTick();assert.equal(span.text,'—');assert.equal(frames.size,0);
 }finally{app.unmount();for(const [key,value] of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});
