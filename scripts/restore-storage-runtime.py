#!/usr/bin/env python3
"""Restore checksum-owned runtime sources after certified undo; never start services."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path('/opt/memedashboard')
FILES = frozenset('''package.json package-lock.json server-py/requirements.txt
server-py/app/api/live_market.py server-py/app/api/public_v1.py server-py/app/api/stream.py
server-py/app/api/watch.py server-py/app/aux_storage_schema.py
server-py/app/collectors/chain_stream.py server-py/app/collectors/factory_discovery.py
server-py/app/collectors/live_quotes.py server-py/app/comparison_service.py server-py/app/db.py
server-py/app/demand_leases.py server-py/app/developer_access.py server-py/app/live_market_store.py
server-py/app/main.py server-py/app/postgres_sql.py server-py/app/product_social.py
server-py/app/read_model_worker.py server-py/app/realtime_projection.py
server-py/app/request_ledger.py server-py/app/state.py server-py/app/storage_migration.py
server-py/app/storage_migration_worker.py server-py/app/storage_runtime.py
server-py/app/storage_schema.py server-py/app/storage_recovery.py
server-py/app/storage_recovery_worker.py server-py/app/stream_hub.py
server-py/app/telegram_alerts.py server-py/app/user_features.py src/lib/candles.ts
src/lib/dashboard-v2.ts src/lib/okx-client.ts src/lib/okx.ts src/lib/postgres-storage.ts
src/lib/request-ledger.ts src/lib/research-store.ts src/lib/xlayer.ts src/server.ts'''.split())
SERVICES = ('memedashboard', 'pyradar', 'pyradar-worker', 'pyradar-projection',
            'pyradar-market', 'pyradar-market-api', 'pyradar-read-model', 'pyradar-storage-migration')
DOMAINS = frozenset(('market', 'research', 'accounts', 'budget', 'leases', 'stream_archive'))
LOCKS = ('architecture-deploy.lock', 'pool-pipeline-repair.lock', 'dashboard-web-only-deploy.lock')


def safe_path(path):
    path = Path(path)
    relative = path.relative_to(ROOT)
    if '..' in relative.parts or path != path.absolute():
        raise ValueError('unowned-path')
    current = ROOT
    if current.is_symlink():
        raise ValueError('linked-project')
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError('linked-owned-path')
    return path


def digest(path):
    path = safe_path(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError('source-not-regular-file')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path, value):
    safe_path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.storage-restore-receipt-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fsync_dir(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_writer_barrier():
    if Path.cwd().resolve() != ROOT:
        raise ValueError('wrong-project-directory')
    binary = '/www/server/nodejs/v22.22.0/bin/pm2'
    result = subprocess.run([binary, 'jlist'], capture_output=True, text=True, check=True, timeout=10,
        env={**os.environ, 'PATH': str(Path(binary).parent) + ':' + os.environ.get('PATH', '')})
    entries = json.loads(result.stdout)
    for name in SERVICES:
        found = [row for row in entries if row.get('name') == name]
        if (len(found) != 1 or found[0].get('pid', 0)
                or found[0].get('pm2_env', {}).get('status') != 'stopped'
                or found[0].get('pm2_env', {}).get('pm_cwd') != str(ROOT)):
            raise ValueError('writer-barrier-not-held')
    for row in entries:
        env = row.get('pm2_env', {})
        script = str(env.get('pm_exec_path') or '')
        project = env.get('pm_cwd') == str(ROOT) or script.startswith(str(ROOT) + '/')
        if project and (env.get('status') != 'stopped' or row.get('pid', 0)):
            raise ValueError('project-process-still-running')


def require_certified_undo(receipt_path):
    path = safe_path(receipt_path)
    if (path.name != 'rollback-receipt.json' or path.parent.parent != ROOT / '.releases'
            or not path.parent.name.startswith('storage-cutover-')):
        raise ValueError('unowned-rollback-receipt')
    receipt = json.loads(path.read_text())
    cutover_path = safe_path(path.parent / 'receipt.json')
    cutover = json.loads(cutover_path.read_text())
    settings = safe_path(ROOT / 'data/architecture-runtime.json')
    config = json.loads(settings.read_text())
    domains = receipt.get('domains') or []
    if (receipt.get('format') != 'cliperx-full-storage-rollback-v1'
            or receipt.get('phase') != 'restored-awaiting-explicit-service-start'
            or receipt.get('settingsRestored') is not True
            or receipt.get('processesRestarted') is not False
            or receipt.get('historyRetentionChanged') is not False
            or receipt.get('cutoverReceipt') != str(cutover_path)
            or cutover.get('format') != 'cliperx-full-storage-cutover-v1'
            or cutover.get('backup') != str(path.parent)
            or receipt.get('restoredConfigSha256') != cutover.get('originalConfigSha256')
            or receipt.get('restoredConfigSha256') != digest(settings)
            or config.get('managedBy') != 'cliperx-architecture-v1'
            or (config.get('storage') or {}).get('enabled')
            or len(domains) != len(DOMAINS) or {row.get('domain') for row in domains} != DOMAINS):
        raise ValueError('source-restore-requires-certified-undo')
    for row in domains:
        forward = row.get('forwardReceipt') or {}
        tables = forward.get('tables') or []
        if (forward.get('schema') != 'cliperx_storage_' + row['domain']
                or forward.get('runtimeCutover') is not False or not tables
                or any(table.get('match') is not True for table in tables)):
            raise ValueError('incomplete-rollback-domain-proof')


def restore_file(current, saved, expected):
    descriptor, temporary = tempfile.mkstemp(prefix='.storage-source-restore-', dir=current.parent)
    os.close(descriptor)
    try:
        shutil.copy2(saved, temporary)
        with open(temporary, 'rb') as handle:
            os.fsync(handle.fileno())
        if hashlib.sha256(Path(temporary).read_bytes()).hexdigest() != expected:
            raise ValueError('restore-temporary-hash-mismatch')
        os.replace(temporary, current)
        fsync_dir(current.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def restore(backup, rollback_receipt, perform=False):
    backup = safe_path(backup)
    if (backup.parent != ROOT / '.releases' or not backup.is_dir()
            or not re.fullmatch(r'storage-runtime-[A-Za-z0-9_-]+', backup.name)):
        raise ValueError('unowned-source-backup')
    output = safe_path(backup / 'source-restore-receipt.json')
    state = {'format': 'cliperx-storage-source-restore-v1', 'backup': str(backup),
             'phase': 'validating', 'sourcesRestored': [], 'dependenciesRetained': True,
             'processesRestarted': False, 'historyRetentionChanged': False,
             'writerBarrierVerified': False}
    try:
        require_writer_barrier()
        state['writerBarrierVerified'] = True
        require_certified_undo(rollback_receipt)
        manifest_path = safe_path(backup / 'runtime-release.json')
        manifest = json.loads(manifest_path.read_text())
        state['manifestSha256'] = digest(manifest_path)
        if (manifest.get('format') != 'cliperx-full-storage-runtime-v1'
                or set(manifest.get('files') or {}) != FILES):
            raise ValueError('unreviewed-source-allowlist')
        plans = []
        # Validate ALL sources and backups before the first source mutation.
        for name in sorted(FILES):
            hashes = manifest['files'][name]
            if (set(hashes) != {'before', 'after'} or
                    not re.fullmatch(r'[a-f0-9]{64}', hashes.get('after') or '') or
                    (hashes['before'] is not None and
                     not re.fullmatch(r'[a-f0-9]{64}', hashes['before']))):
                raise ValueError('invalid-source-hash-record')
            current, saved = safe_path(ROOT / name), safe_path(backup / name)
            actual = digest(current)
            if actual not in (hashes['before'], hashes['after']) or digest(saved) != hashes['before']:
                raise ValueError('unowned-source-or-backup-hash')
            plans.append((name, current, saved, hashes, actual))
        if not perform:
            return {'preflightPassed': True, 'sourceFiles': len(plans),
                    'dependenciesRetained': True, 'processesRestarted': False}
        state.update(phase='restoring', rollbackReceipt=str(rollback_receipt))
        atomic_json(output, state)
        for name, current, saved, hashes, observed in plans:
            state['writerBarrierVerified'] = False
            require_writer_barrier()
            state['writerBarrierVerified'] = True
            require_certified_undo(rollback_receipt)
            if digest(current) != observed or digest(saved) != hashes['before']:
                raise ValueError('source-changed-during-restoration')
            if observed != hashes['before']:
                if hashes['before'] is None:
                    # Only our exact newly-published file can be removed.
                    if observed != hashes['after']:
                        raise ValueError('unowned-new-source')
                    current.unlink()
                    fsync_dir(current.parent)
                else:
                    restore_file(current, saved, hashes['before'])
            if digest(current) != hashes['before']:
                raise ValueError('restored-source-hash-mismatch')
            state['sourcesRestored'].append(name)
            atomic_json(output, state)
        for name, _, _, hashes, _ in plans:
            if digest(ROOT / name) != hashes['before']:
                raise ValueError('final-restored-source-hash-mismatch')
        state['writerBarrierVerified'] = False
        require_writer_barrier()
        state['writerBarrierVerified'] = True
        require_certified_undo(rollback_receipt)
        state.update(phase='restored-awaiting-explicit-service-start', sourceFiles=len(plans))
        atomic_json(output, state)
        return {key: state[key] for key in ('phase', 'sourceFiles', 'dependenciesRetained', 'processesRestarted')}
    except BaseException as exc:
        if perform:
            phase = ('restore-failed-writers-remain-stopped' if state['writerBarrierVerified']
                     else 'restore-failed-services-not-started')
            state.update(phase=phase, errorClass=type(exc).__name__)
            atomic_json(output, state)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True)
    parser.add_argument('--rollback-receipt', required=True)
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    handles = []
    try:
        if Path.cwd().resolve() != ROOT:
            raise ValueError('wrong-project-directory')
        for name in LOCKS:
            handle = open(safe_path(ROOT / '.releases' / name), 'a')
            handles.append(handle)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(restore(Path(args.backup), Path(args.rollback_receipt), args.restore)))
    except Exception as exc:
        raise SystemExit('storage-source-restoration-failed:' + type(exc).__name__) from None
    finally:
        for handle in reversed(handles):
            handle.close()


if __name__ == '__main__':
    main()
