"""Failure-oriented recovery tests against disposable real PostgreSQL schemas."""
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from app import storage_runtime
from app.storage_migration import (RANGE_TABLE, ShadowMigration, inspect_source, quote, sqlite_row,
                                   target_ddl, typed_row_bytes)
from app.storage_recovery import (GUARD_PREFIX, RECOVERY_CHANGES, RECOVERY_COUNTER,
                                  RECOVERY_OWNER, RECOVERY_RANGES, RecoveryMirror)
from app.storage_schema import postgres_runtime_schema_sql


def create_source(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript('''
            CREATE TABLE users(id TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE children(id TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id),
                value REAL,payload BLOB);
            CREATE TABLE realtime_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,body TEXT NOT NULL,at INTEGER NOT NULL);
            CREATE TABLE dashboard_projection(name TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TRIGGER realtime_users_insert AFTER INSERT ON users BEGIN
                INSERT INTO realtime_events(event,body,at) VALUES('audit',NEW.body,1790000000000);
            END;
        ''')
        connection.execute('INSERT INTO users VALUES(?,?)', ('u', '{"name":"中文🙂","zero":0,"missing":null}'))
        connection.execute('INSERT INTO children VALUES(?,?,?,?)', ('c', 'u', .000000000000012345, b'\x00\xff'))
        connection.execute('INSERT INTO dashboard_projection VALUES(?,?)', ('text', '中文文本'))
        connection.execute('INSERT INTO dashboard_projection VALUES(?,?)', ('blob', b'\x00\xff\x01'))
        connection.commit()


@pytest.fixture
def recovery(tmp_path, monkeypatch, request):
    settings = os.environ.get('MIGRATION_TEST_SETTINGS')
    if not settings:
        pytest.skip('Real recovery fixture needs MIGRATION_TEST_SETTINGS')
    config = json.loads(Path(settings).read_text())
    path = tmp_path / 'research.sqlite'
    create_source(path)
    schema = 'cliperx_storage_test_' + uuid.uuid4().hex
    monkeypatch.setattr(storage_runtime, '_SCHEMAS', {**storage_runtime._SCHEMAS, 'research': schema})
    forward = ShadowMigration(config, path, schema, batch_rows=2)
    mirror = None
    try:
        forward.prepare()
        for table in forward.tables:
            list(forward.backfill(table))
        list(forward.reconcile_ranges())
        receipt = forward.verify_barrier()
        extras = []
        runtime_tables = dict(forward.tables)
        if getattr(request, 'param', None) == 'telegram-extras':
            from app.aux_storage_schema import TELEGRAM_TABLES, declared_account_tables
            declarations = declared_account_tables()
            with closing(sqlite3.connect(path)) as source:
                for name in TELEGRAM_TABLES:
                    table = declarations[name]
                    source.execute(table['sql'])
                    for statement in table['indexes']:
                        source.execute(statement)
                    forward.pg.execute(target_ddl(table))
                    extras.append(name)
                    runtime_tables[name] = table
                source.commit()
        mirror = RecoveryMirror(config, path, schema, batch_rows=2)
        mirror.prepare(receipt, allowed_extra_tables=extras)
        forward.pg.execute(postgres_runtime_schema_sql(schema, runtime_tables, realtime_outbox=False))
        yield mirror, forward, path, config, receipt
    finally:
        if mirror is not None:
            mirror.close()
        forward.pg.execute('DROP SCHEMA ' + quote(schema) + ' CASCADE')
        forward.close()


def drain(mirror):
    while mirror.replay():
        pass
    list(mirror.reconcile_ranges(max_ranges=None))


def exact_content(mirror, path):
    with closing(sqlite3.connect(path)) as connection:
        for name, table in mirror.tables.items():
            columns = ','.join(quote(column['name']) for column in table['columns'])
            actual = connection.execute(f'SELECT rowid,{columns} FROM {quote(name)} ORDER BY rowid').fetchall()
            expected = mirror.pg.execute(f'SELECT _cliperx_source_rowid,{columns} FROM {quote(name)} '
                                         'ORDER BY _cliperx_source_rowid').fetchall()
            assert [typed_row_bytes(row) for row in actual] == [typed_row_bytes(sqlite_row(table, row)) for row in expected]


def test_owned_trigger_backup_and_closed_old_writer_gate(recovery):
    mirror, _, path, _, _ = recovery
    manifest = json.loads(Path(str(path) + '.recovery-manifest.json').read_text())
    assert any(trigger['name'] == 'realtime_users_insert' for trigger in manifest['originalTriggers'])
    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE users SET body='old-writer' WHERE id='u'")
        triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert 'realtime_users_insert' not in triggers
    assert all(guard['name'].startswith(GUARD_PREFIX) and guard['name'] in triggers for guard in manifest['guards'])
    assert mirror.verify_barrier()['standbyReady']


def test_all_fields_types_and_atomic_pg_rollback(recovery):
    mirror, _, path, _, _ = recovery
    with pytest.raises(RuntimeError):
        with mirror.pg.transaction():
            mirror.pg.execute("UPDATE users SET body='rolled-back' WHERE id='u'")
            mirror.pg.execute("INSERT INTO realtime_events(event,body,at) VALUES('price','{}',1790000000001)")
            raise RuntimeError('injected-pg-rollback')
    assert mirror.pg.execute(f'SELECT seq FROM {RECOVERY_COUNTER}').fetchone()[0] == 0
    assert mirror.pg.execute(f'SELECT count(*) FROM {RECOVERY_CHANGES}').fetchone()[0] == 0
    with mirror.pg.transaction():
        mirror.pg.execute('UPDATE users SET body=%s WHERE id=%s', ('{"zero":0,"missing":null,"name":"中文🙂"}', 'u'))
        mirror.pg.execute('UPDATE children SET value=%s,payload=%s WHERE id=%s', (1.123456789012345, b'\xff\x00\x01', 'c'))
        mirror.pg.execute('UPDATE dashboard_projection SET body=%s WHERE name=%s', (b's' + '文字🙂'.encode(), 'text'))
        mirror.pg.execute('UPDATE dashboard_projection SET body=%s WHERE name=%s', (b'b\x00\xfe', 'blob'))
    with pytest.raises(ValueError, match='pending-cdc'):
        mirror.verify_barrier()
    drain(mirror)
    exact_content(mirror, path)
    assert mirror.verify_barrier()['foreignKeysValid']


def test_deleted_rowid_reused_by_a_new_primary_key(recovery):
    mirror, _, path, _, _ = recovery
    with mirror.pg.transaction():
        # PostgreSQL converted FKs are deferred: the final transaction is
        # valid even when parent deletion appears before child deletion.
        mirror.pg.execute("DELETE FROM users WHERE id='u'")
        mirror.pg.execute("DELETE FROM children WHERE id='c'")
        mirror.pg.execute("INSERT INTO users(id,body,_cliperx_source_rowid) VALUES('new','replacement',1)")
    drain(mirror)
    exact_content(mirror, path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('SELECT rowid,id FROM users').fetchall() == [(1, 'new')]
        # Mirror writes must not recreate audit events through old triggers.
        assert connection.execute('SELECT count(*) FROM realtime_events').fetchone()[0] == 1
    assert mirror.verify_barrier()['standbyReady']


def test_primary_key_and_hidden_rowid_move(recovery):
    mirror, _, path, _, _ = recovery
    with mirror.pg.transaction():
        mirror.pg.execute("UPDATE users SET id='renamed',_cliperx_source_rowid=-5 WHERE id='u'")
        mirror.pg.execute("UPDATE children SET user_id='renamed' WHERE id='c'")
    drain(mirror)
    exact_content(mirror, path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('SELECT rowid,id FROM users').fetchall() == [(-5, 'renamed')]
    assert mirror.verify_barrier()['standbyReady']


def test_update_during_ack_preserves_newer_journal_entry(recovery):
    mirror, _, path, _, _ = recovery
    mirror.pg.execute("UPDATE users SET body='first' WHERE id='u'")
    once = []
    def raced():
        if not once:
            once.append(True)
            mirror.pg.execute("UPDATE users SET body='second' WHERE id='u'")
    mirror._after_sqlite_commit = raced
    assert mirror.replay() == 1
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT body FROM users WHERE id='u'").fetchone()[0] == 'first'
    assert mirror.pg.execute(f'SELECT count(*) FROM {RECOVERY_CHANGES}').fetchone()[0] == 1
    drain(mirror)
    exact_content(mirror, path)
    assert mirror.verify_barrier()['standbyReady']


def test_commit_without_ack_replays_idempotently(recovery):
    mirror, _, path, _, _ = recovery
    mirror.pg.execute("UPDATE users SET body='committed' WHERE id='u'")
    def failed():
        raise RuntimeError('injected-after-sqlite-commit')
    mirror._after_sqlite_commit = failed
    with pytest.raises(RuntimeError, match='after-sqlite'):
        mirror.replay()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT body FROM users WHERE id='u'").fetchone()[0] == 'committed'
    assert mirror.pg.execute(f'SELECT count(*) FROM {RECOVERY_CHANGES}').fetchone()[0] == 1
    mirror._after_sqlite_commit = lambda: None
    drain(mirror)
    exact_content(mirror, path)
    assert mirror.verify_barrier()['standbyReady']


def test_split_deferrable_foreign_key_transaction_cannot_promote_mid_batch(recovery):
    mirror, _, path, _, _ = recovery
    mirror.batch_rows = 1
    with mirror.pg.transaction():
        mirror.pg.execute("INSERT INTO children(id,user_id,value,payload) VALUES('next-child','next-parent',0,NULL)")
        mirror.pg.execute("INSERT INTO users(id,body) VALUES('next-parent','{}')")
    assert mirror.replay() == 1
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('PRAGMA foreign_key_check').fetchone()
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO users VALUES('ordinary','{}')")
    with pytest.raises(ValueError, match='pending-cdc'):
        mirror.verify_barrier()
    drain(mirror)
    assert mirror.verify_barrier()['foreignKeysValid']
    exact_content(mirror, path)


def test_foreign_key_failure_blocks_promotion_even_with_warm_certificates(recovery):
    mirror, _, _, _, _ = recovery
    with closing(mirror._sqlite(writable=True)) as connection:
        connection.execute("UPDATE children SET user_id='missing'")
        connection.commit()
    with pytest.raises(ValueError, match='foreign-key-check'):
        mirror.verify_barrier()
    with pytest.raises(ValueError, match='requires-current-verification'):
        mirror.restore_sqlite()


def test_dirty_or_uncovered_ranges_block_barrier_until_warmed(recovery):
    mirror, _, path, _, _ = recovery
    mirror.pg.execute("INSERT INTO users(id,body,_cliperx_source_rowid) VALUES('far','{}',100000)")
    while mirror.replay():
        pass
    with pytest.raises(ValueError, match='uncovered-range'):
        mirror.verify_barrier()
    list(mirror.reconcile_ranges(max_ranges=None))
    assert mirror.verify_barrier()['standbyReady']
    mirror.pg.execute("UPDATE users SET body='changed' WHERE id='u'")
    while mirror.replay():
        pass
    with pytest.raises(ValueError, match='dirty-range'):
        mirror.verify_barrier()
    list(mirror.reconcile_ranges(max_ranges=None))
    exact_content(mirror, path)
    assert mirror.verify_barrier()['standbyReady']


def test_restore_original_triggers_and_deleted_event_sequence_frontier(recovery):
    mirror, _, path, _, _ = recovery
    deleted_id = mirror.pg.execute("INSERT INTO realtime_events(event,body,at) VALUES('temporary','{}',1790000000001) RETURNING id").fetchone()[0]
    mirror.pg.execute('DELETE FROM realtime_events WHERE id=%s', (deleted_id,))
    drain(mirror)
    receipt = mirror.verify_barrier()
    restored = mirror.restore_sqlite()
    assert restored['sourceSequence'] == receipt['sourceSequence']
    assert restored['triggersRestored']
    with pytest.raises(Exception, match='postgres-writer-gate-closed'):
        mirror.pg.execute("UPDATE users SET body='late-pg-writer' WHERE id='u'")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute("INSERT INTO users VALUES('after-rollback','restored')")
        connection.commit()
        assert connection.execute('SELECT max(id) FROM realtime_events').fetchone()[0] > deleted_id
        assert not any(row[0].startswith(GUARD_PREFIX) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"))
    with pytest.raises(RuntimeError, match='no-longer-active'):
        mirror.replay()


def test_source_file_replacement_and_schema_drift_fail_closed(recovery, tmp_path):
    mirror, _, path, _, _ = recovery
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('CREATE INDEX changed_standby_schema ON users(body)')
    with pytest.raises(ValueError, match='schema-or-write-guards'):
        mirror.replay()
    replacement = tmp_path / 'replacement.sqlite'
    shutil.copyfile(path, replacement)
    os.replace(replacement, path)
    with pytest.raises(ValueError, match='file-identity'):
        mirror.replay()


def test_prepare_idempotence_preserves_original_trigger_backup(recovery):
    mirror, _, path, _, receipt = recovery
    first = json.loads(Path(str(path) + '.recovery-manifest.json').read_text())
    assert mirror.prepare(receipt) == first
    assert mirror.verify_barrier()['standbyReady']


@pytest.mark.parametrize('recovery', ['telegram-extras'], indirect=True)
def test_authorised_six_telegram_tables_join_the_exact_standby(recovery):
    mirror, _, path, _, receipt = recovery
    from app.aux_storage_schema import TELEGRAM_TABLES
    assert set(TELEGRAM_TABLES).issubset(mirror.tables)
    assert len(mirror.tables) == len(receipt['tables']) + 6
    with closing(sqlite3.connect(path)) as connection:
        for name in TELEGRAM_TABLES:
            assert connection.execute('SELECT count(*) FROM ' + quote(name)).fetchone()[0] == 0
            columns = mirror.tables[name]['columns']
            values = [0 if column['type'] == 'INTEGER' else .0 if column['type'] == 'REAL'
                      else b'' if column['type'] == 'BLOB' else '' for column in columns]
            with pytest.raises(sqlite3.OperationalError):
                connection.execute('INSERT INTO ' + quote(name) + '(' +
                    ','.join(quote(column['name']) for column in columns) + ') VALUES(' +
                    ','.join('?' for _ in values) + ')', values)
        connection.rollback()
    exact_content(mirror, path)
    verified = mirror.verify_barrier()
    assert {row['table'] for row in verified['tables']} == set(mirror.tables)
    assert verified['foreignKeysValid']
    restored = mirror.restore_sqlite()
    assert {row['table'] for row in restored['tables']} == set(mirror.tables)
    assert all(row['match'] and row['proof'] == 'ordered-typed-content-per-range' for row in restored['tables'])


def test_large_dirty_interval_is_split_into_bounded_certificates(recovery):
    mirror, _, path, _, _ = recovery
    with mirror.pg.transaction():
        for index in range(2, 15):
            mirror.pg.execute('INSERT INTO users(id,body,_cliperx_source_rowid) VALUES(%s,%s,%s)',
                              ('u' + str(index), '{}', index))
    while mirror.replay():
        pass
    # Simulate a broad initial forward certificate that needs recalibration.
    mirror.pg.execute(f"DELETE FROM {RECOVERY_RANGES} WHERE table_name='users'")
    mirror.pg.execute(f"INSERT INTO {RECOVERY_RANGES} VALUES('users',1,14,0,'',false,0)")
    statuses = list(mirror.reconcile_ranges(max_ranges=None))
    assert all(status.get('rows', 0) <= mirror.batch_rows for status in statuses)
    intervals = mirror.pg.execute(f"SELECT low_rowid,high_rowid,row_count FROM {RECOVERY_RANGES} WHERE table_name='users' ORDER BY low_rowid").fetchall()
    assert len(intervals) == 7 and sum(row[2] for row in intervals) == 14
    assert all(left[1] + 1 == right[0] for left, right in zip(intervals, intervals[1:]))
    exact_content(mirror, path)
    assert mirror.verify_barrier()['standbyReady']


def test_prepare_pg_commit_before_sqlite_commit_resumes_without_losing_protocol(recovery):
    mirror, _, path, _, baseline = recovery
    mirror.verify_barrier()
    mirror.restore_sqlite()
    mirror.abort()
    def failed():
        raise RuntimeError('injected-after-prepare-pg-commit')
    mirror._after_prepare_pg_commit = failed
    with pytest.raises(RuntimeError, match='prepare-pg-commit'):
        mirror.prepare(baseline)
    assert mirror.pg.execute(f'SELECT state FROM {RECOVERY_OWNER}').fetchone()[0] == 'active'
    with closing(sqlite3.connect(path)) as connection:
        actual = dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
        assert 'realtime_users_insert' in actual
        assert not any(name.startswith(GUARD_PREFIX) for name in actual)
    mirror._after_prepare_pg_commit = lambda: None
    manifest = mirror.prepare(baseline)
    assert 'realtime_users_insert' in {trigger['name'] for trigger in manifest['originalTriggers']}
    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE users SET body='late-old-writer' WHERE id='u'")
    assert mirror.verify_barrier()['standbyReady']


def test_abort_exports_verified_certificates_and_allows_recovery_retry(recovery):
    mirror, forward, path, _, _ = recovery
    with pytest.raises(ValueError, match='requires-verified-restoration'):
        mirror.abort()
    mirror.pg.execute("UPDATE users SET body='postgres-authoritative' WHERE id='u'")
    drain(mirror)
    verified = mirror.verify_barrier()
    restored = mirror.restore_sqlite()
    bundle = mirror.abort()
    assert bundle['sourceSequence'] == verified['sourceSequence']
    assert bundle['restoredReceipt'] == restored
    assert bundle['sourceSchemaHash'] == inspect_source(path)['schemaHash']
    assert bundle['triggersRestored'] and bundle['foreignKeysValid'] and bundle['recoveryRemoved']
    assert {row['table'] for row in bundle['tables']} == set(mirror.tables)
    assert {row['table'] for row in bundle['rangeCertificates']} == set(mirror.tables)
    assert all(row['valid'] and len(row['contentSha256']) == 64 for row in bundle['rangeCertificates'])
    assert json.loads(Path(bundle['archivePath']).read_text()) == bundle
    assert Path(bundle['archivePath']).stat().st_mode & 0o777 == 0o600
    assert mirror.abort() == bundle
    exact_content(mirror, path)
    # Simulate the caller's certified forward reset. This test keeps the same
    # business schema; callers adding approved tables must also rebase owner.
    with forward.pg.transaction():
        forward.pg.execute(f'DELETE FROM {RANGE_TABLE}')
        for certificate in bundle['rangeCertificates']:
            forward.pg.execute(f'INSERT INTO {RANGE_TABLE} VALUES(%s,%s,%s,%s,%s,%s,%s)',
                tuple(certificate[key] for key in ('table', 'lowRowid', 'highRowid', 'rowCount',
                                                  'contentSha256', 'valid', 'verifiedAtMs')))
    baseline = forward.verify_barrier()
    mirror.prepare(baseline)
    mirror.pg.execute("UPDATE users SET body='second-migration' WHERE id='u'")
    drain(mirror)
    exact_content(mirror, path)
    assert mirror.verify_barrier()['standbyReady']


def test_abort_commit_failure_retries_from_durable_complete_proof(recovery):
    mirror, _, path, config, _ = recovery
    mirror.verify_barrier()
    mirror.restore_sqlite()
    def failed():
        raise RuntimeError('injected-after-abort-pg-commit')
    mirror._after_abort_pg_commit = failed
    with pytest.raises(RuntimeError, match='abort-pg-commit'):
        mirror.abort()
    mirror.close()
    replacement = RecoveryMirror(config, path, mirror.schema, batch_rows=2)
    try:
        bundle = replacement.abort()
        assert bundle['recoveryRemoved']
        assert all(row['match'] for row in bundle['tables'])
        assert json.loads(Path(bundle['archivePath']).read_text()) == bundle
        exact_content(replacement, path)
    finally:
        replacement.close()


def test_restored_retries_reject_changed_trigger_protocol_and_new_source_writes(recovery):
    mirror, _, path, _, _ = recovery
    mirror.verify_barrier()
    def failed():
        raise RuntimeError('injected-after-restore-sqlite-commit')
    mirror._after_restore_sqlite_commit = failed
    with pytest.raises(RuntimeError, match='restore-sqlite-commit'):
        mirror.restore_sqlite()
    assert mirror.pg.execute(f'SELECT state FROM {RECOVERY_OWNER}').fetchone()[0] == 'active'
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('DROP TRIGGER realtime_users_insert')
        connection.execute('''CREATE TRIGGER realtime_users_insert AFTER INSERT ON users BEGIN
            INSERT INTO realtime_events(event,body,at) VALUES('wrong','{}',0); END''')
        connection.commit()
    mirror._after_restore_sqlite_commit = lambda: None
    with pytest.raises(ValueError, match='trigger-restoration-incomplete'):
        mirror.restore_sqlite()
    # Recover the exact saved SQL, then ensure a finished restore still rejects
    # subsequent ordinary SQLite mutations instead of returning stale success.
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('DROP TRIGGER realtime_users_insert')
        connection.execute(next(row['sql'] for row in mirror._load()['originalTriggers']
                                if row['name'] == 'realtime_users_insert'))
        connection.commit()
    mirror.restore_sqlite()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE users SET body='resumed-too-early' WHERE id='u'")
        connection.commit()
    with pytest.raises(ValueError, match='has-source-writes'):
        mirror.restore_sqlite()
    with pytest.raises(ValueError, match='has-source-writes'):
        mirror.abort()


def test_utf8_byte_budget_truncates_snapshot_without_acknowledging_unread_rows(recovery):
    mirror, _, path, _, _ = recovery
    mirror.batch_rows = 1000
    mirror.batch_bytes = 1024**2
    body = '🙂' * 160000  # 640kB each; character counting would admit all three.
    with mirror.pg.transaction():
        mirror.pg.execute('UPDATE users SET body=%s WHERE id=%s', (body, 'u'))
        mirror.pg.execute('INSERT INTO users(id,body) VALUES(%s,%s)', ('utf8two', body))
        mirror.pg.execute('INSERT INTO users(id,body) VALUES(%s,%s)', ('utf8three', body))
    pending = mirror.pg.execute(f'SELECT table_name,row_id,seq FROM {RECOVERY_CHANGES} ORDER BY seq,table_name,row_id').fetchall()
    read_rows = []
    original_read = mirror._read_observed_row
    def observed_read(table, rowid):
        read_rows.append((table['name'], rowid))
        return original_read(table, rowid)
    mirror._read_observed_row = observed_read
    changes, observed = mirror._snapshot_changes()
    assert changes == pending[:1]
    assert read_rows == [(pending[0][0], pending[0][1])]
    assert sum(mirror._row_bytes(row) for _, _, row in observed) <= mirror.batch_bytes
    assert mirror.replay() == 1
    assert mirror.pg.execute(f'SELECT table_name,row_id,seq FROM {RECOVERY_CHANGES} ORDER BY seq,table_name,row_id').fetchall() == pending[1:]
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT body FROM users WHERE id='u'").fetchone()[0] == body
        assert connection.execute("SELECT count(*) FROM users WHERE id LIKE 'utf8%'").fetchone()[0] == 0
    assert mirror.replay() == 1
    assert mirror.replay() == 1
    assert mirror.replay() == 0
    exact_content(mirror, path)
    mirror.batch_rows = 1
    list(mirror.reconcile_ranges(max_ranges=None))
    assert mirror.verify_barrier()['standbyReady']


def test_oversized_single_row_fails_before_payload_read_without_ack_or_source_write(recovery):
    mirror, _, path, _, _ = recovery
    mirror.batch_bytes = 1024**2
    body = '🙂' * 300000  # 1.2MB UTF-8; under 1MB when incorrectly counting chars.
    mirror.pg.execute('UPDATE users SET body=%s WHERE id=%s', (body, 'u'))
    pending = mirror.pg.execute(f'SELECT table_name,row_id,seq FROM {RECOVERY_CHANGES}').fetchall()
    def payload_must_not_be_read(table, rowid):
        raise AssertionError('oversized-payload-transferred')
    mirror._read_observed_row = payload_must_not_be_read
    with pytest.raises(ValueError, match='recovery-row-bytes-exceeded'):
        mirror.replay()
    assert mirror.pg.execute(f'SELECT table_name,row_id,seq FROM {RECOVERY_CHANGES}').fetchall() == pending
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT body FROM users WHERE id='u'").fetchone()[0] != body
    with pytest.raises(ValueError, match='pending-cdc'):
        mirror.verify_barrier()


class RecordingPG:
    def __init__(self, connection):
        self.connection, self.statements = connection, []
    def __getattr__(self, name):
        return getattr(self.connection, name)
    def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement))
        return self.connection.execute(statement, *args, **kwargs)


def test_bounded_reconciliation_round_robins_past_continuously_hot_first_table(recovery):
    mirror, _, path, _, _ = recovery
    with mirror.pg.transaction():
        for index in range(2, 26):
            mirror.pg.execute('INSERT INTO users(id,body,_cliperx_source_rowid) VALUES(%s,%s,%s)',
                              ('hot'+str(index), 'before', index))
    drain(mirror)
    mirror.pg.execute(f'UPDATE {RECOVERY_RANGES} SET valid=false')
    mirror.pg.execute("UPDATE users SET body='continuously-hot'")
    seen = set()
    for _ in range(8):
        statuses = list(mirror.reconcile_ranges(max_ranges=1))
        assert len(statuses) <= 1
        seen.update(status['table'] for status in statuses)
        if seen == set(mirror.tables):
            break
    assert seen == set(mirror.tables)
    assert mirror.pg.execute(f'SELECT 1 FROM {RECOVERY_CHANGES} LIMIT 1').fetchone()
    assert mirror.pg.execute(f"SELECT count(*) FROM {RECOVERY_RANGES} "
                             "WHERE table_name='dashboard_projection' AND NOT valid").fetchone()[0] == 0
    with pytest.raises(ValueError, match='pending-cdc'):
        mirror.verify_barrier()


def test_pending_dirty_chunk_skips_payload_and_does_not_block_cold_sparse_suffix(recovery):
    mirror, _, path, _, _ = recovery
    with mirror.pg.transaction():
        for index in range(6):
            mirror.pg.execute('INSERT INTO users(id,body,_cliperx_source_rowid) VALUES(%s,%s,%s)',
                              ('late'+str(index), 'cold', 1_000_000+index))
    drain(mirror)
    mirror.pg.execute(f"DELETE FROM {RECOVERY_RANGES} WHERE table_name='users'")
    mirror.pg.execute(f"INSERT INTO {RECOVERY_RANGES} VALUES('users',1,1000005,0,'',false,0)")
    mirror.pg.execute("UPDATE users SET body='pending' WHERE id='u'")
    recording = RecordingPG(mirror.pg)
    mirror.pg = recording
    first = list(mirror.reconcile_ranges(max_ranges=1))
    assert first[0]['table'] == 'users' and first[0]['pending']
    assert first[0]['highRowid'] == 1_000_000
    assert not any(sql.startswith('SELECT "_cliperx_source_rowid","id","body" FROM "users"')
                   for sql in recording.statements)
    pieces = mirror.pg.execute(f"SELECT low_rowid,high_rowid,valid,content_sha256 "
                               f"FROM {RECOVERY_RANGES} WHERE table_name='users' ORDER BY low_rowid").fetchall()
    assert pieces == [(1,1_000_000,False,''),(1_000_001,1_000_005,False,'')]
    second = list(mirror.reconcile_ranges(max_ranges=1))
    assert second[0]['table'] == 'users' and not second[0]['pending']
    assert second[0]['lowRowid'] == 1_000_001 and second[0]['rows'] == 2
    with pytest.raises(ValueError, match='pending-cdc'):
        mirror.verify_barrier()
    drain(mirror)
    exact_content(mirror, path)
    assert mirror.verify_barrier()['standbyReady']


def test_unbounded_reconciliation_ignores_online_table_and_rowid_cursors(recovery):
    mirror, _, path, _, _ = recovery
    mirror.pg.execute(f'UPDATE {RECOVERY_RANGES} SET valid=false')
    mirror._range_cursors = {name:2**62 for name in mirror.tables}
    mirror._range_last_table = 'users'
    statuses = list(mirror.reconcile_ranges(max_ranges=None))
    assert statuses[0]['table'] == 'users'
    assert {status['table'] for status in statuses} == set(mirror.tables)
    assert all(status.get('match') for status in statuses)
    exact_content(mirror, path)
    assert mirror.verify_barrier()['standbyReady']


def test_rowid_move_keeps_historical_empty_extent_contiguous_for_restore(recovery):
    mirror, _, path, _, _ = recovery
    mirror.pg.execute("UPDATE users SET _cliperx_source_rowid=1000000 WHERE id='u'")
    drain(mirror)
    intervals = mirror.pg.execute(f"SELECT low_rowid,high_rowid,valid FROM {RECOVERY_RANGES} "
                                  "WHERE table_name='users' ORDER BY low_rowid").fetchall()
    assert intervals[0][0] == 1 and intervals[-1][1] == 1_000_000
    assert all(left[1]+1 == right[0] for left, right in zip(intervals, intervals[1:]))
    exact_content(mirror, path)
    assert mirror.verify_barrier()['standbyReady']
    restored = mirror.restore_sqlite()
    assert restored['triggersRestored'] and restored['foreignKeysValid']


def test_reconciliation_splits_utf8_byte_budget_before_payload_transfer(recovery):
    mirror, _, path, _, _ = recovery
    mirror.batch_rows, mirror.batch_bytes = 1000, 1024**2
    body = '🙂'*160000
    with mirror.pg.transaction():
        mirror.pg.execute("UPDATE users SET body=%s WHERE id='u'", (body,))
        mirror.pg.execute('INSERT INTO users(id,body) VALUES(%s,%s)', ('byte2', body))
        mirror.pg.execute('INSERT INTO users(id,body) VALUES(%s,%s)', ('byte3', body))
    while mirror.replay():
        pass
    statuses = list(mirror.reconcile_ranges(max_ranges=None))
    users = [status for status in statuses if status['table']=='users']
    assert len(users) == 3 and all(status['rows'] == 1 for status in users)
    assert all(status['match'] for status in users)
    exact_content(mirror, path)
    assert mirror.verify_barrier()['standbyReady']


def test_explicit_larger_budget_replays_large_event_without_losing_proof(recovery):
    mirror, _, path, _, _ = recovery
    assert mirror.batch_bytes == 32 * 1024**2
    body = 'x' * (34 * 1024**2)
    with closing(sqlite3.connect(path)) as connection:
        before_count = connection.execute('SELECT count(*) FROM realtime_events').fetchone()[0]
    mirror.pg.execute('INSERT INTO realtime_events(event,body,at) VALUES(%s,%s,%s)',
                      ('projection.delta', body, 1790000000001))
    pending = mirror.pg.execute(f'SELECT table_name,row_id,seq FROM {RECOVERY_CHANGES}').fetchall()
    reads = []
    read_observed = mirror._read_observed_row
    def tracked_read(table, rowid):
        reads.append((table['name'], rowid))
        return read_observed(table, rowid)
    mirror._read_observed_row = tracked_read
    with pytest.raises(ValueError, match='recovery-row-bytes-exceeded'):
        mirror.replay()
    assert not reads
    assert mirror.pg.execute(f'SELECT table_name,row_id,seq FROM {RECOVERY_CHANGES}').fetchall() == pending
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute('SELECT count(*) FROM realtime_events').fetchone()[0] == before_count

    mirror.batch_bytes = 64 * 1024**2
    assert mirror.replay() == 1
    assert len(reads) == 1 and reads[0][0] == 'realtime_events'
    assert mirror.pg.execute(f'SELECT count(*) FROM {RECOVERY_CHANGES}').fetchone()[0] == 0
    list(mirror.reconcile_ranges(max_ranges=None))
    exact_content(mirror, path)
    assert mirror.verify_barrier()['standbyReady']
    restored = mirror.restore_sqlite()
    assert restored['triggersRestored'] and restored['foreignKeysValid']
