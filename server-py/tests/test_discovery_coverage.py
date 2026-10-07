"""External-index coverage cannot be promoted from a partial provider sample."""
import tempfile
import asyncio
import httpx
import unittest
from unittest.mock import AsyncMock, patch

from app.collectors.discovery_coverage import (
    CHAIN, REPORT_KIND, compare_pairs, discovery_coverage_snapshot,
    normalize_pairs, reconcile_bnb,
)
from app.db import ResearchStore


STOCK = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
POOL = "0x" + "3" * 40
UNKNOWN = "0x" + "4" * 40


def pair(pool=POOL, *, chain="bsc", stock=STOCK, other=OTHER):
    return {"chainId": chain, "pairAddress": pool,
            "baseToken": {"address": stock}, "quoteToken": {"address": other},
            "liquidity": {"usd": 1500}}


class DiscoveryCoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_supported_network_token_absent_from_index_is_not_provider_outage(self):
        from app.collectors.discovery_coverage import _fetch
        transport=httpx.MockTransport(lambda request: httpx.Response(404, json={'errors':['not indexed']}))
        async with httpx.AsyncClient(transport=transport) as client:
            self.assertEqual(await _fetch(client, STOCK, '196'), [])

    async def test_resume_after_cancellation_and_manifest_subject_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory+'/research.sqlite'
            system = await ResearchStore(path, 'system').connect()
            chain = await ResearchStore(path, CHAIN).connect()
            async def select(scope):
                return system if scope == 'system' else chain
            subjects = [f'0x{i:040x}' for i in range(1, 6)]
            calls = []
            async def interrupted(token):
                calls.append(token)
                if token == subjects[1]:
                    raise asyncio.CancelledError()
                return []
            try:
                with patch('app.collectors.discovery_coverage.store', side_effect=select):
                    with self.assertRaises(asyncio.CancelledError):
                        await reconcile_bnb(fetch=interrupted, contracts=subjects, request_spacing=0)
                    self.assertEqual((await system.get('discovery-scan', CHAIN))['cursor'], 1)
                    resumed = AsyncMock(return_value=[])
                    await reconcile_bnb(fetch=resumed, contracts=subjects, max_contracts=2, request_spacing=0)
                    self.assertEqual([c.args[0] for c in resumed.await_args_list], subjects[1:3])
                    self.assertEqual((await system.get(REPORT_KIND, CHAIN))['unqueriedContracts'], 2)
                    restarted = AsyncMock(return_value=[])
                    await reconcile_bnb(fetch=restarted, contracts=subjects+[OTHER], max_contracts=1, request_spacing=0)
                    self.assertEqual(restarted.await_args.args[0], subjects[0])
                    self.assertEqual((await system.get(REPORT_KIND, CHAIN))['checkedContracts'], 1)
            finally:
                await chain.close()
                await system.close()

    def test_pairs_require_exact_chain_and_stock_side(self):
        valid, bad, capped = normalize_pairs([
            pair(), pair(UNKNOWN, chain="ethereum"), pair(UNKNOWN, stock=OTHER, other=UNKNOWN),
            {"pairAddress": UNKNOWN, "baseToken": "bad", "quoteToken": {}},
        ], STOCK)
        self.assertEqual(set(valid), {POOL})
        self.assertEqual(bad, 3)
        self.assertFalse(capped)
        _, _, capped = normalize_pairs([pair()] * 30, STOCK)
        self.assertTrue(capped)

    def test_pool_address_alone_is_not_verification(self):
        indexed, _, _ = normalize_pairs([pair()], STOCK)
        result = compare_pairs(indexed, [{"pool": POOL, "token0": STOCK, "token1": UNKNOWN}])
        self.assertEqual((result["eligible"], result["covered"], result["missing"]), (1, 0, 1))
        result = compare_pairs(indexed, [{"pool": POOL, "token0": STOCK, "token1": OTHER,
                                          "creationStatus": "orphaned"}])
        self.assertEqual(result["missing"], 1)
        result = compare_pairs(indexed, [{"pool": POOL, "token0": STOCK, "token1": OTHER,
                                          "verificationStatus": "reorged"}])
        self.assertEqual(result["missing"], 1)
        result = compare_pairs(indexed, [{"pool": POOL, "token0": OTHER, "token1": STOCK}])
        self.assertEqual(result["covered"], 1)

    async def test_persisted_report_and_honest_partial_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/research.sqlite"
            system = await ResearchStore(path, "system").connect()
            chain = await ResearchStore(path, CHAIN).connect()
            try:
                await chain.put("pool", POOL, {"pool": POOL, "token0": STOCK, "token1": OTHER})
                async def select(scope):
                    return system if scope == "system" else chain
                with patch("app.collectors.discovery_coverage.store", side_effect=select):
                    result = await reconcile_bnb(fetch=AsyncMock(return_value=[pair()]),
                                                 contracts=[STOCK], request_spacing=0,
                                                 now_ms=lambda: 1_000)
                    report = await system.get(REPORT_KIND, CHAIN)
                    self.assertEqual(result["accepted"], 1)
                    self.assertEqual(report["status"], "complete")
                    self.assertEqual((report["eligible"], report["covered"], report["missing"]), (1, 1, 0))
                    health = await discovery_coverage_snapshot(1_001)
                    self.assertEqual(health["chains"][1]["status"], "complete")
                    health = await discovery_coverage_snapshot(1_000 + 37 * 3600_000)
                    self.assertEqual(health["chains"][1]["status"], "stale")

                    await reconcile_bnb(fetch=AsyncMock(return_value=[pair()] * 30),
                                        contracts=[STOCK], request_spacing=0, now_ms=lambda: 2_000)
                    self.assertEqual((await system.get(REPORT_KIND, CHAIN))["status"], "partial")
            finally:
                await chain.close()
                await system.close()

    async def test_provider_outage_stops_without_claiming_empty_full_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/research.sqlite"
            system = await ResearchStore(path, "system").connect()
            chain = await ResearchStore(path, CHAIN).connect()
            try:
                async def select(scope):
                    return system if scope == "system" else chain
                failed = AsyncMock(side_effect=ValueError("provider down"))
                subjects = [f"0x{i:040x}" for i in range(1, 12)]
                with patch("app.collectors.discovery_coverage.store", side_effect=select):
                    await reconcile_bnb(fetch=failed, contracts=subjects,
                                        request_spacing=0, now_ms=lambda: 3_000)
                    report = await system.get(REPORT_KIND, CHAIN)
                self.assertEqual(failed.await_count, 8)
                self.assertEqual(report["status"], "partial")
                self.assertEqual(report["failedContracts"], 8)
                self.assertEqual(report["unqueriedContracts"], 3)
            finally:
                await chain.close()
                await system.close()
