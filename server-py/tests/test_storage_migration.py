import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest
import app.storage_migration as migration_module

from app.storage_migration import (INTERNAL_PREFIX, ShadowMigration, inspect_source,
    install_journal, pg_row, sqlite_row, target_ddl, target_index_ddl, typed_row_bytes)
from app.storage_migration_worker import MAX_REPLAY_BATCHES, error_category, online_pass, ready_for_barrier


def create_source(path):
    with sqlite3.connect(path) as connection:
        connection.executescript('''
            CREATE TABLE users(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE INDEX users_json_fields ON users(CASE WHEN json_valid(body)
                THEN upper(coalesce(json_extract(body,'$.name'),'')) ELSE '' END);
            CREATE INDEX users_numeric_name ON users(CASE WHEN json_valid(body)
                AND coalesce(json_extract(body,'$.name'),'') NOT GLOB '*[^0-9]*' THEN 1 ELSE 0 END);
            CREATE TABLE children(id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), body BLOB);
            CREATE TABLE cascade_children(id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE CASCADE);
            CREATE TABLE trades(asset TEXT NOT NULL,id TEXT NOT NULL,t INTEGER NOT NULL,body TEXT NOT NULL,
                PRIMARY KEY(asset,id));
            CREATE INDEX trades_time ON trades(asset,t);
            CREATE INDEX trades_scope_time ON trades(substr(asset,1,instr(asset,':')-1),t);
            CREATE TABLE candles(asset TEXT NOT NULL,openTime INTEGER NOT NULL,close REAL,PRIMARY KEY(asset,openTime));
            CREATE TABLE realtime_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event TEXT,body TEXT,at INTEGER);
            CREATE TABLE dashboard_projection(name TEXT PRIMARY KEY,body TEXT NOT NULL);
        ''')
        connection.execute('INSERT INTO users VALUES (?,?)', ('u', '{"unknown":null,"zero":0,"name":"中文"}'))
        connection.execute('INSERT INTO children VALUES (?,?,?)', ('c', 'u', b'\x00\xff'))
        for i in range(8):
            connection.execute('INSERT INTO trades VALUES (?,?,?,?)',
                ('196:token', str(i), 1_790_000_000_000+i, json.dumps({'price': .0000000000000123456789, 'i': i})))
        connection.execute('INSERT INTO candles VALUES (?,?,?)', ('a', 1_790_000_000_000, 1.123456789012345))
        connection.execute('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)', ('price','{"n":9007199254740993}',1_790_000_000_000))
        connection.execute('INSERT INTO dashboard_projection VALUES (?,?)', ('text', '中文🙂'))
        connection.execute('INSERT INTO dashboard_projection VALUES (?,?)', ('blob', b'\x00\xff\x00'))


def test_inventory_includes_every_table_but_internal_journal(tmp_path):
    path = tmp_path/'source.sqlite'; create_source(path)
    before = inspect_source(path); install_journal(before)
    after = inspect_source(path)
    assert before['schemaHash'] == after['schemaHash']
    assert len(after['tables']) == 7


def test_all_mutations_rollback_and_monotonic_counter(tmp_path):
    path = tmp_path/'source.sqlite'; create_source(path); install_journal(inspect_source(path))
    with sqlite3.connect(path) as connection:
        connection.execute('BEGIN')
        connection.execute("UPDATE children SET body=x'1234' WHERE id='c'")
        assert connection.execute(f'SELECT count(*) FROM {INTERNAL_PREFIX}changes').fetchone()[0] == 1
        connection.rollback()
        assert connection.execute(f'SELECT count(*) FROM {INTERNAL_PREFIX}changes').fetchone()[0] == 0
        connection.execute("UPDATE users SET body='changed' WHERE id='u'"); connection.commit()
        first = connection.execute(f'SELECT seq FROM {INTERNAL_PREFIX}changes').fetchone()[0]
        connection.execute(f'DELETE FROM {INTERNAL_PREFIX}changes'); connection.commit()
        connection.execute("DELETE FROM users WHERE id='u'")
        connection.execute("INSERT INTO users VALUES ('u','new')"); connection.commit()
        assert connection.execute(f'SELECT min(seq) FROM {INTERNAL_PREFIX}changes').fetchone()[0] > first


def test_typed_encoding_distinguishes_null_zero_float_blob_and_text():
    values = [None, 0, 0.0, -0.0, b'', '', '🙂', 2**62]
    assert len({typed_row_bytes([v]) for v in values}) == len(values)
    table = {'name':'dashboard_projection','columns':[{'name':'name'},{'name':'body'}]}
    for value in ('', b'', '中文🙂', b'\xff\x00'):
        row = (1,'view',value)
        assert sqlite_row(table, pg_row(table,row)) == row


def test_ast_conversion_preserves_precision_and_literals():
    ddl = target_ddl({'name':'candles','sql':"CREATE TABLE candles(t INTEGER,price REAL,label TEXT DEFAULT 'REAL INTEGER')"})
    assert 'BIGINT' in ddl and 'DOUBLE PRECISION' in ddl
    assert "'REAL INTEGER'" in ddl
    assert 'BYTEA' in target_ddl({'name':'users','sql':'CREATE TABLE users(salt BLOB)'})
    ddl = target_index_ddl("CREATE INDEX t ON trades(substr(asset,1,instr(asset,':')-1),t)")
    assert 'POSITION' in ddl and 'SUBSTRING' in ddl
    assert 'NULLS' not in target_ddl({'name':'t','sql':'CREATE TABLE t(a TEXT,b TEXT,PRIMARY KEY(a,b))'})
    assert "'[^0-9a-f]'" in target_index_ddl("CREATE INDEX h ON users(CASE WHEN id NOT GLOB '*[^0-9a-f]*' THEN 1 ELSE 0 END)")
    assert 'ON DELETE CASCADE' in target_ddl({'name':'child',
        'sql':'CREATE TABLE child(id TEXT PRIMARY KEY,parent TEXT REFERENCES users(id) ON DELETE CASCADE)'})


@pytest.fixture
def migration(tmp_path):
    settings_path = os.environ.get('MIGRATION_TEST_SETTINGS')
    if not settings_path:
        pytest.skip('Real PostgreSQL migration fixture needs MIGRATION_TEST_SETTINGS')
    config = json.loads(Path(settings_path).read_text())
    path = tmp_path/'source.sqlite'; create_source(path)
    schema = 'cliperx_storage_test_' + uuid.uuid4().hex
    instance = ShadowMigration(config, path, schema, batch_rows=2)
    try:
        instance.prepare()
        yield instance, config, path, schema
    finally:
        instance.pg.execute('DROP SCHEMA "'+schema+'" CASCADE')
        instance.close()


def finish_backfill(instance):
    for table in instance.tables:
        list(instance.backfill(table))


def test_real_copy_and_online_ranges_match_full_reconciliation(migration):
    instance, _, _, _ = migration
    finish_backfill(instance)
    list(instance.reconcile_ranges())
    ranged = instance.verify_barrier()
    full = instance.verify()
    assert [(t['table'], t['sourceRows']) for t in ranged['tables']] == [
        (t['table'], t['sourceRows']) for t in full['tables']]
    assert all(t['match'] and t['rangeCount'] > 0 for t in ranged['tables'])


def test_real_ranges_invalidate_replay_updates_and_extend_for_new_rowids(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    list(instance.reconcile_ranges())
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET body='changed' WHERE id='u'")
        source.execute("INSERT INTO users(rowid,id,body) VALUES (-1,'negative','other')")
        source.execute("INSERT INTO trades VALUES ('196:token','new',1790000000000,'last')")
    with pytest.raises(ValueError, match='pending-cdc'):
        instance.verify_barrier()
    while instance.replay():
        pass
    with pytest.raises(ValueError, match='dirty-range|uncovered-range'):
        instance.verify_barrier()
    list(instance.reconcile_ranges())
    assert all(t['match'] for t in instance.verify_barrier()['tables'])
    assert all(t['match'] for t in instance.verify()['tables'])


def test_real_online_ranges_do_not_certify_pending_changes(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    instance.pg.execute("UPDATE _cliperx_migration_ranges SET valid=false WHERE table_name='trades'")
    with sqlite3.connect(path) as source:
        source.execute("DELETE FROM trades WHERE id='0'")
    statuses = list(instance.reconcile_ranges())
    assert any(row['pending'] for row in statuses)
    while instance.replay():
        pass
    list(instance.reconcile_ranges())
    assert all(t['match'] for t in instance.verify_barrier()['tables'])


def test_real_range_validation_failure_does_not_advance_checkpoint(migration):
    instance, _, _, _ = migration
    original = instance._reconcile_range
    def fail(table, low, high, rows=None):
        if table['name'] == 'trades':
            raise ValueError('injected-range-validation')
        return original(table, low, high, rows)
    instance._reconcile_range = fail
    with pytest.raises(ValueError, match='injected'):
        list(instance.backfill('trades'))
    assert instance.pg.execute("SELECT copied_rows FROM _cliperx_migration_state WHERE table_name='trades'").fetchone()[0] == 0
    assert instance.pg.execute('SELECT count(*) FROM trades').fetchone()[0] == 0
    instance._reconcile_range = original
    finish_backfill(instance)
    list(instance.reconcile_ranges())
    assert all(t['match'] for t in instance.verify_barrier()['tables'])


def test_real_full_import_exact_types_indexes_and_millisecond_timestamps(migration):
    instance, _, _, _ = migration
    finish_backfill(instance)
    result = instance.verify()
    assert result['runtimeCutover'] is False
    assert len(result['tables']) == 7 and all(t['match'] for t in result['tables'])
    assert instance.pg.execute('SELECT close FROM candles').fetchone()[0] == 1.123456789012345
    assert instance.pg.execute('SELECT at FROM realtime_events').fetchone()[0] == 1_790_000_000_000


def test_real_resume_and_failure_do_not_advance_checkpoint(migration):
    instance, config, path, schema = migration
    progress = instance.backfill('trades'); next(progress); progress.close()
    state = instance.pg.execute("SELECT last_rowid,copied_rows FROM _cliperx_migration_state WHERE table_name='trades'").fetchone()
    assert state == (2,2)
    original = instance._upsert
    def failed(table, rows):
        original(table, rows)
        raise RuntimeError('injected-after-write')
    instance._upsert = failed
    with pytest.raises(RuntimeError):
        list(instance.backfill('trades'))
    assert instance.pg.execute("SELECT last_rowid,copied_rows FROM _cliperx_migration_state WHERE table_name='trades'").fetchone() == state
    instance._upsert = original
    instance.close()
    resumed = ShadowMigration(config,path,schema,batch_rows=2)
    try:
        resumed.prepare(); finish_backfill(resumed)
        assert all(t['match'] for t in resumed.verify()['tables'])
        assert resumed.pg.execute('SELECT count(*) FROM trades').fetchone()[0] == 8
    finally:
        # Preserve the fixture's usable cleanup handle.
        instance.pg = resumed.pg


def test_real_replay_covers_updates_deletes_replaces_and_foreign_keys(migration):
    instance, _, path, _ = migration; finish_backfill(instance)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET body='changed' WHERE id='u'")
        source.execute("DELETE FROM trades WHERE id='0'")
        source.execute("INSERT OR REPLACE INTO trades VALUES ('196:token','1',1790000000000,'new')")
        source.execute("UPDATE candles SET close=0 WHERE asset='a'")
    while instance.replay():
        pass
    assert all(t['match'] for t in instance.verify()['tables'])
    assert instance.pg.execute('SELECT count(*) FROM children').fetchone()[0] == 1


def test_real_deleted_rowid_reused_for_a_different_business_key(migration):
    instance, _, path, _ = migration; finish_backfill(instance)
    with sqlite3.connect(path) as source:
        source.execute("DELETE FROM children WHERE id='c'")
        source.execute("DELETE FROM users WHERE id='u'")
        source.execute("INSERT INTO users(rowid,id,body) VALUES (1,'replacement','other')")
    while instance.replay():
        pass
    assert all(t['match'] for t in instance.verify()['tables'])
    assert instance.pg.execute('SELECT id FROM users').fetchone()[0] == 'replacement'


def test_real_conditional_ack_keeps_change_arriving_during_pg_write(migration):
    instance, _, path, _ = migration; finish_backfill(instance)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET body='first' WHERE id='u'")
    original = instance._upsert
    once = []
    def race(table, rows):
        original(table, rows)
        if table['name'] == 'users' and not once:
            once.append(True)
            with sqlite3.connect(path) as source:
                source.execute("UPDATE users SET body='second' WHERE id='u'")
    instance._upsert = race
    assert instance.replay() == 1
    with sqlite3.connect(path) as source:
        assert source.execute(f'SELECT count(*) FROM {INTERNAL_PREFIX}changes').fetchone()[0] == 1
    assert instance.replay() == 1
    assert instance.verify()['tables'][0]['match']


def test_real_rejects_pre_backfill_replay_tampering_and_other_owner(migration):
    instance, config, path, schema = migration
    with pytest.raises(ValueError, match='backfill'):
        instance.replay()
    with pytest.raises(RuntimeError, match='already-running'):
        ShadowMigration(config,path,schema)
    finish_backfill(instance)
    instance.pg.execute("UPDATE users SET body='corrupted'")
    with pytest.raises(ValueError, match='reconciliation-failed'):
        instance.verify()


def test_real_negative_zero_rowid_and_source_schema_drift(migration):
    instance, config, path, schema = migration
    # Insertion after high-water capture is represented by CDC even when its
    # rowid lies below the initial backfill lower bound.
    with sqlite3.connect(path) as source:
        source.execute("INSERT INTO users(rowid,id,body) VALUES (-1,'neg','a')")
        source.execute("INSERT INTO users(rowid,id,body) VALUES (0,'zero','b')")
    finish_backfill(instance)
    while instance.replay():
        pass
    assert instance.verify()['tables'][0]['sourceRows'] == 3
    instance.close()
    with sqlite3.connect(path) as source:
        source.execute('ALTER TABLE users ADD COLUMN extra TEXT')
    changed = ShadowMigration(config,path,schema)
    try:
        with pytest.raises(ValueError, match='schema-changed'):
            changed.prepare()
    finally:
        instance.pg = changed.pg


def test_real_disk_guard_leaves_source_and_pg_unchanged(migration):
    instance, _, path, _ = migration
    instance.reserve_bytes = 2**63
    with pytest.raises(RuntimeError, match='disk-reserve'):
        list(instance.backfill('users'))
    assert instance.pg.execute('SELECT count(*) FROM users').fetchone()[0] == 0
    with sqlite3.connect(path) as source:
        assert source.execute('SELECT count(*) FROM users').fetchone()[0] == 1


def test_real_sequence_frontier_survives_deleted_highest_event_and_schema_drift(migration):
    instance, _, path, _ = migration
    with sqlite3.connect(path) as source:
        source.execute("INSERT INTO realtime_events VALUES (100,'temporary','{}',1790000000000)")
        source.execute('DELETE FROM realtime_events WHERE id=100')
    finish_backfill(instance)
    while instance.replay():
        pass
    receipt = instance.verify()
    sequence = next(s for s in receipt['initializedSequences'] if s['table']=='realtime_events' and s['column']=='id')
    assert sequence['sourceFrontier'] == 100
    assert instance.pg.execute("SELECT nextval(pg_get_serial_sequence('realtime_events','id'))").fetchone()[0] == 101
    with sqlite3.connect(path) as source:
        source.execute('CREATE TABLE overlooked(id TEXT PRIMARY KEY)')
    with pytest.raises(ValueError, match='schema-changed'):
        instance.verify()


class RecordingPG:
    """Observe real statement/COPY counts without replacing database behavior."""
    def __init__(self, connection):
        self.connection = connection
        self.statements = []
        self.copies = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement))
        return self.connection.execute(statement, *args, **kwargs)

    def cursor(self, *args, **kwargs):
        recording = self
        cursor = self.connection.cursor(*args, **kwargs)
        class Cursor:
            def __enter__(self):
                cursor.__enter__()
                return self
            def __exit__(self, *args):
                return cursor.__exit__(*args)
            def __getattr__(self, name):
                return getattr(cursor, name)
            def __iter__(self):
                return iter(cursor)
            def copy(self, statement, *args, **kwargs):
                recording.copies.append(str(statement))
                return cursor.copy(statement, *args, **kwargs)
        return Cursor()


def test_real_replay_uses_one_copy_and_delete_per_changed_table(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET body='bulk' WHERE id='u'")
        source.execute("UPDATE trades SET body='bulk' WHERE id IN ('0','1','2')")
        source.execute("DELETE FROM trades WHERE id='3'")
    recording = RecordingPG(instance.pg)
    instance.pg = recording
    assert instance.replay(limit=5000) == 5
    assert len(recording.copies) == 2
    deletes = [sql for sql in recording.statements if sql.startswith('DELETE FROM')]
    assert len(deletes) == 2 and all('USING pg_temp.' in sql for sql in deletes)
    assert len([sql for sql in recording.statements if sql.startswith('INSERT INTO "')]) == 2
    assert instance.pg.execute('SELECT count(*) FROM children').fetchone()[0] == 1
    assert all(t['match'] for t in instance.verify()['tables'])


def test_real_same_primary_key_rowid_move_keeps_parent_and_invalidates_both_ranges(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    list(instance.reconcile_ranges())
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET rowid=8,body='moved' WHERE id='u'")
    assert instance.replay() == 2
    assert instance.pg.execute('SELECT count(*) FROM children').fetchone()[0] == 1
    assert instance.pg.execute('SELECT _cliperx_source_rowid FROM users').fetchone()[0] == 8
    assert instance.pg.execute("SELECT valid FROM _cliperx_migration_ranges WHERE table_name='users'").fetchone()[0] is False
    list(instance.reconcile_ranges())
    assert all(t['match'] for t in instance.verify_barrier()['tables'])


def test_real_rowid_move_at_batch_boundary_is_not_split(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE candles SET close=2 WHERE asset='a'")
        source.execute("UPDATE users SET rowid=8,body='boundary' WHERE id='u'")
    assert instance.replay() == 1
    assert instance.pg.execute('SELECT _cliperx_source_rowid FROM users').fetchone()[0] == 1
    with pytest.raises(ValueError, match='batch-too-small'):
        instance.replay(limit=1)
    assert instance.replay() == 2
    assert instance.pg.execute('SELECT count(*) FROM children').fetchone()[0] == 1
    assert all(t['match'] for t in instance.verify()['tables'])


def test_real_coalesced_parent_moves_fail_closed_before_cascade(migration):
    instance, _, path, _ = migration
    with sqlite3.connect(path) as source:
        source.execute("INSERT INTO cascade_children VALUES ('cascade','u')")
    finish_backfill(instance)
    assert instance.pg.execute("SELECT confdeltype FROM pg_constraint WHERE conrelid='cascade_children'::regclass "
                               "AND contype='f'").fetchone()[0] == 'c'
    while instance.replay():
        pass
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET rowid=8 WHERE id='u'")
        source.execute("UPDATE users SET rowid=9 WHERE id='u'")
    with pytest.raises(ValueError, match='parent-move-crosses-batch'):
        instance.replay()
    assert instance.pg.execute('SELECT count(*) FROM cascade_children').fetchone()[0] == 1
    assert instance.pg.execute('SELECT _cliperx_source_rowid FROM users').fetchone()[0] == 1
    assert instance.has_pending()
    assert instance.replay(limit=5000) == 3
    assert instance.pg.execute('SELECT count(*) FROM cascade_children').fetchone()[0] == 1
    assert all(t['match'] for t in instance.verify()['tables'])


def test_real_pg_replay_failure_rolls_back_data_and_range_invalidation(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    list(instance.reconcile_ranges())
    old_body = instance.pg.execute('SELECT body FROM users').fetchone()[0]
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET body='failed' WHERE id='u'")
    original = instance._upsert
    def fail(table, rows):
        original(table, rows)
        raise ValueError('injected-replay-failure')
    instance._upsert = fail
    with pytest.raises(ValueError, match='injected'):
        instance.replay()
    assert instance.pg.execute('SELECT body FROM users').fetchone()[0] == old_body
    assert instance.pg.execute("SELECT valid FROM _cliperx_migration_ranges WHERE table_name='users'").fetchone()[0] is True
    assert instance.has_pending()
    instance._upsert = original
    assert instance.replay() == 1
    assert all(t['match'] for t in instance.verify()['tables'])


def test_real_ack_crash_after_pg_commit_resumes_idempotently(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET body='committed' WHERE id='u'")
    original = instance._ack_changes
    def crash(changes):
        raise RuntimeError('injected-after-pg-commit')
    instance._ack_changes = crash
    with pytest.raises(RuntimeError, match='injected'):
        instance.replay()
    assert instance.pg.execute('SELECT body FROM users').fetchone()[0] == 'committed'
    assert instance.has_pending()
    assert instance.pg.execute("SELECT valid FROM _cliperx_migration_ranges WHERE table_name='users'").fetchone()[0] is False
    instance._ack_changes = original
    assert instance.replay() == 1
    assert not instance.has_pending()
    assert all(t['match'] for t in instance.verify()['tables'])


def test_real_ack_busy_retries_only_ack_without_repeating_pg_write(migration, monkeypatch):
    instance, _, path, _ = migration
    finish_backfill(instance)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET body='busy' WHERE id='u'")
    original = instance._ack_changes
    calls, delays = [], []
    def busy(changes):
        calls.append(changes)
        if len(calls) < 4:
            raise sqlite3.OperationalError('database is locked')
        return original(changes)
    monkeypatch.setattr(migration_module.time, 'sleep', delays.append)
    instance._ack_changes = busy
    recording = RecordingPG(instance.pg)
    instance.pg = recording
    assert instance.replay() == 1
    assert len(calls) == 4 and delays == [.1, .2, .4]
    assert len(recording.copies) == 1
    assert not instance.has_pending()


def test_real_conditional_ack_keeps_delete_reinsert_aba(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET body='first' WHERE id='u'")
    original = instance._upsert
    once = []
    def race(table, rows):
        original(table, rows)
        if table['name'] == 'users' and not once:
            once.append(True)
            with sqlite3.connect(path) as source:
                source.execute("DELETE FROM users WHERE id='u'")
                source.execute("INSERT INTO users(rowid,id,body) VALUES (1,'u','reinserted')")
    instance._upsert = race
    assert instance.replay() == 1 and instance.has_pending()
    assert instance.replay() == 1 and not instance.has_pending()
    assert instance.pg.execute('SELECT body FROM users').fetchone()[0] == 'reinserted'
    assert instance.pg.execute('SELECT count(*) FROM children').fetchone()[0] == 1
    assert all(t['match'] for t in instance.verify()['tables'])


def test_real_pending_range_skips_source_business_body(migration, monkeypatch):
    instance, _, path, _ = migration
    finish_backfill(instance)
    instance.pg.execute("UPDATE _cliperx_migration_ranges SET valid=false WHERE table_name='users'")
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET body='pending' WHERE id='u'")
    statements = []
    original = migration_module.source_connection
    def traced(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(migration_module, 'source_connection', traced)
    statuses = list(instance.reconcile_ranges())
    assert any(row['table'] == 'users' and row['pending'] for row in statuses)
    assert not any('SELECT rowid,"id","body" FROM "users"' in sql for sql in statements)


def test_sqlite_busy_retry_is_bounded_and_fails_closed(monkeypatch):
    calls, delays = [], []
    monkeypatch.setattr(migration_module.time, 'sleep', delays.append)
    def busy():
        calls.append(True)
        raise sqlite3.OperationalError('database is locked')
    with pytest.raises(sqlite3.OperationalError):
        migration_module.retry_sqlite_busy(busy)
    assert len(calls) == 4 and delays == [.1, .2, .4]
    for code in (sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_SCHEMA, sqlite3.SQLITE_IOERR):
        calls.clear()
        def other():
            calls.append(True)
            exc = sqlite3.OperationalError('private-row-and-dsn')
            exc.sqlite_errorcode = code
            raise exc
        with pytest.raises(sqlite3.OperationalError):
            migration_module.retry_sqlite_busy(other)
        assert len(calls) == 1
    exc = sqlite3.OperationalError('private-row-and-dsn')
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY | (3 << 8)
    assert migration_module.sqlite_busy(exc)
    assert error_category(exc) == 'sqlite-busy-retries-exhausted'
    assert error_category(ValueError('private-row-and-dsn')) == 'migration-validation-failed'


def test_worker_bounded_replay_turn_skips_reconciliation_with_backlog():
    events = []
    class Migration:
        batch_rows = 2000
        def __init__(self, name, pending):
            self.name, self.pending = name, pending
        def replay(self):
            events.append(self.name)
            return self.batch_rows if self.pending else 0
        def has_pending(self):
            return self.pending
        def reconcile_ranges(self, **kwargs):
            assert not self.pending
            yield {'pending': False}
    research = online_pass(Migration('research', True))
    market = online_pass(Migration('market', False))
    assert events == ['research']*MAX_REPLAY_BATCHES + ['market']
    assert research['replayedLastPass'] == 2000*MAX_REPLAY_BATCHES
    assert research['checkedRangesLastPass'] == 0 and research['cdcPending']
    assert market['checkedRangesLastPass'] == 1 and not market['cdcPending']


def test_worker_warms_cold_ranges_at_frontier_despite_continuous_hot_writes():
    class Migration:
        batch_rows = 2000
        def replay(self):
            return 17
        def has_pending(self):
            return True  # Fresh writes arriving after the replay commit.
        def reconcile_ranges(self, *, max_ranges):
            assert max_ranges == 64
            yield {'pending': True}
            yield {'pending': False}
    result = online_pass(Migration())
    assert result['replayBatchesLastPass'] == 1 and result['cdcPending']
    assert result['checkedRangesLastPass'] == 2
    assert result['pendingRangesLastPass'] == 1


def test_real_bounded_reconciliation_round_robins_past_hot_prefix(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    with sqlite3.connect(path) as source:
        for index in range(24):
            source.execute('INSERT INTO users VALUES (?,?)', ('hot'+str(index), 'before'))
    while instance.replay():
        pass
    list(instance.reconcile_ranges())
    instance.pg.execute("UPDATE _cliperx_migration_ranges SET valid=false "
                        "WHERE table_name IN ('users','trades','candles','dashboard_projection')")
    with sqlite3.connect(path) as source:
        source.execute("UPDATE users SET body='continuously-hot'")
    seen = set()
    for _ in range(8):
        statuses = list(instance.reconcile_ranges(max_ranges=2))
        assert len(statuses) <= 2
        seen.update(row['table'] for row in statuses)
        if {'trades','candles','dashboard_projection'} <= seen:
            break
    assert {'users','trades','candles','dashboard_projection'} <= seen
    assert instance.has_pending()  # Hot prefix still pending, cold tables progressed.
    assert instance.pg.execute("SELECT count(*) FROM _cliperx_migration_ranges "
                               "WHERE table_name='candles' AND NOT valid").fetchone()[0] == 0


def test_real_hot_chunk_does_not_block_huge_uncovered_cold_suffix(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    list(instance.reconcile_ranges(max_ranges=None))
    with sqlite3.connect(path) as source:
        for index in range(6):
            source.execute('INSERT INTO trades(rowid,asset,id,t,body) VALUES (?,?,?,?,?)',
                (1_000_000+index, '196:token', 'late'+str(index), index, 'cold'))
    assert instance.replay(limit=5000) == 6
    with sqlite3.connect(path) as source:
        source.execute("UPDATE trades SET body='pending' WHERE id='late0'")
    first = list(instance.reconcile_ranges(max_ranges=1))
    assert first[0]['table'] == 'trades' and first[0]['pending']
    assert first[0]['highRowid'] == 1_000_001
    second = list(instance.reconcile_ranges(max_ranges=1))
    assert second[0]['table'] == 'trades' and not second[0]['pending']
    assert second[0]['lowRowid'] == 1_000_002
    assert second[0]['rows'] <= instance.batch_rows
    assert not instance.range_coverage()['coverageComplete']
    while instance.replay():
        pass
    for _ in range(16):
        list(instance.reconcile_ranges(max_ranges=1))
        if instance.range_coverage()['coverageComplete'] and not instance.range_coverage()['dirtyRanges']:
            break
    assert instance.range_coverage()['coverageComplete']
    assert all(table['match'] for table in instance.verify_barrier()['tables'])


def test_real_coverage_checks_source_target_edges_and_internal_missing_certificates(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    list(instance.reconcile_ranges(max_ranges=None))
    assert instance.range_coverage()['coverageComplete']
    with sqlite3.connect(path) as source:
        source.execute("INSERT INTO users(rowid,id,body) VALUES (99,'edge','source-only')")
    source_only = instance.range_coverage()
    assert source_only['dirtyRanges'] == 0 and not source_only['coverageComplete']
    assert source_only['uncoveredByTable'] == [{'table':'users', 'ranges':1}]
    instance.replay()
    list(instance.reconcile_ranges())
    assert instance.range_coverage()['coverageComplete']
    instance.pg.execute("INSERT INTO users(_cliperx_source_rowid,id,body) VALUES (100,'target-only','extra')")
    assert not instance.range_coverage()['coverageComplete']
    instance.pg.execute("DELETE FROM users WHERE id='target-only'")
    instance.pg.execute("DELETE FROM _cliperx_migration_ranges WHERE table_name='trades' AND low_rowid=3")
    gap = instance.range_coverage()
    assert gap['dirtyRanges'] == 0 and not gap['coverageComplete']
    assert {'table':'trades', 'ranges':1} in gap['uncoveredByTable']
    with pytest.raises(ValueError, match='noncontiguous-range'):
        instance.verify_barrier()
    list(instance.reconcile_ranges())
    assert instance.range_coverage()['coverageComplete']
    assert all(table['match'] for table in instance.verify_barrier()['tables'])


def test_real_unbounded_reconciliation_ignores_online_cursors(migration):
    instance, _, _, _ = migration
    finish_backfill(instance)
    instance.pg.execute('UPDATE _cliperx_migration_ranges SET valid=false')
    instance._range_cursors = {name:2**62 for name in instance.tables}
    instance._range_last_table = 'users'
    statuses = list(instance.reconcile_ranges())
    assert statuses[0]['table'] == 'users'
    assert {row['table'] for row in statuses} == set(instance.tables)
    assert not instance.range_coverage()['dirtyRanges']
    assert all(table['match'] for table in instance.verify_barrier()['tables'])


def test_real_oversized_dirty_certificate_splits_without_borrowing_proof(migration):
    instance, _, path, _ = migration
    finish_backfill(instance)
    list(instance.reconcile_ranges(max_ranges=None))
    with sqlite3.connect(path) as source:
        for index in range(6):
            source.execute('INSERT INTO trades(rowid,asset,id,t,body) VALUES (?,?,?,?,?)',
                (1_000_000+index, '196:token', 'late'+str(index), index, 'more'))
    instance.replay(limit=5000)
    instance.pg.execute("DELETE FROM _cliperx_migration_ranges WHERE table_name='trades'")
    instance.pg.execute("INSERT INTO _cliperx_migration_ranges "
                        "VALUES ('trades',1,1000005,0,%s,false,0)", ('0'*64,))
    first = list(instance.reconcile_ranges(max_ranges=1))
    assert first[0]['table'] == 'trades' and first[0]['rows'] == 2
    pieces = instance.pg.execute("SELECT low_rowid,high_rowid,valid,content_sha256 "
                                "FROM _cliperx_migration_ranges WHERE table_name='trades' ORDER BY low_rowid").fetchall()
    assert pieces[0][:3] == (1,2,True)
    assert pieces[1][:3] == (3,1000005,False) and pieces[1][3] == '0'*64
    with pytest.raises(ValueError, match='dirty-range'):
        instance.verify_barrier()
    rest = list(instance.reconcile_ranges())
    assert all(row.get('rows', 0) <= instance.batch_rows for row in rest)
    assert all(table['match'] for table in instance.verify_barrier()['tables'])


def test_worker_never_calls_dirty_zero_uncovered_domain_ready():
    progress = {'cdcPending':False, 'pendingRangesLastPass':0}
    coverage = {'coverageComplete':False, 'dirtyRanges':0}
    assert not ready_for_barrier(progress, coverage, 0)
    coverage['coverageComplete'] = True
    assert ready_for_barrier(progress, coverage, 0)
    assert not ready_for_barrier(progress, coverage, 1)
