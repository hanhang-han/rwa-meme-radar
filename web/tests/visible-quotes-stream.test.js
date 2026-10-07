import test from 'node:test';
import assert from 'node:assert/strict';
import { createVisibleQuoteStream } from '../src/utils/visible-quotes-stream.js';
const token='0x'+'a'.repeat(40),other='0x'+'b'.repeat(40);
const frame=(event,data,id)=>`${id==null?'':`id: ${id}\r\n`}event: ${event}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
const tick=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
function harness(cursor=7){
  const calls=[],timers=[],quotes=[],statuses=[],resets=[],controllers=[];
  const stream=createVisibleQuoteStream({apiBase:'/dashboard/api/',tokens:[`56:${token}`],getCursor:()=>cursor,onQuote:q=>quotes.push(q),onStatus:s=>statuses.push(s),onReset:p=>resets.push(p),
    schedule:(fn,ms)=>{const task={fn,ms,cancelled:false};timers.push(task);return task;},cancel:task=>{if(task)task.cancelled=true;},
    fetchImpl:async(url,options)=>{
      calls.push({url,options});
      const body=new ReadableStream({start(controller){controllers.push(controller);options.signal.addEventListener('abort',()=>{try{controller.close();}catch{}});}});
      return {ok:true,body};
    }});
  const send=(event,data,id,index=controllers.length-1)=>controllers[index].enqueue(new TextEncoder().encode(frame(event,data,id)));
  return {stream,calls,timers,quotes,statuses,resets,controllers,send};
}

test('one scoped connection replays from the HTTP cursor, ignoring hello upper bound and unrelated assets',async()=>{
  const h=harness();h.stream.start();await tick();
  const q=new URL(h.calls[0].url,'https://local.test').searchParams;
  assert.equal(q.get('scope'),'quotes');assert.equal(q.get('tokens'),`56:${token}`);assert.equal(q.get('candles'),'none');assert.equal(q.get('trades'),'none');assert.equal(q.get('after'),'7');
  h.send('hello',{cursor:100});h.send('heartbeat',{at:123});h.send('price',{chainId:'56',token:other,price:9},8);
  h.send('price',{chainId:'56',token,price:2},9);await tick();
  assert.equal(h.quotes.length,1);assert.equal(h.quotes[0].price,2);
  h.send('price',{chainId:'56',token,price:3},9);await tick();assert.equal(h.quotes.length,1);
  h.stream.stop();await tick();assert.equal(h.calls[0].options.signal.aborted,true);
});

test('checkpoint cursor survives reconnect and stop cancels every future retry',async()=>{
  const h=harness();h.stream.start();await tick();h.send('checkpoint',{cursor:25},25);await tick();
  h.controllers[0].close();await tick();
  const retry=h.timers.find(t=>t.ms===1000&&!t.cancelled);assert.ok(retry);retry.fn();await tick();
  assert.equal(new URL(h.calls[1].url,'https://local.test').searchParams.get('after'),'25');
  h.stream.stop();await tick();
  for(const task of h.timers.filter(t=>!t.cancelled))task.fn();await tick();assert.equal(h.calls.length,2);
});

test('reset reconciles and reconnects in the new cursor range; it cannot freshen a quote',async()=>{
  const h=harness(999);h.stream.start();await tick();h.send('hello',{cursor:1000});h.send('reset',{cursor:3,reason:'expired'},3);await tick();
  assert.equal(h.resets.length,1);assert.equal(h.quotes.length,0);
  h.timers.find(t=>t.ms===1000&&!t.cancelled).fn();await tick();assert.equal(new URL(h.calls[1].url,'https://local.test').searchParams.get('after'),'3');h.stream.stop();
});

test('invalid identities and overlarge incomplete frames never create unbounded subscriptions or buffers',async()=>{
  let count=0;
  for(const tokens of [[],['56:bad'],Array.from({length:81},(_,i)=>`56:0x${i.toString(16).padStart(40,'0')}`)]){
    createVisibleQuoteStream({apiBase:'/',tokens,fetchImpl:()=>{count++;}}).start();
  }
  assert.equal(count,0);
  const h=harness();h.stream.start();await tick();h.controllers[0].enqueue(new TextEncoder().encode('x'.repeat(65537)));await tick();
  assert.equal(h.calls[0].options.signal.aborted,true);assert.equal(h.quotes.length,0);h.stream.stop();
});
