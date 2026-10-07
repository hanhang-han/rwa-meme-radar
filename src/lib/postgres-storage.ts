import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { Pool, types, type PoolClient, type QueryResultRow } from 'pg';

type Domain = 'research' | 'budget';
const pools = new Map<string, Pool>();
const verifiedSchemas = new Map<string,Promise<void>>();
const domainSchemas = Object.fromEntries(['research','market','accounts','budget','leases','stream_archive'].map(domain=>[domain,`cliperx_storage_${domain}`]));
const identifier = (name:string) => {
  if (!/^[a-z_][a-z0-9_]*$/.test(name)) throw new Error('Invalid PostgreSQL storage identifier');
  return `"${name}"`;
};

function validateConnectionUri(dsn:string) {
  // pg's connectionString parser does not accept libpq key=value strings.
  // Reject them before constructing a pool; interpreting one as a hostname
  // otherwise produces a misleading DNS error and can target the wrong host.
  if(!/^postgres(?:ql)?:\/\//.test(dsn)) {
    throw new Error('PostgreSQL storage DSN must use a postgres:// or postgresql:// URI');
  }
  try { new URL(dsn); }
  catch { throw new Error('PostgreSQL storage connection URI is invalid'); }
}

function configuration() {
  const path = process.env.ARCHITECTURE_SETTINGS || 'data/architecture-runtime.json';
  if (!existsSync(path)) return null;
  let config:any;
  try { config = JSON.parse(readFileSync(path, 'utf8')); }
  catch { throw new Error('Storage configuration cannot be read'); }
  if (!config?.storage?.enabled) return null;
  const schemas=config.storage.domainSchemas;
  if (config.managedBy !== 'cliperx-architecture-v1' || config.storage.enabled !== true
      || config.storage.backend !== 'postgres' || typeof config.dsn !== 'string' || !config.dsn
      || !schemas || Object.keys(schemas).length!==Object.keys(domainSchemas).length
      || Object.entries(domainSchemas).some(([domain,schema])=>schemas[domain]!==schema)) {
    throw new Error('Invalid PostgreSQL storage switch');
  }
  if(config.storageDsns!==undefined&&(typeof config.storageDsns!=='object'||config.storageDsns===null
      ||Object.entries(config.storageDsns).some(([domain,dsn])=>domain!=='market'||typeof dsn!=='string'||!dsn))) {
    throw new Error('Invalid PostgreSQL domain connection override');
  }
  validateConnectionUri(config.dsn);
  return config;
}

export function storageWriterLock(schema:string) {
  return createHash('sha256').update(`cliperx-storage-writer:${schema}`).digest().readBigInt64BE(0).toString();
}

/** One pool per connection string, shared by short-lived read stores and collectors. */
export class PostgresStorage {
  private pool:Pool;
  private verificationKey:string;
  constructor(private schema:string, dsn:string) {
    identifier(schema);
    validateConnectionUri(dsn);
    this.verificationKey=dsn+'\n'+schema;
    let pool = pools.get(dsn);
    if (!pool) {
      pool = new Pool({connectionString:dsn, max:4, idleTimeoutMillis:30_000,
        connectionTimeoutMillis:10_000, allowExitOnIdle:true, application_name:'cliperx-node-storage',
        statement_timeout:15_000, lock_timeout:10_000, query_timeout:30_000,
        options:'-c idle_in_transaction_session_timeout=60000 -c work_mem=2048kB',
        types:{ getTypeParser(oid:number, format?:any) {
          if (oid === 20 && format !== 'binary') return (value:string) => {
            const number = Number(value);
            if (!Number.isSafeInteger(number)) throw new Error('Storage integer exceeds JavaScript precision');
            return number;
          };
          return types.getTypeParser(oid, format);
        } },
      });
      // An idle connection failure must not crash the service. A subsequent
      // query receives its own error and the pool replaces the failed client.
      pool.on('error', () => {});
      pools.set(dsn, pool);
    }
    this.pool = pool;
  }
  table(name:string) { return `${identifier(this.schema)}.${identifier(name)}`; }
  private async verify() {
    let verification=verifiedSchemas.get(this.verificationKey);
    if(!verification){
      verification=(async()=>{
        const {rows:owners}=await this.pool.query(`SELECT source_id,source_schema_hash FROM ${this.table('_cliperx_migration_owner')}`);
        if(owners.length!==1||[owners[0].source_id,owners[0].source_schema_hash].some(value=>typeof value!=='string'||!/^[a-f0-9]{64}$/.test(value)))throw new Error('PostgreSQL schema ownership is unverified');
        const {rows:[manifest]}=await this.pool.query(`SELECT count(*) total,count(*) FILTER(WHERE NOT backfill_done OR NOT indexes_done) pending FROM ${this.table('_cliperx_migration_state')}`);
        if(!Number(manifest.total)||Number(manifest.pending))throw new Error('PostgreSQL historical import is incomplete');
        const {rows:[helper]}=await this.pool.query('SELECT to_regprocedure($1) helper',[this.schema+'._cliperx_sqlite_num(text)']);
        if(!helper?.helper)throw new Error('PostgreSQL runtime support is missing');
        if(this.schema===domainSchemas.research){
          const {rows:markers}=await this.pool.query(`SELECT name,value FROM ${this.table('realtime_schema_version')} WHERE name IN ('postgres-write-order','postgres-triggers')`);
          if(markers.length!==2||markers.some(row=>!/^[a-f0-9]{64}$/.test(row.value))||markers[0].value!==markers[1].value)throw new Error('PostgreSQL research publication triggers are unverified');
        }
      })();
      verifiedSchemas.set(this.verificationKey,verification);
    }
    try{await verification;}catch(error){verifiedSchemas.delete(this.verificationKey);throw error;}
  }
  async query<T extends QueryResultRow = any>(sql:string, values:unknown[] = []) {
    await this.verify();
    return (await this.pool.query<T>(sql, values)).rows;
  }
  async transaction<T>(action:(client:PoolClient)=>Promise<T>):Promise<T> {
    await this.verify();
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      // The Python adapter takes this same lock before sequence allocation,
      // keeping quota changes and shared stream IDs in commit order.
      await client.query('SELECT pg_advisory_xact_lock($1::bigint)', [storageWriterLock(this.schema)]);
      const result = await action(client);
      await client.query('COMMIT');
      return result;
    } catch (error) {
      try { await client.query('ROLLBACK'); } catch {}
      throw error;
    } finally { client.release(); }
  }
}

export function postgresStorage(domain:Domain, file:string):PostgresStorage|null {
  if (file === ':memory:') return null;
  const config = configuration();
  if (!config) return null;
  const expected = domain === 'research'
    ? process.env.RESEARCH_DB || 'data/research.sqlite'
    : process.env.OKX_LEDGER_PATH || 'data/okx-budget.sqlite';
  if (resolve(file) !== resolve(expected)) throw new Error('Unregistered PostgreSQL storage path');
  const schema = config.storage.domainSchemas?.[domain];
  if (typeof schema !== 'string') throw new Error(`PostgreSQL ${domain} schema is missing`);
  return new PostgresStorage(schema, config.dsn);
}

export function postgresResearchEnabled() {
  return !!configuration();
}

export async function closePostgresStoragePools() {
  const current = [...pools.values()];
  pools.clear();
  verifiedSchemas.clear();
  await Promise.all(current.map(pool => pool.end()));
}
