"""Bounded, restartable full-history shadow migration; never switches authority."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .storage_migration import ShadowMigration, RANGE_TABLE, CONTROL_TABLE, sqlite_busy


DOMAINS = (
    ('market', 'data/live-market.sqlite'),
    ('research', 'data/research.sqlite'),
    ('accounts', 'data/developer-access.sqlite'),
    ('budget', 'data/okx-budget.sqlite'),
    ('leases', 'data/research.sqlite.leases.sqlite'),
    ('stream_archive', 'data/stream-events.sqlite'),
)
MAX_REPLAY_BATCHES = 8


def error_category(exc):
    """Fixed diagnostic labels: no DSN, SQL statement or business row values."""
    if sqlite_busy(exc):
        return 'sqlite-busy-retries-exhausted'
    if isinstance(exc, sqlite3.IntegrityError):
        return 'sqlite-integrity-error'
    if isinstance(exc, sqlite3.OperationalError):
        return 'sqlite-operational-error'
    sqlstate = getattr(exc, 'sqlstate', None) or ''
    if sqlstate == '53300' or sqlstate.startswith('08'):
        return 'postgres-connection-unavailable'
    if sqlstate == '55P03':
        return 'postgres-lock-timeout'
    if sqlstate == '57014':
        return 'postgres-statement-timeout'
    if sqlstate.startswith('23'):
        return 'postgres-integrity-error'
    if sqlstate.startswith('42'):
        return 'postgres-schema-error'
    if sqlstate.startswith('22'):
        return 'postgres-data-error'
    if isinstance(exc, ValueError):
        return 'migration-validation-failed'
    if isinstance(exc, RuntimeError):
        return 'migration-runtime-error'
    return 'unexpected-error'


def online_pass(migration, *, stopped=lambda: False, on_batch=lambda: None):
    """Bounded domain turn; busy research never starves market/accounts."""
    replayed = batches = checked = pending_ranges = 0
    caught_up = False
    for _ in range(MAX_REPLAY_BATCHES):
        if stopped():
            break
        count = migration.replay()
        replayed += count
        batches += 1
        on_batch()
        if count < migration.batch_rows:
            caught_up = True
            break
    pending = migration.has_pending()
    # Index-only journal peek is cheap even with millions of pending entries.
    # Certifying bodies while CDC is catching up wastes most migration time.
    # A short batch means we caught the frontier. Continuous hot writes may
    # already have produced a few new entries; still warm bounded cold ranges
    # while each pending range skips its source bodies before certification.
    if (not pending or caught_up) and not stopped():
        for progress in migration.reconcile_ranges(max_ranges=64):
            checked += 1
            pending_ranges += int(progress['pending'])
            if stopped():
                break
    return {'replayedLastPass': replayed, 'replayBatchesLastPass': batches,
            'checkedRangesLastPass': checked, 'pendingRangesLastPass': pending_ranges,
            'cdcPending': pending}


def ready_for_barrier(progress, coverage, incomplete):
    return bool(coverage['coverageComplete'] and not coverage['dirtyRanges']
                and not incomplete and not progress['cdcPending']
                and not progress['pendingRangesLastPass'])


def run(settings_path, status_path):
    config = json.loads(Path(settings_path).read_text())
    if config.get('managedBy') != 'cliperx-architecture-v1':
        raise ValueError('unmanaged-postgres-configuration')
    overrides = config.get('storageDsns') or {}
    if set(overrides) - {'market'} or any(not isinstance(value, str) for value in overrides.values()):
        raise ValueError('unsupported-storage-instance-override')
    if (config.get('storage') or {}).get('enabled'):
        raise ValueError('refusing-shadow-migration-after-authority-switch')
    status_path = Path(status_path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path = status_path.with_suffix('.stop')
    if stop_path.exists():
        raise ValueError('migration-stop-request-already-present')
    state = {'pid': os.getpid(), 'phase': 'preparing', 'startedAtMs': int(time.time()*1000),
             'runtimeCutover': False, 'historyRetentionChanged': False, 'domains': {}}
    state_lock = threading.Lock()
    halted = threading.Event()

    def record(**changes):
        with state_lock:
            state.update(changes)
            state['updatedAtMs'] = int(time.time()*1000)
            temporary = status_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2)+'\n')
            temporary.chmod(0o600)
            os.replace(temporary, status_path)

    def heartbeat():
        while not halted.wait(5):
            record()

    thread = threading.Thread(target=heartbeat, daemon=True)
    migrations = []
    try:
        record()
        thread.start()
        for domain, source in DOMAINS:
            record(phase='preparing', domain=domain)
            domain_config = {**config, 'dsn': overrides.get(domain, config['dsn'])}
            migration = ShadowMigration(domain_config, source, 'cliperx_storage_'+domain,
                                        batch_rows=2000, reserve_bytes=20*1024**3)
            migrations.append((domain, migration))
            migration.prepare()
            with state_lock:
                state['domains'][domain] = {'schema': migration.schema, 'tables': len(migration.tables),
                                             'backfillDone': False, 'rowsCopiedThisRun': 0,
                                             'dedicatedInstance': domain in overrides}
        for domain, migration in migrations:
            for name in migration.tables:
                record(phase='backfill', domain=domain, table=name)
                began = time.monotonic()
                logged = 0
                for progress in migration.backfill(name):
                    with state_lock:
                        state['domains'][domain]['rowsCopiedThisRun'] += progress.get('rows', 0)
                    record(**progress, domain=domain)
                    if time.monotonic()-logged > 30:
                        print(json.dumps({'phase': progress['phase'], 'domain': domain, 'table': name,
                                          'elapsedSeconds': round(time.monotonic()-began),
                                          'lastRowid': progress.get('lastRowid')}), flush=True)
                        logged = time.monotonic()
                    if stop_path.exists():
                        record(phase='stopped-before-cutover')
                        return
                    time.sleep(.005)
            with state_lock:
                state['domains'][domain]['backfillDone'] = True
        while not stop_path.exists():
            record(phase='online-replay-and-reconciliation', table=None)
            all_ready = True
            for domain, migration in migrations:
                if stop_path.exists():
                    break
                record(domain=domain)
                progress = online_pass(migration, stopped=stop_path.exists, on_batch=record)
                coverage = migration.range_coverage()
                incomplete = migration.pg.execute(f'SELECT count(*) FROM {CONTROL_TABLE} '
                    'WHERE NOT backfill_done OR NOT indexes_done').fetchone()[0]
                with state_lock:
                    state['domains'][domain].update({**progress, **coverage, 'incompleteTables': incomplete})
                all_ready = all_ready and ready_for_barrier(progress, coverage, incomplete)
                record()
            record(phase='awaiting-final-writer-barrier' if all_ready else 'online-replay-and-reconciliation', domain=None)
            halted.wait(.5 if all_ready else .05)
        record(phase='stopped-before-cutover')
    except Exception as exc:
        failure = {'phase': 'failed', 'errorClass': type(exc).__name__, 'errorCategory': error_category(exc)}
        record(**failure)
        print(json.dumps(failure), flush=True)
        raise SystemExit(1) from None
    finally:
        halted.set()
        if thread.is_alive():
            thread.join(timeout=6)
        for _, migration in migrations:
            migration.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', default='data/architecture-runtime.json')
    parser.add_argument('--status', default='data/storage-migration-health.json')
    args = parser.parse_args()
    run(args.settings, args.status)
