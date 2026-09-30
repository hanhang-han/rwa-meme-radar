import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch

from app import db as storage


def sqlite_error(code, text='database is locked'):
    error = sqlite3.OperationalError(text)
    error.sqlite_errorcode = code
    return error


class FakeConnection:
    def __init__(self, open_error=None, close_error=None):
        self.open_error = open_error
        self.rollback = AsyncMock()
        self.close = AsyncMock(side_effect=close_error)

    def __await__(self):
        async def open_connection():
            if self.open_error:
                raise self.open_error
            return self
        return open_connection().__await__()


class StartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rollback_writer_releases_and_connect_enables_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/research.sqlite'
            with closing(sqlite3.connect(path)) as writer:
                writer.execute('CREATE TABLE existing (value INTEGER)')
                writer.commit()
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('INSERT INTO existing VALUES (1)')
                with closing(sqlite3.connect(path, timeout=0)) as probe:
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        probe.execute('PRAGMA journal_mode=WAL')
                    self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)

                scoped = storage.ResearchStore(path)
                original_fetch = scoped.fetchone
                changing_mode = asyncio.Event()

                async def fetch(query, parameters=()):
                    if query == 'PRAGMA journal_mode=WAL':
                        changing_mode.set()
                    return await original_fetch(query, parameters)

                async def release_writer():
                    await changing_mode.wait()
                    await asyncio.sleep(0.1)
                    writer.commit()

                release = asyncio.create_task(release_writer())
                try:
                    with patch.object(scoped, 'fetchone', side_effect=fetch):
                        self.assertIs(await asyncio.wait_for(scoped.connect(), 5), scoped)
                    self.assertTrue(changing_mode.is_set())
                    self.assertEqual((await scoped.fetchone('PRAGMA journal_mode'))[0], 'wal')
                    self.assertEqual((await scoped.fetchone('SELECT value FROM existing'))[0], 1)
                    await scoped.put('asset', 'ready', {'ok': True})
                    self.assertEqual(await scoped.get('asset', 'ready'), {'ok': True})
                finally:
                    release.cancel()
                    try:
                        await release
                    except asyncio.CancelledError:
                        pass
                    await scoped.close()
                    if writer.in_transaction:
                        writer.rollback()

    async def test_existing_wal_connect_skips_journal_mode_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/research.sqlite'
            with closing(sqlite3.connect(path)) as writer:
                self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                writer.execute('CREATE TABLE existing (value INTEGER)')
                writer.commit()
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('INSERT INTO existing VALUES (2)')

                scoped = storage.ResearchStore(path)
                original_fetch = scoped.fetchone
                queries = []

                async def fetch(query, parameters=()):
                    queries.append(query)
                    return await original_fetch(query, parameters)

                async def release_writer():
                    await asyncio.sleep(0.1)
                    writer.commit()

                release = asyncio.create_task(release_writer())
                try:
                    with patch.object(scoped, 'fetchone', side_effect=fetch):
                        self.assertIs(await asyncio.wait_for(scoped.connect(), 5), scoped)
                    self.assertTrue(queries[0].startswith('PRAGMA busy_timeout='))
                    self.assertEqual(queries[1], 'PRAGMA journal_mode')
                    self.assertNotIn('PRAGMA journal_mode=WAL', queries)
                    self.assertEqual((await scoped.fetchone('SELECT value FROM existing'))[0], 2)
                finally:
                    release.cancel()
                    try:
                        await release
                    except asyncio.CancelledError:
                        pass
                    await scoped.close()
                    if writer.in_transaction:
                        writer.rollback()

    async def test_recovery_then_busy_reopens_and_preserves_idempotent_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            scoped = storage.ResearchStore(directory+'/research.sqlite', busy_timeout_ms=2000)
            original_connect = storage.aiosqlite.connect
            original_fetch = scoped.fetchone
            connections = []
            failures = [sqlite_error(261), sqlite_error(5)]
            def connect(*args, **kwargs):
                connection = original_connect(*args, **kwargs)
                connections.append(connection)
                return connection
            async def fetch(query, parameters=()):
                # Fail after additive DDL, which must remain safe to repeat.
                if query.startswith('SELECT value FROM realtime_schema_version') and failures:
                    raise failures.pop(0)
                return await original_fetch(query, parameters)
            async def backoff(_):
                self.assertFalse(scoped._write_lock.locked())
                self.assertIsNone(scoped.db)
                self.assertIsNone(connections[-1]._connection)
            try:
                with patch.object(storage.aiosqlite, 'connect', side_effect=connect), \
                     patch.object(scoped, 'fetchone', side_effect=fetch), \
                     patch.object(storage.asyncio, 'sleep', side_effect=backoff) as sleep:
                    self.assertIs(await scoped.connect(), scoped)
                self.assertEqual(sleep.await_count, 2)
                self.assertEqual(len(connections), 3)
                self.assertIsNone(connections[0]._connection)
                self.assertIsNone(connections[1]._connection)
                self.assertEqual((await scoped.fetchone('PRAGMA busy_timeout'))[0], 2000)
                await scoped.put('asset', 'test', {'value': 1})
                self.assertEqual(await scoped.get('asset', 'test'), {'value': 1})
            finally:
                await scoped.close()

    async def test_elapsed_budget_does_not_retry_forever(self):
        scoped = storage.ResearchStore(':memory:')
        with patch.object(storage, 'STARTUP_RETRY_SECONDS', 0), \
             patch.object(scoped, 'fetchone', AsyncMock(side_effect=sqlite_error(261))), \
             patch.object(storage.asyncio, 'sleep', AsyncMock()) as sleep:
            with self.assertRaises(sqlite3.OperationalError):
                await scoped.connect()
        self.assertIsNone(scoped.db)
        sleep.assert_not_awaited()

    async def test_attempt_cap_applies_when_clock_does_not_advance(self):
        scoped = storage.ResearchStore(':memory:')
        with patch.object(storage, 'STARTUP_MAX_ATTEMPTS', 3), \
             patch.object(scoped, 'fetchone', AsyncMock(side_effect=sqlite_error(773))) as fetch, \
             patch.object(storage.asyncio, 'sleep', AsyncMock()) as sleep:
            with self.assertRaises(sqlite3.OperationalError):
                await scoped.connect()
        self.assertEqual(fetch.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertIsNone(scoped.db)

    async def test_nontransient_snapshot_and_locked_errors_fail_without_retry(self):
        for code in (sqlite3.SQLITE_ERROR, sqlite3.SQLITE_LOCKED, 517):
            with self.subTest(code=code):
                scoped = storage.ResearchStore(':memory:')
                with patch.object(scoped, 'fetchone', AsyncMock(side_effect=sqlite_error(code))), \
                     patch.object(storage.asyncio, 'sleep', AsyncMock()) as sleep:
                    with self.assertRaises(sqlite3.OperationalError):
                        await scoped.connect()
                self.assertIsNone(scoped.db)
                sleep.assert_not_awaited()

    async def test_cancelled_startup_closes_handle_and_is_not_retried(self):
        scoped = storage.ResearchStore(':memory:')
        connection = FakeConnection()
        with patch.object(storage.aiosqlite, 'connect', return_value=connection), \
             patch.object(scoped, 'fetchone', AsyncMock(side_effect=asyncio.CancelledError)), \
             patch.object(storage.asyncio, 'sleep', AsyncMock()) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await scoped.connect()
        connection.close.assert_awaited_once()
        self.assertIsNone(scoped.db)
        sleep.assert_not_awaited()

    async def test_failed_open_closes_handle_and_failed_close_still_clears_reference(self):
        for close_error in (None, RuntimeError('close failed')):
            with self.subTest(close_error=close_error):
                scoped = storage.ResearchStore(':memory:')
                connection = FakeConnection(open_error=sqlite_error(sqlite3.SQLITE_CANTOPEN), close_error=close_error)
                with patch.object(storage.aiosqlite, 'connect', return_value=connection):
                    with self.assertRaises(RuntimeError if close_error else sqlite3.OperationalError):
                        await scoped.connect()
                connection.close.assert_awaited_once()
                self.assertIsNone(scoped.db)

    def test_live_write_retry_policy_is_not_broadened(self):
        self.assertTrue(storage._is_transient_write_busy(sqlite_error(5)))
        for code in (261, 773, 517, sqlite3.SQLITE_LOCKED):
            self.assertFalse(storage._is_transient_write_busy(sqlite_error(code)))
        self.assertTrue(storage._is_startup_busy(sqlite_error(261)))
        self.assertTrue(storage._is_startup_busy(sqlite_error(773)))


if __name__ == '__main__':
    unittest.main()
