import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.db import ResearchStore
from app.demand_leases import (all_lease_kv, lease_path, publish_lease,
                               flush_lease_writer, stop_lease_writer)


class DemandLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_database_writer_cannot_block_sidecar_lease(self):
        with tempfile.TemporaryDirectory() as folder:
            path=os.path.join(folder,'research.sqlite')
            store=await ResearchStore(path,'196').connect()
            blocker=sqlite3.connect(path)
            try:
                await store.put('asset','token',{'price':3})
                blocker.execute('BEGIN IMMEDIATE')
                expires=int(time.time()*1000)+90_000
                publish_lease(store,'watch','token',{'expiresAt':expires-1})
                publish_lease(store,'watch','token',{'expiresAt':expires})
                await asyncio.wait_for(flush_lease_writer(),2)
                row=await asyncio.wait_for(store.get('asset','token'),.2)
                self.assertEqual(row['price'],3)
                self.assertIsNone(await store.get('watch','token'))
                self.assertEqual(dict(await all_lease_kv(store,'watch'))['token'],
                                 {'expiresAt':expires})
                self.assertTrue(os.path.isfile(lease_path(store)))
                with sqlite3.connect(lease_path(store)) as sidecar:
                    self.assertEqual(sidecar.execute('PRAGMA journal_mode').fetchone()[0],
                                     'wal')
                    self.assertEqual(sidecar.execute(
                        "SELECT count(*) FROM leases WHERE kind='196:watch'").fetchone()[0],1)
            finally:
                blocker.rollback()
                blocker.close()
                await stop_lease_writer()
                await store.close()

    async def test_legacy_overlap_prefers_newer_lease_and_ends_after_ttl(self):
        with tempfile.TemporaryDirectory() as folder:
            store=await ResearchStore(os.path.join(folder,'research.sqlite'),'56').connect()
            now=int(time.time()*1000)
            try:
                await store.put('watch','shared',{'expiresAt':now+30_000,'token':'old'})
                await store.put('watch','legacy',{'expiresAt':now+30_000,'token':'legacy'})
                publish_lease(store,'watch','shared',{'expiresAt':now+90_000,'token':'new'})
                await flush_lease_writer()
                leases=dict(await all_lease_kv(store,'watch'))
                self.assertEqual(leases['shared']['token'],'new')
                self.assertEqual(leases['legacy']['token'],'legacy')
                with sqlite3.connect(lease_path(store)) as sidecar:
                    sidecar.execute("UPDATE lease_meta SET value=? WHERE name='createdAt'",
                                    (now-180_000,))
                leases=dict(await all_lease_kv(store,'watch'))
                self.assertEqual(leases['shared']['token'],'new')
                self.assertNotIn('legacy',leases)
                script = """import asyncio, json, sys
from app.db import ResearchStore
from app.demand_leases import all_lease_kv
async def main():
    store = ResearchStore(sys.argv[1], '56')
    print(json.dumps(dict(await all_lease_kv(store, 'watch'))))
asyncio.run(main())
"""
                env = {**os.environ,
                       'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
                output = subprocess.check_output(
                    [sys.executable, '-c', script, store.path], cwd=folder,
                    env=env, text=True, timeout=10)
                self.assertEqual(json.loads(output)['shared']['token'],'new')
            finally:
                await stop_lease_writer()
                await store.close()

    async def test_memory_research_database_does_not_create_file_in_cwd(self):
        store = ResearchStore(':memory:')
        path = lease_path(store)
        self.assertTrue(os.path.isabs(path))
        self.assertTrue(path.startswith(tempfile.gettempdir()))
        self.assertEqual(lease_path(store),path)
