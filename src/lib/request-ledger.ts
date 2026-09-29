import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, readFileSync } from 'node:fs';
import { dirname } from 'node:path';
let db: DatabaseSync | undefined;
function ledger() {
  if (db) return db;
  const path = process.env.OKX_LEDGER_PATH || 'data/okx-budget.sqlite';
  mkdirSync(dirname(path), { recursive: true });
  db = new DatabaseSync(path);
  db.exec('PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS budget(day TEXT PRIMARY KEY, used INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS lane_budget(day TEXT,lane TEXT,used INTEGER NOT NULL,PRIMARY KEY(day,lane)); CREATE TABLE IF NOT EXISTS request_rate(provider TEXT PRIMARY KEY,next_at INTEGER NOT NULL)');
  return db;
}
export function sharedUsage(day: string, limit?: number, lane?: string, laneLimit?: number): number {
  const db = ledger();
  db.exec('BEGIN IMMEDIATE');
  try {
    let seed = 0;
    try { const old = JSON.parse(readFileSync('data/okx-usage.json', 'utf8')); if (old.day === day) seed = Math.max(0, Number(old.daily) || 0); } catch {}
    db.prepare('INSERT OR IGNORE INTO budget(day,used) VALUES (?,?)').run(day, seed);
    const used = Number((db.prepare('SELECT used FROM budget WHERE day=?').get(day) as {used:number}).used);
    if (limit !== undefined && used >= limit) throw new Error('OKX local request budget exhausted');
    if(limit !== undefined && lane && laneLimit !== undefined){
      db.prepare('INSERT OR IGNORE INTO lane_budget VALUES (?,?,0)').run(day,lane);
      const count=Number((db.prepare('SELECT used FROM lane_budget WHERE day=? AND lane=?').get(day,lane) as {used:number}).used);
      if(count>=laneLimit)throw new Error(`OKX ${lane} daily allowance exhausted`);
      db.prepare('UPDATE lane_budget SET used=used+1 WHERE day=? AND lane=?').run(day,lane);
    }
    if (limit !== undefined) db.prepare('UPDATE budget SET used=used+1 WHERE day=?').run(day);
    db.exec('COMMIT');
    return used + (limit === undefined ? 0 : 1);
  } catch (e) { db.exec('ROLLBACK'); throw e; }
}
export function reserveRequestSlot(intervalMs=500,now=Date.now()):number{
  const db=ledger();db.exec('BEGIN IMMEDIATE');
  try{
    const row=db.prepare("SELECT next_at FROM request_rate WHERE provider='okx'").get() as {next_at:number}|undefined;
    const start=Math.max(now,Number(row?.next_at??now));
    db.prepare("INSERT INTO request_rate VALUES ('okx',?) ON CONFLICT(provider) DO UPDATE SET next_at=excluded.next_at").run(start+Math.max(400,intervalMs));
    db.exec('COMMIT');return Math.max(0,start-now);
  }catch(e){db.exec('ROLLBACK');throw e;}
}
