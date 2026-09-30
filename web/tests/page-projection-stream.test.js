import test from 'node:test';
import assert from 'node:assert/strict';
import {createPinia,setActivePinia} from 'pinia';
import {useDashboardStore,applyFieldOperations} from '../src/stores/dashboard.js';
import {useDetailStore} from '../src/stores/detail.js';
import {applyAssetProjection,handleStreamEvent,startStream,stopStream} from '../src/composables/useStream.js';

const snapshot=(scope='overview',revision=10)=>({now:1000,realtime:{schema:1,revision,cursor:100},unified:{snapshotScope:scope,
 assets:[{projectionKey:'196:a',chainId:'196',token:'a',price:1,risk:{status:'unknown'}},{projectionKey:'196:b',chainId:'196',token:'b',price:2}],
 stockTokens:[],relations:[],sectors:[],metrics:{assets:1300,newAssets24h:10},hotStocks:[{ticker:'NVDA',assetCount:10},{ticker:'TSLA',assetCount:8}]}});
const packet=(revision=11,baseRevision=10)=>({schema:2,scope:'overview',revision,baseRevision,now:2000,
 upserts:{assets:[{projectionKey:'196:new',chainId:'196',token:'new',price:0}]},
 patches:{assets:[['196:a',[[['price'],null],[['risk','status'],'triggered']]]]},removes:{assets:['196:b']},orders:{assets:['196:new','196:a']},
 metaOps:[[['metrics','assets'],1301],[['metrics','newAssets24h'],11],[['hotStocks'],[{ticker:'TSLA',assetCount:11},{ticker:'NVDA',assetCount:10}]]]});
const setup=()=>{setActivePinia(createPinia());const dash=useDashboardStore();dash.setProjectionScope('overview');dash.acceptSnapshot(snapshot(),'overview');return dash;};

test('scoped updates keep canonical identity, exact ranking, new rows, null values and total counts',()=>{
 const dash=setup(),old=dash.assets[0];assert.equal(dash.applyProjection(packet()),true);
 assert.deepEqual(dash.assets.map(row=>row.token),['new','a']);assert.equal(dash.assets[1],old);assert.equal(old.price,null);assert.equal(old.risk.status,'triggered');
 assert.equal(dash.metrics.assets,1301);assert.equal(dash.metrics.newAssets24h,11);assert.equal(dash.snapshot.unified.hotStocks[0].ticker,'TSLA');assert.equal(dash.snapshot.now,2000);
 dash.applyProjection(packet());assert.equal(dash.assets.length,2,'same-revision replay is idempotent');
});

test('newer field patches replay over a slow same-view snapshot while retaining unknown data',()=>{
 const dash=setup();dash.applyProjection(packet());dash.acceptSnapshot(snapshot(),'overview');
 assert.equal(dash.revision,11);assert.equal(dash.assets[1].price,null);assert.equal(dash.metrics.newAssets24h,11);
 const kept=structuredClone(packet());dash.applyProjection({...kept,revision:12,baseRevision:11,upserts:{},removes:{},orders:{},patches:{assets:[['196:a',[[['price'],5]]]]}});
 assert.equal(dash.assets[1].price,5);assert.equal(dash.recentDeltas[0].patches.assets[0][1][0][1],null,'buffered packet stays immutable');
});

test('a missing predecessor or missing row cannot partially mutate the page',()=>{
 const dash=setup();const initial=JSON.stringify(dash.snapshot);
 assert.throws(()=>dash.applyProjection(packet(12,11)),/projection-base-mismatch/);assert.equal(JSON.stringify(dash.snapshot),initial);
 const malformed=packet();malformed.patches.assets.push(['196:missing',[[['price'],10]]]);
 assert.throws(()=>dash.applyProjection(malformed),/projection-row-missing/);assert.equal(JSON.stringify(dash.snapshot),initial);
});

test('scope changes discard foreign replay and allow market to compact back to overview',()=>{
 const dash=setup();dash.applyProjection(packet());dash.setProjectionScope('market');dash.acceptSnapshot(snapshot('market',12),'market');
 assert.equal(dash.snapshot.unified.snapshotScope,'market');assert.equal(dash.recentDeltas.length,0);
 dash.applyProjection(packet(13,12));assert.equal(dash.revision,12,'late overview frame must not patch market');
 dash.setProjectionScope('overview');dash.acceptSnapshot(snapshot('overview',14),'overview');
 assert.equal(dash.snapshot.unified.snapshotScope,'overview');assert.equal(dash.revision,14);
});

test('field edits distinguish deletion from null/zero and reject prototype paths',()=>{
 const old={a:5,b:8,c:[1,2],nested:{unknown:2}};
 const next=applyFieldOperations(old,[[['a'],null],[['b']],[['c',0],0],[['nested','unknown'],null]]);
 assert.deepEqual(next,{a:null,c:[0,2],nested:{unknown:null}});assert.equal(old.a,5);assert.equal(old.b,8);
 assert.throws(()=>applyFieldOperations(old,[[['__proto__','polluted'],true]]),/invalid-projection-path/);assert.equal({}.polluted,undefined);
});

test('asset scope updates only the selected detail canonical price and relations with full section evidence intact',()=>{
 setup();const detail=useDetailStore();detail.watch('196','a');
 const entry={data:{revision:10,asset:{token:'a',price:1,exchangeMarkets:[{marketId:'explicit'}]},relations:[{chainId:'196',id:'old'}],holders:[{address:'holder'}]},sections:{relations:1}};
 detail.cache.set('196:a',entry);detail.cache.set('56:a',{data:{asset:{price:777}}});
 applyAssetProjection({scope:'asset',asset:'196:a',revision:11,upserts:{assets:[{chainId:'196',token:'a',price:2,primaryQuote:{provider:'CoinGecko'}}],relations:[{chainId:'196',id:'new',token:'a'}]},removes:{relations:['196:old']}});
 assert.equal(entry.data.asset.price,2);assert.equal(entry.data.asset.exchangeMarkets[0].marketId,'explicit');assert.equal(entry.data.holders[0].address,'holder');
 assert.deepEqual(entry.data.relations.map(row=>row.id),['new']);assert.equal(entry.data.relationCount,1);assert.equal(detail.cache.get('56:a').data.asset.price,777);
});

test('early discovery and relation frames never expand a scoped overview preview',async()=>{
 const dash=setup();await handleStreamEvent('discovery',JSON.stringify({id:'discover',chainId:'196',t:2000,asset:{token:'unranked',symbol:'NEW'}}));
 await handleStreamEvent('relationship',JSON.stringify({id:'relation',chainId:'196',t:2000,relation:{id:'pair',token:'unranked'}}));
 assert.deepEqual(dash.assets.map(row=>row.token),['a','b']);assert.equal(dash.relations.length,0);
});

const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(fn){for(let i=0;i<150;i++){if(fn())return;await pause(20);}assert.fail('stream transition timed out');}

test('page transition gets a new scoped snapshot cursor and return to overview remains compact',async()=>{
 setActivePinia(createPinia());const dash=useDashboardStore(),oldFetch=global.fetch,requests=[];
 global.fetch=async(input,options={})=>{
  const url=String(input);requests.push(url);
  if(url.includes('/dashboard?')){const scope=new URL(url,'http://local').searchParams.get('view');const data=snapshot(scope,scope==='market'?30:20);data.realtime.cursor=scope==='market'?300:200;return new Response(JSON.stringify(data));}
  if(url.includes('/stream?'))return new Response(new ReadableStream({start(c){c.enqueue(new TextEncoder().encode('event: hello\ndata: {"schema":1,"cursor":999}\n\n'));options.signal.addEventListener('abort',()=>c.error(new Error('abort')),{once:true});}}));
  if(url.endsWith('/feed'))return new Response(JSON.stringify({trades:[],relationships:[]}));
  throw new Error(url);
 };
 try{
  startStream();await until(()=>requests.filter(x=>x.includes('/stream?')).length===1);
  dash.requestedView='market';await until(()=>requests.filter(x=>x.includes('/stream?')).length===2);
  dash.requestedView='overview';await until(()=>requests.filter(x=>x.includes('/stream?')).length===3);
  const urls=requests.filter(x=>x.includes('/stream?')).map(x=>new URL(x,'http://local'));
  assert.deepEqual(urls.map(x=>x.searchParams.get('scope')),['overview','market','overview']);
  assert.deepEqual(urls.map(x=>x.searchParams.get('after')),['200','300','200']);
  assert.ok(urls.every(x=>x.searchParams.get('protocol')==='2'));assert.equal(dash.snapshot.unified.snapshotScope,'overview');
 }finally{stopStream();global.fetch=oldFetch;}
});

test('a gap reconciles a published snapshot and reconnects from its cursor without acknowledging the failed patch',async()=>{
 setActivePinia(createPinia());const dash=useDashboardStore(),oldFetch=global.fetch,streams=[];let reads=0;
 global.fetch=async(input,options={})=>{
  const url=String(input);
  if(url.includes('/dashboard?')){reads+=1;const data=snapshot('overview',reads===1?10:12);data.realtime.cursor=reads===1?100:120;data.unified.metrics.assets=reads===1?1300:1302;return new Response(JSON.stringify(data));}
  if(url.includes('/stream?')){
   streams.push(url);const first=streams.length===1;
   return new Response(new ReadableStream({start(c){
    options.signal.addEventListener('abort',()=>c.error(new Error('abort')),{once:true});
    if(first)c.enqueue(new TextEncoder().encode(`id: 110\nevent: projection.delta\ndata: ${JSON.stringify(packet(12,11))}\n\n`));
    else c.enqueue(new TextEncoder().encode('event: hello\ndata: {"schema":1,"cursor":120}\n\n'));
   }}));
  }
  throw new Error(url);
 };
 try{startStream();await until(()=>streams.length===2);assert.equal(new URL(streams[1],'http://local').searchParams.get('after'),'120');assert.equal(dash.revision,12);assert.equal(dash.metrics.assets,1302);assert.equal(reads,2);}
 finally{stopStream();global.fetch=oldFetch;}
});

test('removing a stock mapping keeps a coexisting asset quote and reconciles only that identity',()=>{
 setup();const detail=useDetailStore();
 const entry={at:100,data:{revision:10,asset:{chainId:'196',token:'a',price:17,primaryQuote:{provider:'CoinGecko'}},stock:{tokenContractAddress:'a',price:17},relations:[]}};
 const other={at:100,data:{asset:{chainId:'56',token:'a',price:88}}};
 detail.cache.set('196:a',entry);detail.cache.set('56:a',other);
 applyAssetProjection({scope:'asset',asset:'196:a',revision:11,removes:{stockTokens:['196:a']}});
 assert.equal(entry.data.stock,null);assert.equal(entry.data.asset.price,17);assert.equal(entry.data.asset.primaryQuote.provider,'CoinGecko');
 assert.equal(entry.at,0,'the deleted mapping requests exact summary reconciliation');assert.equal(other.at,100,'other-chain detail is not invalidated');assert.equal(other.data.asset.price,88);
 applyAssetProjection({scope:'asset',asset:'196:a',revision:12,removes:{assets:['196:a']}});
 assert.equal(entry.data.asset.price,null,'only an actual asset deletion clears its quote');assert.equal(entry.data.asset.quoteReason,'asset-removed');
});

test('a failed request from the previous scope cannot overwrite the current page error state',async()=>{
 const dash=setup(),oldFetch=global.fetch;let rejectOld;
 global.fetch=input=>String(input).includes('view=overview')?new Promise((_,reject)=>{rejectOld=reject;}):Promise.resolve(new Response(JSON.stringify(snapshot('market',12))));
 try{
  const oldRequest=dash.poll({view:'overview'});
  dash.setProjectionScope('market');await dash.poll({view:'market'});
  assert.equal(dash.error,null);rejectOld(new Error('old overview disconnected'));await oldRequest;
  assert.equal(dash.error,null);assert.equal(dash.snapshot.unified.snapshotScope,'market');assert.equal(dash.revision,12);
 }finally{global.fetch=oldFetch;}
});

test('canonical missing fields clear stale quotes while lazy detail sections survive',()=>{
 setup();const detail=useDetailStore();
 const markets=[{marketId:'explicit'}],pools=[{poolId:'pool'}],holders={count:100,distribution:{top10Percent:20}};
 const entry={data:{revision:10,asset:{token:'a',price:17,volume24h:45,quoteAt:99,fieldTimes:{price:99},primaryQuote:{provider:'old'},exchangeMarkets:markets,poolMarkets:pools,risk:{rawEvidence:'section-only'}},holdersSummary:holders,marketTrades:[{id:'kept'}]}};
 detail.cache.set('196:a',entry);
 applyAssetProjection({scope:'asset',asset:'196:a',revision:11,canonicalFields:{assets:['token','price','volume24h','quoteAt','fieldTimes','primaryQuote','exchangeMarkets','quoteStatus']},upserts:{assets:[{token:'a',quoteStatus:'missing'}]}});
 for(const field of ['price','volume24h','quoteAt','fieldTimes','primaryQuote'])assert.equal(entry.data.asset[field],undefined,field+' must not retain a prior quote');
 assert.equal(entry.data.asset.quoteStatus,'missing');assert.deepEqual(entry.data.asset.exchangeMarkets,markets);assert.deepEqual(entry.data.asset.poolMarkets,pools);assert.deepEqual(entry.data.holdersSummary,holders);assert.equal(entry.data.marketTrades[0].id,'kept');assert.equal(entry.data.asset.risk.rawEvidence,'section-only');
});

test('an asset packet arriving before its HTTP cache waits for that cache before acknowledgement',async()=>{
 const dash=setup(),detail=useDetailStore(),oldFetch=global.fetch;detail.watch('196','a');dash.setProjectionScope('asset');let release;
 global.fetch=()=>new Promise(resolve=>{release=()=>resolve(new Response(JSON.stringify({revision:10,realtime:{schema:1,revision:10,cursor:90},asset:{chainId:'196',token:'a',price:1},relations:[]})));});
 try{
  let completed=false;const pending=handleStreamEvent('projection.delta',JSON.stringify({scope:'asset',asset:'196:a',schema:1,revision:11,upserts:{assets:[{chainId:'196',token:'a',price:2}]}})).then(result=>{completed=true;return result;});
  await pause(0);assert.equal(completed,false);release();assert.equal(await pending,true);assert.equal(detail.currentData.asset.price,2);assert.equal(detail.currentData.revision,11);
 }finally{global.fetch=oldFetch;}
});

test('asset subscription uses the detail snapshot cursor and waits for its parallel request',async()=>{
 const dash=setup(),detail=useDetailStore(),oldFetch=global.fetch,streams=[];let releaseDetail;
 // The overview was published later than the in-flight detail summary.
 dash.snapshot.realtime.cursor=500;dash.snapshot.realtime.revision=50;dash.revision=50;
 detail.watch('196','a');
 global.fetch=async(input,options={})=>{
  const url=String(input);
  if(url.includes('/token/'))return new Promise(resolve=>{releaseDetail=()=>resolve(new Response(JSON.stringify({revision:10,realtime:{schema:1,revision:10,cursor:100},asset:{chainId:'196',token:'a',price:1},relations:[]})));});
  if(url.includes('/stream?')){streams.push(url);return new Response(new ReadableStream({start(c){
   options.signal.addEventListener('abort',()=>c.error(new Error('abort')),{once:true});
   c.enqueue(new TextEncoder().encode(`id: 110\nevent: projection.delta\ndata: ${JSON.stringify({schema:1,scope:'asset',asset:'196:a',revision:11,upserts:{assets:[{chainId:'196',token:'a',price:2}]}})}\n\n`));
  }}));}
  if(url.endsWith('/feed'))return new Response(JSON.stringify({trades:[],relationships:[]}));
  throw new Error('unexpected '+url);
 };
 try{
  const pending=detail.fetch('196','a',{force:true});startStream();await pause(30);assert.equal(streams.length,0,'no stream before the actual detail snapshot is ready');
  releaseDetail();await pending;await until(()=>streams.length===1&&detail.currentData.asset.price===2);
  assert.equal(new URL(streams[0],'http://local').searchParams.get('after'),'100','a later overview cursor must never skip asset revision 11');
  assert.equal(detail.currentData.asset.price,2);assert.equal(detail.currentData.revision,11);
 }finally{stopStream();global.fetch=oldFetch;}
});

test('an old API global packet cannot expand a scoped page during a rolling deployment',async()=>{
 const dash=setup(),oldFetch=global.fetch;const next=snapshot('overview',12);next.unified.metrics.assets=1302;
 global.fetch=async input=>{assert.match(String(input),/view=overview/);return new Response(JSON.stringify(next));};
 const legacy={schema:1,revision:12,upserts:{assets:[{chainId:'56',token:'foreign',projectionKey:'56:foreign',price:99}]},meta:{snapshotScope:'full',metrics:{assets:999999}}};
 try{
  assert.equal(dash.applyProjection(legacy),false);assert.equal(dash.snapshot.unified.snapshotScope,'overview');
  assert.equal(await handleStreamEvent('projection.delta',JSON.stringify(legacy)),true);
  assert.deepEqual(dash.assets.map(row=>row.token),['a','b']);assert.equal(dash.metrics.assets,1302);assert.equal(dash.snapshot.unified.snapshotScope,'overview');assert.equal(dash.stream.projection,false);assert.equal(dash.stream.compatibilityWarning,'scoped-stream-unavailable');
 }finally{global.fetch=oldFetch;}
});

test('a stock quote for a shared identity cannot replace the candidate asset primary quote',()=>{
 setup();const detail=useDetailStore();const entry={data:{revision:10,asset:{chainId:'196',token:'a',kind:'candidate',price:9,primaryQuote:{provider:'CoinGecko'}},stock:{tokenContractAddress:'a',price:10},relations:[]}};
 detail.cache.set('196:a',entry);
 applyAssetProjection({scope:'asset',asset:'196:a',revision:11,upserts:{stockTokens:[{chainId:'196',tokenContractAddress:'a',price:12}]}});
 assert.equal(entry.data.stock.price,12);assert.equal(entry.data.asset.price,9);assert.equal(entry.data.asset.primaryQuote.provider,'CoinGecko');
});
