import os
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import registry

MEME = '0x' + '1' * 40
POOL = '0x' + '3' * 40
NATIVE = '0xc845b2894dbddd03858fd2d643b4ef725fe0849d'
WRAPPER = '0xa8ddb5cd96b5222afe198316e9a57caa642850d5'


def relation(**changes):
    now = int(time.time() * 1000)
    row = {'chainId': '196', 'token': MEME, 'stock': NATIVE,
           'stockSide': WRAPPER, 'pool': POOL, 'token0': MEME,
           'token1': WRAPPER, 'ticker': 'NVDA', 'status': 'verified',
           'liquidityUsd': 2500, 'liquidityAt': now - 1000,
           'checkedAt': now - 1000, 'block': 1}
    row.update(changes)
    return row


class RegistryV21Test(unittest.IsolatedAsyncioTestCase):
    async def test_cold_rpc_cache_is_shared_by_concurrent_readers(self):
        calls = 0

        def read():
            nonlocal calls
            calls += 1
            time.sleep(0.03)
            return [(MEME, NATIVE, 'NVDA', bytes(32), 1)]

        contract = SimpleNamespace(functions=SimpleNamespace(all=lambda: SimpleNamespace(call=read)))
        client = SimpleNamespace(eth=SimpleNamespace(contract=lambda **_: contract))
        with patch.object(registry, '_contract', return_value='0x' + 'a' * 40), \
             patch.object(registry, 'w3', return_value=client), \
             patch.object(registry, '_cache', None):
            results = await asyncio.gather(*(registry.onchain_pairs() for _ in range(8)))
        self.assertEqual(calls, 1)
        self.assertTrue(all(result == results[0] for result in results))

    async def test_historical_onchain_record_is_not_current_evidence_when_relation_downgrades(self):
        rows = [{'meme': MEME, 'stock': NATIVE, 'ticker': 'NVDA',
                 'evidence': '0x' + '0' * 64, 'registeredAt': 1}]
        with patch.object(registry, '_contract', return_value='0x' + 'a' * 40), \
             patch.object(registry, '_key', return_value=''), \
             patch.object(registry, 'onchain_pairs', AsyncMock(return_value=rows)):
            current = await registry.registry_info([relation()])
            self.assertEqual(current['localVerified'], 1)
            self.assertIsNotNone(current['pairs'][0]['evidenceSource'])
            degraded = await registry.registry_info([relation(liquidityAt=1)])
            self.assertEqual(degraded['onchainCount'], 1)
            self.assertEqual(degraded['localVerified'], 0)
            self.assertIsNone(degraded['pairs'][0]['evidenceSource'])
            self.assertFalse(degraded['autoSyncEnabled'])

    async def test_manual_sync_is_disabled_without_explicit_flag(self):
        with patch.dict(os.environ, {'REGISTRY_MANUAL_SYNC_APPROVED': ''}), \
             patch.object(registry, 'registry_configured', return_value=True), \
             patch.object(registry, 'onchain_pairs', AsyncMock()) as read:
            await registry.sync_registry([relation()])
            read.assert_not_awaited()
