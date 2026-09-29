import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ResearchStore } from '../src/lib/research-store';

test('persistent Node writer caps reusable WAL and remains readable and writable', () => {
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
      store.put('health', 'probe', { ok: true });
      assert.deepEqual(store.get('health', 'probe'), { ok: true });

      const reader = new DatabaseSync(file);
      try {
        assert.equal(reader.prepare('PRAGMA journal_mode').get()!.journal_mode, 'wal');
        assert.equal(reader.prepare('PRAGMA journal_size_limit').get()!.journal_size_limit, original);
        assert.ok(reader.prepare('SELECT body FROM facts WHERE kind=? AND id=?').get('56:health', 'probe'));
      } finally {
        reader.close();
      }
    } finally {
      store.close();
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
