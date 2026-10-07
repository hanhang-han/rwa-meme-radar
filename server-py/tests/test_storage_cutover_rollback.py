"""Certified rollback/retry against disposable real PostgreSQL schemas only."""
import importlib.util
import hashlib
import json
import os
import sqlite3
import uuid
from types import SimpleNamespace
from contextlib import closing
from pathlib import Path

import pytest

from app import storage_runtime
from app import storage_migration_worker
from app.aux_storage_schema import (TELEGRAM_TABLES, declared_account_tables,
    install_account_auxiliary_schema, install_standby_account_auxiliary_schema)
from app.db import SCHEMA
from app.realtime_schema import REALTIME_SCHEMA, trigger_schema
from app.storage_migration import (CONTROL_TABLE, OWNER_TABLE, ShadowMigration,
    _source_id, inspect_source, quote)
from app.storage_recovery import RecoveryMirror
from app.storage_schema import postgres_runtime_schema_sql


def rollback_module():
    path = Path(__file__).resolve().parents[2] / 'scripts/undo-full-storage-cutover.py'
    spec = importlib.util.spec_from_file_location('storage_cutover_rollback', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def drain(mirror):
    while mirror.replay():
        pass
    list(mirror.reconcile_ranges(max_ranges=None))


def test_certified_account_17_to_23_rebase_and_research_outbox_retry(tmp_path, monkeypatch):
    settings = os.environ.get('MIGRATION_TEST_SETTINGS')
    if not settings:
        pytest.skip('Real rollback test needs MIGRATION_TEST_SETTINGS')
    config = json.loads(Path(settings).read_text())
    schemas = {name: 'cliperx_storage_test_' + uuid.uuid4().hex for name in ('accounts', 'research')}
    monkeypatch.setattr(storage_runtime, '_SCHEMAS', {**storage_runtime._SCHEMAS, **schemas})
    declarations = declared_account_tables()
    rollback = rollback_module()
    migrations, mirrors, forward_records, domain_sources = [], [], [], []
    try:
        for domain, schema in schemas.items():
            path = tmp_path / (domain + '.sqlite')
            domain_sources.append((domain, str(path)))
            with closing(sqlite3.connect(path)) as sqlite:
                if domain == 'accounts':
                    for name, table in declarations.items():
                        if name not in TELEGRAM_TABLES:
                            sqlite.execute(table['sql'])
                            for statement in table['indexes']:
                                sqlite.execute(statement)
                    sqlite.execute('INSERT INTO users VALUES(?,?,?,?,?,NULL)',
                                   ('user', 'synthetic@example.com', b'salt', b'hash', 123))
                else:
                    sqlite.executescript(SCHEMA + REALTIME_SCHEMA + trigger_schema())
                    sqlite.execute("INSERT INTO facts VALUES('196:asset','token','{\"value\":1}')")
                sqlite.commit()
            forward = ShadowMigration(config, path, schema, batch_rows=2)
            migrations.append(forward)
            forward.prepare()
            for name in forward.tables:
                list(forward.backfill(name))
            list(forward.reconcile_ranges(max_ranges=None))
            baseline = forward.verify_barrier()
            extras = ()
            if domain == 'accounts':
                assert len(baseline['tables']) == 17
                install_account_auxiliary_schema(forward.pg, schema)
                install_standby_account_auxiliary_schema(path)
                extras = TELEGRAM_TABLES
            mirror = RecoveryMirror(config, path, schema, domain=domain, batch_rows=2)
            mirrors.append(mirror)
            mirror.prepare(baseline, allowed_extra_tables=extras)
            mirror.pg.execute(postgres_runtime_schema_sql(schema, mirror.tables,
                                                          realtime_outbox=domain == 'research'))
            if domain == 'accounts':
                mirror.pg.execute("INSERT INTO telegram_runtime VALUES('first','from-postgres',456,DEFAULT)")
            else:
                mirror.pg.execute("UPDATE facts SET body='{\"value\":2}' WHERE id='token'")
            drain(mirror)
            verified = mirror.verify_barrier()
            mirror.restore_sqlite()
            bundle = mirror.abort()
            result = rollback.certified_forward_reset(mirror, bundle)
            assert result['historyReimported'] is False
            assert result['settingsChanged'] is False
            if domain == 'accounts':
                assert result['businessTables'] == 23
                assert set(result['addedForwardTables']) == set(TELEGRAM_TABLES)
                assert mirror.pg.execute(f'SELECT source_id,source_schema_hash FROM {OWNER_TABLE}').fetchall() == [
                    (_source_id(inspect_source(path)), inspect_source(path)['schemaHash'])]
                states = mirror.pg.execute(f'''SELECT table_name,backfill_done,indexes_done FROM {CONTROL_TABLE}
                    WHERE table_name=ANY(%s)''', (list(TELEGRAM_TABLES),)).fetchall()
                assert len(states) == 6 and all(done and indexed for _, done, indexed in states)
                with closing(sqlite3.connect(path)) as sqlite:
                    names = {row[0] for row in sqlite.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                    assert all('_cliperx_migration_' + name + '_' + operation in names
                               for name in TELEGRAM_TABLES for operation in ('insert', 'update', 'delete'))
                # Exact protocol validation accepts the completed 17→23
                # journal even when no outer checkpoint has been saved yet.
                rollback._validate_forward_journal(inspect_source(path), bundle, allow_new_missing=False)
            assert not mirror.pg.execute('''SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND t.tgname=ANY(%s)''',
                (schema, ['_cliperx_write_order', '_cliperx_identity_rowid', '_cliperx_realtime_change'])).fetchone()
            # Refresh the same importer in place. All tables remain completed;
            # no backfill/COPY is used after the verified reverse recovery.
            forward.source = inspect_source(path)
            forward.source_id = _source_id(forward.source)
            forward.tables = {table['name']: table for table in forward.source['tables']}
            forward.prepare()
            if domain == 'research':
                with closing(sqlite3.connect(path)) as sqlite:
                    before = sqlite.execute('SELECT count(*) FROM change_outbox').fetchone()[0]
                    sqlite.execute("UPDATE facts SET body='{\"value\":3}' WHERE id='token'")
                    sqlite.commit()
                    expected = sqlite.execute('SELECT count(*) FROM change_outbox').fetchone()[0]
                    assert expected == before + 1
                while forward.replay():
                    pass
                list(forward.reconcile_ranges(max_ranges=None))
                assert forward.pg.execute('SELECT count(*) FROM change_outbox').fetchone()[0] == expected
            current = forward.verify_barrier()
            forward_records.append({'domain': domain, **current})
            assert len(current['tables']) == result['businessTables']
            assert all(row['match'] for row in current['tables'])
            mirror.prepare(current)
            mirror.pg.execute(postgres_runtime_schema_sql(schema, mirror.tables,
                                                          realtime_outbox=domain == 'research'))
            if domain == 'accounts':
                mirror.pg.execute("UPDATE telegram_runtime SET value='retry' WHERE key='first'")
            else:
                mirror.pg.execute("UPDATE facts SET body='{\"value\":4}' WHERE id='token'")
            drain(mirror)
            retried = mirror.verify_barrier()
            assert all(row['match'] for row in retried['tables'])
            assert len(retried['tables']) == len(verified['tables'])
        # Exercise the outer command with PostgreSQL still authoritative. Only
        # the OS/PM2 barrier is stubbed; all recovery/certification is real.
        for mirror in mirrors:
            mirror.close()
        for migration in migrations:
            migration.close()
        root = tmp_path / 'project'
        backup = root / '.releases/storage-cutover-test'
        backup.mkdir(parents=True)
        (root / 'data').mkdir()
        original = {**config, 'managedBy': 'cliperx-architecture-v1',
                    'storage': {'enabled': False, 'domainSchemas': schemas}}
        original_bytes = (json.dumps(original, ensure_ascii=False, indent=2) + '\n').encode()
        (backup / 'architecture-runtime.before.json').write_bytes(original_bytes)
        active = {**original, 'storage': {'enabled': True, 'backend': 'postgres', 'domainSchemas': schemas}}
        active_bytes = (json.dumps(active, ensure_ascii=False, indent=2) + '\n').encode()
        (root / 'data/architecture-runtime.json').write_bytes(active_bytes)
        receipt = {'format': 'cliperx-full-storage-cutover-v1', 'backup': str(backup),
                   'originalConfigSha256': hashlib.sha256(original_bytes).hexdigest(),
                   'activatedConfigSha256': hashlib.sha256(active_bytes).hexdigest(),
                   'forward': forward_records, 'historyRetentionChanged': False}
        receipt_path = backup / 'receipt.json'
        receipt_path.write_text(json.dumps(receipt))
        monkeypatch.setattr(rollback, 'ROOT', root)
        monkeypatch.setattr(rollback, 'require_writer_barrier', lambda: None)
        monkeypatch.setattr(storage_migration_worker, 'DOMAINS', tuple(domain_sources))
        # An interruption after install_journal but before the outer checkpoint
        # must resume from PG certificates and exact source triggers.
        actual_reset = rollback.certified_forward_reset
        calls = []
        def interrupted(mirror, bundle):
            result = actual_reset(mirror, bundle)
            calls.append(mirror.schema)
            raise RuntimeError('after-journal-before-checkpoint')
        monkeypatch.setattr(rollback, 'certified_forward_reset', interrupted)
        with pytest.raises(RuntimeError, match='before-checkpoint'):
            rollback.undo(receipt_path)
        assert hashlib.sha256((root / 'data/architecture-runtime.json').read_bytes()).hexdigest() == hashlib.sha256(active_bytes).hexdigest()
        assert len(calls) == 1
        monkeypatch.setattr(rollback, 'certified_forward_reset', actual_reset)
        completed = rollback.undo(receipt_path)
        assert completed['settingsRestored'] and not completed['processesRestarted']
        assert hashlib.sha256((root / 'data/architecture-runtime.json').read_bytes()).hexdigest() == hashlib.sha256(original_bytes).hexdigest()
        assert rollback.undo(receipt_path)['settingsRestored']
    finally:
        for mirror in mirrors:
            mirror.close()
        for migration in migrations:
            migration.close()
        import psycopg
        with psycopg.connect(config['dsn'], autocommit=True) as cleanup:
            for schema in schemas.values():
                cleanup.execute('DROP SCHEMA IF EXISTS ' + quote(schema) + ' CASCADE')


def test_project_barrier_rejects_running_recovery_worker(tmp_path, monkeypatch):
    rollback = rollback_module()
    monkeypatch.setattr(rollback, 'ROOT', tmp_path)
    monkeypatch.chdir(tmp_path)
    entries = [{'name': name, 'pid': 0, 'pm2_env': {'status': 'stopped', 'pm_cwd': str(tmp_path)}}
               for name in rollback.SERVICES]
    entries.append({'name': 'pyradar-storage-recovery', 'pid': 123,
                    'pm2_env': {'status': 'online', 'pm_cwd': str(tmp_path)}})
    monkeypatch.setattr(rollback.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(stdout=json.dumps(entries)))
    with pytest.raises(ValueError, match='unexpected-project-process-running'):
        rollback.require_writer_barrier()


def test_rollback_rejects_changed_activated_config_before_database_access(tmp_path, monkeypatch):
    rollback = rollback_module()
    root, backup = tmp_path, tmp_path / '.releases/storage-cutover-test'
    backup.mkdir(parents=True)
    (root / 'data').mkdir()
    before = {'managedBy': 'cliperx-architecture-v1', 'dsn': 'never-connect', 'storage': {'enabled': False}}
    before_bytes = (json.dumps(before, ensure_ascii=False, indent=2) + '\n').encode()
    (backup / 'architecture-runtime.before.json').write_bytes(before_bytes)
    schemas = {domain: 'cliperx_storage_' + domain for domain, _ in storage_migration_worker.DOMAINS}
    active = {**before, 'storage': {'enabled': True, 'backend': 'postgres', 'domainSchemas': schemas}}
    active_bytes = (json.dumps(active, ensure_ascii=False, indent=2) + '\n').encode()
    settings = root / 'data/architecture-runtime.json'
    settings.write_bytes(active_bytes)
    receipt = {'format': 'cliperx-full-storage-cutover-v1', 'backup': str(backup),
               'originalConfigSha256': hashlib.sha256(before_bytes).hexdigest(),
               'activatedConfigSha256': '0' * 64, 'forward': [], 'historyRetentionChanged': False}
    path = backup / 'receipt.json'
    path.write_text(json.dumps(receipt))
    monkeypatch.setattr(rollback, 'ROOT', root)
    monkeypatch.setattr(rollback, 'require_writer_barrier', lambda: None)
    with pytest.raises(ValueError, match='runtime-configuration-not-owned-by-cutover'):
        rollback.undo(path)
    assert hashlib.sha256(settings.read_bytes()).hexdigest() == hashlib.sha256(active_bytes).hexdigest()
