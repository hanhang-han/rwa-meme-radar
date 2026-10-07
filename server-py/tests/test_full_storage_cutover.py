"""Real six-domain prepare/enable tests; PM2 is the only external boundary.

NODE_STORAGE_TEST_SETTINGS must name a private disposable PostgreSQL database.
The 59-table old generation includes the production 17-table account schema;
the real explicit installers add six Telegram tables and their three indexes.
No receipt, typed certificate, migration, recovery, or installer is mocked.
"""
import ast
import hashlib
import importlib.util
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import storage_migration_worker, storage_runtime
from app.aux_storage_schema import TELEGRAM_TABLES, declared_account_tables
from app.db import SCHEMA
from app.demand_leases import SCHEMA as LEASE_SCHEMA
from app.live_market_store import RAW_LOG_SCHEMA
from app.realtime_schema import REALTIME_SCHEMA, trigger_schema
from app.storage_migration import ShadowMigration, inspect_source, quote, sqlite_row, typed_row_bytes
from app.storage_recovery import RECOVERY_CHANGES, RECOVERY_OWNER, RecoveryMirror
from app.storage_schema import PG_TRIGGER_VERSION


PROJECT = Path(__file__).resolve().parents[2]
SOURCE_FILES = {
    'market': 'live-market.sqlite', 'research': 'research.sqlite',
    'accounts': 'developer-access.sqlite', 'budget': 'okx-budget.sqlite',
    'leases': 'research.sqlite.leases.sqlite', 'stream_archive': 'stream-events.sqlite',
}
DOMAIN_TABLES = {'market': 17, 'research': 18, 'accounts': 17,
                 'budget': 3, 'leases': 2, 'stream_archive': 2}


class PrivateConfig(dict):
    def __repr__(self):
        return '<private disposable PostgreSQL configuration>'


class CutoverFixture(SimpleNamespace):
    def __repr__(self):
        return '<disposable six-domain cutover fixture>'


def config_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cutover():
    spec = importlib.util.spec_from_file_location(
        'full_storage_cutover_e2e', PROJECT / 'scripts/prepare-full-storage-cutover.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_factory_source_schema(connection):
    # Extract actual literal DDL without importing RPC clients or invoking a
    # collector. These are the two additional production research tables.
    module = PROJECT / 'server-py/app/collectors/factory_discovery.py'
    for node in ast.walk(ast.parse(module.read_text())):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.lstrip().upper().startswith('CREATE')
                and 'factory_discovery_' in node.value):
            connection.executescript(node.value)


def create_source(domain, path):
    with closing(sqlite3.connect(path)) as connection:
        if domain in ('research', 'market'):
            connection.executescript(SCHEMA + REALTIME_SCHEMA + RAW_LOG_SCHEMA)
            if domain == 'research':
                install_factory_source_schema(connection)
                connection.executescript(trigger_schema())
            else:
                connection.execute('CREATE TABLE live_market_identity(name TEXT PRIMARY KEY,value TEXT NOT NULL)')
                connection.execute("INSERT INTO live_market_identity VALUES('identity','native-market-v1')")
            connection.execute('INSERT INTO facts VALUES(?,?,?)',
                               ('196:asset', domain, '{"name":"中文🙂","zero":0,"missing":null}'))
            connection.execute('INSERT INTO facts VALUES(?,?,?)', ('196:asset', 'other', '{"price":0}'))
            for name, body in [('text', '中文🙂'), ('binary', b'\x00\xff\x01')]:
                connection.execute('INSERT INTO dashboard_projection VALUES(?,?,?,?,?,?)',
                                   (name, 1, 0, 0, body, 1790000000000))
            connection.execute('INSERT INTO samples VALUES(?,?,?,?)',
                               ('196:token', 1790000000000, .000000000000012345, None))
        elif domain == 'accounts':
            for name, table in declared_account_tables().items():
                if name not in TELEGRAM_TABLES:
                    connection.execute(table['sql'])
                    for index in table['indexes']:
                        connection.execute(index)
            connection.execute('INSERT INTO users VALUES(?,?,?,?,?,NULL)',
                               ('user', 'e2e@example.com', b'\x00salt', b'\xffhash', 1790000000000))
        elif domain == 'budget':
            connection.executescript('''CREATE TABLE budget(day TEXT PRIMARY KEY,used INTEGER NOT NULL);
                CREATE TABLE lane_budget(day TEXT,lane TEXT,used INTEGER NOT NULL,PRIMARY KEY(day,lane));
                CREATE TABLE request_rate(provider TEXT PRIMARY KEY,next_at INTEGER NOT NULL);''')
            connection.execute("INSERT INTO budget VALUES('e2e',7)")
        elif domain == 'leases':
            connection.executescript(LEASE_SCHEMA)
            connection.execute("INSERT INTO leases VALUES('detail','asset','{}',1790000100000)")
        else:
            # Preserve the existing two-table stream archive, rather than
            # replacing history with the six-table current research journal.
            connection.executescript('''CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT,body TEXT,at INTEGER);
                CREATE TABLE latest(key TEXT PRIMARY KEY,event TEXT,body TEXT,at INTEGER);''')
            connection.execute("INSERT INTO events(event,body,at) VALUES('price','{}',1790000000000)")
            connection.execute("INSERT INTO latest VALUES('asset','price','{}',1790000000000)")
        connection.commit()
    assert len(inspect_source(path)['tables']) == DOMAIN_TABLES[domain]


def table_bytes(path):
    result = {}
    with closing(sqlite3.connect(path)) as connection:
        for table in inspect_source(path)['tables']:
            columns = ','.join(quote(column['name']) for column in table['columns'])
            result[table['name']] = [typed_row_bytes(row) for row in connection.execute(
                f'SELECT rowid,{columns} FROM {quote(table["name"])} ORDER BY rowid')]
    return result


@pytest.fixture
def cutover_pg(tmp_path, monkeypatch):
    settings = os.environ.get('NODE_STORAGE_TEST_SETTINGS')
    if not settings:
        pytest.skip('NODE_STORAGE_TEST_SETTINGS must name a private disposable test database config')
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    private = PrivateConfig(json.loads(Path(settings).read_text()))
    if not conninfo_to_dict(private['dsn']).get('dbname', '').startswith(('cliperx_node_test_', 'cliperx_test_')):
        raise ValueError('cutover-fixture-requires-disposable-test-database')
    # All six namespaces go to this disposable DB; never inherit an optional
    # market route from a host configuration that may name a production DB.
    config = PrivateConfig({'managedBy': 'cliperx-architecture-v1', 'dsn': private['dsn'],
                            'storage': {'enabled': False, 'backend': 'sqlite'}})
    schemas = {domain: 'cliperx_storage_' + domain for domain in SOURCE_FILES}
    with psycopg.connect(config['dsn'], autocommit=True) as admin:
        if any(admin.execute('SELECT to_regnamespace(%s)', (schema,)).fetchone()[0] is not None
               for schema in schemas.values()):
            raise ValueError('cutover-fixture-refuses-existing-storage-schema')
    root = tmp_path / 'project'
    (root / 'data').mkdir(parents=True)
    (root / '.releases').mkdir()
    sources = {domain: root / 'data' / name for domain, name in SOURCE_FILES.items()}
    runtime = root / 'data/architecture-runtime.json'
    runtime.write_text(json.dumps(config))
    runtime.chmod(0o600)
    (root / 'data/storage-migration-health.json').write_text(json.dumps({'phase': 'stopped-before-cutover'}))
    cutover = load_cutover()
    monkeypatch.setattr(cutover, 'ROOT', root)
    monkeypatch.setattr(storage_migration_worker, 'DOMAINS', tuple(
        (domain, str(sources[domain])) for domain, _ in storage_migration_worker.DOMAINS))
    monkeypatch.chdir(root)
    processes = [{'name': name, 'pid': 0, 'pm2_env': {'status': 'stopped', 'pm_cwd': str(root)}}
                 for name in cutover.SERVICES]

    def pm2_run(arguments, **kwargs):
        assert arguments == ['/www/server/nodejs/v22.22.0/bin/pm2', 'jlist']
        assert kwargs['capture_output'] and kwargs['check'] and kwargs['timeout'] == 10
        return SimpleNamespace(stdout=json.dumps(processes))

    monkeypatch.setattr(cutover.subprocess, 'run', pm2_run)
    created = []
    try:
        for domain, path in sources.items():
            create_source(domain, path)
            migration = ShadowMigration(config, path, schemas[domain], batch_rows=100)
            created.append(schemas[domain])
            try:
                migration.prepare()
                for table in migration.tables:
                    list(migration.backfill(table))
                list(migration.reconcile_ranges())
            finally:
                migration.close()
        yield CutoverFixture(cutover=cutover, root=root, config=config, runtime=runtime,
                             sources=sources, schemas=schemas, processes=processes,
                             originalConfigSha256=config_digest(runtime),
                             baseline={domain: table_bytes(path) for domain, path in sources.items()})
    finally:
        if created:
            with psycopg.connect(config['dsn'], autocommit=True) as admin:
                for schema in reversed(created):
                    admin.execute('DROP SCHEMA IF EXISTS ' + quote(schema) + ' CASCADE')


def assert_old_writer_closed(path):
    with closing(sqlite3.connect(path)) as connection:
        name = next(table['name'] for table in inspect_source(path)['tables']
                    if connection.execute('SELECT 1 FROM ' + quote(table['name']) + ' LIMIT 1').fetchone())
        column = connection.execute('PRAGMA table_info(' + quote(name) + ')').fetchone()[1]
        with pytest.raises(sqlite3.OperationalError):
            connection.execute('UPDATE ' + quote(name) + ' SET ' + quote(column) + '=' + quote(column) +
                               ' WHERE rowid=(SELECT rowid FROM ' + quote(name) + ' LIMIT 1)')
        connection.rollback()


def assert_exact_standby(env, domain):
    mirror = RecoveryMirror(env.config, env.sources[domain], env.schemas[domain], domain=domain)
    try:
        actual = table_bytes(env.sources[domain])
        for name, table in mirror.tables.items():
            columns = ','.join(quote(column['name']) for column in table['columns'])
            rows = mirror.pg.execute(f'SELECT _cliperx_source_rowid,{columns} FROM {quote(name)} '
                                     'ORDER BY _cliperx_source_rowid').fetchall()
            assert actual[name] == [typed_row_bytes(sqlite_row(table, row)) for row in rows]
        assert mirror.status()['pendingRows'] == 0
        assert mirror.verify_barrier()['standbyReady']
    finally:
        mirror.close()


def test_real_prepare_adds_six_empty_tables_and_mirrors_markers_before_enable(cutover_pg):
    env = cutover_pg
    result = env.cutover.prepare(120)
    receipt_path = Path(result['receipt'])
    receipt = json.loads(receipt_path.read_text())
    assert result['phase'] == 'prepared' and result['businessTables'] == 65
    assert result['runtimeCutover'] is False and config_digest(env.runtime) == env.originalConfigSha256
    assert len(receipt['forward']) == len(receipt['recovery']) == 6
    assert sum(len(row['tables']) for row in receipt['forward']) == 59
    assert sum(len(row['tables']) for row in receipt['recovery']) == 65
    for field in ('accountTargetAddition', 'accountStandbyAddition'):
        assert set(receipt[field]['installedTables']) == set(TELEGRAM_TABLES)
        assert len(receipt[field]['installedIndexes']) == 3
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    for domain, path in env.sources.items():
        assert_old_writer_closed(path)
        assert_exact_standby(env, domain)
        actual = table_bytes(path)
        for name, rows in env.baseline[domain].items():
            if name != 'realtime_schema_version':
                assert actual[name] == rows
        if domain in ('research', 'market'):
            with closing(sqlite3.connect(path)) as connection:
                markers = dict(connection.execute('SELECT name,value FROM realtime_schema_version'))
            assert markers['postgres-write-order'] == PG_TRIGGER_VERSION
            if domain == 'research':
                assert markers['postgres-triggers'] == PG_TRIGGER_VERSION
            proof = next(row for row in receipt['recovery'] if row['domain'] == domain)
            assert proof['sourceSequence'] >= (2 if domain == 'research' else 1)
        if domain == 'accounts':
            assert all(actual[name] == [] for name in TELEGRAM_TABLES)
    activated = env.cutover.enable(receipt_path)
    config = PrivateConfig(json.loads(env.runtime.read_text()))
    enabled = json.loads(receipt_path.read_text())
    assert config['storage'] == {'enabled': True, 'backend': 'postgres', 'domainSchemas': env.schemas}
    assert activated['runtimeCutover'] is True and activated['fullMigrationComplete'] is False
    assert enabled['phase'] == 'enabled-awaiting-runtime-acceptance'
    assert len(enabled['recovery']) == 6
    assert all(row['standbyReady'] and row['foreignKeysValid'] for row in enabled['recovery'])


@pytest.mark.parametrize('field,corruption', [('forward', 'missing'), ('forward', 'duplicate'),
                                            ('recovery', 'missing'), ('recovery', 'duplicate')])
def test_enable_rejects_missing_or_duplicate_domain_proofs(cutover_pg, field, corruption):
    env = cutover_pg
    prepared = env.cutover.prepare(120)
    path = Path(prepared['receipt'])
    receipt = json.loads(path.read_text())
    if corruption == 'missing':
        receipt[field].pop()
    else:
        receipt[field].append(receipt[field][0])
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        env.cutover.enable(path)
    assert config_digest(env.runtime) == env.originalConfigSha256


def test_enable_rejects_changed_runtime_configuration(cutover_pg):
    env = cutover_pg
    prepared = env.cutover.prepare(120)
    config = PrivateConfig(json.loads(env.runtime.read_text()))
    config['changedAfterPreparation'] = True
    env.runtime.write_text(json.dumps(config))
    changed = config_digest(env.runtime)
    with pytest.raises(ValueError, match='unverified-cutover-receipt'):
        env.cutover.enable(prepared['receipt'])
    assert config_digest(env.runtime) == changed and config['storage']['enabled'] is False


def test_enable_rejects_new_postgres_write_after_preparation(cutover_pg):
    env = cutover_pg
    prepared = env.cutover.prepare(120)
    mirror = RecoveryMirror(env.config, env.sources['research'], env.schemas['research'], domain='research')
    try:
        mirror.pg.execute("UPDATE facts SET body=%s WHERE kind='196:asset' AND id='research'",
                          ('{"late":true}',))
        assert mirror.pg.execute(f'SELECT 1 FROM {RECOVERY_CHANGES} LIMIT 1').fetchone()
    finally:
        mirror.close()
    with pytest.raises(ValueError, match='pending-cdc'):
        env.cutover.enable(prepared['receipt'])
    assert config_digest(env.runtime) == env.originalConfigSha256
    assert_old_writer_closed(env.sources['research'])


@pytest.mark.parametrize('process_kind', ['duplicate', 'extra-running'])
def test_prepare_requires_every_project_instance_stopped(cutover_pg, process_kind):
    env = cutover_pg
    extra = {'name': env.cutover.SERVICES[0] if process_kind == 'duplicate' else 'unknown-writer',
             'pid': 4242, 'pm2_env': {'status': 'online', 'pm_cwd': str(env.root)}}
    env.processes.append(extra)
    with pytest.raises(ValueError, match='writer-barrier-not-held|unexpected-project-process-running'):
        env.cutover.prepare(120)
    assert config_digest(env.runtime) == env.originalConfigSha256
    for domain, path in env.sources.items():
        assert table_bytes(path) == env.baseline[domain]
    import psycopg
    with psycopg.connect(env.config['dsn'], autocommit=True) as connection:
        assert all(connection.execute('SELECT to_regclass(%s)', (schema + '.' + RECOVERY_OWNER,)).fetchone()[0] is None
                   for schema in env.schemas.values())


def test_helper_failure_keeps_authority_off_and_real_recovery_can_abort(cutover_pg, monkeypatch):
    env = cutover_pg
    original = storage_runtime.runtime_support_sql

    def injected(schema):
        if schema == env.schemas['research']:
            raise RuntimeError('injected-runtime-helper-failure')
        return original(schema)

    # Fault injection changes only this call outcome: every completed migration,
    # capture, marker, mirror, range, and subsequent restoration remains real.
    monkeypatch.setattr(storage_runtime, 'runtime_support_sql', injected)
    with pytest.raises(RuntimeError, match='injected-runtime-helper-failure'):
        env.cutover.prepare(120)
    assert config_digest(env.runtime) == env.originalConfigSha256
    assert all(row['pm2_env']['status'] == 'stopped' and row['pid'] == 0 for row in env.processes)
    receipt = json.loads(next((env.root / '.releases').glob('storage-cutover-*/receipt.json')).read_text())
    assert receipt['phase'] == 'prepare-failed'
    assert receipt['preparedRecoveryDomains'] == ['market', 'research']
    assert receipt['attemptedRecoveryDomains'] == ['market', 'research']
    for domain in receipt['preparedRecoveryDomains']:
        assert_old_writer_closed(env.sources[domain])
        mirror = RecoveryMirror(env.config, env.sources[domain], env.schemas[domain], domain=domain)
        try:
            while mirror.replay():
                pass
            list(mirror.reconcile_ranges(max_ranges=None))
            verified = mirror.verify_barrier()
            restored = mirror.restore_sqlite()
            archive = mirror.abort()
            assert archive['restoredReceipt'] == restored
            assert archive['sourceSequence'] == verified['sourceSequence']
            assert archive['recoveryRemoved'] and archive['foreignKeysValid']
            assert {row['table'] for row in archive['rangeCertificates']} == set(mirror.tables)
            assert mirror.pg.execute('SELECT to_regclass(%s)',
                                     (mirror.schema + '.' + RECOVERY_OWNER,)).fetchone()[0] is None
        finally:
            mirror.close()
    assert config_digest(env.runtime) == env.originalConfigSha256
