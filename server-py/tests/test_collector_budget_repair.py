"""The collector must resume each five-minute window without overspending."""
import os
import math
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

os.environ['NODE_ENV'] = 'test'


class CollectorBudgetRepairTest(unittest.IsolatedAsyncioTestCase):
    def test_scan_round_reopens_only_in_next_five_minute_window(self):
        from app import okx_client as okx

        quota = okx.Quota()
        clock = [1_800_000_000.0]
        with patch.object(okx, 'USAGE', quota), patch.object(okx.time, 'time', side_effect=lambda: clock[0]), \
             patch.dict(os.environ, OKX_DAILY_REQUEST_LIMIT='100', OKX_ROUND_REQUEST_LIMIT='3',
                        OKX_BACKGROUND_RESERVE='1', OKX_CRITICAL_RESERVE='1'):
            for _ in range(3):
                okx.charge(priority='background')
            self.assertEqual((quota.round, quota.daily), (3, 3))
            with self.assertRaises(okx.QuotaExceeded):
                okx.charge(priority='background')
            # A second discovery invocation inside the same slot cannot
            # re-open the scan allowance.
            okx.reset_round()
            with self.assertRaises(okx.QuotaExceeded):
                okx.charge(priority='background')
            clock[0] += 300
            okx.charge(priority='background')
            self.assertEqual((quota.round, quota.daily), (1, 4))

    def test_new_scan_round_does_not_refill_daily_cap(self):
        from app import okx_client as okx

        quota = okx.Quota()
        clock = [1_800_000_000.0]
        with patch.object(okx, 'USAGE', quota), patch.object(okx.time, 'time', side_effect=lambda: clock[0]), \
             patch.dict(os.environ, OKX_DAILY_REQUEST_LIMIT='5', OKX_ROUND_REQUEST_LIMIT='2',
                        OKX_BACKGROUND_RESERVE='1', OKX_CRITICAL_RESERVE='1'):
            for _ in range(2):
                okx.charge(priority='critical')
            clock[0] += 300
            for _ in range(2):
                okx.charge(priority='critical')
            clock[0] += 300
            okx.charge(priority='critical')
            self.assertEqual(quota.daily, 5)
            clock[0] += 300
            with self.assertRaises(okx.QuotaExceeded):
                okx.charge(priority='critical')
            self.assertEqual(quota.daily, 5)

    def test_new_scan_round_cannot_bypass_shared_lane_cap(self):
        from app import okx_client as okx
        from app.request_ledger import shared_usage

        with tempfile.TemporaryDirectory() as directory:
            quota = okx.Quota()
            quota._loaded = True
            quota.day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            clock = [1_800_000_000.0]
            with patch.object(okx, 'USAGE', quota), patch.object(okx.time, 'time', side_effect=lambda: clock[0]), \
                 patch('app.request_ledger.open', side_effect=FileNotFoundError, create=True), \
                 patch.dict(os.environ, NODE_ENV='production', OKX_LEDGER_PATH=directory + '/budget.sqlite',
                            OKX_DAILY_REQUEST_LIMIT='10', OKX_ROUND_REQUEST_LIMIT='2',
                            OKX_BACKGROUND_RESERVE='1', OKX_CRITICAL_RESERVE='1'):
                with okx.request_lane('discovery', 2):
                    okx.charge()
                    okx.charge()
                    clock[0] += 300
                    with self.assertRaises(okx.QuotaExceeded):
                        okx.charge()
                self.assertEqual(shared_usage(quota.day), 2)

    def test_shared_8000_request_and_quote_lane_limits(self):
        from app import okx_client as okx
        from app.request_ledger import shared_usage

        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/budget.sqlite'
            day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE budget(day TEXT PRIMARY KEY,used INTEGER NOT NULL)')
                db.execute('CREATE TABLE lane_budget(day TEXT,lane TEXT,used INTEGER NOT NULL,PRIMARY KEY(day,lane))')
                db.execute('INSERT INTO budget VALUES (?,100)', (day,))
                db.execute('INSERT INTO lane_budget VALUES (?,\'base-stocks\',1727)', (day,))

            quota = okx.Quota()
            quota._loaded = True
            quota.day = day
            quota.daily = 100
            with patch.object(okx, 'USAGE', quota), \
                 patch('app.request_ledger.open', side_effect=FileNotFoundError, create=True), \
                 patch.dict(os.environ, NODE_ENV='production', OKX_LEDGER_PATH=path,
                            OKX_DAILY_REQUEST_LIMIT='8000', OKX_BACKGROUND_RESERVE='1200',
                            OKX_CRITICAL_RESERVE='400'):
                with okx.request_lane('base-stocks', 1728):
                    okx.charge(skip_round=True, priority='background')
                    with self.assertRaises(okx.QuotaExceeded):
                        okx.charge(skip_round=True, priority='background')
                self.assertEqual(shared_usage(day), 101)

                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('UPDATE budget SET used=200 WHERE day=?', (day,))
                    db.execute('INSERT INTO lane_budget VALUES (?,\'base-candidates\',1727)', (day,))
                quota.daily = 200
                with okx.request_lane('base-candidates', 1728):
                    okx.charge(skip_round=True, priority='background')
                    with self.assertRaises(okx.QuotaExceeded):
                        okx.charge(skip_round=True, priority='background')
                self.assertEqual(shared_usage(day), 201)

                # The total and priority reserves still win even when an
                # individual lane has room left.
                for near_limit, priority in ((6799, 'background'),
                                             (7599, 'interactive'),
                                             (7999, 'critical')):
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute('UPDATE budget SET used=? WHERE day=?', (near_limit, day))
                    quota.daily = near_limit
                    okx.charge(skip_round=True, priority=priority)
                    with self.assertRaises(okx.QuotaExceeded):
                        okx.charge(skip_round=True, priority=priority)
                    self.assertEqual(shared_usage(day), near_limit + 1)

    async def test_candidate_baseline_reaches_250_per_round_and_rotates(self):
        from app.collectors import live_quotes as quotes

        clock = [10_000_000]
        tokens = ['0x' + format(i, '040x') for i in range(1, 351)]
        queue = {}
        price_times = {}
        calls = []
        stores = {chain: AsyncMock() for chain in quotes.CHAINS}

        async def rows(kind):
            if kind == 'asset':
                return [{'token': token, 'kind': 'candidate',
                         'price': 1,
                         'fieldTimes': {'price': price_times[token]}} if token in price_times
                        else {'token': token, 'kind': 'candidate'} for token in tokens]
            if kind == 'collector-job':
                return list(queue.values())
            return []

        for chain in quotes.CHAINS:
            stores[chain].all.return_value = []
        stores['56'].all.side_effect = rows

        async def scoped_store(chain):
            return stores[chain]

        async def fetch(entries, lane, priority='background'):
            calls.append((list(entries), lane))
            for _, token in entries:
                queue[token] = {'domain': 'quote', 'key': token,
                                'lastAttemptAt': clock[0], 'lastSuccessAt': clock[0]}
                price_times[token] = clock[0]
            return {'requested': (len(entries) + 49) // 50, 'accepted': len(entries)}

        with patch.object(quotes, 'store', scoped_store), \
             patch.object(quotes, 'now_ms', side_effect=lambda: clock[0]), \
             patch.object(quotes, '_fetch_entries', side_effect=fetch):
            await quotes.refresh_base_candidates()
            clock[0] += 300_000
            await quotes.refresh_base_candidates()

        self.assertEqual([len(entries) for entries, _ in calls], [300, 50])
        self.assertEqual({token for entries, _ in calls for _, token in entries}, set(tokens))
        self.assertEqual([lane for _, lane in calls], ['base-candidates', 'base-candidates'])

    async def test_stale_price_is_due_even_after_recent_successful_job(self):
        from app.collectors import live_quotes as quotes

        now = 1_800_000_000_000
        token = '0x' + 'a' * 40
        assets = [{'token': token, 'kind': 'candidate', 'price': 2,
                   'fieldTimes': {'price': now - 1_550_000}}]
        jobs = [{'domain': 'quote', 'key': token, 'lastSuccessAt': now - 600_000,
                 'lastAttemptAt': now - 600_000}]
        stores = {chain: AsyncMock() for chain in quotes.CHAINS}
        for chain in quotes.CHAINS:
            stores[chain].all.return_value = []

        async def rows(kind):
            return assets if kind == 'asset' else jobs if kind == 'collector-job' else []

        stores['56'].all.side_effect = rows

        async def scoped_store(chain):
            return stores[chain]

        fetch = AsyncMock(return_value={'accepted': 1})
        with patch.object(quotes, 'store', scoped_store), \
             patch.object(quotes, 'now_ms', return_value=now), \
             patch.object(quotes, '_fetch_entries', fetch):
            await quotes.refresh_base_candidates()
        self.assertEqual(fetch.call_args.args[0], [('56', token)])
        self.assertFalse(quotes.quote_due(assets[0], {**jobs[0], 'nextRetryAt': now + 1000},
                                               now, 1_500_000))

    def test_candidate_25m_schedule_stays_inside_daily_background_budget(self):
        # Five runs fit in a 25-minute window; 1,419 candidates require no
        # more than 1,500 positions if each provider row yields a new price.
        self.assertGreaterEqual(5 * 6 * 50, 1419)
        self.assertEqual(6 * 288, 1728)
        self.assertLessEqual(math.ceil(1419 * 86_400_000 / (1_500_000 * 50)), 1728)
        # Stock capacity is reallocated from the largely unused discovery
        # lane; every background lane can still reach its own published cap.
        self.assertEqual(1728 + 1728 + 1544 + 1000 + 800, 6800)

    async def test_stale_provider_time_does_not_advance_quote_success(self):
        from app.collectors import live_quotes as quotes
        from app.db import ResearchStore

        now = int(time.time() * 1000)
        token = '0x' + 'b' * 40
        with tempfile.TemporaryDirectory() as directory:
            s = await ResearchStore(directory + '/research.sqlite', '196').connect()
            try:
                old_success = now - 600_000
                await s.put('asset', token, {'token': token, 'kind': 'candidate', 'price': 2,
                                             'fieldTimes': {'price': now - 1_900_000}})
                await s.put('collector-job', 'quote:' + token, {
                    'id': 'quote:' + token, 'domain': 'quote', 'key': token,
                    'lastSuccessAt': old_success, 'lastAttemptAt': old_success})
                stale = [{'chainIndex': '196', 'tokenContractAddress': token,
                          'price': '3', 'time': now - 2_400_000}]

                async def scoped_store(_chain):
                    return s

                with patch.object(quotes, 'store', scoped_store), \
                     patch.object(quotes, 'okx_post', AsyncMock(return_value=stale)), \
                     patch.object(quotes, 'broadcast'):
                    result = await quotes._fetch_and_merge('196', [token], 'base-candidates')
                self.assertEqual((result['accepted'], result['unsupported']), (0, 1))
                job = await s.get('collector-job', 'quote:' + token)
                self.assertEqual(job['lastSuccessAt'], old_success)
                self.assertEqual(job['reason'], 'stale-price-observation')
                self.assertGreater(job['nextRetryAt'], now)

                fresh = [{'chainIndex': '196', 'tokenContractAddress': token,
                          'price': '4', 'time': now - 1_650_000}]
                with patch.object(quotes, 'store', scoped_store), \
                     patch.object(quotes, 'okx_post', AsyncMock(return_value=fresh)), \
                     patch.object(quotes, 'broadcast'):
                    result = await quotes._fetch_and_merge('196', [token], 'base-candidates')
                self.assertEqual(result['accepted'], 1)
                self.assertGreater((await s.get('collector-job', 'quote:' + token))['lastSuccessAt'], old_success)
                saved = await s.get('asset', token)
                self.assertTrue(quotes.fresh_price(saved, now, 1_800_000))
                self.assertTrue(quotes.quote_due(saved, await s.get('collector-job', 'quote:' + token),
                                                 now, 1_500_000))
            finally:
                await s.close()

    async def test_candidate_prefetch_uses_remaining_positions_up_to_six_batches(self):
        from app.collectors import live_quotes as quotes

        now = 1_800_000_000_000
        due_tokens = ['0x' + format(i, '040x') for i in range(1, 203)]
        soon_tokens = ['0x' + format(i, '040x') for i in range(203, 252)]
        assets = ([{'token': token, 'kind': 'candidate', 'price': 1,
                    'fieldTimes': {'price': now - 1_900_000}} for token in due_tokens] +
                  [{'token': token, 'kind': 'candidate', 'price': 1,
                    'fieldTimes': {'price': now - 1_200_000}} for token in soon_tokens])
        jobs = [{'domain': 'quote', 'key': asset['token'],
                 'lastSuccessAt': asset['fieldTimes']['price'],
                 'lastAttemptAt': asset['fieldTimes']['price']} for asset in assets]
        stores = {chain: AsyncMock() for chain in quotes.CHAINS}
        for chain in quotes.CHAINS:
            stores[chain].all.return_value = []

        async def rows(kind):
            return assets if kind == 'asset' else jobs if kind == 'collector-job' else []

        stores['56'].all.side_effect = rows

        async def scoped_store(chain):
            return stores[chain]

        fetch = AsyncMock(return_value={'accepted': 250})
        with patch.object(quotes, 'store', scoped_store), \
             patch.object(quotes, 'now_ms', return_value=now), \
             patch.object(quotes, '_fetch_entries', fetch):
            await quotes.refresh_base_candidates()
            selected = fetch.call_args.args[0]
            self.assertEqual(len(selected), 251)  # 202 due + 49 pre-expiry assets
            self.assertEqual({token for _, token in selected[:202]}, set(due_tokens))
            self.assertEqual({token for _, token in selected[202:]}, set(soon_tokens))
            self.assertEqual((len(selected) + 49) // 50, 6)

            # A full due batch can use the next idle batch for pre-expiry work.
            assets[:] = assets[:200] + assets[202:203]
            await quotes.refresh_base_candidates()
            selected = fetch.call_args.args[0]
            self.assertEqual(len(selected), 201)
            self.assertEqual((len(selected) + 49) // 50, 5)

    async def test_candidate_prefetch_uses_idle_whole_batches_without_displacing_due_quotes(self):
        from app.collectors import live_quotes as quotes

        now = 1_800_000_000_000
        due_tokens = ['0x' + format(i, '040x') for i in range(1, 201)]
        soon_tokens = ['0x' + format(i, '040x') for i in range(201, 281)]
        assets = ([{'token': token, 'kind': 'candidate', 'price': 1,
                    'fieldTimes': {'price': now - 1_900_000}} for token in due_tokens] +
                  [{'token': token, 'kind': 'candidate', 'price': 1,
                    'fieldTimes': {'price': now - 1_200_000}} for token in soon_tokens])
        job_rows = [{'domain': 'quote', 'key': asset['token'],
                     'lastSuccessAt': asset['fieldTimes']['price'],
                     'lastAttemptAt': asset['fieldTimes']['price']} for asset in assets]
        stores = {chain: AsyncMock() for chain in quotes.CHAINS}
        for chain in quotes.CHAINS:
            stores[chain].all.return_value = []

        async def rows(kind):
            return assets if kind == 'asset' else job_rows if kind == 'collector-job' else []

        stores['56'].all.side_effect = rows

        async def scoped_store(chain):
            return stores[chain]

        fetch = AsyncMock(return_value={'requested': 6, 'accepted': 280})
        with patch.object(quotes, 'store', scoped_store), \
             patch.object(quotes, 'now_ms', return_value=now), \
             patch.object(quotes, '_fetch_entries', fetch):
            await quotes.refresh_base_candidates()

            # When all six batches are needed for overdue quotes, prefetch
            # must not displace one of them.
            more_due = ['0x' + format(i, '040x') for i in range(281, 381)]
            assets.extend({'token': token, 'kind': 'candidate', 'price': 1,
                           'fieldTimes': {'price': now - 1_900_000}} for token in more_due)
            job_rows.extend({'domain': 'quote', 'key': token,
                             'lastSuccessAt': now - 1_900_000,
                             'lastAttemptAt': now - 1_900_000} for token in more_due)
            await quotes.refresh_base_candidates()
            due_only = fetch.call_args.args[0]

            # An otherwise idle slot can still prefetch soon-to-expire quotes.
            assets[:] = assets[200:280]
            await quotes.refresh_base_candidates()
            soon_only = fetch.call_args.args[0]

        selected = fetch.call_args_list[0].args[0]
        self.assertEqual(fetch.call_args.args[1], 'base-candidates')
        self.assertEqual(len(selected), 280)  # Four due batches, two prefetch batches.
        self.assertEqual({token for _, token in selected[:200]}, set(due_tokens))
        self.assertEqual({token for _, token in selected[200:]}, set(soon_tokens))
        self.assertEqual((len(selected) + 49) // 50, 6)
        self.assertEqual(len(due_only), 300)
        self.assertEqual({token for _, token in due_only}, set(due_tokens + more_due))
        self.assertEqual({token for _, token in soon_only}, set(soon_tokens))
        self.assertEqual((len(soon_only) + 49) // 50, 2)

    async def test_stock_budget_is_paced_across_day_and_each_hour(self):
        from app.collectors import live_quotes as quotes

        slots = [quotes.stock_batch_allowance(i * 300_000, None) for i in range(288)]
        self.assertEqual(sum(slots), 1728)
        self.assertTrue(all(sum((slots * 2)[i:i + 12]) == 72 for i in range(288)))

        tokens = ['0x' + format(i, '040x') for i in range(1, 301)]
        stores = {chain: AsyncMock() for chain in quotes.CHAINS}
        for chain in quotes.CHAINS:
            stores[chain].all.return_value = []

        async def rows(kind):
            if kind == 'asset':
                return [{'token': token, 'kind': 'stock'} for token in tokens]
            return []

        stores['56'].all.side_effect = rows

        async def scoped_store(chain):
            return stores[chain]

        clock = [34 * 300_000]
        fetch = AsyncMock(return_value={'requested': 6, 'accepted': 300})
        with patch.object(quotes, 'store', scoped_store), \
             patch.object(quotes, 'now_ms', side_effect=lambda: clock[0]), \
             patch.object(quotes, 'stock_lane_used', return_value=None), \
             patch.object(quotes, '_fetch_entries', fetch):
            await quotes.refresh_base_stocks()
            self.assertEqual(len(fetch.call_args.args[0]), 300)
            clock[0] = 35 * 300_000
            await quotes.refresh_base_stocks()
            self.assertEqual(len(fetch.call_args.args[0]), 300)

    async def test_stock_spare_batch_refreshes_quotes_before_60m_expiry(self):
        from app.collectors import live_quotes as quotes

        now = 1_800_000_000_000
        due_tokens = ['0x' + format(i, '040x') for i in range(1, 251)]
        soon_tokens = ['0x' + format(i, '040x') for i in range(251, 351)]
        assets = ([{'token': token, 'kind': 'stock', 'price': 1,
                    'fieldTimes': {'price': now - 3_400_000}} for token in due_tokens] +
                  [{'token': token, 'kind': 'stock', 'price': 1,
                    'fieldTimes': {'price': now - 3_000_000}} for token in soon_tokens])
        job_rows = [{'domain': 'quote', 'key': asset['token'],
                     'lastSuccessAt': asset['fieldTimes']['price'],
                     'lastAttemptAt': asset['fieldTimes']['price']} for asset in assets]
        stores = {chain: AsyncMock() for chain in quotes.CHAINS}
        for chain in quotes.CHAINS:
            stores[chain].all.return_value = []

        async def rows(kind):
            return assets if kind == 'asset' else job_rows if kind == 'collector-job' else []

        stores['56'].all.side_effect = rows

        async def scoped_store(chain):
            return stores[chain]

        fetch = AsyncMock(return_value={'requested': 6, 'accepted': 300})
        with patch.object(quotes, 'store', scoped_store), \
             patch.object(quotes, 'now_ms', return_value=now), \
             patch.object(quotes, 'stock_lane_used', return_value=None), \
             patch.object(quotes, '_fetch_entries', fetch):
            await quotes.refresh_base_stocks()
        selected = fetch.call_args.args[0]
        self.assertEqual(fetch.call_args.args[1:3], ('base-stocks',))
        self.assertEqual(len(selected), 300)
        self.assertEqual({token for _, token in selected[:250]}, set(due_tokens))
        self.assertEqual({token for _, token in selected[250:]}, set(soon_tokens[:50]))
        self.assertEqual((len(selected) + 49) // 50, 6)

    def test_stock_restart_never_frontloads_unspent_lane_credit(self):
        from app.collectors import live_quotes as quotes

        now = 100 * 300_000
        scheduled = quotes.stock_batches_scheduled_through(now)
        # A missed window leaves credit, but no later run exceeds six batches.
        self.assertEqual(quotes.stock_batch_allowance(now, scheduled - 10), 6)
        self.assertEqual(quotes.stock_batch_allowance(now, scheduled - 3), 3)
        self.assertEqual(quotes.stock_batch_allowance(now, scheduled), 0)
        self.assertEqual(quotes.stock_batch_allowance(now, None), 6)
        last_slot = 287 * 300_000
        self.assertEqual(quotes.stock_batches_scheduled_through(last_slot), 1728)
        self.assertEqual(quotes.stock_batch_allowance(last_slot, 1727), 1)
        self.assertEqual(quotes.stock_batch_allowance(last_slot, 1728), 0)
        # Day rollover gets a fresh allocation; the old day's debt does not
        # leak into the new quota row.
        self.assertEqual(quotes.stock_batches_scheduled_through(288 * 300_000), 6)
        self.assertEqual(quotes.stock_batch_allowance(288 * 300_000, 0), 6)

        spent = 0
        for slot in range(288):
            if slot == 50:  # the worker was stopped for one scheduled run
                continue
            allowance = quotes.stock_batch_allowance(slot * 300_000, spent)
            self.assertLessEqual(allowance, 6)
            spent += allowance
            self.assertLessEqual(spent, quotes.stock_batches_scheduled_through(slot * 300_000))
        self.assertEqual(spent, 1722)

    def test_stock_catchup_reads_shared_lane_without_charging(self):
        from app.collectors import live_quotes as quotes

        now = 1_790_585_000_000
        day = datetime.fromtimestamp(now / 1000, timezone.utc).strftime('%Y-%m-%d')
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/budget.sqlite'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE lane_budget(day TEXT,lane TEXT,used INTEGER,PRIMARY KEY(day,lane))')
                db.execute('INSERT INTO lane_budget VALUES (?,?,?)', (day, 'base-stocks', 137))
            with patch.dict(os.environ, OKX_LEDGER_PATH=path):
                self.assertEqual(quotes.stock_lane_used(now), 137)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('SELECT used FROM lane_budget').fetchone()[0], 137)


if __name__ == '__main__':
    unittest.main()
