import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.db import ResearchStore
from app.product_history import direct_pool_volumes


class DirectVolumeBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = await ResearchStore(self.tmp.name+'/research.sqlite', '56').connect()
        self.relations = []
        for index in range(2):
            pool, token, storage = 'pool'+str(index), 'token'+str(index), 'market'+str(index)
            self.relations.append({'chainId': '56', 'pool': pool, 'token': token, 'status': 'verified', 'level': 'A'})
            await self.s.put('market-registry', storage, {'definition': {'venue': 'dex', 'pool_id': pool, 'token': token}})
            await self.s.put('pool-time-coverage', pool, {'pool': pool, 'canonical': True, 'decoded': True, 'fromMs': 0, 'throughMs': 3000})

    async def asyncTearDown(self):
        await self.s.close()
        self.tmp.cleanup()

    async def trade(self, market, at, value, valid=True):
        body = {'finality': 'confirmed', 'blockHash': 'hash', 'canonicalBlockHash': 'hash',
                'usdObservation': {'currency': 'USD', 'method': 'trade-time-quote' if valid else 'current-price',
                                   'at': at, 'value': value, 'provider': 'dated-usd-source'}}
        await self.s.db.execute('INSERT INTO trades VALUES (?,?,?,?)', (self.s.key(market), str(at), at, json.dumps(body)))
        await self.s.db.commit()

    async def test_batches_pools_keeps_last_bucket_and_only_covered_empty_buckets_are_zero(self):
        await self.trade('market0', 100, 4)
        await self.trade('market1', 2050, 6)
        with patch('app.product_history.store', AsyncMock(return_value=self.s)), \
             patch.object(self.s, 'fetchall', wraps=self.s.fetchall) as fetch:
            result = await direct_pool_volumes(self.relations, 0, 2000, 1000)
        self.assertEqual([r['volumeUsd'] for r in result], [4, 0, 6])
        self.assertEqual(fetch.call_count, 3)  # registry, coverage, grouped swaps
        await self.s.put('pool-time-coverage', 'pool1', {'pool': 'pool1', 'canonical': True, 'decoded': True, 'fromMs': 0, 'throughMs': 2000})
        with patch('app.product_history.store', AsyncMock(return_value=self.s)):
            result = await direct_pool_volumes(self.relations, 0, 2000, 1000)
        self.assertIsNone(result[-1]['volumeUsd'])

    async def test_unpriced_confirmed_trade_and_unmapped_pool_remain_unknown(self):
        await self.trade('market1', 1500, 6, valid=False)
        with patch('app.product_history.store', AsyncMock(return_value=self.s)):
            result = await direct_pool_volumes(self.relations, 0, 2000, 1000)
        self.assertIsNone(result[1]['volumeUsd'])
        self.relations.append({'chainId': '56', 'pool': 'unmapped', 'token': 'token2', 'status': 'verified', 'level': 'A'})
        with patch('app.product_history.store', AsyncMock(return_value=self.s)):
            result = await direct_pool_volumes(self.relations, 0, 2000, 1000)
        self.assertTrue(all(row['volumeUsd'] is None for row in result))

    async def test_incomplete_hours_do_not_scan_swaps_or_claim_zero(self):
        for index in range(2):
            await self.s.put('pool-time-coverage', 'pool'+str(index), {'pool': 'pool'+str(index),
                'canonical': True, 'decoded': True, 'fromMs': 100, 'throughMs': 900})
        with patch('app.product_history.store', AsyncMock(return_value=self.s)), \
             patch.object(self.s, 'fetchall', wraps=self.s.fetchall) as fetch:
            result = await direct_pool_volumes(self.relations, 0, 2000, 1000)
        self.assertEqual(fetch.call_count, 2)  # no unneeded swap scan
        self.assertTrue(all(row['volumeUsd'] is None for row in result))

    async def test_canonical_but_undecoded_coverage_cannot_claim_zero(self):
        await self.s.put('pool-time-coverage', 'pool0', {'pool': 'pool0', 'canonical': True,
            'decoded': False, 'fromMs': 0, 'throughMs': 3000})
        with patch('app.product_history.store', AsyncMock(return_value=self.s)):
            result = await direct_pool_volumes(self.relations, 0, 2000, 1000)
        self.assertTrue(all(row['volumeUsd'] is None for row in result))
