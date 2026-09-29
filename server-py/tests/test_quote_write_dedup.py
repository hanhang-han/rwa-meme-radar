"""Repeated OKX market observations should not create quote write traffic."""
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

os.environ['NODE_ENV'] = 'test'

from app.collectors import assets, live_quotes
from app.db import ResearchStore


class QuoteWriteDedupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = await ResearchStore(self.temp.name + '/research.sqlite', '196').connect()
        self.at = int(time.time() * 1000) - 10_000

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    async def outbox_count(self):
        return (await self.store.fetchone('SELECT COUNT(*) FROM change_outbox'))[0]

    def quote(self, token, **changes):
        return {'chainIndex': '196', 'tokenContractAddress': token,
                'tokenSymbol': 'TEST', 'price': '2', 'marketCap': '100',
                'volume24H': '10', 'time': self.at, **changes}

    async def test_same_market_time_and_payload_preserve_asset_sample_and_outbox(self):
        token = '0x' + '1' * 40
        row = self.quote(token)
        first, changed = await assets.save_asset(self.store, row, return_changed=True)
        self.assertTrue(changed)
        initial_outbox = await self.outbox_count()
        initial_samples = await self.store.samples(token)
        initial_received = first['fieldObservations']['price']['receivedAt']
        initial_changes = self.store.db.total_changes

        with patch.object(assets, 'now_ms', return_value=self.at + 40_000):
            repeated, changed = await assets.save_asset(self.store, row, return_changed=True)

        self.assertFalse(changed)
        self.assertEqual(repeated, first)
        self.assertEqual(repeated['fieldObservations']['price']['receivedAt'], initial_received)
        self.assertEqual(await self.store.samples(token), initial_samples)
        self.assertEqual(await self.outbox_count(), initial_outbox)
        self.assertEqual(self.store.db.total_changes, initial_changes)

    async def test_same_timestamp_changes_and_newer_market_time_are_saved(self):
        token = '0x' + '2' * 40
        row = self.quote(token)
        await assets.save_asset(self.store, row)
        original_outbox = await self.outbox_count()

        changed_metric, changed = await assets.save_asset(
            self.store, {**row, 'volume24H': '11'}, return_changed=True)
        self.assertTrue(changed)
        self.assertEqual(changed_metric['volume24h'], 11)
        self.assertGreater(await self.outbox_count(), original_outbox)

        changed_cap, changed = await assets.save_asset(
            self.store, {**row, 'volume24H': '11', 'marketCap': '110'},
            return_changed=True)
        self.assertTrue(changed)
        self.assertEqual(changed_cap['marketCap'], 110)
        self.assertEqual((await self.store.samples(token))[0]['cap'], 110)

        changed_price, changed = await assets.save_asset(
            self.store, {**row, 'volume24H': '11', 'marketCap': '110', 'price': '3'},
            return_changed=True)
        self.assertTrue(changed)
        self.assertEqual(changed_price['price'], 3)
        self.assertEqual((await self.store.samples(token))[0]['price'], 3)

        newer, changed = await assets.save_asset(self.store, {
            **row, 'volume24H': '11', 'marketCap': '110', 'price': '3',
            'time': self.at + 1_000}, return_changed=True)
        self.assertTrue(changed)
        self.assertEqual(newer['fieldTimes']['price'], self.at + 1_000)
        before_old = await self.outbox_count()
        old, changed = await assets.save_asset(self.store, {
            **row, 'volume24H': '11', 'marketCap': '110', 'price': '1',
            'time': self.at - 1_000}, return_changed=True)
        self.assertFalse(changed)
        self.assertEqual(old['price'], 3)
        self.assertEqual(await self.outbox_count(), before_old)

    async def test_received_time_without_provider_timestamp_still_advances(self):
        token = '0x' + '3' * 40
        row = self.quote(token)
        row.pop('time')
        with patch.object(assets, 'now_ms', side_effect=[self.at, self.at + 1_000]):
            first, first_changed = await assets.save_asset(self.store, row, return_changed=True)
            second, second_changed = await assets.save_asset(self.store, row, return_changed=True)
        self.assertTrue(first_changed)
        self.assertTrue(second_changed)
        self.assertEqual(first['fieldTimeKinds']['price'], 'received')
        self.assertEqual(second['fieldTimes']['price'], self.at + 1_000)

    async def test_duplicate_repairs_missing_sample_without_rewriting_asset(self):
        token = '0x' + '4' * 40
        row = self.quote(token)
        await assets.save_asset(self.store, row)
        await self.store.db.execute('DELETE FROM samples WHERE asset=?', (self.store.key(token),))
        await self.store.db.execute('DELETE FROM sample_evidence WHERE asset=?', (self.store.key(token),))
        await self.store.db.commit()

        before = await self.store.get('asset', token)
        repaired, changed = await assets.save_asset(self.store, row, return_changed=True)
        self.assertTrue(changed)
        self.assertEqual(repaired, before)
        self.assertEqual((await self.store.samples(token))[0]['price'], 2)

    async def test_three_hundred_unchanged_stocks_keep_checkpoints_without_quote_events(self):
        tokens = ['0x' + format(i, '040x') for i in range(1, 301)]
        entries = [('196', token) for token in tokens]
        clock = [self.at + 10_000]

        async def scoped_store(_chain):
            return self.store

        async def fetch(_path, request, _options):
            return [self.quote(item['tokenContractAddress']) for item in request]

        post = AsyncMock(side_effect=fetch)
        with patch.object(live_quotes, 'store', scoped_store), \
             patch.object(live_quotes, 'okx_post', post), \
             patch('app.collectors.queue.now_ms', side_effect=lambda: clock[0]), \
             patch.object(live_quotes, 'broadcast') as broadcast:
            first = await live_quotes._fetch_entries(entries, 'base-stocks')
            initial_outbox = await self.outbox_count()
            initial_changes = self.store.db.total_changes
            initial_checkpoint_changes = self.store._checkpoint_db.total_changes
            clock[0] += 300_000
            second = await live_quotes._fetch_entries(entries, 'base-stocks')

        self.assertEqual((first['requested'], first['accepted'], first['updated']), (6, 300, 300))
        self.assertEqual((second['requested'], second['accepted'], second['updated']), (6, 300, 0))
        self.assertEqual(await self.outbox_count(), initial_outbox)
        self.assertEqual(self.store.db.total_changes - initial_changes, 0)
        self.assertEqual(self.store._checkpoint_db.total_changes - initial_checkpoint_changes, 300)
        self.assertEqual(broadcast.call_count, 300)
        self.assertEqual(post.await_count, 12)
        self.assertEqual((await self.store.get('collector-job', 'quote:' + tokens[0]))['lastSuccessAt'],
                         clock[0])


if __name__ == '__main__':
    unittest.main()
