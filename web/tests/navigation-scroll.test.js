import test from 'node:test';
import assert from 'node:assert/strict';
import { createNavigationScroll } from '../src/utils/navigation-scroll.js';
const page=(fullPath)=>({fullPath,path:fullPath.split('?')[0],query:Object.fromEntries(new URLSearchParams(fullPath.split('?')[1])),matched:[{}]});
const list=page('/meme?chain=all&page=4'), detail={...page('/asset/56/'+'0x'+'a'.repeat(40)),query:{chain:'all',back:list.fullPath,from:'meme'}};
function fixture(){
  let time=0,ticks=0;const events=new Map();
  const win={scrollY:800,scrollX:0,innerHeight:600,addEventListener:(key,fn)=>events.set(key,fn),removeEventListener:key=>events.delete(key)};
  let rows=[{getAttribute:()=> '56:row',getBoundingClientRect:()=>({top:40,bottom:120,height:80})}];
  const doc={documentElement:{scrollHeight:2400},body:{scrollHeight:2400},querySelectorAll:()=>rows};
  const scroll=createNavigationScroll({getWindow:()=>win,getDocument:()=>doc,now:()=>time,delay:async ms=>{time+=ms;ticks++;fixtureTick?.(ticks);},timeout:1200});
  let fixtureTick;
  return {scroll,win,doc,events,setRows:r=>{rows=r;},onTick:fn=>{fixtureTick=fn;},ticks:()=>ticks};
}
test('app return waits for delayed rows and restores row identity after live reordering',async()=>{
  const f=fixture();f.scroll.capture(detail,list);f.win.scrollY=0;f.setRows([]);f.doc.documentElement.scrollHeight=f.doc.body.scrollHeight=900;
  f.scroll.capture(list,detail);
  f.onTick(n=>{if(n===3){f.doc.documentElement.scrollHeight=f.doc.body.scrollHeight=2400;f.setRows([{getAttribute:()=> '56:row',getBoundingClientRect:()=>({top:960,bottom:1040,height:80})}]);}});
  assert.deepEqual(await f.scroll.scrollBehavior(list,detail,null),{left:0,top:920});assert.ok(f.ticks()>=6);assert.equal(f.events.size,0);
});
test('browser saved position wins and same-page detail settings do not force a scroll',async()=>{
  const f=fixture();f.scroll.capture(detail,list);f.win.scrollY=0;
  assert.deepEqual(await f.scroll.scrollBehavior(list,detail,{left:0,top:450}),{left:0,top:450});
  assert.equal(await f.scroll.scrollBehavior({...detail,fullPath:detail.fullPath+'?tab=trades'},detail,null),false);
  assert.deepEqual(await f.scroll.scrollBehavior(page('/stock?chain=all'),detail,null),{top:0});
});
test('new navigation or user scrolling cancels a pending restoration',async()=>{
  for(const mode of ['navigate','wheel']){
    const f=fixture();f.scroll.capture(detail,list);f.setRows([]);f.win.scrollY=0;
    f.onTick(n=>{if(n===1){if(mode==='wheel')f.events.get('wheel')();else f.scroll.capture(page('/stock?chain=all'),list);}});
    assert.equal(await f.scroll.scrollBehavior(list,detail,null),false);assert.equal(f.events.size,0);
  }
});
