import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { postgresStorage, type PostgresStorage } from './postgres-storage';
let db: DatabaseSync | undefined;
let pg:PostgresStorage|null|undefined;
function postgresLedger(){if(pg===undefined)pg=postgresStorage('budget',process.env.OKX_LEDGER_PATH||'data/okx-budget.sqlite');return pg;}
function legacySeed(day:string){
  try{const old=JSON.parse(readFileSync('data/okx-usage.json','utf8'));return old.day===day?Math.max(0,Number(old.daily)||0):0;}catch{return 0;}
}
function ledger() {
  if (db) return db;
  const path = process.env.OKX_LEDGER_PATH || 'data/okx-budget.sqlite';
  mkdirSync(dirname(path), { recursive: true });
  db = new DatabaseSync(path);
  db.exec('PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS budget(day TEXT PRIMARY KEY, used INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS lane_budget(day TEXT,lane TEXT,used INTEGER NOT NULL,PRIMARY KEY(day,lane)); CREATE TABLE IF NOT EXISTS request_rate(provider TEXT PRIMARY KEY,next_at INTEGER NOT NULL)');
  return db;
}
export async function sharedUsage(day:string,limit?:number,lane?:string,laneLimit?:number):Promise<number> {
  const pg=postgresLedger();
  if(pg){
    // Rendering collection status never reserves a writer transaction.
    if(limit===undefined){
      const [row]=await pg.query(`SELECT used FROM ${pg.table('budget')} WHERE day=$1`,[day]);
      return row?Number(row.used):legacySeed(day);
    }
    return pg.transaction(async client=>{
      await client.query(`INSERT INTO ${pg.table('budget')}(day,used) VALUES ($1,$2) ON CONFLICT DO NOTHING`,[day,legacySeed(day)]);
      const {rows:[row]}=await client.query(`SELECT used FROM ${pg.table('budget')} WHERE day=$1`,[day]);
      const used=Number(row.used);
      if(used>=limit)throw new Error('OKX local request budget exhausted');
      if(lane&&laneLimit!==undefined){
        await client.query(`INSERT INTO ${pg.table('lane_budget')}(day,lane,used) VALUES ($1,$2,0) ON CONFLICT DO NOTHING`,[day,lane]);
        const {rows:[laneRow]}=await client.query(`SELECT used FROM ${pg.table('lane_budget')} WHERE day=$1 AND lane=$2`,[day,lane]);
        if(Number(laneRow.used)>=laneLimit)throw new Error(`OKX ${lane} daily allowance exhausted`);
        await client.query(`UPDATE ${pg.table('lane_budget')} SET used=used+1 WHERE day=$1 AND lane=$2`,[day,lane]);
      }
      await client.query(`UPDATE ${pg.table('budget')} SET used=used+1 WHERE day=$1`,[day]);
      return used+1;
    });
  }
  const db = ledger();
  if(limit===undefined){
    const row=db.prepare('SELECT used FROM budget WHERE day=?').get(day) as {used:number}|undefined;
    return row?Number(row.used):legacySeed(day);
  }
  db.exec('BEGIN IMMEDIATE');
  try {
    const seed=legacySeed(day);
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
export async function reserveRequestSlot(intervalMs=500,now=Date.now()):Promise<number>{
  const pg=postgresLedger();
  if(pg)return pg.transaction(async client=>{
    const {rows:[row]}=await client.query(`SELECT next_at FROM ${pg.table('request_rate')} WHERE provider='okx'`);
    const start=Math.max(now,Number(row?.next_at??now));
    await client.query(`INSERT INTO ${pg.table('request_rate')}(provider,next_at) VALUES ('okx',$1) ON CONFLICT(provider) DO UPDATE SET next_at=excluded.next_at`,[start+Math.max(400,intervalMs)]);
    return Math.max(0,start-now);
  });
  const db=ledger();db.exec('BEGIN IMMEDIATE');
  try{
    const row=db.prepare("SELECT next_at FROM request_rate WHERE provider='okx'").get() as {next_at:number}|undefined;
    const start=Math.max(now,Number(row?.next_at??now));
    db.prepare("INSERT INTO request_rate VALUES ('okx',?) ON CONFLICT(provider) DO UPDATE SET next_at=excluded.next_at").run(start+Math.max(400,intervalMs));
    db.exec('COMMIT');return Math.max(0,start-now);
  }catch(e){db.exec('ROLLBACK');throw e;}
}
