import asyncio
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from app.api import misc
from app.market_history import TRADE_ORDER, _bounded_trade_rows, market_trades, token_trades


class ReadScope:
    scope = '56'

    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.queries = []
        self.db.executescript('''
            CREATE TABLE trades(asset TEXT,id TEXT,t INTEGER,body TEXT,PRIMARY KEY(asset,id));
            CREATE INDEX trades_time ON trades(asset,t);
            CREATE TABLE facts(kind TEXT,id TEXT,body TEXT,PRIMARY KEY(kind,id));
        ''')

    def key(self, value):
        return self.scope+':'+value

    async def fetchall(self, query, params):
        self.queries.append((query, params))
        return self.db.execute(query, params).fetchall()


class BoundedTapeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scope = ReadScope()

    async def asyncTearDown(self):
        self.scope.db.close()

    async def test_large_history_uses_index_only_cutoff_before_reading_json(self):
        storages = [self.scope.key('dex:'+str(n)) for n in range(24)]
        # Invalid old JSON must never be read or sorted. This also catches a
        # return to scanning complete history under an asset IN predicate.
        self.scope.db.executemany('INSERT INTO trades VALUES (?,?,?,?)',
            ((storage, str(i), i, 'old body is intentionally invalid')
             for storage in storages for i in range(2000)))
        recent = [(storage, 'new:'+str(i), 10_000+i*24+n,
                   json.dumps({'id': 'new:'+str(i), 't': 10_000+i*24+n, 'token': str(n)}))
                  for n, storage in enumerate(storages) for i in range(100)]
        self.scope.db.executemany('INSERT INTO trades VALUES (?,?,?,?)', recent)
        instructions = 0
        def bounded_work():
            nonlocal instructions
            instructions += 1000
            return instructions > 200_000
        self.scope.db.set_progress_handler(bounded_work, 1000)
        rows = await _bounded_trade_rows(self.scope, storages, 100)
        self.scope.db.set_progress_handler(None, 0)
        self.assertEqual([row['t'] for row in rows], list(range(12_399, 12_299, -1)))
        self.assertLess(instructions, 200_000)
        time_queries = [(q, p) for q, p in self.scope.queries if q.startswith('SELECT t FROM')]
        self.assertTrue(time_queries)
        plan = ' '.join(str(tuple(r)) for r in self.scope.db.execute('EXPLAIN QUERY PLAN '+time_queries[0][0], time_queries[0][1]))
        self.assertIn('COVERING INDEX trades_time', plan)
        for query, _ in self.scope.queries:
            if query.startswith('SELECT asset,id,t,body'):
                self.assertIn('AND t>=?', query)

    async def test_equal_time_and_offset_match_full_numeric_order_before_limit(self):
        storages = [self.scope.key('dex:'+str(n)) for n in range(20)]
        self.scope.db.executemany('INSERT INTO trades VALUES (?,?,?,?)',
            ((storage, 'chain:'+str(999-i), 1000,
              json.dumps({'id': 'chain:'+str(999-i), 't': 1000, 'blockNumber': 10,
                          'transactionIndex': n, 'logIndex': i}))
             for n, storage in enumerate(storages) for i in reversed(range(30))))
        expected = self.scope.db.execute('SELECT asset,id,t,body FROM trades ORDER BY '+TRADE_ORDER+' LIMIT 17 OFFSET 29').fetchall()
        actual = await _bounded_trade_rows(self.scope, storages, 17, 29)
        self.assertEqual([tuple(row) for row in actual], [tuple(row) for row in expected])

    async def test_filter_catalogue_before_time_cutoff_and_merge_legacy_tokens(self):
        records = [
            {'storage': 'dex:listed', 'token': 'listed', 'venue': 'dex'},
            {'storage': 'dex:other', 'token': 'other', 'venue': 'dex'},
            {'storage': 'binance:listed', 'token': 'listed', 'venue': 'binance'},
        ]
        for n, record in enumerate(records):
            self.scope.db.execute('INSERT INTO facts VALUES (?,?,?)',
                (self.scope.key('market-registry'), record['storage'], json.dumps(record)))
            self.scope.db.execute('INSERT INTO trades VALUES (?,?,?,?)',
                (self.scope.key(record['storage']), 'id', 1000+n,
                 json.dumps({**record, 'id': 'id', 't': 1000+n})))
        result = await market_trades(self.scope, tokens=['listed'], dex_only=True, limit=1)
        self.assertEqual([row['storage'] for row in result], ['dex:listed'])
        for n, token in enumerate(('one', 'two')):
            self.scope.db.execute('INSERT INTO trades VALUES (?,?,?,?)',
                (self.scope.key(token), token, 2000+n, json.dumps({'id': token, 't': 2000+n})))
        rows = await token_trades(self.scope, ['one', 'two'])
        self.assertEqual([r['token'] for r in rows], ['two', 'one'])


class FeedCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        misc._feed_cache.clear()
        misc._feed_locks.clear()

    async def asyncTearDown(self):
        misc._feed_cache.clear()
        misc._feed_locks.clear()

    async def test_concurrent_first_visitors_share_one_read_and_ttl_expires(self):
        async def load(chain):
            await asyncio.sleep(0.01)
            return {'trades': [], 'relationships': [], 'chain': chain}
        with patch.object(misc, '_load_feed', AsyncMock(side_effect=load)) as read:
            results = await asyncio.gather(*(misc.get_feed('56') for _ in range(15)))
            self.assertEqual(read.await_count, 1)
            self.assertTrue(all(r['chain'] == '56' for r in results))
            await misc.get_feed('56')
            self.assertEqual(read.await_count, 1)
            misc._feed_cache['56'] = (0, results[0])
            await misc.get_feed('56')
            self.assertEqual(read.await_count, 2)
            await misc.get_feed('196')
            self.assertEqual(read.await_count, 3)
            await misc.get_feed()
            await misc.get_feed('all')
            self.assertEqual(read.await_count, 4)


if __name__ == '__main__':
    unittest.main()
