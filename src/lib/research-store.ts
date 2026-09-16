import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

// Persistent facts and evidence are independent of API pagination and caches.
export class ResearchStore {
  private db: DatabaseSync;
  constructor(file = 'data/research.sqlite', private scope = '196') {
    if (file !== ':memory:') mkdirSync(dirname(file), { recursive: true });
    this.db = new DatabaseSync(file);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS facts (kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(kind,id));
      CREATE TABLE IF NOT EXISTS samples (asset TEXT NOT NULL, t INTEGER NOT NULL, price REAL NOT NULL, cap REAL, PRIMARY KEY(asset,t));
      CREATE TABLE IF NOT EXISTS trades (asset TEXT NOT NULL,id TEXT NOT NULL,t INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(asset,id));
      CREATE INDEX IF NOT EXISTS trades_time ON trades(asset,t);
      CREATE TABLE IF NOT EXISTS candles (asset TEXT NOT NULL, bar TEXT NOT NULL, openTime INTEGER NOT NULL,
        open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
        volume REAL, volumeUsd REAL, confirmed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(asset,bar,openTime));
      CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, asset TEXT NOT NULL,t INTEGER NOT NULL,body TEXT NOT NULL);`);
    // Version 1 records belong to X Layer. Migrate all identities atomically;
    // the JSON bodies and original observation timestamps remain unchanged.
    if (Number((this.db.prepare('PRAGMA user_version').get() as any).user_version) < 2) {
      this.db.exec(`BEGIN IMMEDIATE;
        UPDATE facts SET kind='196:'||kind;
        UPDATE samples SET asset='196:'||asset;
        UPDATE trades SET asset='196:'||asset;
        UPDATE events SET id='196:'||id,asset='196:'||asset;
        PRAGMA user_version=2; COMMIT;`);
    }
  }
  private key(value:string) { return `${this.scope}:${value}`; }
  get<T>(kind: string, id: string): T | null {
    const row = this.db.prepare('SELECT body FROM facts WHERE kind=? AND id=?').get(this.key(kind), id);
    return row ? JSON.parse(String(row.body)) : null;
  }
  all<T>(kind: string): T[] {
    return this.db.prepare('SELECT body FROM facts WHERE kind=?').all(this.key(kind)).map(row => JSON.parse(String(row.body)));
  }
  put(kind: string, id: string, value: unknown) {
    this.db.prepare('INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body').run(this.key(kind), id, JSON.stringify(value));
  }
  event(id: string, asset: string, value: Record<string, unknown>, now = Date.now()) {
    this.db.prepare('INSERT OR IGNORE INTO events VALUES (?,?,?,?)').run(this.key(id), this.key(asset), now, JSON.stringify({ ...value, id, asset, chainId:this.scope, t: now }));
  }
  events(asset?: string, limit = 100): any[] {
    const rows = asset ? this.db.prepare('SELECT body FROM events WHERE asset=? ORDER BY t DESC LIMIT ?').all(this.key(asset),limit)
      : this.db.prepare('SELECT body FROM events WHERE asset LIKE ? ORDER BY t DESC LIMIT ?').all(this.key('%'),limit);
    return rows.map(row => JSON.parse(String(row.body)));
  }
  sample(asset: string, price: number, cap: number | null, now = Date.now()) {
    if (!Number.isFinite(price) || price <= 0) return;
    this.db.prepare('INSERT INTO samples VALUES (?,?,?,?) ON CONFLICT(asset,t) DO UPDATE SET price=excluded.price,cap=excluded.cap')
      .run(this.key(asset), Math.floor(now / 300000) * 300000, price, cap);
  }
  samples(asset: string, limit = 288): {t:number;price:number;cap:number|null}[] {
    return this.db.prepare('SELECT t,price,cap FROM samples WHERE asset=? ORDER BY t DESC LIMIT ?').all(this.key(asset), limit).reverse() as any;
  }
  candles(asset: string, bar: string, rows: { t:number;o:number;h:number;l:number;c:number;v:number|null;vu:number|null;confirmed:boolean }[]) {
    const upsert = this.db.prepare(`INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(asset,bar,openTime) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,volumeUsd=excluded.volumeUsd,confirmed=excluded.confirmed`);
    this.db.exec('BEGIN');
    try {
      for (const r of rows) upsert.run(this.key(asset), bar, r.t, r.o, r.h, r.l, r.c, r.v, r.vu, r.confirmed ? 1 : 0);
      this.db.exec('COMMIT');
    } catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  candleRange(asset: string, bar: string, limit = 300) {
    return this.db.prepare('SELECT openTime t,open o,high h,low l,close c,volume v,volumeUsd vu,confirmed FROM candles WHERE asset=? AND bar=? ORDER BY openTime DESC LIMIT ?')
      .all(this.key(asset), bar, limit).reverse().map((r:any) => ({ ...r, confirmed: !!r.confirmed }));
  }
  hasTrade(asset: string, id: string) { return !!this.db.prepare('SELECT 1 FROM trades WHERE asset=? AND id=?').get(this.key(asset),id); }
  trades(asset: string, rows: any[]) {
    this.db.exec('BEGIN');
    try {
      const insert = this.db.prepare('INSERT OR IGNORE INTO trades VALUES (?,?,?,?)');
      for (const r of rows) insert.run(this.key(asset), r.id, r.t, JSON.stringify(r));
      this.db.exec('COMMIT');
    } catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  recentTrades(asset: string, limit = 30): any[] {
    return this.db.prepare('SELECT body FROM trades WHERE asset=? ORDER BY t DESC LIMIT ?').all(this.key(asset), limit).map(row => JSON.parse(String(row.body)));
  }
  activity(asset: string, since: number) {
    const rows = this.db.prepare('SELECT body FROM trades WHERE asset=? AND t>=?').all(this.key(asset), since).map(r => JSON.parse(String(r.body)));
    return { buys: rows.filter(r=>r.type==='buy').length, sells: rows.filter(r=>r.type==='sell').length,
      volume: rows.every(r=>r.volume != null) ? rows.reduce((n,r)=>n+r.volume,0) : null,
      count: rows.length, traders: new Set(rows.map(r=>r.user).filter(Boolean)).size };
  }
  close() { this.db.close(); }
  eventsPage(before?: {t:number;id:string}, limit=50) {
    const rows=this.db.prepare(`SELECT body FROM events WHERE asset LIKE ? AND (t < ? OR (t = ? AND id < ?)) ORDER BY t DESC,id DESC LIMIT ?`)
      .all(this.key('%'),before?.t??Number.MAX_SAFE_INTEGER,before?.t??Number.MAX_SAFE_INTEGER,this.key(before?.id??'~'),limit+1)
      .map(r=>JSON.parse(String(r.body)));
    const hasMore=rows.length>limit, items=rows.slice(0,limit), last=items.at(-1);
    return {items,next:hasMore&&last?{t:last.t,id:last.id}:null};
  }
}
