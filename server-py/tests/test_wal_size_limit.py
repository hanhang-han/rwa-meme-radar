import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.db import ResearchStore, WAL_JOURNAL_SIZE_LIMIT_BYTES


class WalSizeLimitTest(unittest.TestCase):
    def test_persistent_writer_limits_reusable_wal_without_changing_reads_or_writes(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / 'research.sqlite')
                with closing(sqlite3.connect(path)) as initial:
                    original = initial.execute('PRAGMA journal_size_limit').fetchone()[0]
                self.assertEqual(original, -1)

                store = await ResearchStore(path, '196').connect()
                try:
                    configured = (await store.fetchone('PRAGMA journal_size_limit'))[0]
                    self.assertEqual(configured, WAL_JOURNAL_SIZE_LIMIT_BYTES)
                    await store.put('health', 'probe', {'ok': True})
                    self.assertEqual(await store.get('health', 'probe'), {'ok': True})
                    with closing(sqlite3.connect(path)) as reader:
                        self.assertEqual(reader.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                        self.assertEqual(reader.execute('PRAGMA journal_size_limit').fetchone()[0], original)
                        self.assertIsNotNone(reader.execute(
                            'SELECT body FROM facts WHERE kind=? AND id=?', ('196:health', 'probe')
                        ).fetchone())
                finally:
                    await store.close()

        asyncio.run(run())
