import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {readSnapshot,writeSnapshot} from '../src/lib/snapshot';
test('snapshots preserve BigInt and original freshness timestamps across restarts',()=>{
 const dir=mkdtempSync(join(tmpdir(),'radar-snapshot-'));
 try {
  const file=join(dir,'state.json');
  assert.equal(readSnapshot(file),null);
  const state={radar:{updatedAt:12345,tokens:[{volumeRaw:12345678901234567890n}]},health:{radar:12345}};
  writeSnapshot(file,state);
  assert.deepEqual(readSnapshot(file),state);
  writeFileSync(file,'broken');
  assert.equal(readSnapshot(file),null);
 } finally {rmSync(dir,{recursive:true});}
});
