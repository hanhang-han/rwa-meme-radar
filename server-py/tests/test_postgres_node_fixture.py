"""Fixture connection capacity must not consume the concurrency under test."""
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from postgres_node_fixture import prepare_node_pg_fixture


def fixture_connections(*, fail_verification=False):
    state = SimpleNamespace(active_migrations=0, peak_migrations=0, active_admin=0,
                            peak_admin=0, dropped=[], closed_migrations=0)

    class Session:
        def __init__(self, kind):
            self.kind = kind
            self.closed = False
            if kind == 'migration':
                state.active_migrations += 1
                state.peak_migrations = max(state.peak_migrations, state.active_migrations)
            else:
                state.active_admin += 1
                state.peak_admin = max(state.peak_admin, state.active_admin)

        def execute(self, statement, *args):
            assert not self.closed, 'fixture reused a closed migration connection'
            if statement.startswith('DROP SCHEMA'):
                state.dropped.append(statement)
            return SimpleNamespace(fetchone=lambda: (None,))

        def close(self):
            if not self.closed:
                self.closed = True
                if self.kind == 'migration':
                    state.active_migrations -= 1
                    state.closed_migrations += 1
                else:
                    state.active_admin -= 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    class Migration:
        def __init__(self, config, path, schema, **kwargs):
            self.schema = schema
            self.tables = {'facts': {}}
            self.pg = Session('migration')

        def prepare(self):
            pass

        def backfill(self, table):
            return iter(())

        def verify(self):
            return {'tables': [{'match': not fail_verification}]}

        def close(self):
            self.pg.close()

    return state, Migration, lambda *args, **kwargs: Session('admin')


def test_fixture_releases_all_shadow_sessions_before_eight_thread_quota_race():
    state, migration, connect = fixture_connections()
    with tempfile.TemporaryDirectory() as directory, \
         patch('psycopg.connect', connect), \
         patch('postgres_node_fixture.ShadowMigration', migration), \
         patch('postgres_node_fixture.runtime_support_sql', return_value=''), \
         patch('postgres_node_fixture.postgres_runtime_schema_sql', return_value=''), \
         patch('postgres_node_fixture.install_account_auxiliary_schema',
               return_value={'installedTables': ['telegram_bindings', 'telegram_bind_codes', 'telegram_runtime',
                                                'telegram_asset_state', 'telegram_outbox', 'telegram_daily_budget']}):
        with prepare_node_pg_fixture({'dsn': 'postgresql://test@localhost/cliperx_node_test_capacity'}, directory):
            assert state.active_migrations == 0
            assert state.active_admin == 0
            assert state.closed_migrations == 6
        assert state.peak_migrations == 1
        assert state.peak_admin == 1
        assert len(state.dropped) == 6
        assert state.active_migrations == state.active_admin == 0


def test_fixture_closes_failed_setup_connection_and_uses_one_new_cleanup_session():
    state, migration, connect = fixture_connections(fail_verification=True)
    with tempfile.TemporaryDirectory() as directory, \
         patch('psycopg.connect', connect), \
         patch('postgres_node_fixture.ShadowMigration', migration):
        with pytest.raises(ValueError, match='history-verification-failed'):
            with prepare_node_pg_fixture({'dsn': 'postgresql://test@localhost/cliperx_node_test_failure'}, directory):
                raise AssertionError('failed verification must not yield the fixture')
        assert state.active_migrations == state.active_admin == 0
        assert state.closed_migrations == 1
        assert state.peak_admin == 1
        assert len(state.dropped) == 1
