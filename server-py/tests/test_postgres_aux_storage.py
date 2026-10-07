"""Real, disposable PostgreSQL parity and concurrency tests for auxiliaries."""
import asyncio
import json
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import developer_access, telegram_alerts, user_features
from app.aux_storage_schema import install_account_auxiliary_schema
from app.db import ResearchStore, WriterLock
from app.demand_leases import all_lease_kv, flush_lease_writer, publish_lease, stop_lease_writer
from app.request_ledger import shared_usage
from app.storage_runtime import sync_connect
from postgres_node_fixture import prepare_node_pg_fixture


@pytest.fixture(scope='module')
def auxiliary_pg(tmp_path_factory):
    settings = os.environ.get('NODE_STORAGE_TEST_SETTINGS')
    if not settings:
        pytest.skip('NODE_STORAGE_TEST_SETTINGS must name a private disposable test database config')
    config = json.loads(Path(settings).read_text())
    with prepare_node_pg_fixture(config, tmp_path_factory.mktemp('node-pg')) as fixture:
        yield fixture


@pytest.fixture
def active_auxiliary_pg(auxiliary_pg, monkeypatch):
    for key in ('ARCHITECTURE_SETTINGS', 'RESEARCH_DB', 'LIVE_MARKET_DB',
                'DEVELOPER_DB_PATH', 'OKX_LEDGER_PATH', 'DEMAND_LEASE_DB'):
        monkeypatch.setenv(key, auxiliary_pg['env'][key])
    yield auxiliary_pg


def test_accounts_sessions_watches_api_keys_and_new_telegram_tables(active_auxiliary_pg, monkeypatch):
    fixture = active_auxiliary_pg
    import psycopg
    config = json.loads(fixture['runtimePath'].read_text())
    with psycopg.connect(config['dsn'], autocommit=True) as connection:
        result = install_account_auxiliary_schema(connection)
        assert result['installedTables'] == []
        assert result['runtimeTables'] == 23
    monkeypatch.setenv('DEVELOPER_SHARED_INVITE_CODE', 'pg-test-invitation')
    email = 'pg-test-' + uuid.uuid4().hex + '@example.com'
    user, token, csrf = developer_access.register(email, 'pg-test-invitation', 'a-strong-test-password', 'pg-test-peer')
    assert developer_access.session(token)[0] == user
    assert isinstance(csrf, str)
    assert user_features.change_watches(user['id'], ['stock:NVDA'], []) == ['stock:NVDA']
    assert user_features.list_watches(user['id']) == ['stock:NVDA']
    key, secret = developer_access.create_key(user['id'], 'Postgres test')
    assert developer_access.authenticate_and_consume(secret)[0] == user['id']
    assert developer_access.usage(user['id'])['used'] == 1
    assert developer_access.revoke_key(user['id'], key['id'])
    with pytest.raises(developer_access.AccessError):
        developer_access.authenticate_and_consume(secret)
    telegram_alerts.init()
    # No bot API is called by this test. Capability rendering must be able to
    # query the explicitly installed empty binding/runtime tables.
    status = telegram_alerts.status(user['id'])
    assert status['linked'] is False
    with developer_access.connection() as connection:
        assert connection.backend == 'postgres'
        assert connection.execute('SELECT email FROM users WHERE id=?', (user['id'],)).fetchone()['email'] == email


def test_node_adapter_and_provider_quota_integration(active_auxiliary_pg):
    result = subprocess.run(['node', '--import', 'tsx', '--test', 'tests/postgres-storage.integration.test.ts'],
                            env=active_auxiliary_pg['env'], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '# fail 0' in result.stdout
    assert '# skipped 0' in result.stdout


def test_python_and_node_share_one_atomic_request_budget(active_auxiliary_pg):
    day = 'cross-language-' + uuid.uuid4().hex
    # Each participant proves a successful charge before the race begins.
    # Catch only genuine quota exhaustion: connection/schema/SQL failures
    # must fail the subprocess instead of reporting a misleading zero.
    js = """import {sharedUsage} from './src/lib/request-ledger.ts';
let n=0;
await sharedUsage(process.argv[1],30);n++;
process.stdout.write('ready\\n');
await new Promise(resolve=>process.stdin.once('data',resolve));
for(let i=0;i<60;i++){
  try{await sharedUsage(process.argv[1],30);n++;}
  catch(error){if(error?.message!=='OKX local request budget exhausted')throw error;}
}
console.log(n);
"""
    child = subprocess.Popen(['node', '--import', 'tsx', '--input-type=module', '-e', js, day],
                             env=active_auxiliary_pg['env'], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def charge(_):
        try:
            shared_usage(day, 30)
            return 1
        except RuntimeError as error:
            if str(error) != 'OKX local request budget exhausted':
                raise
            return 0
    try:
        marker = child.stdout.readline()
        if marker != 'ready\n':
            out, err = child.communicate(timeout=60)
            pytest.fail('Node could not establish an initial PostgreSQL quota charge: ' + out + err)
        assert shared_usage(day, 30) == 2
        child.stdin.write('start\n')
        child.stdin.flush()
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = 1 + sum(pool.map(charge, range(60)))
        out, err = child.communicate(timeout=60)
        assert child.returncode == 0, err
        node_accepted = int(out.strip())
        assert node_accepted >= 1
        assert accepted >= 1
        assert accepted + node_accepted == 30
        assert shared_usage(day) == 30
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_page_demand_commits_while_research_writer_is_reserved(active_auxiliary_pg):
    path = str(active_auxiliary_pg['paths']['research'])
    async def check():
        store = await ResearchStore(path, '196', write_lock=WriterLock()).connect()
        blocker = sync_connect(path)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            expires = int(time.time() * 1000) + 90_000
            publish_lease(store, 'watch', 'pg-page-demand', {'expiresAt': expires})
            await asyncio.wait_for(flush_lease_writer(), 5)
            assert dict(await all_lease_kv(store, 'watch'))['pg-page-demand'] == {'expiresAt': expires}
        finally:
            blocker.rollback()
            blocker.close()
            await stop_lease_writer()
            await store.close()
    asyncio.run(check())
