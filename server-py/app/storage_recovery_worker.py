"""Bounded six-domain PostgreSQL to guarded SQLite standby maintenance."""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path

from .storage_migration_worker import DOMAINS
from .storage_recovery import RecoveryMirror


def run(settings_path, status_path):
    config = json.loads(Path(settings_path).read_text())
    storage = config.get('storage') or {}
    expected = {domain: 'cliperx_storage_' + domain for domain, _ in DOMAINS}
    overrides = config.get('storageDsns', {})
    if (config.get('managedBy') != 'cliperx-architecture-v1'
            or not isinstance(config.get('dsn'), str) or not config['dsn'].strip()
            or not isinstance(overrides, dict) or set(overrides) - {'market'}
            or any(not isinstance(value, str) or not value.strip() for value in overrides.values())
            or storage.get('enabled') is not True
            or storage.get('backend') != 'postgres'
            or storage.get('domainSchemas') != expected):
        raise ValueError('recovery-requires-complete-postgres-authority')
    status_path = Path(status_path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path = status_path.with_suffix('.stop')
    if stop_path.exists():
        raise ValueError('recovery-stop-request-already-present')
    state = {'pid': os.getpid(), 'phase': 'starting', 'runtimeCutover': True,
             'ready': False, 'lastPassCompletedAtMs': None,
             'startedAtMs': int(time.time() * 1000), 'domains': {}}
    lock = threading.Lock()
    halted = threading.Event()
    mirrors = []

    def record(**changes):
        with lock:
            state.update(changes)
            state['updatedAtMs'] = int(time.time() * 1000)
            temporary = status_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
            temporary.chmod(0o600)
            os.replace(temporary, status_path)

    def heartbeat():
        while not halted.wait(5):
            record()

    thread = threading.Thread(target=heartbeat, daemon=True)
    try:
        record()
        thread.start()
        for domain, source in DOMAINS:
            mirror = RecoveryMirror(config, source, expected[domain], domain=domain,
                                    batch_rows=1000,
                                    batch_bytes=(64 if domain == 'research' else 32) * 1024**2,
                                    reserve_bytes=20 * 1024**3)
            mirrors.append((domain, mirror))
            mirror._load()
            mirror._validate_file()
        last_log = 0
        last_reconcile = {}
        while not stop_path.exists():
            record(phase='mirroring')
            busy = False
            for domain, mirror in mirrors:
                if stop_path.exists():
                    break
                record(domain=domain)
                replayed = mirror.replay()
                busy = busy or replayed >= mirror.batch_rows
                checked = 0
                if replayed < mirror.batch_rows and (replayed or
                        time.monotonic() - last_reconcile.get(domain, 0) >= 5):
                    for _ in mirror.reconcile_ranges(max_ranges=8):
                        checked += 1
                        if stop_path.exists():
                            break
                    last_reconcile[domain] = time.monotonic()
                status = mirror.status()
                with lock:
                    state['domains'][domain] = {**status, 'replayedLastPass': replayed,
                                               'checkedRangesLastPass': checked,
                                               'lastCompletedAtMs': int(time.time() * 1000)}
                record()
            if not stop_path.exists():
                record(ready=len(state['domains']) == len(DOMAINS),
                       lastPassCompletedAtMs=int(time.time() * 1000))
            if time.monotonic() - last_log > 30:
                print(json.dumps({'phase': state['phase'], 'domains': state['domains']}), flush=True)
                last_log = time.monotonic()
            halted.wait(.1 if busy else 1)
        record(phase='stopped-before-recovery-barrier', domain=None)
    except Exception as exc:
        record(phase='failed', errorClass=type(exc).__name__)
        print(json.dumps({'phase': 'failed', 'errorClass': type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
    finally:
        halted.set()
        if thread.is_alive():
            thread.join(timeout=6)
        for _, mirror in mirrors:
            mirror.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', default='data/architecture-runtime.json')
    parser.add_argument('--status', default='data/storage-recovery-health.json')
    args = parser.parse_args()
    run(args.settings, args.status)
