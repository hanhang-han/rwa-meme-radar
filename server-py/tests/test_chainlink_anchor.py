"""Exact BNB Chain quote-token feeds must never turn weak evidence into USD."""
import asyncio
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ['NODE_ENV'] = 'test'

from app.collectors import chainlink_anchor as anchor
from app.collectors.trade_valuation import dated_usd_volume
from app.db import ResearchStore


def word(value):
    return int(value).to_bytes(32, 'big', signed=int(value) < 0)


def description(value):
    raw = value.encode('ascii')
    return '0x' + (word(32) + word(len(raw)) + raw.ljust((len(raw) + 31) // 32 * 32, b'\0')).hex()


def latest(round_id, answer, started, updated, answered=None):
    return '0x' + b''.join(word(v) for v in (
        round_id, answer, started, updated, round_id if answered is None else answered)).hex()


def rpc_for(*, feed=anchor.FEED, chain='0x38', desc=anchor.DESCRIPTION, decimals=8,
            round_id=2, answer=765_54049297, started=1_800_000_000,
            updated=1_800_000_001, answered=None):
    calls = []

    async def rpc(method, params):
        calls.append((method, params))
        if method == 'eth_chainId':
            return chain
        assert method == 'eth_call'
        assert params[0]['to'] == feed
        selectors = {
            '0x7284e416': description(desc),
            '0x313ce567': '0x' + word(decimals).hex(),
            '0xfeaf968c': latest(round_id, answer, started, updated, answered),
        }
        return selectors[params[0]['data']]

    return rpc, calls


class ChainlinkAnchorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/research.sqlite'
        self.scoped = await ResearchStore(self.path, '56').connect()
        self.now = 1_800_000_031_000
        self.trade_at = 1_800_000_061_000
        self.market = SimpleNamespace(chain_id='56', quote_token=anchor.WBNB)
        self.trade = {'t': self.trade_at, 'quoteQuantity': 2.0}

    async def asyncTearDown(self):
        await self.scoped.close()
        self.temp.cleanup()

    async def sample(self, quote_token=anchor.WBNB, **rpc_changes):
        spec = anchor.FEEDS[quote_token]
        rpc, calls = rpc_for(feed=spec['feed'],
                             desc=rpc_changes.pop('desc', spec['description']),
                             **rpc_changes)
        sample = await anchor.verified_sample(rpc, quote_token=quote_token,
                                              observed_at=self.now)
        return sample, calls

    async def value(self, market=None, trade=None):
        return await dated_usd_volume(self.scoped.db, self.scoped,
                                      market or self.market, trade or self.trade)

    async def test_verified_round_values_exact_wbnb_trade_without_headline_quote(self):
        sample, calls = await self.sample()
        self.assertEqual([name for name, _ in calls],
                         ['eth_chainId', 'eth_call', 'eth_call', 'eth_call'])
        self.assertEqual(sample['updatedAt'], 1_800_000_001_000)
        self.assertEqual(sample['answer'], '76554049297')
        self.assertTrue(await anchor.save_sample(self.scoped, sample))
        result = await self.value()
        self.assertAlmostEqual(result['volume'], 2 * 765.54049297)
        self.assertEqual(result['volumeCurrency'], 'USD')
        self.assertEqual(result['quoteAt'], sample['updatedAt'])
        self.assertEqual(result['volumeProvenance']['feedAddress'], anchor.FEED)
        self.assertEqual(result['volumeProvenance']['evidence'], 'oracle-feed')
        self.assertIsNone(await self.scoped.get('asset', anchor.WBNB))

    async def test_usdt_and_usdc_use_their_own_rates_including_depegs(self):
        for token, feed, answer, expected in (
            (anchor.USDT, anchor.USDT_FEED, 97_000_000, 1.94),
            (anchor.USDC, anchor.USDC_FEED, 103_000_000, 2.06),
        ):
            with self.subTest(token=token):
                sample, calls = await self.sample(token, answer=answer)
                self.assertEqual(calls[-1][1][0]['to'], feed)
                self.assertTrue(await anchor.save_sample(self.scoped, sample))
                market = SimpleNamespace(chain_id='56', quote_token=token)
                valued = await self.value(market=market)
                self.assertAlmostEqual(valued['volume'], expected)
                self.assertEqual(valued['volumeProvenance']['feedAddress'], feed)
                self.assertEqual(valued['quoteAt'], sample['updatedAt'])

    async def test_stablecoin_quote_is_not_assumed_to_be_one_dollar(self):
        for token in (anchor.USDT, anchor.USDC):
            with self.subTest(token=token):
                market = SimpleNamespace(chain_id='56', quote_token=token)
                self.assertEqual(await self.value(market=market), {})
                await self.scoped.put('asset', token, {
                    'token': token, 'chainId': '56', 'price': 1,
                    'fieldTimes': {'price': self.trade_at - 1000},
                    'fieldObservations': {'price': {
                        'provider': 'OKX', 'scope': 'token', 'currency': 'USD',
                        'timeKind': 'market', 'marketAt': self.trade_at - 1000,
                        'independent': True,
                    }},
                })
                self.assertEqual(await self.value(market=market), {})

    async def test_feed_samples_cannot_cross_price_tokens(self):
        bnb, _ = await self.sample()
        self.assertTrue(await anchor.save_sample(self.scoped, bnb))
        usdt_market = SimpleNamespace(chain_id='56', quote_token=anchor.USDT)
        self.assertEqual(await self.value(market=usdt_market), {})
        forged = {**bnb, 'quoteToken': anchor.USDT}
        self.assertFalse(await anchor.save_sample(self.scoped, forged))
        await self.scoped.put(anchor.KIND, anchor._sample_id(bnb['updatedAt'], anchor.USDT_FEED),
                              forged)
        self.assertEqual(await self.value(market=usdt_market), {})

    async def test_stablecoin_feed_older_than_15m_remains_unknown(self):
        sample, _ = await self.sample(anchor.USDT)
        sample['updatedAt'] = self.trade_at - 900_001
        sample['startedAt'] = sample['updatedAt'] - 1000
        self.assertTrue(await anchor.save_sample(self.scoped, sample))
        market = SimpleNamespace(chain_id='56', quote_token=anchor.USDT)
        self.assertEqual(await self.value(market=market), {})

    async def test_stale_future_and_invalid_quantity_have_no_usd_amount(self):
        sample, _ = await self.sample()
        await anchor.save_sample(self.scoped, sample)
        self.assertEqual(await self.value(trade={'t': sample['updatedAt'] - 1,
                                                 'quoteQuantity': 2}), {})
        self.assertEqual(await self.value(trade={'t': sample['updatedAt'] + 900_001,
                                                 'quoteQuantity': 2}), {})
        self.assertEqual(await self.value(trade={'t': self.trade_at,
                                                 'quoteQuantity': 0}), {})

    async def test_wrong_chain_token_or_feed_is_rejected(self):
        sample, _ = await self.sample()
        await anchor.save_sample(self.scoped, sample)
        wrong_token = SimpleNamespace(chain_id='56', quote_token='0x' + '1' * 40)
        self.assertEqual(await self.value(market=wrong_token), {})
        wrong_chain = SimpleNamespace(chain_id='196', quote_token=anchor.WBNB)
        self.assertEqual(await self.value(market=wrong_chain), {})
        sample_id = anchor._sample_id(sample['updatedAt'])
        record = await self.scoped.get(anchor.KIND, sample_id)
        record['feed'] = '0x' + '2' * 40
        await self.scoped.put(anchor.KIND, sample_id, record)
        self.assertEqual(await self.value(), {})
        record['feed'] = anchor.FEED
        record['quoteToken'] = '0x' + '2' * 40
        await self.scoped.put(anchor.KIND, sample_id, record)
        self.assertEqual(await self.value(), {})
        record['quoteToken'] = anchor.WBNB
        record['updatedAt'] -= 1000
        await self.scoped.put(anchor.KIND, sample_id, record)
        self.assertEqual(await self.value(), {})

    async def test_no_asset_or_pool_price_can_substitute_for_oracle(self):
        await self.scoped.put('asset', anchor.WBNB, {
            'token': anchor.WBNB, 'chainId': '56', 'price': 765,
            'fieldTimes': {'price': self.trade_at - 1000},
            'fieldObservations': {'price': {
                'provider': 'OKX', 'scope': 'token', 'currency': 'USD',
                'timeKind': 'market', 'marketAt': self.trade_at - 1000,
                'independent': True,
            }},
        })
        self.assertEqual(await self.value(), {})

    async def test_feed_identity_and_round_fail_closed(self):
        for changes in (
            {'chain': '0xc4'}, {'desc': 'BNB / BUSD'}, {'decimals': 18},
            {'answer': 0}, {'answer': -1}, {'round_id': 2, 'answered': 1},
            {'updated': 1_800_000_032},
        ):
            with self.subTest(changes=changes):
                sample, _ = await self.sample(**changes)
                self.assertIsNone(sample)

    async def test_restart_deduplicates_round_without_another_write(self):
        sample, _ = await self.sample()
        self.assertTrue(await anchor.save_sample(self.scoped, sample))
        changes = self.scoped.db.total_changes
        self.assertFalse(await anchor.save_sample(self.scoped, {**sample, 'observedAt': self.now + 1000}))
        self.assertEqual(self.scoped.db.total_changes, changes)
        await self.scoped.close()
        self.scoped = await ResearchStore(self.path, '56').connect()
        self.assertFalse(await anchor.save_sample(self.scoped, {**sample, 'observedAt': self.now + 2000}))
        self.assertEqual((await self.scoped.fetchone('SELECT COUNT(*) FROM facts WHERE kind=?',
                                                       (self.scoped.key(anchor.KIND),)))[0], 1)
        newer = {**sample, 'roundId': '3', 'answeredInRound': '3',
                 'answer': '76560000000', 'updatedAt': sample['updatedAt'] + 60_000,
                 'observedAt': self.now + 60_000}
        self.assertTrue(await anchor.save_sample(self.scoped, newer))
        self.assertAlmostEqual((await self.value())['volume'], 2 * 765.6)

    async def test_trade_read_is_index_bounded_even_with_dense_history(self):
        sample, _ = await self.sample()
        for i in range(anchor.MAX_WINDOW_ROWS + 1):
            row = {**sample, 'roundId': str(2 + i), 'answeredInRound': str(2 + i),
                   'updatedAt': self.trade_at - (anchor.MAX_WINDOW_ROWS - i) * 30_000,
                   'startedAt': self.trade_at - (anchor.MAX_WINDOW_ROWS - i) * 30_000 - 1000,
                   'observedAt': self.trade_at + 1000}
            await self.scoped.put(anchor.KIND, anchor._sample_id(row['updatedAt']), row)
        self.assertEqual(await self.value(), {})
        await self.scoped.db.execute('DELETE FROM facts WHERE kind=? AND id=?',
                                     (self.scoped.key(anchor.KIND),
                                      anchor._sample_id(self.trade_at - 16 * 30_000)))
        await self.scoped.db.commit()
        self.assertAlmostEqual((await self.value())['volume'], 2 * 765.54049297)

    async def test_history_is_bounded_across_restarts(self):
        sample, _ = await self.sample()
        with patch.object(anchor, 'MAX_SAMPLES', 3):
            for i in range(4):
                row = {**sample, 'roundId': str(2 + i), 'answeredInRound': str(2 + i),
                       'updatedAt': sample['updatedAt'] + i * 300_000,
                       'observedAt': sample['updatedAt'] + i * 300_000 + 1000}
                self.assertTrue(await anchor.save_sample(self.scoped, row))
                if i == 1:
                    await self.scoped.close()
                    self.scoped = await ResearchStore(self.path, '56').connect()
        rows = await self.scoped.fetchall('SELECT id FROM facts WHERE kind=? ORDER BY id',
                                          (self.scoped.key(anchor.KIND),))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], anchor._sample_id(sample['updatedAt'] + 300_000))

    async def test_feed_retention_is_independent(self):
        bnb, _ = await self.sample()
        usdt, _ = await self.sample(anchor.USDT)
        with patch.object(anchor, 'MAX_SAMPLES', 1):
            self.assertTrue(await anchor.save_sample(self.scoped, bnb))
            self.assertTrue(await anchor.save_sample(self.scoped, usdt))
            newer = {**bnb, 'roundId': '3', 'answeredInRound': '3',
                     'updatedAt': bnb['updatedAt'] + 300_000,
                     'observedAt': bnb['updatedAt'] + 301_000}
            self.assertTrue(await anchor.save_sample(self.scoped, newer))
        self.assertIsNotNone(await self.scoped.get(anchor.KIND,
            anchor._sample_id(usdt['updatedAt'], anchor.USDT_FEED)))
        self.assertIsNone(await self.scoped.get(anchor.KIND,
            anchor._sample_id(bnb['updatedAt'], anchor.FEED)))

    async def test_sampler_defers_when_live_logs_backed_up(self):
        for token in anchor.FEEDS:
            sample, _ = await self.sample(token)
            self.assertTrue(await anchor.save_sample(self.scoped, sample))
        stream = SimpleNamespace(queue=asyncio.Queue(), live_processing=False,
                                 rpc=AsyncMock())
        for _ in range(32):
            stream.queue.put_nowait(object())
        with patch('app.collectors.chain_stream.stream_for', return_value=stream), \
             patch.object(anchor, 'store', new_callable=AsyncMock, return_value=self.scoped) as store, \
             patch.object(anchor.asyncio, 'sleep', new_callable=AsyncMock) as sleep, \
             patch.object(anchor.time, 'time', return_value=(self.now + 300_000) / 1000):
            result = await anchor.refresh_bnb_anchor()
        self.assertEqual(result['skipped'], 3)
        self.assertEqual(result['deferredReason'], 'live-backlog')
        stream.rpc.assert_not_called()
        store.assert_awaited_once()
        self.assertEqual(sleep.await_count, anchor.BACKLOG_WAIT_STEPS)

    async def test_persistent_backlog_forces_bounded_round_after_ten_minutes(self):
        for token in anchor.FEEDS:
            sample, _ = await self.sample(token)
            self.assertTrue(await anchor.save_sample(self.scoped, sample))
        queue = asyncio.Queue()
        for _ in range(32):
            queue.put_nowait(object())
        clock = [self.now + 300_000]
        by_feed = {spec['feed']: spec for spec in anchor.FEEDS.values()}
        calls = []

        async def rpc(method, params):
            calls.append(method)
            if method == 'eth_chainId':
                return '0x38'
            spec = by_feed[params[0]['to']]
            selector = params[0]['data']
            if selector == '0x7284e416':
                return description(spec['description'])
            if selector == '0x313ce567':
                return '0x' + word(8).hex()
            updated = clock[0] // 1000 - 30
            return latest(3, 100_000_000, updated - 1, updated)

        stream = SimpleNamespace(queue=queue, live_processing=True, rpc=rpc)
        with patch('app.collectors.chain_stream.stream_for', return_value=stream), \
             patch.object(anchor, 'store', new_callable=AsyncMock, return_value=self.scoped), \
             patch.object(anchor.asyncio, 'sleep', new_callable=AsyncMock), \
             patch.object(anchor.time, 'time', side_effect=lambda: clock[0] / 1000):
            deferred = await anchor.refresh_bnb_anchor()
            self.assertEqual(deferred['skipped'], 3)
            self.assertEqual(deferred['deferredReason'], 'live-backlog')
            self.assertEqual(calls, [])
            clock[0] = self.now + anchor.FORCE_SAMPLE_AGE_MS + 1000
            forced = await anchor.refresh_bnb_anchor()
        self.assertEqual(forced, {'requested': 3, 'accepted': 3, 'updated': 3,
                                  'failed': 0, 'unsupported': 0, 'skipped': 0})
        self.assertEqual(len(calls), 12)
        self.assertEqual(calls.count('eth_call'), 9)

    async def test_missing_stream_reports_source_unavailable(self):
        with patch('app.collectors.chain_stream.stream_for', return_value=None), \
             patch.object(anchor, 'store', new_callable=AsyncMock) as store:
            outcome = await anchor.refresh_bnb_anchor()
        self.assertEqual(outcome, {'requested': 0, 'accepted': 0, 'updated': 0,
                                   'failed': 0, 'unsupported': 0, 'skipped': 3,
                                   'unavailableReason': 'stream-missing'})
        store.assert_not_awaited()

    async def test_cancelled_backlog_wait_does_not_reset_restart_freshness(self):
        for token in anchor.FEEDS:
            sample, _ = await self.sample(token)
            self.assertTrue(await anchor.save_sample(self.scoped, sample))
        queue = asyncio.Queue()
        for _ in range(32):
            queue.put_nowait(object())
        entered = asyncio.Event()

        async def paused_sleep(_seconds):
            entered.set()
            await asyncio.Future()

        stream = SimpleNamespace(queue=queue, live_processing=False, rpc=AsyncMock())
        with patch('app.collectors.chain_stream.stream_for', return_value=stream), \
             patch.object(anchor, 'store', new_callable=AsyncMock, return_value=self.scoped), \
             patch.object(anchor.asyncio, 'sleep', side_effect=paused_sleep):
            task = asyncio.create_task(anchor.refresh_bnb_anchor())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        stream.rpc.assert_not_called()
        await self.scoped.close()
        self.scoped = await ResearchStore(self.path, '56').connect()
        with patch.object(anchor.time, 'time', return_value=(self.now + 601_000) / 1000):
            self.assertTrue(await anchor._force_due(self.scoped, self.now + 601_000))

    async def test_sampler_tracks_three_feeds_and_stale_provider_time(self):
        current = int(time.time())
        feeds = {row['feed']: (token, row) for token, row in anchor.FEEDS.items()}
        calls = []

        async def rpc(method, params):
            calls.append(method)
            if method == 'eth_chainId':
                return '0x38'
            token, spec = feeds[params[0]['to']]
            selector = params[0]['data']
            if selector == '0x7284e416':
                return description(spec['description'])
            if selector == '0x313ce567':
                return '0x' + word(8).hex()
            updated = current - (1000 if token == anchor.USDT else 30)
            return latest(2, 97_000_000, updated - 1, updated)

        stream = SimpleNamespace(queue=asyncio.Queue(), live_processing=True, rpc=rpc)
        stream.queue.put_nowait(object())  # Normal live activity is not backlog.
        with patch('app.collectors.chain_stream.stream_for', return_value=stream), \
             patch.object(anchor, 'store', new_callable=AsyncMock, return_value=self.scoped):
            outcome = await anchor.refresh_bnb_anchor()
        self.assertEqual(outcome, {'requested': 3, 'accepted': 2, 'updated': 2,
                                   'failed': 0, 'unsupported': 1, 'skipped': 0})
        self.assertEqual(calls.count('eth_call'), 9)
        self.assertEqual(calls.count('eth_chainId'), 3)
        status = await anchor.latest_sample_status(self.scoped)
        self.assertEqual(set(status), set(anchor.FEEDS))
        self.assertEqual(status[anchor.USDT]['updatedAt'], (current - 1000) * 1000)
        old_trade = {'t': current * 1000, 'quoteQuantity': 2}
        self.assertEqual(await self.value(
            market=SimpleNamespace(chain_id='56', quote_token=anchor.USDT),
            trade=old_trade), {})


if __name__ == '__main__':
    unittest.main()
