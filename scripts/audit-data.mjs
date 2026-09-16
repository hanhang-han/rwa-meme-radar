// Read-only production audit. Reads our cached APIs; never calls upstream providers.
const base=(process.argv[2]||'https://cliperx.com/dashboard').replace(/\/$/,'');
const get=async path=>{const response=await fetch(base+path,{signal:AbortSignal.timeout(40000)});if(!response.ok)throw new Error(`${path}: HTTP ${response.status}`);return response.json();};
const snapshot=await get('/api/dashboard'),u=snapshot.unified,now=snapshot.now;
const id=r=>r.assetId??`${r.chainId}:${String(r.token??r.tokenContractAddress).toLowerCase()}`;
const duplicateCount=rows=>rows.length-new Set(rows.map(id)).size;
const assets=new Set(u.assets.map(id)),stocks=new Set(u.stockTokens.map(id));
const findings={duplicateCandidates:duplicateCount(u.assets),duplicateDeployments:duplicateCount(u.stockTokens),orphanRelations:u.relations.filter(r=>!assets.has(id(r))||!stocks.has(`${r.chainId}:${r.stock.toLowerCase()}`)).length,
 negativeValues:u.assets.filter(a=>['price','volume24h','liquidity','buys24h','sells24h','holders'].some(f=>a[f]!=null&&a[f]<0)).length,
 futureFieldTimes:u.assets.filter(a=>Object.values(a.fieldTimes??{}).some(t=>t>now+60000)).length};
const samples=[];
for(const chain of ['196','56','4663']){
 const rs=u.relations.filter(r=>r.chainId===chain&&r.status==='verified').sort((a,b)=>(b.liquidityUsd??0)-(a.liquidityUsd??0));
 const r=rs[0];if(!r)continue;
 const d=await get(`/api/token/${chain}/${r.token}`);
 samples.push({chain,address:r.token,symbol:d.asset?.symbol,relations:d.relations?.length,samples:d.samples?.length,trades:d.trades?.length,tradeAt:d.asset?.tradeAt??null,tradeCoverage:d.asset?.tradeCoverage??null,priceAt:d.asset?.fieldTimes?.price??null});
}
const result={snapshotAt:new Date(now).toISOString(),collection:u.collection,quality:u.quality??null,findings,counts:{candidates:u.assets.length,deployments:u.stockTokens.length,relations:u.relations.length},samples,
 targets:u.stockTokens.filter(s=>['1','1024','1038'].includes(s.stockCode)).map(s=>({chain:s.chainId,code:s.stockCode,symbol:s.tokenSymbol,identity:s.stockIdentity??null})),
 baskets:u.sectors.filter(s=>s.members>0).map(s=>({chain:s.chainId,sector:s.sector,members:s.members,current:s.value!=null,lastAt:s.lastAt??null}))};
console.log(JSON.stringify(result,null,2));
if(Object.values(findings).some(n=>n>0))process.exitCode=2;
