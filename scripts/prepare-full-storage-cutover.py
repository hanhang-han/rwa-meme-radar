#!/usr/bin/env python3
"""Prepare verified PostgreSQL authority while every project writer is stopped."""
import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from contextlib import closing

ROOT = Path('/opt/memedashboard')
SERVICES = ('memedashboard', 'pyradar', 'pyradar-worker', 'pyradar-projection',
            'pyradar-market', 'pyradar-market-api', 'pyradar-read-model',
            'pyradar-storage-migration')
sys.path.insert(0, str(ROOT / 'server-py'))


def write_json(path, value):
    temporary = path.with_name(path.name + '.cutover-tmp')
    if os.path.lexists(temporary):
        raise ValueError('occupied-cutover-temporary')
    with open(temporary, 'x') as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def require_barrier():
    if Path.cwd().resolve() != ROOT:
        raise ValueError('wrong-project-directory')
    env = {**os.environ, 'PATH': '/www/server/nodejs/v22.22.0/bin:' + os.environ['PATH']}
    result = subprocess.run(['/www/server/nodejs/v22.22.0/bin/pm2', 'jlist'],
                            env=env, capture_output=True, text=True, check=True, timeout=10)
    entries = json.loads(result.stdout)
    for name in SERVICES:
        matching = [row for row in entries if row['name'] == name]
        if (len(matching) != 1 or matching[0]['pm2_env']['status'] != 'stopped'
                or matching[0].get('pid', 0)
                or matching[0]['pm2_env'].get('pm_cwd') != str(ROOT)):
            raise ValueError('writer-barrier-not-held:' + name)
    if any(row['pm2_env'].get('pm_cwd') == str(ROOT)
           and (row['pm2_env']['status'] != 'stopped' or row.get('pid', 0)) for row in entries):
        raise ValueError('unexpected-project-process-running')
    health = json.loads((ROOT / 'data/storage-migration-health.json').read_text())
    if health['phase'] != 'stopped-before-cutover':
        raise ValueError('migration-not-gracefully-stopped')
    settings = ROOT / 'data/architecture-runtime.json'
    config = json.loads(settings.read_text())
    if config.get('managedBy') != 'cliperx-architecture-v1' or (config.get('storage') or {}).get('enabled'):
        raise ValueError('authority-already-changed')
    return config, settings


def prepare(seconds):
    from app.aux_storage_schema import (TELEGRAM_TABLES, install_account_auxiliary_schema,
                                      install_standby_account_auxiliary_schema)
    from app.storage_runtime import runtime_support_sql
    from app.storage_migration import ShadowMigration
    from app.storage_migration_worker import DOMAINS
    from app.storage_recovery import RecoveryMirror
    from app.storage_schema import postgres_runtime_schema_sql
    import psycopg

    config, settings = require_barrier()
    start = time.monotonic()
    deadline = start + seconds
    backup = ROOT / '.releases' / ('storage-cutover-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + str(os.getpid()))
    backup.mkdir(mode=0o700)
    shutil.copy2(settings, backup / 'architecture-runtime.before.json')
    os.chmod(backup / 'architecture-runtime.before.json', 0o600)
    result = {'format': 'cliperx-full-storage-cutover-v1', 'backup': str(backup),
              'phase': 'forward-verification', 'runtimeCutover': False,
              'originalConfigSha256': hashlib.sha256(settings.read_bytes()).hexdigest(),
              'forward': [], 'recovery': [], 'historyRetentionChanged': False}
    migrations, mirrors, prepared = [], [], []

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('short-writer-barrier-exceeded')

    def save():
        result['barrierElapsedSeconds'] = round(time.monotonic() - start, 3)
        write_json(backup / 'receipt.json', result)

    try:
        save()
        # A retry must never recreate forward CDC inside an already guarded
        # standby. Restore, abort and explicitly reset its certified baseline first.
        for domain, source in DOMAINS:
            check()
            with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as sqlite:
                if any(row[0].startswith('_cliperx_recovery_guard_') for row in sqlite.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'")):
                    raise ValueError('existing-recovery-requires-abort')
            dsn = (config.get('storageDsns') or {}).get(domain, config['dsn'])
            with psycopg.connect(dsn, autocommit=True, connect_timeout=3,
                    options='-c default_transaction_read_only=on -c statement_timeout=3000') as pg:
                if pg.execute('SELECT to_regclass(%s)',
                        ('cliperx_storage_' + domain + '._cliperx_recovery_owner',)).fetchone()[0]:
                    raise ValueError('existing-recovery-requires-abort')
        for domain, source in DOMAINS:
            check()
            domain_config = {**config, 'dsn': (config.get('storageDsns') or {}).get(domain, config['dsn'])}
            migration = ShadowMigration(domain_config, source, 'cliperx_storage_' + domain,
                                        batch_rows=2000, reserve_bytes=20 * 1024**3)
            migrations.append((domain, migration))
            migration.prepare()
            while migration.replay():
                check()
            for _ in migration.reconcile_ranges(max_ranges=None):
                check()
            check()
            receipt = migration.verify_barrier()
            result['forward'].append({'domain': domain, **receipt})
            save()
        result['phase'] = 'preparing-guarded-standby'
        save()
        require_barrier()
        if hashlib.sha256(settings.read_bytes()).hexdigest() != result['originalConfigSha256']:
            raise ValueError('configuration-changed-during-prepare')
        accounts = next(migration for domain, migration in migrations if domain == 'accounts')
        result['accountTargetAddition'] = install_account_auxiliary_schema(accounts.pg)
        result['accountStandbyAddition'] = install_standby_account_auxiliary_schema(accounts.source['path'])
        for domain, migration in migrations:
            check()
            mirror = RecoveryMirror(config, migration.source['path'], migration.schema, domain=domain,
                                    batch_rows=1000, reserve_bytes=20 * 1024**3)
            mirrors.append((domain, mirror))
            receipt = next(row for row in result['forward'] if row['domain'] == domain)
            mirror.prepare(receipt, allowed_extra_tables=TELEGRAM_TABLES if domain == 'accounts' else ())
            prepared.append(domain)
            # Capture must exist BEFORE any runtime marker changes business rows.
            with mirror.pg.transaction():
                mirror.pg.execute(runtime_support_sql(mirror.schema))
                mirror.pg.execute(postgres_runtime_schema_sql(mirror.schema, mirror.tables,
                                                              realtime_outbox=domain == 'research'))
            while mirror.replay():
                check()
            for _ in mirror.reconcile_ranges(max_ranges=None):
                check()
            result['recovery'].append({'domain': domain, **mirror.verify_barrier()})
            save()
        check()
        result['phase'] = 'prepared'
        result['businessTables'] = sum(len(row['tables']) for row in result['recovery'])
        save()
        return {'phase': result['phase'], 'receipt': str(backup / 'receipt.json'),
                'businessTables': result['businessTables'], 'barrierElapsedSeconds': result['barrierElapsedSeconds'],
                'runtimeCutover': False}
    except BaseException as error:
        result['phase'] = 'prepare-failed'
        result['errorClass'] = type(error).__name__
        result['attemptedRecoveryDomains'] = [domain for domain, _ in mirrors]
        result['preparedRecoveryDomains'] = prepared
        save()
        # Never resume old writers while a guarded standby is installed.
        # A failed prepare is explicitly unwound using verified reverse recovery.
        raise
    finally:
        for _, mirror in mirrors:
            mirror.close()
        for _, migration in migrations:
            migration.close()


def enable(receipt_path):
    from app.storage_migration_worker import DOMAINS
    from app.storage_recovery import RecoveryMirror
    config, settings = require_barrier()
    receipt_path = Path(receipt_path).resolve()
    if receipt_path.parent.parent != ROOT / '.releases':
        raise ValueError('unowned-cutover-receipt')
    receipt = json.loads(receipt_path.read_text())
    expected_domains = {domain for domain, _ in DOMAINS}
    for key in ('forward', 'recovery'):
        rows = receipt.get(key)
        if (not isinstance(rows, list) or len(rows) != len(expected_domains)
                or {row.get('domain') for row in rows} != expected_domains):
            raise ValueError('incomplete-or-duplicate-domain-receipt')
    if (receipt.get('format') != 'cliperx-full-storage-cutover-v1' or receipt.get('phase') != 'prepared'
            or receipt.get('businessTables') != 65
            or receipt.get('originalConfigSha256') != hashlib.sha256(settings.read_bytes()).hexdigest()):
        raise ValueError('unverified-cutover-receipt')
    domains = {domain: 'cliperx_storage_' + domain for domain, _ in DOMAINS}
    latest = []
    for domain, source in DOMAINS:
        mirror = RecoveryMirror(config, source, domains[domain], domain=domain)
        try:
            current = mirror.verify_barrier()
            original = next(row for row in receipt['recovery'] if row['domain'] == domain)
            if any(current[key] != original[key] for key in ('sourceSequence', 'sourceId', 'sourceSchemaHash', 'tables')):
                raise ValueError('prepared-standby-changed')
            latest.append({'domain': domain, **current})
        finally:
            mirror.close()
    if sum(len(row['tables']) for row in latest) != 65:
        raise ValueError('unexpected-verified-business-tables')
    require_barrier()
    if receipt['originalConfigSha256'] != hashlib.sha256(settings.read_bytes()).hexdigest():
        raise ValueError('configuration-changed-before-activation')
    config['storage'] = {'enabled': True, 'backend': 'postgres', 'domainSchemas': domains}
    write_json(settings, config)
    receipt.update(phase='enabled-awaiting-runtime-acceptance', runtimeCutover=True,
                   enabledAtMs=int(time.time() * 1000), recovery=latest,
                   activatedConfigSha256=hashlib.sha256(settings.read_bytes()).hexdigest())
    write_json(receipt_path, receipt)
    return {'phase': receipt['phase'], 'receipt': str(receipt_path), 'businessTables': 65,
            'runtimeCutover': True, 'fullMigrationComplete': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'enable', 'prepare-enable'))
    parser.add_argument('--barrier-seconds', type=int, default=90)
    parser.add_argument('--receipt')
    args = parser.parse_args()
    if not 30 <= args.barrier_seconds <= 120:
        parser.error('barrier seconds must be 30..120')
    handles = []
    def expired(signum, frame):
        raise TimeoutError('short-writer-barrier-deadline')
    try:
        for name in ('architecture-deploy.lock', 'pool-pipeline-repair.lock', 'dashboard-web-only-deploy.lock'):
            handle = open(ROOT / '.releases' / name, 'a')
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handles.append(handle)
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, args.barrier_seconds)
        if args.operation == 'enable' and not args.receipt:
            parser.error('enable requires a prepared receipt')
        if args.operation == 'prepare-enable':
            prepared = prepare(args.barrier_seconds)
            result = enable(prepared['receipt'])
        else:
            result = prepare(args.barrier_seconds) if args.operation == 'prepare' else enable(args.receipt)
        print(json.dumps(result))
    except Exception as error:
        raise SystemExit('full-storage-cutover-failed:' + type(error).__name__)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for handle in reversed(handles):
            handle.close()
