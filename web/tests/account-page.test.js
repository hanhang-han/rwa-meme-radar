import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parse,compileScript} from '@vue/compiler-sfc';
import * as vue from 'vue';
import * as format from '../src/utils/format.js';
import * as navigation from '../src/utils/navigation-context.js';
import * as pageState from '../src/utils/page-navigation-state.js';
import * as chain from '../src/utils/chain-scope.js';
import * as accountStatus from '../src/utils/account-status.js';
import * as preferences from '../src/utils/product-preferences.js';
const now=Date.now(),token='0x'+'a'.repeat(40),key='56:'+token;
const flush=async()=>{for(let i=0;i<12;i++){await Promise.resolve();await vue.nextTick();}};
const blank=vue.defineComponent({render:()=>null});
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
const session=id=>({user:{id,email:id+'@example.com'},csrfToken:'csrf'});
const settings={newPool:true,riskChange:true,largeTrade:false,tradeUsd:10000};
function compile(file,imports){
  const compiled=compileScript(parse(readFileSync(new URL(`../src/${file}.vue`,import.meta.url),'utf8')).descriptor,{id:file.replaceAll('/','-'),inlineTemplate:true}).content
    .replace(/^import\s+\{([^}]+)\}\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,bindings,path)=>`const {${bindings.replace(/\s+as\s+/g,':')}}=imports[${JSON.stringify(path)}];`)
    .replace(/^import\s+(\w+)\s+from\s+['"]([^'"]+)['"];?\s*$/gm,(_,name,path)=>`const ${name}=imports[${JSON.stringify(path)}];`)
    .replace('export default','return');
  return new Function('imports',compiled)(imports);
}

function mount(file,{route={path:'/me',fullPath:'/me?chain=all',query:{chain:'all'}},imports={},props={},router}={}){
  const saved={setInterval:globalThis.setInterval,clearInterval:globalThis.clearInterval,window:globalThis.window,document:globalThis.document,Document:globalThis.Document,ShadowRoot:globalThis.ShadowRoot,localStorage:globalThis.localStorage};
  const doc={activeElement:null,addEventListener(){},removeEventListener(){}};
  globalThis.setInterval=()=>1;globalThis.clearInterval=()=>{};globalThis.document=doc;globalThis.window={addEventListener(){},removeEventListener(){}};globalThis.localStorage={getItem:()=>null,setItem(){}};
  globalThis.Document=class {};globalThis.ShadowRoot=class {};
  const component=compile(file,{vue,'vue-router':{useRoute:()=>route,useRouter:()=>({push(){},replace(){}})},'../i18n':{tr:zh=>zh,useI18n:()=>({lang:vue.reactive({lang:'zh'})})},
    '../utils/format':format,'../utils/navigation-context':navigation,'../utils/page-navigation-state':pageState,'../utils/chain-scope':chain,
    ...imports});
  const node=(type,text='')=>({type,tagName:type.toUpperCase(),text,props:{},children:[],parent:null,style:{},ownerDocument:doc,getRootNode:()=>doc,addEventListener(){},removeEventListener(){},setAttribute(){},removeAttribute(){},focus(){},blur(){}});
  const detach=child=>{if(child.parent){const children=child.parent.children,index=children.indexOf(child);if(index>=0)children.splice(index,1);child.parent=null;}};
  const renderer=vue.createRenderer({createElement:tag=>{const target=node(tag);if(tag==='select')Object.defineProperty(target,'options',{get:()=>target.children.filter(child=>child.tagName==='OPTION')});return target;},createText:text=>node('text',text),createComment:()=>node('comment'),
    insert:(child,parent,anchor)=>{detach(child);child.parent=parent;const at=anchor?parent.children.indexOf(anchor):-1;if(at<0)parent.children.push(child);else parent.children.splice(at,0,child);},
    remove:detach,setText:(target,text)=>target.text=text,setElementText:(target,text)=>{target.text=text;target.children=[];},patchProp:(target,key,_previous,value)=>{target.props[key]=value;if(key==='type')target.type=value;if(key==='value'){target.value=value;target._value=value;}if(key==='multiple')target.multiple=value;},
    parentNode:target=>target.parent,nextSibling:target=>target.parent?.children[target.parent.children.indexOf(target)+1]??null});
  const app=renderer.createApp({render:()=>vue.h(component,props)});
  if(router)app.use(router);
  if(!router)app.component('RouterLink',{inheritAttrs:false,props:['to'],setup:(props,{slots,attrs})=>()=>vue.h('a',{...attrs,to:props.to},slots.default?.())});
  const root=node('root');app.mount(root);
  const all=()=>{const rows=[];const walk=target=>{rows.push(target);target.children.forEach(walk);};walk(root);return rows;};
  const textOf=node=>node.text+node.children.map(textOf).join('');
  const findClass=name=>all().find(node=>String(node.props.class??'').split(' ').includes(name));
  const button=label=>all().find(node=>node.tagName==='BUTTON'&&textOf(node)===label);
  return {route,all,textOf,findClass,button,unmount:()=>{app.unmount();globalThis.setInterval=saved.setInterval;globalThis.clearInterval=saved.clearInterval;globalThis.window=saved.window;globalThis.document=saved.document;globalThis.Document=saved.Document;globalThis.ShadowRoot=saved.ShadowRoot;globalThis.localStorage=saved.localStorage;}};
}

function accountImports(account,request,extra={}){return {'../stores/account':{useAccountStore:()=>account},'../api/developer':{developerRequest:request,loginDeveloper:async()=>session('owner'),registerDeveloper:async()=>session('owner'),...extra},'../api/client':{getDetail:async()=>({asset:{chainId:'56',token,symbol:'REAL'}})},'../utils/product-preferences':preferences,'../utils/account-status':accountStatus,'../components/WalletProfile.vue':blank,'../components/AliasReview.vue':blank};}
const defaultRequest=async path=>path==='alerts'?{...settings}:path==='telegram'?{available:true,linked:true,capabilities:{largeTrade:{available:false}}}:path==='telegram/health'?{configured:true,worker:{status:'ready',updatedAt:now},workerAgeMs:0,recent:[]}:{};

test('unknown session keeps retry distinct from a guest login, and Telegram read failures never become unconfigured',async()=>{
 const account=vue.reactive({ready:true,session:null,sessionState:'error',watches:[],start(){}});
 const env=mount('views/AccountView',{imports:accountImports(account,defaultRequest)});
 try{await flush();assert.ok(env.button('重试登录状态'));assert.equal(env.all().filter(n=>n.tagName==='FORM').length,0);}finally{env.unmount();}
 const logged=vue.reactive({ready:true,session:session('owner'),sessionState:'authenticated',syncState:'error',error:'unavailable',watches:[],start(){},refresh(){}});
 const failed=mount('views/AccountView',{imports:accountImports(logged,async path=>{if(path==='alerts')return {...settings};throw new Error('offline');})});
 try{await flush();const text=failed.textOf(failed.findClass('account-page'));assert.ok(text.includes('owner@example.com'));assert.ok(text.includes('已登录；关注暂未同步'));assert.ok(text.includes('Telegram 状态读取失败'));assert.equal(text.includes('Telegram 渠道尚未开通'),false);}finally{failed.unmount();}
});
test('successful authentication with failed follow synchronization shows the account instead of a login error',async()=>{
 const account=vue.reactive({ready:true,session:null,sessionState:'guest',watches:[],start(){},async useSession(value){this.session=value;this.sessionState='authenticated';this.syncState='error';this.error='unavailable';throw new Error('follow-read-failed');}});
 const env=mount('views/AccountView',{imports:accountImports(account,defaultRequest)});
 try{await flush();const form=env.findClass('account-login');await form.props.onSubmit({preventDefault(){}});await flush();assert.ok(env.textOf(env.findClass('account-summary')).includes('owner@example.com'));assert.ok(env.textOf(env.findClass('account-sync')).includes('已登录；关注暂未同步'));assert.equal(env.findClass('account-login'),undefined);}finally{env.unmount();}
});
test('saving alert rules locks editing, submits only scalar rules and ignores an old account response',async()=>{
 const pending=deferred(),writes=[];
 const account=vue.reactive({ready:true,session:session('old'),sessionState:'authenticated',syncState:'synced',watches:[],start(){}});
 const request=async(path,options)=>{if(options?.method==='PUT'){writes.push(options.body);return pending.promise;}return defaultRequest(path);};
 const env=mount('views/AccountView',{imports:accountImports(account,request)});
 try{await flush();const form=env.findClass('alert-settings').children.find(n=>n.tagName==='FORM');const actual=form??env.all().find(n=>n.tagName==='FORM'&&n.children.some(c=>c.tagName==='FIELDSET'));const task=actual.props.onSubmit({preventDefault(){}});await flush();assert.equal(env.all().find(n=>n.tagName==='FIELDSET').props.disabled,true);assert.deepEqual(writes,[settings]);
   account.session=session('new');await flush();pending.resolve({...settings,newPool:false,riskChange:false});await task;await flush();assert.equal(env.all().find(n=>n.tagName==='FIELDSET').props.disabled,false);assert.equal(env.button('保存规则')!=null,true);assert.equal(env.textOf(env.findClass('save-rule')).includes('已保存'),false);
 }finally{env.unmount();}
});
test('preview responses cannot cross accounts, and displayed asset choices use names and chain labels',async()=>{
 const preview=deferred();const account=vue.reactive({ready:true,session:session('old'),sessionState:'authenticated',syncState:'synced',watches:[key],start(){}});
 const env=mount('views/AccountView',{imports:accountImports(account,async(path,options)=>path==='telegram/dry-run'?preview.promise:defaultRequest(path))});
 try{await flush();assert.ok(env.all().some(n=>n.tagName==='OPTION'&&env.textOf(n).includes('REAL · BNB Chain')));const button=env.button('预览提醒');const form=button.parent;const task=form.props.onSubmit({preventDefault(){}});await flush();account.session=session('new');account.watches=[];await flush();preview.resolve({text:'OLD PRIVATE PREVIEW',eligible:true});await task;await flush();assert.equal(env.textOf(env.findClass('account-page')).includes('OLD PRIVATE PREVIEW'),false);}finally{env.unmount();}
});

test('saved wallet failures are retryable and choosing an address never silently queries or saves it',async()=>{
 const account=vue.reactive({session:session('wallet-owner')});let reads=0,queries=0,saves=0;
 const wallet='0x'+'b'.repeat(40);
 const request=async(path,options)=>{if(path==='capabilities')return {wallet:{available:true,chains:[]}};if(path==='wallet/saved'&&!options){if(++reads===1)throw new Error('offline');return {items:[{address:wallet,label:'主钱包'}]};}if(path==='wallet/profile'){++queries;return {address:wallet,at:now,metrics:{totalValueUsd:0,themeCoverage:0,effectiveThemeCount:1,effectiveHoldingCount:0,themeHHI:null,riskValueShare:null},summary:[],themes:[],holdings:[],unvalued:[]};}++saves;return {};};
 const env=mount('components/WalletProfile',{imports:{'../stores/account':{useAccountStore:()=>account},'../api/developer':{productRequest:request}}});
 try{await flush();assert.ok(env.textOf(env.findClass('saved-wallet-state')).includes('已保存地址读取失败'));assert.equal(env.textOf(env.findClass('wallet-profile')).includes('尚未保存地址'),false);await env.button('重试').props.onClick();await flush();assert.ok(env.button('选择地址 · 主钱包'));env.button('选择地址 · 主钱包').props.onClick();await flush();assert.equal(queries,0);assert.equal(saves,0);assert.ok(env.textOf(env.findClass('wallet-profile')).includes('点击“查看持仓”进行查询'));const form=env.all().find(n=>n.tagName==='FORM');await form.props.onSubmit({preventDefault(){}});await flush();assert.equal(queries,1);assert.equal(saves,0);
 }finally{env.unmount();}
});
