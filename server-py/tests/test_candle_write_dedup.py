"""Repeated candle history must not rewrite closed bars or regress live tails."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api.candles import _put_exchange_history
from app.db import ResearchStore, WriterLock


class CandleWriteDedupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = await ResearchStore(str(Path(self.directory.name) / 'research.sqlite'),
                                         '56', write_lock=WriterLock()).connect()
        self.asset = 'binance:0x' + 'a' * 40
        self.key = 'binance:56:0x' + 'a' * 40 + ':1m'
        self.closed = {'t': 60_000, 'o': 1, 'h': 2, 'l': 1, 'c': 2,
                       'v': 10, 'vu': 20, 'confirmed': True}
        self.tail = {'t': 120_000, 'o': 2, 'h': 3, 'l': 2, 'c': 3,
                     'v': 4, 'vu': None, 'confirmed': False}

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def changes(self):
        return (await self.store.fetchone('SELECT total_changes()'))[0]

    async def test_store_skips_identical_bars_but_updates_live_tail_and_confirmation(self):
        await self.store.put_candles(self.asset, '1m', [self.closed, self.tail])
        first = await self.changes()
        await self.store.put_candles(self.asset, '1m', [self.closed, self.tail])
        self.assertEqual(await self.changes(), first)

        updated = {**self.tail, 'c': 2.5, 'v': 5}
        await self.store.put_candles(self.asset, '1m', [self.closed, updated])
        self.assertEqual((await self.store.candle_range(self.asset, '1m'))[-1]['c'], 2.5)
        confirmed = {**updated, 'confirmed': True}
        await self.store.put_candles(self.asset, '1m', [confirmed])
        self.assertTrue((await self.store.candle_range(self.asset, '1m'))[-1]['confirmed'])
        settled = await self.changes()

        await self.store.put_candles(self.asset, '1m', [{**self.tail, 'c': 99}])
        self.assertEqual(await self.changes(), settled)
        self.assertEqual((await self.store.candle_range(self.asset, '1m'))[-1]['c'], 2.5)
        await self.store.put_candles(self.asset, '1m', [{**confirmed, 'c': 2.7}])
        self.assertEqual((await self.store.candle_range(self.asset, '1m'))[-1]['c'], 2.7)

    async def test_history_replay_changes_only_freshness_meta(self):
        await _put_exchange_history(self.store, self.asset, '1m', [self.closed], self.key, 100)
        first = await self.changes()
        await _put_exchange_history(self.store, self.asset, '1m', [self.closed], self.key, 200)
        self.assertEqual(await self.changes() - first, 1)
        self.assertEqual((await self.store.get('candle-meta', self.key))['lastHistoryRequestAt'], 200)
        self.assertEqual((await self.store.candle_range(self.asset, '1m'))[0]['c'], 2)

        updated = {**self.closed, 'h': 2.5}
        await _put_exchange_history(self.store, self.asset, '1m', [updated], self.key, 300)
        self.assertEqual((await self.store.candle_range(self.asset, '1m'))[0]['h'], 2.5)
        settled = await self.changes()
        await _put_exchange_history(self.store, self.asset, '1m', [{**self.closed, 'confirmed': False}],
                                    self.key, 400)
        self.assertEqual(await self.changes() - settled, 1)
        self.assertEqual((await self.store.candle_range(self.asset, '1m'))[0]['h'], 2.5)

    async def test_history_candle_and_meta_still_roll_back_together(self):
        await _put_exchange_history(self.store, self.asset, '1m', [self.closed], self.key, 100)
        execute = self.store.db.execute

        async def fail_meta(query, *args):
            if query.startswith('INSERT INTO facts'):
                raise RuntimeError('meta failed')
            return await execute(query, *args)

        with patch.object(self.store.db, 'execute', side_effect=fail_meta):
            with self.assertRaisesRegex(RuntimeError, 'meta failed'):
                await _put_exchange_history(self.store, self.asset, '1m',
                                            [{**self.closed, 'h': 4}], self.key, 200)
        self.assertEqual((await self.store.candle_range(self.asset, '1m'))[0]['h'], 2)
        self.assertEqual((await self.store.get('candle-meta', self.key))['lastHistoryRequestAt'], 100)


if __name__ == '__main__':
    unittest.main()
