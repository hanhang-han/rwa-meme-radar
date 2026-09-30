"""Routine liquidity scans must not erase independent pool provenance."""
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ["NODE_ENV"] = "test"

from app.collectors import main_round
from app.collectors.assets import now_ms
from app.db import ResearchStore


STOCK = "0xc845b2894dbddd03858fd2d643b4ef725fe0849d"
MEME = "0x" + "1" * 40
POOL = "0x" + "3" * 40
FACTORY = "0x" + "4" * 40
RELATION_ID = f"56:{POOL}:{MEME}:{STOCK}"


class PoolEnrichmentPreservationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = await ResearchStore(self.temp.name + "/research.sqlite", "56").connect()
        self.chain_token = main_round.CHAIN.set("56")
        self.stock = {"tokenContractAddress": STOCK, "stockCode": "NVDA"}
        self.checked = {"token0": MEME, "token1": STOCK, "relation": {
            "token": MEME, "stockSide": STOCK, "stock": self.stock, "wrapper": False,
        }}

    async def asyncTearDown(self):
        main_round.CHAIN.reset(self.chain_token)
        await self.store.close()
        self.temp.cleanup()

    async def scan(self, liquidity):
        row = {"poolAddress": POOL, "protocolName": "index label",
               "liquidityProviderFeePercent": "0.25%", "pool": "Meme/NVDAx"}
        if liquidity is not None:
            row["liquidityUsd"] = str(liquidity)
        with patch.object(main_round, "okx_get", AsyncMock(return_value=[row])), \
             patch.object(main_round, "verify_pool", AsyncMock(return_value=self.checked)), \
             patch.object(main_round, "rpc_call", AsyncMock(return_value=bytes(32))), \
             patch.object(main_round, "broadcast") as broadcast:
            result = await main_round.scan_pools(self.store, MEME, [self.stock], "0x20")
        return result, broadcast

    async def test_historical_proof_survives_and_is_not_announced_as_new(self):
        at = now_ms() - 1000
        await self.store.put("asset", MEME, {"token": MEME, "kind": "candidate",
                                             "symbol": "MEME", "historicalDiscovery": True,
                                             "discoveryEventPending": True})
        pool_proof = {
            "pool": POOL, "token0": MEME, "token1": STOCK,
            "classification": "stock-meme", "factory": FACTORY,
            "protocol": "PancakeSwap V2", "fee": None,
            "verificationSource": "index-plus-factory-getter",
            "verificationBlockHash": "0x" + "a" * 64,
            "verificationHeadHash": "0x" + "a" * 64,
            "streamBackfillFromBlock": 12345,
            "verifiedAt": at, "indexedLiquidityUsd": 9000,
        }
        await self.store.put("pool", POOL, pool_proof)
        await self.store.put("relation", RELATION_ID, {
            "id": RELATION_ID, "chainId": "56", "token": MEME,
            "stock": STOCK, "stockSide": STOCK, "pool": POOL,
            "token0": MEME, "token1": STOCK, "status": "verified",
            "liquidityUsd": None, "liquidityAt": None,
            "historicalDiscovery": True, "creationAnnouncementPending": False,
        })

        outcome, broadcast = await self.scan(2500)

        self.assertEqual(outcome["status"], "ready")
        pool = await self.store.get("pool", POOL)
        relation = await self.store.get("relation", RELATION_ID)
        for field, expected in pool_proof.items():
            if field not in ("pool", "token0", "token1"):
                self.assertEqual(pool[field], expected, field)
        self.assertEqual(pool["liquidityUsd"], 2500)
        self.assertEqual(pool["feePct"], 0.25)
        self.assertEqual(relation["liquidityUsd"], 2500)
        self.assertTrue(relation["historicalDiscovery"])
        self.assertFalse(relation["creationAnnouncementPending"])
        self.assertNotIn("poolCreatedAt", pool)
        self.assertNotIn("creationTx", relation)
        self.assertFalse((await self.store.get("asset", MEME))["discoveryEventPending"])
        self.assertEqual(await self.store.events(None, 10), [])
        broadcast.assert_not_called()

    async def test_missing_new_liquidity_does_not_refresh_stale_old_value(self):
        old = now_ms() - 3_600_000
        await self.store.put("asset", MEME, {"token": MEME, "kind": "candidate",
                                             "historicalDiscovery": True})
        await self.store.put("pool", POOL, {
            "pool": POOL, "classification": "stock-meme", "liquidityUsd": 9000,
            "verificationSource": "index-plus-factory-getter",
            "indexedLiquidityUsd": 9000,
        })
        await self.store.put("relation", RELATION_ID, {
            "id": RELATION_ID, "chainId": "56", "token": MEME,
            "stock": STOCK, "stockSide": STOCK, "pool": POOL,
            "token0": MEME, "token1": STOCK, "status": "verified",
            "liquidityUsd": 9000, "liquidityAt": old,
            "historicalDiscovery": True, "creationAnnouncementPending": False,
        })

        await self.scan(None)

        relation = await self.store.get("relation", RELATION_ID)
        self.assertEqual(relation["liquidityUsd"], 9000)
        self.assertEqual(relation["liquidityAt"], old)
        self.assertIsNone(relation["level"])
        self.assertEqual(relation["evidenceStatus"], "liquidity-stale")

    async def test_non_meme_repair_classes_are_not_added_as_meme_relations(self):
        for classification in ("stock-quote", "quarantined-unknown-counterpart"):
            with self.subTest(classification=classification):
                await self.store.put("pool", POOL, {
                    "pool": POOL, "classification": classification,
                    "verificationSource": "index-plus-factory-getter",
                    "indexedLiquidityUsd": 20_000,
                })
                outcome, broadcast = await self.scan(20_000)
                self.assertEqual(outcome["verifiedPools"], 1)
                self.assertIsNone(await self.store.get("relation", RELATION_ID))
                self.assertEqual((await self.store.get("pool", POOL))["classification"], classification)
                broadcast.assert_not_called()

    async def test_revoked_pool_scan_cannot_resurrect_relation_or_emit_event(self):
        for pool_revocation in ({"verificationStatus": "reorged"},
                                {"creationStatus": "orphaned"}):
            with self.subTest(pool_revocation=pool_revocation):
                await self.store.put("pool", POOL, {"pool": POOL, **pool_revocation})
                await self.store.put("relation", RELATION_ID, {
                    "id": RELATION_ID, "pool": POOL, "token": MEME,
                    "status": "invalid", "level": None,
                    "historicalDiscovery": True,
                })
                with patch.object(main_round, "okx_get", AsyncMock(return_value=[{
                        "poolAddress": POOL, "liquidityUsd": "10000"}])), \
                     patch.object(main_round, "verify_pool", AsyncMock()) as verify, \
                     patch.object(main_round, "broadcast") as broadcast:
                    outcome = await main_round.scan_pools(self.store, MEME, [self.stock], "0x20")
                self.assertEqual(outcome["verifiedPools"], 0)
                self.assertEqual(outcome["unsupportedPools"], 1)
                self.assertEqual((await self.store.get("relation", RELATION_ID))["status"], "invalid")
                self.assertIsNone((await self.store.get("pool", POOL)).get("liquidityUsd"))
                verify.assert_not_awaited()
                broadcast.assert_not_called()

    async def test_reorg_during_rpc_scan_still_blocks_full_write(self):
        await self.store.put("pool", POOL, {"pool": POOL, "classification": "stock-meme"})
        async def verify_during_reorg(pool, stocks):
            await self.store.patch_fact("pool", POOL, {"verificationStatus": "reorged"})
            return self.checked
        with patch.object(main_round, "okx_get", AsyncMock(return_value=[{
                "poolAddress": POOL, "liquidityUsd": "10000"}])), \
             patch.object(main_round, "verify_pool", verify_during_reorg), \
             patch.object(main_round, "broadcast") as broadcast:
            outcome = await main_round.scan_pools(self.store, MEME, [self.stock], "0x20")
        self.assertEqual(outcome["verifiedPools"], 0)
        self.assertIsNone(await self.store.get("relation", RELATION_ID))
        broadcast.assert_not_called()

    async def test_identity_recheck_invalidates_revoked_pool_without_rpc(self):
        await self.store.put("stock", STOCK, self.stock)
        await self.store.put("pool", POOL, {"pool": POOL,
                                             "verificationStatus": "reorged"})
        await self.store.put("relation", RELATION_ID, {
            "id": RELATION_ID, "chainId": "56", "pool": POOL,
            "token": MEME, "stock": STOCK, "stockSide": STOCK,
            "token0": MEME, "token1": STOCK,
            "status": "verified", "level": "A", "liquidityUsd": 5000,
            "historicalDiscovery": True,
        })
        with patch.object(main_round, "store", AsyncMock(return_value=self.store)), \
             patch.object(main_round, "_block_hex", AsyncMock(return_value="0x20")), \
             patch.object(main_round, "verify_pool", AsyncMock()) as verify, \
             patch.object(main_round, "broadcast") as broadcast:
            await main_round.refresh_main_round()
        relation = await self.store.get("relation", RELATION_ID)
        self.assertEqual(relation["status"], "invalid")
        self.assertIsNone(relation["level"])
        self.assertEqual(relation["evidenceStatus"], "verification-block-reorg")
        verify.assert_not_awaited()
        broadcast.assert_not_called()

    async def test_liquidity_refresh_cannot_regrade_revoked_pool(self):
        await self.store.put("pool", POOL, {"pool": POOL,
                                             "verificationStatus": "reorged"})
        relation = {"id": RELATION_ID, "pool": POOL, "token": MEME,
                    "stock": STOCK, "token0": MEME, "token1": STOCK,
                    "status": "invalid", "level": None,
                    "verificationStatus": "reorged"}
        await self.store.put("relation", RELATION_ID, relation)
        with patch.object(main_round, "rpc_call", AsyncMock()) as rpc:
            await main_round._pool_quote(self.store, relation, 32, now_ms())
        self.assertIsNone(await self.store.get("pool-quote", POOL))
        self.assertEqual((await self.store.get("relation", RELATION_ID))["status"], "invalid")
        rpc.assert_not_awaited()
