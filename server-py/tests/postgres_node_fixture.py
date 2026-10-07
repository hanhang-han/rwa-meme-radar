"""Build all six owned schemas in a disposable Node integration database.

Callers provide a private config whose dsn names a database starting with
cliperx_node_test_ or cliperx_test_. The helper never creates a database and
refuses existing schemas; production storage cannot be used by this fixture.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.aux_storage_schema import TELEGRAM_TABLES, declared_account_tables, install_account_auxiliary_schema
from app.db import SCHEMA
from app.demand_leases import SCHEMA as LEASE_SCHEMA
from app.live_market_store import RAW_LOG_SCHEMA
from app.realtime_schema import REALTIME_SCHEMA, trigger_schema
from app.storage_migration import ShadowMigration, quote
from app.storage_runtime import runtime_support_sql
from app.storage_schema import postgres_runtime_schema_sql


@contextmanager
def prepare_node_pg_fixture(config, directory):
    from psycopg import connect
    from psycopg.conninfo import conninfo_to_dict
    database = conninfo_to_dict(config['dsn']).get('dbname', '')
    if not database.startswith(('cliperx_node_test_', 'cliperx_test_')):
        raise ValueError('node-fixture-requires-disposable-test-database')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    domains = ('research', 'market', 'accounts', 'budget', 'leases', 'stream_archive')
    schemas = {domain: 'cliperx_storage_' + domain for domain in domains}
    paths = {domain: directory / (domain + '.sqlite') for domain in domains}
    migrations = []
    with connect(config['dsn'], autocommit=True) as connection:
        if any(connection.execute('SELECT to_regnamespace(%s)', (schema,)).fetchone()[0] is not None
               for schema in schemas.values()):
            raise ValueError('node-fixture-refuses-existing-storage-schema')
    try:
        for domain, path in paths.items():
            with sqlite3.connect(path) as connection:
                if domain in ('research', 'market'):
                    connection.executescript(SCHEMA + REALTIME_SCHEMA + RAW_LOG_SCHEMA)
                    if domain == 'research':
                        connection.executescript(trigger_schema())
                    else:
                        connection.execute('CREATE TABLE live_market_identity(name TEXT PRIMARY KEY,value TEXT NOT NULL)')
                elif domain == 'accounts':
                    for name, table in declared_account_tables().items():
                        if name not in TELEGRAM_TABLES:
                            connection.execute(table['sql'])
                            for statement in table['indexes']:
                                connection.execute(statement)
                elif domain == 'budget':
                    connection.executescript('''CREATE TABLE budget(day TEXT PRIMARY KEY,used INTEGER NOT NULL);
                        CREATE TABLE lane_budget(day TEXT,lane TEXT,used INTEGER NOT NULL,PRIMARY KEY(day,lane));
                        CREATE TABLE request_rate(provider TEXT PRIMARY KEY,next_at INTEGER NOT NULL);''')
                elif domain == 'leases':
                    connection.executescript(LEASE_SCHEMA)
                else:
                    connection.executescript(REALTIME_SCHEMA)
            migration = ShadowMigration(config, path, schemas[domain], batch_rows=100)
            migrations.append(migration)
            try:
                migration.prepare()
                for table in migration.tables:
                    list(migration.backfill(table))
                if not all(table['match'] for table in migration.verify()['tables']):
                    raise ValueError('node-fixture-history-verification-failed')
                migration.pg.execute(runtime_support_sql(schemas[domain]))
                migration.pg.execute(postgres_runtime_schema_sql(schemas[domain], list(migration.tables),
                                                                 realtime_outbox=domain == 'research'))
                if domain == 'accounts':
                    result = install_account_auxiliary_schema(migration.pg, schemas[domain])
                    if set(result['installedTables']) != set(TELEGRAM_TABLES):
                        raise ValueError('node-fixture-auxiliary-schema-incomplete')
            finally:
                # Do not hold six idle migration connections throughout the
                # eight-thread Python plus four-client Node quota race.
                migration.close()
        runtime = {'managedBy': 'cliperx-architecture-v1', 'dsn': config['dsn'],
                   'storage': {'enabled': True, 'backend': 'postgres', 'domainSchemas': schemas}}
        runtime_path = directory / 'node-runtime.json'
        runtime_path.write_text(json.dumps(runtime))
        runtime_path.chmod(0o600)
        env = {**os.environ, 'NODE_ENV': 'test', 'ARCHITECTURE_SETTINGS': str(runtime_path),
               'CLIPERX_NODE_PG_TEST_SETTINGS': str(runtime_path),
               'RESEARCH_DB': str(paths['research']), 'LIVE_MARKET_DB': str(paths['market']),
               'DEVELOPER_DB_PATH': str(paths['accounts']), 'OKX_LEDGER_PATH': str(paths['budget']),
               'DEMAND_LEASE_DB': str(paths['leases'])}
        yield {'env': env, 'runtimePath': runtime_path, 'paths': paths, 'schemas': schemas}
    finally:
        if migrations:
            with connect(config['dsn'], autocommit=True) as cleanup:
                for migration in reversed(migrations):
                    cleanup.execute('DROP SCHEMA IF EXISTS ' + quote(migration.schema) + ' CASCADE')
