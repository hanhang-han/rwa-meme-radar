"""Live market commits must survive an unrelated physical catalogue writer."""
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from app import db as research_db
from app import live_market_store as live
from app.demand_leases import lease_path
from app.realtime_schema import enqueue_events


class LiveMarketStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_path = self.temp.name + '/research.sqlite'
        self.market_path = self.temp.name + '/live.sqlite'
        self.original_setting = patch.object(research_db, 'DB_PATH', self.original_path)
        self.original_setting.start()
        self.environment = patch.dict(os.environ, {'LIVE_MARKET_DB': self.market_path})
        self.environment.start()
        self.prior_lease = os.environ.pop('DEMAND_LEASE_DB', None)
        await live.close_all()
        self.original = await research_db.ResearchStore(self.original_path).connect()
        await self.original.put('asset', 'token', {'token': 'token', 'price': 1})

    async def asyncTearDown(self):
        await live.close_all()
        await self.original.close()
        os.environ.pop('DEMAND_LEASE_DB', None)
        if self.prior_lease is not None:
            os.environ['DEMAND_LEASE_DB'] = self.prior_lease
        self.environment.stop()
        self.original_setting.stop()
        self.temp.cleanup()

    async def test_physical_catalogue_writer_does_not_block_live_transaction(self):
        # Hold the old file's actual SQLite reservation, not only a Python lock.
        external = sqlite3.connect(self.original_path)
        external.execute('BEGIN IMMEDIATE')
        external.execute('UPDATE facts SET body=? WHERE kind=? AND id=?',
                         ('{"price":999}', '196:asset', 'token'))
        try:
            async with self.original._write_lock:
                scoped = await asyncio.wait_for(live.market_store(), timeout=2)
                self.assertNotEqual(scoped.path, self.original.path)
                self.assertIsNot(scoped._write_lock, self.original._write_lock)

                async def commit():
                    async with scoped._guard_write():
                        await scoped.db.execute('BEGIN IMMEDIATE')
                        await scoped.db.execute('INSERT INTO trades VALUES (?,?,?,?)',
                                                ('196:dex:pool', 'trade:1', 1000, '{"price":2}'))
                        await scoped.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)',
                                                ('196:dex:pool', '1m', 0, 2, 2, 2, 2, 1, 2, 0))
                        await enqueue_events(scoped.db, [('candle', {'price': 2})])
                        await scoped.db.commit()

                await asyncio.wait_for(commit(), timeout=1)
                self.assertTrue(external.in_transaction)
                self.assertEqual((await scoped.fetchone('SELECT COUNT(*) FROM trades'))[0], 1)
                self.assertEqual((await scoped.fetchone('SELECT COUNT(*) FROM realtime_events'))[0], 1)
                catalogue = await asyncio.wait_for(live.catalogue_store(), timeout=1)
                self.assertEqual((await catalogue.get('asset', 'token'))['price'], 1)
        finally:
            external.rollback()
            external.close()

    async def test_trade_candle_and_outbox_roll_back_together(self):
        scoped = await live.market_store()
        with self.assertRaisesRegex(RuntimeError, 'before-commit'):
            async with scoped._guard_write():
                await scoped.db.execute('BEGIN IMMEDIATE')
                await scoped.db.execute('INSERT INTO trades VALUES (?,?,?,?)',
                                        ('196:dex:pool', 'trade:1', 1000, '{}'))
                await scoped.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)',
                                        ('196:dex:pool', '1m', 0, 2, 2, 2, 2, 1, 2, 0))
                await enqueue_events(scoped.db, [('trade', {'id': 'trade:1'})])
                raise RuntimeError('before-commit')
        for table in ('trades', 'candles', 'realtime_events', 'change_outbox'):
            self.assertEqual((await scoped.fetchone('SELECT COUNT(*) FROM ' + table))[0], 0)
        self.assertFalse(scoped.db.in_transaction)

    async def test_factory_single_flight_and_per_file_writer_lock(self):
        stores = await asyncio.gather(*(live.market_store('196') for _ in range(20)))
        self.assertTrue(all(scoped is stores[0] for scoped in stores))
        other_chain = await live.market_store('56')
        self.assertIsNot(other_chain, stores[0])
        self.assertIs(other_chain._write_lock, stores[0]._write_lock)
        await stores[0].put('market-registry', 'pool', {'chain': '196'})
        self.assertIsNone(await other_chain.get('market-registry', 'pool'))

    async def test_slow_catalogue_initialization_cannot_hold_market_factory(self):
        cached = await live.market_store('196')
        started, release = asyncio.Event(), asyncio.Event()
        connect = live.CatalogueReadStore.connect

        async def slow_catalogue(scoped):
            started.set()
            await release.wait()
            return await connect(scoped)

        with patch.object(live.CatalogueReadStore, 'connect', slow_catalogue):
            reader = asyncio.create_task(live.catalogue_store('196'))
            try:
                await asyncio.wait_for(started.wait(), 1)
                self.assertIs(await asyncio.wait_for(live.market_store('196'), .5), cached)
                cold = await asyncio.wait_for(live.market_store('56'), .5)
                await asyncio.wait_for(cold.put('market-candle', 'probe', {'price': 2}), .5)
                self.assertFalse(reader.done())
            finally:
                release.set()
                await reader

    async def test_existing_market_handle_does_not_wait_for_other_chain_initialization(self):
        cached = await live.market_store('196')
        async with live._market_init_lock:
            self.assertIs(await asyncio.wait_for(live.market_store('196'), .5), cached)

    async def test_live_mutations_do_not_grow_unused_projection_outbox(self):
        scoped = await live.market_store()
        await scoped.put('market-registry', 'pool', {'chain': '196'})
        await scoped.put_trades('dex:pool', [{'id': 'one', 't': 1000, 'price': 2}])
        await scoped.put_candles('dex:pool', '1m', [
            {'t': 0, 'o': 2, 'h': 2, 'l': 2, 'c': 2, 'v': 1}])
        self.assertEqual((await scoped.fetchone('SELECT COUNT(*) FROM change_outbox'))[0], 0)
        self.assertGreater((await self.original.fetchone('SELECT COUNT(*) FROM change_outbox'))[0], 0)
        self.assertEqual((await scoped.fetchone(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'realtime_%'"))[0], 0)

    async def test_catalogue_does_not_initialize_or_modify_schema(self):
        minimal = self.temp.name + '/minimal.sqlite'
        with sqlite3.connect(minimal) as connection:
            connection.execute('CREATE TABLE facts(kind TEXT,id TEXT,body TEXT)')
            connection.execute('INSERT INTO facts VALUES (?,?,?)',
                               ('196:asset', 'one', json.dumps({'token': 'one'})))
        with patch.object(research_db, 'DB_PATH', minimal):
            catalogue = await live.catalogue_store()
            self.assertEqual(await catalogue.get('asset', 'one'), {'token': 'one'})
            self.assertEqual((await catalogue.fetchone('PRAGMA query_only'))[0], 1)
            tables = await catalogue.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
            self.assertEqual([row[0] for row in tables], ['facts'])
            with self.assertRaises(PermissionError):
                await catalogue.put('asset', 'one', {'token': 'changed'})
            with self.assertRaises(aiosqlite.OperationalError):
                await catalogue.db.execute("UPDATE facts SET body='{}'")

    async def test_missing_catalogue_is_not_created(self):
        missing = self.temp.name + '/missing/research.sqlite'
        with patch.object(research_db, 'DB_PATH', missing):
            with self.assertRaises(aiosqlite.OperationalError):
                await live.catalogue_store()
        self.assertFalse(Path(missing).exists())
        self.assertFalse(Path(missing).parent.exists())

    async def test_live_path_rejects_research_file_symlink_and_hardlink(self):
        aliases = [self.original_path, self.temp.name + '/symlink.sqlite',
                   self.temp.name + '/hardlink.sqlite']
        Path(aliases[1]).symlink_to(self.original_path)
        os.link(self.original_path, aliases[2])
        for path in aliases:
            with self.subTest(path=path), patch.dict(os.environ, {'LIVE_MARKET_DB': path}):
                with self.assertRaisesRegex(ValueError, 'physically separate'):
                    await live.market_store()

    async def test_close_reopens_and_different_paths_do_not_leak(self):
        first = await live.market_store()
        await first.put('market-registry', 'pool', {'chain': '196'})
        await live.close_all()
        self.assertIsNone(first.db)
        reopened = await live.market_store()
        self.assertIsNot(first, reopened)
        self.assertEqual(await reopened.get('market-registry', 'pool'), {'chain': '196'})
        await reopened.close()
        directly_reopened = await live.market_store()
        self.assertIsNot(reopened, directly_reopened)
        separate_path = self.temp.name + '/other.sqlite'
        with patch.dict(os.environ, {'LIVE_MARKET_DB': separate_path}):
            separate = await live.market_store()
            self.assertIsNot(separate, directly_reopened)
            self.assertIsNot(separate._write_lock, directly_reopened._write_lock)
            self.assertIsNone(await separate.get('market-registry', 'pool'))

    async def test_live_and_catalogue_demand_share_original_sidecar(self):
        path = live.configure_demand_leases()
        market, catalogue = await live.market_store(), await live.catalogue_store()
        self.assertEqual(path, str(Path(self.original_path).resolve()) + '.leases.sqlite')
        self.assertEqual(lease_path(market), lease_path(catalogue))
        self.assertEqual(lease_path(market), path)
        self.assertFalse(Path(path).exists())
        custom = self.temp.name + '/demand.sqlite'
        with patch.dict(os.environ, {'DEMAND_LEASE_DB': custom}):
            expected = str(Path(custom).resolve())
            self.assertEqual(live.configure_demand_leases(), expected)
            self.assertEqual(lease_path(market), expected)


if __name__ == '__main__':
    unittest.main()
