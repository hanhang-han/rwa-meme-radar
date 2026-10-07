#!/usr/bin/env python3
"""Publish only checksum-reviewed storage sources and absent PG dependencies."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path


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
PACKAGES = frozenset('''@types/pg pg pg-cloudflare pg-connection-string pg-int8 pg-pool
pg-protocol pg-types pgpass postgres-array postgres-bytea postgres-date postgres-interval
split2 xtend'''.split())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def verify_tree(root, package, dependencies):
    expected = {name: value for name, value in dependencies['files'].items()
                if name.startswith(package + '/')}
    actual = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('symlink-dependency')
        if path.is_file():
            actual[package + '/' + str(path.relative_to(root))] = digest(path)
    if actual != expected:
        raise ValueError('dependency-tree-check-failed:' + package)


def publish(candidate, perform):
    root = Path('/opt/memedashboard')
    if Path.cwd().resolve() != root:
        raise ValueError('wrong-project-directory')
    manifest = json.loads((candidate / 'runtime-release.json').read_text())
    if manifest.get('format') != 'cliperx-full-storage-runtime-v1' or set(manifest['files']) != FILES:
        raise ValueError('unreviewed-source-allowlist')
    config = json.loads((root / 'data/architecture-runtime.json').read_text())
    if config.get('managedBy') != 'cliperx-architecture-v1' or (config.get('storage') or {}).get('enabled'):
        raise ValueError('publication-requires-old-authority')
    for name, hashes in manifest['files'].items():
        current, source = root / name, candidate / name
        if current.is_symlink() or source.is_symlink():
            raise ValueError('symlink-source:' + name)
        if digest(current) != hashes['before'] or digest(source) != hashes['after']:
            raise ValueError('source-hash-mismatch:' + name)
    dependencies = json.loads((candidate / 'node-incremental-manifest.json').read_text())
    if set(dependencies['packages']) != {'node_modules/' + name for name in PACKAGES}:
        raise ValueError('unreviewed-dependency-allowlist')
    lockfile = json.loads((candidate / 'package-lock.json').read_text())['packages']
    listed = set(dependencies['files'])
    if any(not any(name.startswith(package + '/') for package in dependencies['packages'])
           for name in listed):
        raise ValueError('dependency-file-outside-allowlist')
    for package, spec in dependencies['packages'].items():
        source, destination = candidate / package, root / package
        if os.path.lexists(destination):
            raise ValueError('refusing-existing-dependency:' + package)
        if source.is_symlink() or not source.is_dir():
            raise ValueError('invalid-staged-package:' + package)
        if json.loads((source / 'package.json').read_text())['version'] != spec['version']:
            raise ValueError('dependency-version-mismatch:' + package)
        if any(spec[key] != lockfile[package].get(key) for key in ('version', 'integrity')):
            raise ValueError('dependency-lock-mismatch:' + package)
        actual = set()
        for path in source.rglob('*'):
            if path.is_symlink():
                raise ValueError('symlink-dependency')
            if path.is_file():
                relative = str(path.relative_to(candidate))
                actual.add(relative)
                if digest(path) != dependencies['files'].get(relative):
                    raise ValueError('dependency-hash-mismatch:' + relative)
        if actual != {name for name in listed if name.startswith(package + '/')}:
            raise ValueError('unexpected-staged-dependency-files')
    summary = {'sourceFiles': len(FILES), 'newDependencies': len(PACKAGES), 'runtimeCutover': False}
    if not perform:
        return {**summary, 'preflightPassed': True}
    backup = root / '.releases' / ('storage-runtime-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + str(os.getpid()))
    backup.mkdir(mode=0o700)
    shutil.copy2(candidate / 'runtime-release.json', backup / 'runtime-release.json')
    shutil.copy2(candidate / 'node-incremental-manifest.json', backup / 'node-incremental-manifest.json')
    for name in FILES:
        current = root / name
        if current.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current, saved)
    added = []
    try:
        for package in sorted(dependencies['packages']):
            current = root / package
            current.parent.mkdir(parents=True, exist_ok=True)
            temporary = current.with_name(current.name + '.storage-release')
            if os.path.lexists(temporary) or os.path.lexists(current):
                raise ValueError('dependency-path-became-occupied')
            shutil.copytree(candidate / package, temporary)
            verify_tree(temporary, package, dependencies)
            os.rename(temporary, current)
            added.append(package)
        for name in sorted(FILES):
            current = root / name
            temporary = current.with_name(current.name + '.storage-release')
            if os.path.lexists(temporary):
                raise ValueError('occupied-release-temporary')
            shutil.copy2(candidate / name, temporary)
            os.replace(temporary, current)
        for name, hashes in manifest['files'].items():
            if digest(root / name) != hashes['after']:
                raise ValueError('published-source-check-failed')
        for package in added:
            verify_tree(root / package, package, dependencies)
    except BaseException:
        restored, failed = [], []
        for name, hashes in manifest['files'].items():
            current = root / name
            try:
                if hashes['before'] is None:
                    current.unlink(missing_ok=True)
                else:
                    temporary = current.with_name(current.name + '.storage-restore')
                    if os.path.lexists(temporary):
                        raise ValueError('occupied-restore-temporary')
                    shutil.copy2(backup / name, temporary)
                    os.replace(temporary, current)
                if digest(current) != hashes['before']:
                    raise ValueError('source-restoration-check-failed')
                restored.append(name)
            except Exception as error:
                failed.append({'path': name, 'errorClass': type(error).__name__})
        (backup / 'publication-failed.json').write_text(json.dumps({
            'newDependenciesRetained': added, 'restoredSources': restored,
            'failedRestorations': failed, 'sourceRollbackVerified': not failed}, indent=2) + '\n')
        raise
    result = {**summary, 'backup': str(backup), 'published': True,
              'newDependencyPaths': added, 'files': manifest['files']}
    (backup / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    handles = []
    try:
        for name in ('architecture-deploy.lock', 'pool-pipeline-repair.lock', 'dashboard-web-only-deploy.lock'):
            handle = open(Path('/opt/memedashboard/.releases') / name, 'a')
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handles.append(handle)
        print(json.dumps(publish(Path(args.candidate).resolve(), args.publish)))
    except Exception as exc:
        raise SystemExit('storage-publication-failed:' + type(exc).__name__)
    finally:
        for handle in reversed(handles):
            handle.close()
