import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { ResearchStore } from '../src/lib/research-store';

test('a smaller valid OKX page retains each durable stock with its original quote time', async () => {
  const directory=mkdtempSync(join(tmpdir(),'okx-catalogue-retention-'));
  const file=join(directory,'research.sqlite');
  const cwd=process.cwd(),originalFetch=globalThis.fetch;
  let writerLock:DatabaseSync|undefined;
  const names=['NODE_ENV','RESEARCH_DB','OKX_LEDGER_PATH','OKX_API_KEY','OKX_SECRET_KEY','OKX_PASSPHRASE'];
  const previous=Object.fromEntries(names.map(name=>[name,process.env[name]]));
  const a='0x'+'a'.repeat(40),b='0x'+'b'.repeat(40),c='0x'+'c'.repeat(40),other='0x'+'d'.repeat(40);
  const oldAt=Date.now()-3_600_000,existingFirstSeen=oldAt-86_400_000;
  const stock=(address:string,price:number,quoteAt?:number)=>({chainIndex:'56',tokenContractAddress:address,
    tokenSymbol:'TESTx',tokenName:'Test stock',stockCode:'TEST',issuer:'Test',price,stockPrice:10,
    volume24h:1,marketCap:100,...(quoteAt?{quoteAt}:{})});
  try {
    process.chdir(directory);
    process.env.NODE_ENV='development';
    process.env.RESEARCH_DB=file;
    process.env.OKX_LEDGER_PATH=join(directory,'okx-budget.sqlite');
    process.env.OKX_API_KEY='test';process.env.OKX_SECRET_KEY='test';process.env.OKX_PASSPHRASE='test';
    const bnb=new ResearchStore(file,'56');
    bnb.put('stock',a,stock(a,1,oldAt));
    bnb.put('stock',b,stock(b,2,oldAt));
    bnb.put('stock',c,stock(c,3));
    bnb.put('asset',b,{token:b,chain:'56',symbol:'TESTx',name:'Test stock',kind:'stock',
      firstSeen:existingFirstSeen,updatedAt:oldAt,price:2,fieldTimes:{price:oldAt}});
    bnb.close();
    const xlayer=new ResearchStore(file,'196');
    xlayer.put('stock',other,{...stock(other,4,oldAt),chainIndex:'196'});
    xlayer.close();
    mkdirSync(join(directory,'data'));
    writeFileSync(join(directory,'data/okx-56.json'),JSON.stringify({status:'ready',updatedAt:oldAt,
      tokens:[stock(a,1,oldAt),stock(c,3)],error:null}));

    globalThis.fetch=(async()=>new Response(JSON.stringify({code:'0',data:{cursor:'',list:[{
      chainIndex:'56',tokenContractAddress:a,tokenSymbol:'TESTx',tokenName:'Test stock',stockCode:'TEST',
      issuer:'Test',price:'9',stockPrice:'10',volume24h:'5',marketCap:'900',
    }]}}))) as typeof fetch;
    const {refreshOkx,restoreOkxCatalogue,extraOkx}=await import('../src/lib/okx');
    const restored=restoreOkxCatalogue('56');
    assert.equal(restored.status,'partial');
    assert.equal(restored.tokens.length,3);
    assert.equal(JSON.parse(readFileSync(join(directory,'data/okx-56.json'),'utf8')).tokens.length,3);
    writerLock=new DatabaseSync(file);
    writerLock.exec('BEGIN IMMEDIATE');
    await refreshOkx('56');
    writerLock.exec('ROLLBACK');writerLock.close();writerLock=undefined;
    const result=extraOkx['56'];
    assert.equal(result.status,'partial');
    assert.match(result.error??'',/omitted 2/);
    assert.equal(result.tokens.length,3);
    assert.equal(result.tokens.some(t=>t.tokenContractAddress===other),false);
    const byAddress=new Map(result.tokens.map(t=>[t.tokenContractAddress,t]));
    assert.equal(byAddress.get(a)?.price,9);
    assert.ok((byAddress.get(a)?.quoteAt??0)>oldAt);
    assert.equal(byAddress.get(b)?.price,2);
    assert.equal(byAddress.get(b)?.quoteAt,oldAt);
    assert.equal(byAddress.get(c)?.price,3);
    assert.equal(byAddress.get(c)?.quoteAt,null);

    const snapshot=JSON.parse(readFileSync(join(directory,'data/okx-56.json'),'utf8'));
    assert.equal(snapshot.status,'partial');
    assert.equal(snapshot.tokens.length,3);
    assert.equal(snapshot.tokens.find((t:any)=>t.tokenContractAddress===b).quoteAt,oldAt);
    assert.equal(snapshot.tokens.find((t:any)=>t.tokenContractAddress===c).quoteAt,null);

    const persisted=new ResearchStore(file,'56');
    assert.equal(persisted.get<any>('stock',a)?.price,1);
    assert.equal(persisted.get<any>('asset',b)?.fieldTimes?.price,oldAt);
    assert.equal(persisted.get<any>('asset',b)?.firstSeen,existingFirstSeen);
    assert.equal(persisted.get<any>('asset',c),null);
    persisted.close();
    const {dashboardState}=await import('../src/lib/dashboard-v2');
    const rows=new Map(dashboardState().stockTokens.filter((t:any)=>t.chainId==='56').map((t:any)=>[t.tokenContractAddress,t]));
    assert.equal((rows.get(b) as any)?.updatedAt,oldAt);
    assert.equal((rows.get(c) as any)?.updatedAt,null);
    const freshQuoteAt=Date.now();
    const quoteWriter=new ResearchStore(file,'56');
    quoteWriter.put('asset',b,{token:b,price:8,fieldTimes:{price:freshQuoteAt},updatedAt:freshQuoteAt});
    assert.equal(quoteWriter.get<any>('asset',b)?.fieldTimes?.price,freshQuoteAt);
    quoteWriter.close();
    const quoted=dashboardState().stockTokens.find((t:any)=>t.chainId==='56'&&t.tokenContractAddress===b) as any;
    assert.equal(quoted.updatedAt,freshQuoteAt);
    assert.equal(quoted.referenceAt,oldAt);
    assert.equal(quoted.premium.value,null);
  } finally {
    if(writerLock){try{writerLock.exec('ROLLBACK');}catch{}writerLock.close();}
    globalThis.fetch=originalFetch;
    process.chdir(cwd);
    for(const name of names)previous[name]===undefined?delete process.env[name]:process.env[name]=previous[name];
    rmSync(directory,{recursive:true,force:true});
  }
});
