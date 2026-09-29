import asyncio
import os
import sqlite3
import tempfile
import unittest

from app.db import ResearchStore
from app.demand_leases import publish_lease, stop_lease_writer


class DemandLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_writer_does_not_block_reads_and_latest_lease_is_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            path=os.path.join(folder,'research.sqlite')
            store=await ResearchStore(path,'196').connect()
            blocker=sqlite3.connect(path)
            try:
                await store.put('asset','token',{'price':3})
                blocker.execute('BEGIN IMMEDIATE')
                publish_lease(store,'watch','token',{'expiresAt':1})
                publish_lease(store,'watch','token',{'expiresAt':2})
                await asyncio.sleep(.4)  # background writer hits its short busy timeout
                row=await asyncio.wait_for(store.get('asset','token'),.2)
                self.assertEqual(row['price'],3)
                blocker.rollback()
                for _ in range(20):
                    lease=await store.get('watch','token')
                    if lease:break
                    await asyncio.sleep(.1)
                self.assertEqual(lease,{'expiresAt':2})
            finally:
                blocker.close()
                await stop_lease_writer()
                await store.close()
