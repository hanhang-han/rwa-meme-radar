import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ResearchStore } from '../src/lib/research-store';

test('persistent Node writer caps reusable WAL and remains readable and writable', async()=> {
  const directory = mkdtempSync(join(tmpdir(), 'research-wal-'));
  const file = join(directory, 'research.sqlite');
  try {
    const initial = new DatabaseSync(file);
    const original = Number(initial.prepare('PRAGMA journal_size_limit').get()!.journal_size_limit);
    initial.close();
    assert.equal(original, -1);

    const store = new ResearchStore(file, '56');
    try {
      const writer = (store as any).db as DatabaseSync;
      assert.equal(writer.prepare('PRAGMA journal_size_limit').get()!.journal_size_limit, 256 * 1024 * 1024);
      (await store.put('health', 'probe', { ok: true }));
      assert.deepEqual((await store.get('health', 'probe')), { ok: true });

      const reader = new DatabaseSync(file);
      try {
        assert.equal(reader.prepare('PRAGMA journal_mode').get()!.journal_mode, 'wal');
        assert.equal(reader.prepare('PRAGMA journal_size_limit').get()!.journal_size_limit, original);
        assert.ok(reader.prepare('SELECT body FROM facts WHERE kind=? AND id=?').get('56:health', 'probe'));
      } finally {
        (await reader.close());
      }
    } finally {
      (await store.close());
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('read-only catalogue reader opens and reads while another connection holds a write transaction', async()=> {
  const directory=mkdtempSync(join(tmpdir(),'research-read-only-'));
  const file=join(directory,'research.sqlite');
  let writer:DatabaseSync|undefined,reader:ResearchStore|undefined;
  try{
    const seed=new ResearchStore(file,'56');
    (await seed.put('stock','token',{chainIndex:'56',tokenContractAddress:'token',price:1}));
    (await seed.close());
    writer=new DatabaseSync(file);
    writer.exec('BEGIN IMMEDIATE');
    writer.prepare('UPDATE facts SET body=? WHERE kind=? AND id=?').run(JSON.stringify({price:2}),'56:stock','token');
    reader=new ResearchStore(file,'56',{readOnly:true});
    assert.equal((await reader.get<any>('stock','token'))?.price,1);
    assert.equal((await reader.all<any>('stock')).length,1);
    await assert.rejects(reader!.put('stock','token',{price:3}),/read-only|readonly/i);
    writer.exec('ROLLBACK');
    assert.equal((await reader.get<any>('stock','token'))?.price,1);
  }finally{
    (await reader?.close());
    if(writer){try{writer.exec('ROLLBACK');}catch{}writer.close();}
    rmSync(directory,{recursive:true,force:true});
  }
});
