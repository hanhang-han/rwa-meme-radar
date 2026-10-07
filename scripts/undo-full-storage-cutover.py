#!/usr/bin/env python3
"""Certified shadow reset after reverse recovery; never resumes writers.

The outer rollback command must hold the project writer/process barrier and
keep runtime settings unchanged until every domain is restored. This function
retains all business rows and copies verified interval proofs, not history.
"""
from contextlib import closing
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path('/opt/memedashboard')
SERVICES = ('memedashboard', 'pyradar', 'pyradar-worker', 'pyradar-projection',
            'pyradar-market', 'pyradar-market-api', 'pyradar-read-model', 'pyradar-storage-migration')


def certified_forward_reset(mirror, abort_bundle):
    """Rebase forward CDC after verified abort, with both writer groups stopped.

    Accept the actual RecoveryMirror.abort() result. PostgreSQL authority may
    still be enabled in settings: switching settings belongs to the outer
    command only after all domains finish. No settings/processes are changed.
    """
    from app.aux_storage_schema import TELEGRAM_TABLES
    from app.storage_migration import (CONTROL_TABLE, IDENTITY_COLUMN, INTERNAL_PREFIX,
        OWNER_TABLE, RANGE_TABLE, _source_id, inspect_source, install_journal, quote)
    from app.storage_runtime import advisory_lock_key
    from sqlglot import parse_one

    source = inspect_source(mirror.source['path'])
    tables = {table['name']: table for table in source['tables']}
    if (abort_bundle.get('schema') != mirror.schema or not abort_bundle.get('recoveryRemoved')
            or not abort_bundle.get('triggersRestored') or not abort_bundle.get('foreignKeysValid')
            or abort_bundle.get('sourceSchemaHash') != source['schemaHash']
            or abort_bundle.get('sourceId') != _source_id(source)
            or abort_bundle.get('tables') != (abort_bundle.get('restoredReceipt') or {}).get('tables')):
        raise ValueError('reset-requires-current-verified-abort')
    certificates = abort_bundle.get('rangeCertificates') or []
    receipts = {row['table']: row for row in abort_bundle.get('tables') or []}
    if (set(receipts) != set(tables) or {row['table'] for row in certificates} != set(tables)
            or any(row.get('match') is not True for row in receipts.values())):
        raise ValueError('reset-incomplete-table-certificates')
    with closing(mirror._sqlite(writable=True)) as sqlite:
        sqlite.execute('BEGIN IMMEDIATE')
        try:
            mirror._validate_restored_sqlite(sqlite, abort_bundle['restoredReceipt'])
            with mirror.pg.transaction():
                mirror.pg.execute('SELECT pg_advisory_xact_lock(%s)', (advisory_lock_key(mirror.schema),))
                mirror._assert_recovery_removed()
                owner = mirror.pg.execute(f'SELECT source_id,source_schema_hash FROM {OWNER_TABLE}').fetchall()
                controls = {row[0] for row in mirror.pg.execute(f'SELECT table_name FROM {CONTROL_TABLE}')}
                missing = set(tables) - controls
                if len(owner) != 1 or controls - set(tables) or missing - set(TELEGRAM_TABLES):
                    raise ValueError('reset-unowned-forward-baseline')
                old_tables = [table for table in source['tables'] if table['name'] in controls]
                old_hash = hashlib.sha256(json.dumps(old_tables, sort_keys=True).encode()).hexdigest()
                if owner[0] != (_source_id({**source, 'schemaHash': old_hash}), old_hash):
                    raise ValueError('reset-forward-owner-mismatch')
                for name, table in tables.items():
                    actual = mirror.pg.execute('''SELECT a.attname,format_type(a.atttypid,a.atttypmod)
                        FROM pg_attribute a WHERE a.attrelid=%s::regclass AND a.attnum>0
                        AND NOT a.attisdropped ORDER BY a.attnum''',
                        (quote(mirror.schema) + '.' + quote(name),)).fetchall()
                    types = {'TEXT': 'text', 'INTEGER': 'bigint', 'REAL': 'double precision', 'BLOB': 'bytea'}
                    expected = [(column['name'], 'bytea' if name == 'dashboard_projection'
                        and column['name'] == 'body' else types[column['type'].upper()]) for column in table['columns']]
                    if actual != expected + [(IDENTITY_COLUMN, 'bigint')]:
                        raise ValueError('reset-target-column-mismatch:' + name)
                    for statement in table['indexes']:
                        index_name = parse_one(statement, read='sqlite').this.name
                        if not mirror.pg.execute('''SELECT 1 FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid
                            JOIN pg_namespace n ON n.oid=x.relnamespace WHERE n.nspname=%s AND x.relname=%s
                            AND i.indrelid=%s::regclass AND i.indisvalid AND i.indisready''',
                            (mirror.schema, index_name, quote(mirror.schema) + '.' + quote(name))).fetchone():
                            raise ValueError('reset-target-index-mismatch:' + name)
                    rows = sorted((row for row in certificates if row['table'] == name), key=lambda row: row['lowRowid'])
                    typed = [(row['lowRowid'], row['highRowid'], row['rowCount'], row['contentSha256']) for row in rows]
                    receipt = receipts[name]
                    if (not rows or any(row.get('valid') is not True or row['lowRowid'] > row['highRowid']
                            or row['rowCount'] < 0 or not re.fullmatch(r'[a-f0-9]{64}', row['contentSha256']) for row in rows)
                            or any(left[1] + 1 != right[0] for left, right in zip(typed, typed[1:]))
                            or receipt.get('rangeCount') != len(rows) or receipt.get('rows') != sum(row[2] for row in typed)
                            or receipt.get('rangeManifestSha256') != mirror._range_digest(typed)):
                        raise ValueError('reset-certificate-proof-mismatch:' + name)
                    src_bounds = [sqlite.execute('SELECT rowid FROM ' + quote(name) +
                        ' ORDER BY rowid ' + order + ' LIMIT 1').fetchone() for order in ('ASC', 'DESC')]
                    dst_bounds = [mirror.pg.execute('SELECT ' + quote(IDENTITY_COLUMN) + ' FROM ' + quote(name) +
                        ' ORDER BY ' + quote(IDENTITY_COLUMN) + ' ' + order + ' LIMIT 1').fetchone() for order in ('ASC', 'DESC')]
                    if src_bounds != dst_bounds or (src_bounds[0] and
                            (src_bounds[0][0] < typed[0][0] or src_bounds[1][0] > typed[-1][1])):
                        raise ValueError('reset-uncovered-or-moved-rows:' + name)
                    high = src_bounds[1][0] if src_bounds[1] else 0
                    mirror.pg.execute(f'''INSERT INTO {CONTROL_TABLE}
                        (table_name,high_rowid,last_rowid,copied_rows,backfill_done,indexes_done,verified_at_ms)
                        VALUES(%s,%s,%s,%s,true,true,%s) ON CONFLICT(table_name) DO UPDATE SET
                        high_rowid=excluded.high_rowid,last_rowid=excluded.last_rowid,copied_rows=excluded.copied_rows,
                        verified_at_ms=excluded.verified_at_ms''', (name, high, high, receipt['rows'], int(time.time()*1000)))
                    for trigger in ('_cliperx_write_order', '_cliperx_identity_rowid', '_cliperx_realtime_change'):
                        owned = mirror.pg.execute('''SELECT n.nspname,p.proname FROM pg_trigger t
                            JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace n ON n.oid=p.pronamespace
                            WHERE t.tgrelid=%s::regclass AND t.tgname=%s''',
                            (quote(mirror.schema) + '.' + quote(name), trigger)).fetchone()
                        if owned and owned != (mirror.schema, trigger):
                            raise ValueError('reset-unowned-runtime-trigger:' + name)
                        mirror.pg.execute('DROP TRIGGER IF EXISTS ' + quote(trigger) + ' ON ' + quote(name))
                mirror.pg.execute(f'DELETE FROM {RANGE_TABLE}')
                with mirror.pg.cursor() as cursor:
                    cursor.executemany(f'INSERT INTO {RANGE_TABLE} VALUES(%s,%s,%s,%s,%s,true,%s)',
                        [(row['table'], row['lowRowid'], row['highRowid'], row['rowCount'],
                          row['contentSha256'], row['verifiedAtMs']) for row in certificates])
                mirror.pg.execute(f'UPDATE {OWNER_TABLE} SET source_id=%s,source_schema_hash=%s',
                                  (_source_id(source), source['schemaHash']))
            # The metadata transaction is committed before adding new source
            # CDC triggers. A stopped-writer retry can safely finish this step.
            sqlite.commit()
            install_journal(source)
        except BaseException:
            sqlite.rollback()
            raise
    return {'schema': mirror.schema, 'sourceId': _source_id(source), 'sourceSchemaHash': source['schemaHash'],
            'businessTables': len(tables), 'addedForwardTables': sorted(missing),
            'historyReimported': False, 'settingsChanged': False, 'processesRestarted': False}


def _atomic_bytes(path, contents):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.undo-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            target.write(contents)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _save(path, value):
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def require_writer_barrier():
    """Require all project processes stopped; configuration may still be PG."""
    if Path.cwd().resolve() != ROOT:
        raise ValueError('wrong-project-directory')
    binary = '/www/server/nodejs/v22.22.0/bin/pm2'
    result = subprocess.run([binary, 'jlist'], capture_output=True, text=True, check=True, timeout=10,
        env={**os.environ, 'PATH': str(Path(binary).parent) + ':' + os.environ.get('PATH', '')})
    entries = json.loads(result.stdout)
    for name in SERVICES:
        found = [row for row in entries if row.get('name') == name]
        if (len(found) != 1 or found[0].get('pid', 0)
                or found[0]['pm2_env'].get('status') != 'stopped'
                or found[0]['pm2_env'].get('pm_cwd') != str(ROOT)):
            raise ValueError('writer-barrier-not-held:' + name)
    if any(row['pm2_env'].get('pm_cwd') == str(ROOT) and
           (row['pm2_env'].get('status') != 'stopped' or row.get('pid', 0)) for row in entries):
        raise ValueError('unexpected-project-process-running')


def _current_abort_archive(source, schema, baseline):
    path = Path(str(source) + '.recovery-manifest.json')
    if not path.exists():
        return None
    manifest = json.loads(path.read_text())
    if manifest.get('schema') != schema or manifest.get('baselineSourceSequence') != baseline.get('sourceSequence'):
        return None  # A completed archive from an earlier forward generation.
    generation = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    archive_path = Path(str(source) + '.recovery-abort-' + generation + '.json')
    if not archive_path.exists():
        return None
    bundle = json.loads(archive_path.read_text())
    if bundle.get('manifestSha256') != generation or bundle.get('manifest') != manifest or bundle.get('schema') != schema:
        raise ValueError('unowned-domain-abort-archive')
    return bundle


def _certified_rebase_present(migration, bundle):
    from app.storage_migration import CONTROL_TABLE, OWNER_TABLE, RANGE_TABLE
    owner = migration.pg.execute(f'SELECT source_id,source_schema_hash FROM {OWNER_TABLE}').fetchall()
    controls = migration.pg.execute(f'SELECT table_name,backfill_done,indexes_done FROM {CONTROL_TABLE}').fetchall()
    if (owner != [(migration.source_id, migration.source['schemaHash'])]
            or {row[0] for row in controls} != set(migration.tables)
            or any(not row[1] or not row[2] for row in controls)):
        return False
    expected = {row['table']: row for row in bundle['tables']}
    if set(expected) != set(migration.tables):
        return False
    from app.storage_recovery import RecoveryMirror
    for name in migration.tables:
        rows = migration.pg.execute(f'''SELECT low_rowid,high_rowid,row_count,content_sha256,valid
            FROM {RANGE_TABLE} WHERE table_name=%s ORDER BY low_rowid''', (name,)).fetchall()
        receipt = expected[name]
        if (not rows or any(not row[4] for row in rows) or len(rows) != receipt['rangeCount']
                or sum(row[2] for row in rows) != receipt['rows']
                or RecoveryMirror._range_digest(rows) != receipt['rangeManifestSha256']):
            return False
    # A no-op PG generation can have the same content certificates as the old
    # shadow. Matching hashes alone do not prove its runtime hooks were reset.
    if migration.pg.execute('''SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND t.tgname=ANY(%s) LIMIT 1''',
        (migration.schema, ['_cliperx_write_order', '_cliperx_identity_rowid', '_cliperx_realtime_change'])).fetchone():
        return False
    return True


def _validate_forward_journal(source, bundle, *, allow_new_missing):
    """Validate a reset committed before its durable checkpoint was saved."""
    from app.storage_migration import INTERNAL_PREFIX, _source_id, install_journal, quote
    if _source_id(source) != bundle.get('sourceId') or source['schemaHash'] != bundle.get('sourceSchemaHash'):
        raise ValueError('reset-retry-source-identity-changed')
    manifest = bundle['manifest']
    originals = {row['name']: row['sql'] for row in manifest['originalTriggers']}
    # Generate the exact current library protocol on a schema-only temporary
    # file. No historical rows or private values are copied into the template.
    with tempfile.TemporaryDirectory(prefix='cliperx-journal-proof-') as temporary:
        template = Path(temporary) / 'schema.sqlite'
        with closing(sqlite3.connect(template)) as sqlite:
            for table in source['tables']:
                sqlite.execute(table['sql'])
            sqlite.commit()
        install_journal({**source, 'path': str(template)})
        with closing(sqlite3.connect(template)) as sqlite:
            expected = dict(sqlite.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    expected.update({name: sql for name, sql in originals.items() if not name.startswith(INTERNAL_PREFIX)})
    with closing(sqlite3.connect(Path(source['path']).as_uri() + '?mode=ro', uri=True)) as sqlite:
        actual = {name: sql for name, table, sql in sqlite.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'")
                  if table in {row['name'] for row in source['tables']}}
        if (any(expected.get(name) != sql for name, sql in actual.items())
                or any(actual.get(name) != sql for name, sql in originals.items())
                or not allow_new_missing and actual != expected):
            raise ValueError('reset-retry-source-journal-protocol-changed')
        counter = sqlite.execute(f'SELECT value FROM {INTERNAL_PREFIX}counter WHERE id=1').fetchone()[0]
        if (counter != bundle['standbySourceSequence']
                or sqlite.execute(f'SELECT 1 FROM {INTERNAL_PREFIX}changes LIMIT 1').fetchone()):
            raise ValueError('reset-retry-has-new-source-writes')


def undo(receipt_path, seconds=100):
    """Restore all six verified standbys, then atomically restore old settings."""
    from app.aux_storage_schema import TELEGRAM_TABLES
    from app.storage_migration import CONTROL_TABLE, OWNER_TABLE, ShadowMigration, quote
    from app.storage_migration_worker import DOMAINS
    from app.storage_recovery import GUARD_PREFIX, RECOVERY_OWNER, RecoveryMirror
    require_writer_barrier()
    path = Path(receipt_path)
    if path.is_symlink():
        raise ValueError('linked-cutover-receipt')
    path = path.resolve()
    if path.parent.parent != ROOT / '.releases':
        raise ValueError('unowned-cutover-receipt')
    receipt = json.loads(path.read_text())
    before_path, settings = path.parent / 'architecture-runtime.before.json', ROOT / 'data/architecture-runtime.json'
    if before_path.is_symlink() or settings.is_symlink():
        raise ValueError('linked-private-configuration')
    original_bytes, current_bytes = before_path.read_bytes(), settings.read_bytes()
    before, config = json.loads(original_bytes), json.loads(current_bytes)
    current_sha = hashlib.sha256(current_bytes).hexdigest()
    if (receipt.get('format') != 'cliperx-full-storage-cutover-v1' or receipt.get('backup') != str(path.parent)
            or receipt.get('originalConfigSha256') != hashlib.sha256(original_bytes).hexdigest()
            or before.get('managedBy') != 'cliperx-architecture-v1' or config.get('managedBy') != before['managedBy']
            or (before.get('storage') or {}).get('enabled') or receipt.get('historyRetentionChanged') is not False):
        raise ValueError('unverified-private-original-configuration')
    schemas = (config.get('storage') or {}).get('domainSchemas') or {domain: 'cliperx_storage_' + domain for domain, _ in DOMAINS}
    if set(schemas) != {domain for domain, _ in DOMAINS}:
        raise ValueError('incomplete-runtime-domain-schema-map')
    enabled = {**before, 'storage': {'enabled': True, 'backend': 'postgres', 'domainSchemas': schemas}}
    enabled_sha = hashlib.sha256((json.dumps(enabled, ensure_ascii=False, indent=2) + '\n').encode()).hexdigest()
    if current_sha != receipt['originalConfigSha256']:
        activated = receipt.get('activatedConfigSha256') or enabled_sha
        if current_sha != activated or config != enabled or current_sha != enabled_sha:
            raise ValueError('runtime-configuration-not-owned-by-cutover')
    forward = receipt.get('forward') or []
    if (len({row['domain'] for row in forward}) != len(forward)
            or {row['domain'] for row in forward} - set(schemas)):
        raise ValueError('unowned-or-duplicate-forward-domain-receipt')
    baselines = {row['domain']: row for row in forward}
    output = path.parent / 'rollback-receipt.json'
    state = {'format': 'cliperx-full-storage-rollback-v1', 'cutoverReceipt': str(path), 'phase': 'restoring-domains',
             'settingsRestored': False, 'processesRestarted': False, 'historyRetentionChanged': False, 'domains': []}
    deadline = time.monotonic() + seconds

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('rollback-writer-barrier-deadline')

    def save():
        _save(output, state)

    save()
    try:
        for domain, source_path in DOMAINS:
            check()
            require_writer_barrier()
            source_path = str((ROOT / source_path).resolve())
            schema, baseline = schemas[domain], baselines.get(domain)
            domain_config = {**config, 'dsn': (config.get('storageDsns') or {}).get(domain, config['dsn'])}
            migration = ShadowMigration(domain_config, source_path, schema, batch_rows=1000)
            mirror = None
            domain_state = {'domain': domain, 'schema': schema, 'phase': 'checking-domain'}
            state['domains'].append(domain_state)
            save()
            try:
                migration.pg.execute('SET search_path TO ' + quote(schema) + ',pg_catalog')
                owner = migration.pg.execute(f'SELECT source_id,source_schema_hash FROM {OWNER_TABLE}').fetchall()
                if len(owner) != 1:
                    raise ValueError('unowned-forward-domain')
                with closing(sqlite3.connect(Path(source_path).as_uri() + '?mode=ro', uri=True)) as sqlite:
                    guarded = any(row[0].startswith(GUARD_PREFIX) for row in sqlite.execute("SELECT name FROM sqlite_master WHERE type='trigger'"))
                recovery_exists = migration.pg.execute('SELECT to_regclass(%s)', (schema + '.' + RECOVERY_OWNER,)).fetchone()[0]
                if guarded and not recovery_exists:
                    raise ValueError('guarded-source-without-recovery-owner')
                bundle = _current_abort_archive(source_path, schema, baseline or {}) if not recovery_exists else None
                if bundle and _certified_rebase_present(migration, bundle):
                    _validate_forward_journal(migration.source, bundle, allow_new_missing=True)
                    migration.prepare()
                    _validate_forward_journal(migration.source, bundle, allow_new_missing=False)
                    domain_state['phase'] = 'resumed-certified-forward-reset'
                else:
                    source_changed = owner != [(migration.source_id, migration.source['schemaHash'])]
                    if recovery_exists or bundle or source_changed:
                        if not baseline or baseline.get('schema') != schema or not all(row.get('match') is True for row in baseline.get('tables') or []):
                            raise ValueError('missing-verified-domain-forward-baseline')
                        controls = {row[0] for row in migration.pg.execute(f'SELECT table_name FROM {CONTROL_TABLE}')}
                        if not bundle and (baseline.get('sourceId') != owner[0][0]
                                or {row['table'] for row in baseline.get('tables') or []} != controls):
                            raise ValueError('recovery-baseline-not-owned-by-domain')
                        mirror = RecoveryMirror(config, source_path, schema, domain=domain, batch_rows=1000)
                        if recovery_exists:
                            manifest = mirror._load()
                            if manifest['baselineSourceSequence'] != baseline.get('sourceSequence'):
                                raise ValueError('recovery-generation-baseline-mismatch')
                            status = mirror.pg.execute(f'SELECT state FROM {RECOVERY_OWNER} WHERE singleton=1').fetchone()[0]
                        elif bundle:
                            status = 'aborted'
                        else:
                            # Partial prepare appended the six empty account
                            # tables before starting recovery in this domain.
                            controls = {row[0] for row in migration.pg.execute(f'SELECT table_name FROM {CONTROL_TABLE}')}
                            extra = set(migration.tables) - controls
                            if domain != 'accounts' or extra != set(TELEGRAM_TABLES):
                                raise ValueError('unapproved-unprepared-schema-change')
                            mirror.prepare(baseline, allowed_extra_tables=TELEGRAM_TABLES)
                            status = 'active'
                        if status == 'active':
                            mirror.prepare(baseline)
                            while mirror.replay():
                                check()
                            while True:
                                checked = 0
                                for _ in mirror.reconcile_ranges(max_ranges=8):
                                    checked += 1
                                    check()
                                if not checked:
                                    break
                            check()
                            domain_state['restoredReceipt'] = mirror.verify_barrier()
                            mirror.restore_sqlite()
                        elif status == 'restored':
                            mirror.restore_sqlite()
                        elif status != 'aborted':
                            raise ValueError('unowned-recovery-owner-state')
                        bundle = mirror.abort()
                        domain_state.update(phase='recovery-aborted', abortArchivePath=bundle['archivePath'])
                        save()
                        check()
                        domain_state['reset'] = certified_forward_reset(mirror, bundle)
                        domain_state['phase'] = 'certified-forward-reset'
                        save()
                        from app.storage_migration import _source_id, inspect_source
                        migration.source = inspect_source(source_path)
                        migration.source_id = _source_id(migration.source)
                        migration.tables = {row['name']: row for row in migration.source['tables']}
                    migration.prepare()
                check()
                # This verifies the pre-warmed interval certificates and seeks
                # endpoints. Never call verify(), which hashes all history.
                domain_state['forwardReceipt'] = migration.verify_barrier()
                domain_state['phase'] = 'verified-forward-ready'
                save()
            finally:
                if mirror is not None:
                    mirror.close()
                migration.close()
        check()
        require_writer_barrier()
        if hashlib.sha256(settings.read_bytes()).hexdigest() != current_sha:
            raise ValueError('runtime-configuration-changed-during-rollback')
        if hashlib.sha256(before_path.read_bytes()).hexdigest() != receipt['originalConfigSha256']:
            raise ValueError('private-original-configuration-changed')
        state['phase'] = 'all-domains-verified-before-settings'
        save()
        _atomic_bytes(settings, original_bytes)
        state.update(phase='restored-awaiting-explicit-service-start', settingsRestored=True,
                     runtimeCutover=False, restoredConfigSha256=receipt['originalConfigSha256'])
        save()
        return {'phase': state['phase'], 'receipt': str(output), 'domains': len(state['domains']),
                'businessTables': sum(len(row['forwardReceipt']['tables']) for row in state['domains']),
                'settingsRestored': True, 'processesRestarted': False, 'historyRetentionChanged': False}
    except BaseException as error:
        state.update(phase='rollback-failed-writers-remain-stopped', errorClass=type(error).__name__)
        save()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('undo',))
    parser.add_argument('--receipt', required=True)
    parser.add_argument('--barrier-seconds', type=int, default=100)
    args = parser.parse_args()
    if not 30 <= args.barrier_seconds <= 120:
        parser.error('barrier seconds must be 30..120')
    sys.path.insert(0, str(ROOT / 'server-py'))
    handles = []
    def expired(signum, frame):
        raise TimeoutError('rollback-writer-barrier-deadline')
    try:
        for name in ('architecture-deploy.lock', 'pool-pipeline-repair.lock', 'dashboard-web-only-deploy.lock'):
            handle = open(ROOT / '.releases' / name, 'a')
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handles.append(handle)
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, args.barrier_seconds)
        print(json.dumps(undo(args.receipt, args.barrier_seconds)))
    except Exception as error:
        raise SystemExit('full-storage-rollback-failed:' + type(error).__name__)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for handle in reversed(handles):
            handle.close()


if __name__ == '__main__':
    main()
