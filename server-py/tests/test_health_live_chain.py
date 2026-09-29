"""Health must report the collector's live lag, not a delayed projection copy."""
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.main import _health_storage_snapshot, chain_stream_degraded, health_data


class LiveChainHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_fact_overrides_a_healthy_projection(self):
        now = int(time.time() * 1000)
        projected = {'provider': 'Chain RPC', 'id': 'chain-stream:56',
                     'chainId': '56', 'status': 'live', 'updatedAt': now - 40_000,
                     'queueDepth': 1, 'sourceLagMs': 100, 'nearTipLagBlocks': 1}
        live = {**projected, 'updatedAt': now - 500, 'queueDepth': 2402,
                'sourceLagMs': 236_000, 'nearTipLagBlocks': 300}
        snapshot = {
            'projection': ({'unified': {'assets': [{'price': 1, 'fieldTimes': {'price': now}}],
                                        'stockTokens': [], 'relations': [], 'sources': [projected]}},
                           {'signals': []}),
            'assets': 1, 'latestAssetAt': now,
            'domains': {chain: {} for chain in ('196', '56', '4663')},
            'scanQuality': {chain: {'total': 0, 'partial': 0, 'unsupportedPools': 0,
                                   'failedPools': 0} for chain in ('196', '56', '4663')},
            'liveChainStreams': {'56': live},
        }
        running = {'ageMs': 0, 'ok': True, 'tasks': {}}
        with patch('app.main._health_storage_snapshot', AsyncMock(return_value=snapshot)), \
             patch('app.state._source_statuses', return_value=[]), \
             patch('app.main.process_health', side_effect=[running, running]), \
             patch('app.main.disk_history', return_value=[]), \
             patch('shutil.disk_usage', return_value=SimpleNamespace(total=100, free=90)):
            result = await health_data()
        self.assertEqual(result['status'], 'degraded')
        self.assertIn('chain-stream-degraded', result['issues'])
        self.assertEqual(result['chainStreams'][0]['queueDepth'], 2402)
        self.assertTrue(result['chainStreams'][0]['degraded'])
        self.assertLess(result['chainStreams'][0]['sampleAgeMs'], 2000)
        self.assertFalse(chain_stream_degraded({**live, 'queueDepth': 0,
                                                'nearTipLagBlocks': 0}, now))

    async def test_live_fact_read_is_bounded_and_malformed_fact_is_ignored(self):
        from app.db import ResearchStore
        with tempfile.TemporaryDirectory() as directory:
            scoped = await ResearchStore(directory + '/research.sqlite', '196').connect()
            try:
                await scoped.db.executemany('INSERT INTO facts(kind,id,body) VALUES (?,?,?)', [
                    ('56:chain-stream', 'pools', json.dumps({'chainId': '56',
                        'updatedAt': 123, 'queueDepth': 2402, 'sourceLagMs': 236000})),
                    ('196:chain-stream', 'pools', '{invalid'),
                    ('4663:chain-stream', 'pools', json.dumps({'chainId': '56',
                        'queueDepth': 5000})),
                ])
                await scoped.db.commit()
                with patch('app.db.store', AsyncMock(return_value=scoped)):
                    snapshot = await _health_storage_snapshot(int(time.time() * 1000))
                self.assertEqual(set(snapshot['liveChainStreams']), {'56'})
                self.assertEqual(snapshot['liveChainStreams']['56']['queueDepth'], 2402)
                self.assertIsNone(snapshot['projection'])
            finally:
                await scoped.close()
