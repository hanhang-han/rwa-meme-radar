"""Missing indexed pools need chain proof and durable, scoped replay."""
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.db import ResearchStore
from app.collectors.chain_stream import ChainPoolStream
from app.collectors.discovery_coverage import reconcile_bnb
from app.collectors.factory_discovery import BSC_FACTORIES, FACTORY_SELECTOR, GET_PAIR_SELECTOR, GET_POOL_SELECTOR
from app.collectors.pool_gap_repair import KIND, PoolGapRepair, queue_indexed_gap, verify_indexed_pool
from app.stock_identity import token_identity


STOCK = "0x000fe5820d42183fa294454e1c753f4066fcb3b2"
MEME = "0x" + "2" * 40
OTHER = "0x" + "4" * 40
POOL = "0x" + "3" * 40
QUOTE = "0x55d398326f99059ff775485246999027b3197955"


def word(value):
    return "0x" + value[2:].rjust(64, "0")


def uint(value):
    return "0x" + f"{value:064x}"


def abi_text(value):
    return "0x" + value.encode().hex().ljust(64, "0")


def index_pair(pool=POOL, other=MEME, *, dex="pancakeswap"):
    return {"chainId": "bsc", "pairAddress": pool,
            "baseToken": {"address": STOCK}, "quoteToken": {"address": other},
            "liquidity": {"usd": 1200}, "dexId": dex}


def gap(pool=POOL, other=MEME):
    return {"pool": pool, "chainId": "56", "token0": STOCK, "token1": other,
            "stockSide": STOCK, "liquidityUsd": 1200, "dexId": "pancakeswap"}


class FakeProofRpc:
    def __init__(self, *, factory=None, other=MEME, pool=POOL, fee=2500, symbol="MOON"):
        self.factory = factory or BSC_FACTORIES[1]
        self.other, self.pool, self.fee = other, pool, fee
        self.symbol = symbol
        self.calls = []
        self.fail_once = False
        self.revert_pool = False
        self.wrong_getter = False
        self.wrong_side = False
        self.reorg = False
        self.empty_code = False
        self.headers = 0

    async def __call__(self, method, params):
        self.calls.append((method, params))
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError("rpc timeout")
        if method == "eth_chainId":
            return "0x38"
        if method == "eth_getBlockByNumber":
            self.headers += 1
            return {"number": "0x64" if params[0] == "latest" else params[0],
                    "hash": "0x" + ("b" if self.reorg and self.headers > 2 else "a") * 64,
                    "timestamp": "0x60000000"}
        if method == "eth_getCode":
            return "0x" if self.empty_code else "0x" + "60" * 20
        if method == "eth_call":
            call, block = params
            assert block == "0x5e", block  # latest 100, six confirmations
            address, data = call["to"], call["data"]
            if address == self.pool:
                if self.revert_pool and data == "0x0dfe1681":
                    raise RuntimeError("execution reverted")
                if data == "0x0dfe1681":
                    return word(STOCK)
                if data == "0xd21220a7":
                    return word(OTHER if self.wrong_side else self.other)
                if data == FACTORY_SELECTOR:
                    return word(self.factory.address)
                if data == "0xddca3f43":
                    return uint(self.fee)
            if address == self.factory.address and data.startswith((GET_PAIR_SELECTOR, GET_POOL_SELECTOR)):
                return word(OTHER if self.wrong_getter else self.pool)
            if address == self.other and data == "0x95d89b41":
                return abi_text(self.symbol)
            if address == self.other and data == "0x06fdde03":
                return abi_text("Moon token")
            return "0x"
        raise AssertionError((method, params))


class GapRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = self.tmp.name + "/research.sqlite"
        self.chain = await ResearchStore(path, "56").connect()
        self.system = await ResearchStore(path, "system").connect()
        self.clock_ms = [1_000_000]
        await self.chain.put("stock", STOCK, {
            "tokenContractAddress": STOCK,
            "stockCode": token_identity("56", STOCK)["ticker"],
        })

    async def asyncTearDown(self):
        await self.chain.close()
        await self.system.close()
        self.tmp.cleanup()

    async def test_complete_68_candidate_queue_and_idempotent_rescan(self):
        rows = [index_pair("0x" + f"{i+1:040x}") for i in range(68)]
        async def select(scope):
            return self.system if scope == "system" else self.chain
        with patch("app.collectors.discovery_coverage.store", side_effect=select):
            await reconcile_bnb(fetch=AsyncMock(return_value=rows), contracts=[STOCK],
                                request_spacing=0, now_ms=lambda: self.clock_ms[0])
            queued = await self.system.all(KIND)
            self.assertEqual(len(queued), 68)
            report = await self.system.get("discovery-coverage", "56")
            self.assertEqual((report["missing"], report["queuedCandidates"], len(report["samples"])),
                             (68, 68, 12))
            self.assertEqual(report["status"], "partial")  # 30-row index cap
            await self.system.patch_fact(KIND, queued[0]["id"], {"status": "quarantined", "reason": "unsupported-factory"})
            await reconcile_bnb(fetch=AsyncMock(return_value=rows), contracts=[STOCK],
                                request_spacing=0, now_ms=lambda: self.clock_ms[0] + 86_400_000)
            self.assertEqual(len(await self.system.all(KIND)), 68)
            self.assertEqual((await self.system.get(KIND, queued[0]["id"]))["status"], "quarantined")

    async def test_queue_bound_and_expiry_are_reported_without_changing_pool_facts(self):
        old = gap(pool="0x" + "f" * 40)
        await queue_indexed_gap(self.system, old, seen_at=1)
        await self.chain.put("pool", old["pool"], {"pool": old["pool"], "token0": STOCK,
                                                    "token1": MEME, "verifiedAt": 1})
        rows = [index_pair("0x" + f"{i+1:040x}") for i in range(2)]
        async def select(scope):
            return self.system if scope == "system" else self.chain
        with patch("app.collectors.discovery_coverage.store", side_effect=select), \
                patch("app.collectors.discovery_coverage.MAX_QUEUED_CANDIDATES", 1):
            await reconcile_bnb(fetch=AsyncMock(return_value=rows), contracts=[STOCK],
                                request_spacing=0, now_ms=lambda: 100 * 86_400_000)
        report = await self.system.get("discovery-coverage", "56")
        self.assertEqual(len(await self.system.all(KIND)), 1)
        self.assertEqual(report["queueOverflow"], 1)
        self.assertEqual(report["status"], "partial")
        self.assertIsNotNone(await self.chain.get("pool", old["pool"]))

    async def test_v2_stock_meme_only_after_existing_asset_evidence(self):
        rpc = FakeProofRpc()
        row = gap()
        await self.chain.put("asset", MEME, {"token": MEME, "kind": "candidate", "symbol": "MOON",
                                              "price": 0.01, "fieldSources": {"price": "OKX"}})
        await queue_indexed_gap(self.system, row, seen_at=self.clock_ms[0])
        repair = PoolGapRepair(self.chain, self.system, rpc, clock=lambda: self.clock_ms[0])
        self.assertEqual((await repair.run_once())["accepted"], 1)
        pool = await self.chain.get("pool", POOL)
        rel = await self.chain.get("relation", f"56:{POOL}:{MEME}:{STOCK}")
        self.assertEqual(pool["classification"], "stock-meme")
        self.assertEqual((pool["verificationBlock"], pool["streamBackfillFromBlock"]), (94, 93))
        self.assertEqual(pool["verificationBlockHash"], "0x" + "a" * 64)
        self.assertIsNone(pool.get("creationTx"))
        self.assertIsNone(pool.get("poolCreatedAt"))
        self.assertEqual((rel["status"], rel["level"], rel["evidenceStatus"]),
                         ("verified", None, "liquidity-unknown"))
        self.assertFalse(rel["creationAnnouncementPending"])
        self.assertEqual(await repair.run_once(), {"requested": 0, "accepted": 0, "processed": 0,
                                                    "updated": 0, "failed": 0, "unsupported": 0, "skipped": 0})
        self.assertTrue(all(params[-1] == "0x5e" for method, params in rpc.calls if method in ("eth_call", "eth_getCode")))

    async def test_v3_getpool_and_quote_do_not_make_meme_relation(self):
        rpc = FakeProofRpc(factory=BSC_FACTORIES[0], other=QUOTE)
        row = gap(other=QUOTE)
        await queue_indexed_gap(self.system, row, seen_at=self.clock_ms[0])
        result = await PoolGapRepair(self.chain, self.system, rpc, clock=lambda: self.clock_ms[0]).run_once()
        self.assertEqual(result["accepted"], 1)
        self.assertEqual((await self.chain.get("pool", POOL))["classification"], "stock-quote")
        self.assertEqual(await self.chain.all("relation"), [])
        getter = [params[0]["data"] for method, params in rpc.calls
                  if method == "eth_call" and params[0]["to"] == BSC_FACTORIES[0].address]
        self.assertEqual(len(getter), 1)
        self.assertTrue(getter[0].startswith(GET_POOL_SELECTOR))
        self.assertTrue(getter[0].endswith(f"{2500:064x}"))

    async def test_unknown_counter_is_isolated_not_a_relation(self):
        rpc = FakeProofRpc()
        await queue_indexed_gap(self.system, gap(), seen_at=self.clock_ms[0])
        await PoolGapRepair(self.chain, self.system, rpc, clock=lambda: self.clock_ms[0]).run_once()
        pool = await self.chain.get("pool", POOL)
        self.assertEqual(pool["classification"], "stock-unclassified")
        self.assertEqual(await self.chain.all("relation"), [])
        self.assertEqual((await self.chain.get("asset", MEME))["classificationStatus"], "unverified")
        self.assertEqual((await self.system.all(KIND))[0]["status"], "awaiting-classification")
        # Own index metadata cannot classify the token. An independent quote
        # observation can, after which the same pool is re-proven on-chain.
        self.clock_ms[0] += 300_000
        idle = await PoolGapRepair(self.chain, self.system, rpc, clock=lambda: self.clock_ms[0]).run_once()
        self.assertTrue(idle["noChange"])
        self.assertEqual(idle["processed"], 1)
        self.clock_ms[0] += 300_000
        await self.chain.patch_fact("asset", MEME, {"price": 0.01, "fieldSources": {"price": "OKX"}})
        await PoolGapRepair(self.chain, self.system, rpc, clock=lambda: self.clock_ms[0]).run_once()
        self.assertEqual((await self.chain.get("pool", POOL))["classification"], "stock-meme")
        self.assertEqual((await self.system.all(KIND))[0]["status"], "registered")
        self.assertEqual(len(await self.chain.all("relation")), 1)

    async def test_quote_like_symbol_cannot_create_a_meme_relation(self):
        rpc = FakeProofRpc(symbol="USDT")
        await self.chain.put("asset", MEME, {"token": MEME, "kind": "candidate",
                                              "price": 0.01, "fieldSources": {"price": "OKX"}})
        await queue_indexed_gap(self.system, gap(), seen_at=self.clock_ms[0])
        await PoolGapRepair(self.chain, self.system, rpc, clock=lambda: self.clock_ms[0]).run_once()
        self.assertEqual((await self.chain.get("pool", POOL))["classification"], "stock-unclassified")
        self.assertEqual(await self.chain.all("relation"), [])
        self.assertEqual((await self.system.all(KIND))[0]["status"], "needs-review")

    async def test_unsupported_revert_fake_getter_and_reorg_quarantine_or_retry(self):
        for kind in ("unsupported", "revert", "fake", "reorg", "empty"):
            with self.subTest(kind=kind):
                pool = "0x" + f"{len(await self.system.all(KIND))+100:040x}"
                rpc = FakeProofRpc(pool=pool)
                if kind == "unsupported":
                    from app.collectors.factory_discovery import Factory
                    rpc.factory = Factory("0x" + "9" * 40, "flapsh", "0x" + "1" * 64)
                elif kind == "revert":
                    rpc.revert_pool = True
                elif kind == "fake":
                    rpc.wrong_getter = True
                elif kind == "reorg":
                    rpc.reorg = True
                else:
                    rpc.empty_code = True
                row = gap(pool=pool)
                await queue_indexed_gap(self.system, row, seen_at=self.clock_ms[0])
                result = await PoolGapRepair(self.chain, self.system, rpc,
                                             clock=lambda: self.clock_ms[0]).run_once(limit=4)
                state = await self.system.get(KIND, row["pool"] + ":" + STOCK + ":" + MEME)
                if kind in ("reorg", "empty"):
                    self.assertEqual(state["status"], "retry")
                else:
                    self.assertEqual(state["status"], "quarantined")
                self.assertIsNone(await self.chain.get("pool", pool))

    async def test_all_unsupported_candidates_complete_the_review(self):
        from app.collectors.factory_discovery import Factory
        rpc = FakeProofRpc(factory=Factory("0x" + "9" * 40, "flapsh", "0x" + "1" * 64))
        for index in range(2):
            await queue_indexed_gap(self.system, gap(pool="0x" + f"{index + 100:040x}"),
                                    seen_at=self.clock_ms[0])
        outcome = await PoolGapRepair(self.chain, self.system, rpc,
                                      clock=lambda: self.clock_ms[0]).run_once(limit=2)
        self.assertEqual((outcome["requested"], outcome["processed"], outcome["unsupported"]),
                         (2, 2, 2))
        self.assertEqual((outcome["accepted"], outcome["failed"], outcome["noChange"]),
                         (0, 0, True))
        self.assertEqual([row["status"] for row in await self.system.all(KIND)],
                         ["quarantined", "quarantined"])

    async def test_retry_resume_and_bounded_review(self):
        rpc = FakeProofRpc()
        rpc.fail_once = True
        await self.chain.put("asset", MEME, {"token": MEME, "kind": "candidate",
                                              "price": 0.01, "fieldSources": {"price": "OKX"}})
        row = gap()
        await queue_indexed_gap(self.system, row, seen_at=self.clock_ms[0])
        repair = PoolGapRepair(self.chain, self.system, rpc, clock=lambda: self.clock_ms[0])
        self.assertEqual((await repair.run_once())["failed"], 1)
        self.assertEqual((await repair.run_once())["requested"], 0)
        self.clock_ms[0] += 60_000
        self.assertEqual((await repair.run_once())["accepted"], 1)
        self.assertEqual((await self.system.get(KIND, row["pool"] + ":" + STOCK + ":" + MEME))["status"], "registered")

    async def test_persistent_code_unavailable_stops_after_bounded_attempts(self):
        rpc = FakeProofRpc()
        rpc.empty_code = True
        row = gap()
        await queue_indexed_gap(self.system, row, seen_at=self.clock_ms[0])
        repair = PoolGapRepair(self.chain, self.system, rpc, clock=lambda: self.clock_ms[0])
        for _ in range(8):
            self.assertEqual((await repair.run_once())["failed"], 1)
            state = (await self.system.all(KIND))[0]
            self.clock_ms[0] = state["nextRetryAt"] or self.clock_ms[0]
        self.assertEqual((await self.system.all(KIND))[0]["status"], "needs-review")
        self.assertEqual((await repair.run_once())["requested"], 0)
        self.assertIsNone(await self.chain.get("pool", POOL))

    async def test_per_pool_backfill_survives_restart_and_waits_for_subscription(self):
        await self.chain.put("pool", POOL, {"pool": POOL, "token0": STOCK, "token1": MEME,
                                             "streamBackfillFromBlock": 93})
        async def rpc(method, params):
            if method == "eth_getBlockByNumber":
                height = 101 if params[0] == "latest" else int(params[0], 16)
                return {"number": hex(height), "hash": "0x" + f"{height:064x}",
                        "timestamp": "0x60000000"}
            if method == "eth_getLogs":
                calls.append(params[0])
                return []
            raise AssertionError(method)
        calls = []
        first = ChainPoolStream("56")
        first.s = self.chain
        first.rpc = rpc
        first.last_prune = 10**15
        first.wait_for_live = AsyncMock(return_value=True)
        first.retract_orphans = AsyncMock()
        await first.catalogue()
        await first.backfill_pending_pools({"number": "0x65"})
        self.assertEqual(calls[0]["fromBlock"], hex(93))
        self.assertEqual((await self.chain.get("pool-stream-backfill", POOL))["block"], 101)
        self.assertIsNone(await self.chain.get("pool-stream-coverage", POOL))
        second = ChainPoolStream("56")
        second.s = self.chain
        second.rpc = rpc
        second.last_prune = 10**15
        second.wait_for_live = AsyncMock(return_value=True)
        second.retract_orphans = AsyncMock()
        await second.catalogue()
        second.subscribed_pools.add(POOL)
        await second.backfill_pending_pools({"number": "0x65"})
        self.assertEqual((await self.chain.get("pool-stream-coverage", POOL))["fromBlock"], 93)
        self.assertEqual((await self.chain.get("pool-stream-coverage", POOL))["coverage"], "since-verification")
        self.assertEqual(calls, [calls[0]])

    async def test_backfill_rejects_malformed_provider_log_without_advancing_cursor(self):
        await self.chain.put("pool", POOL, {"pool": POOL, "token0": STOCK, "token1": MEME,
                                             "streamBackfillFromBlock": 93})
        async def rpc(method, params):
            if method == "eth_getBlockByNumber":
                height = int(params[0], 16)
                return {"number": params[0], "hash": "0x" + f"{height:064x}"}
            if method == "eth_getLogs":
                return [{"address": OTHER, "blockNumber": "0x5d"}]
            raise AssertionError(method)
        stream = ChainPoolStream("56")
        stream.s = self.chain
        stream.rpc = rpc
        stream.wait_for_live = AsyncMock(return_value=True)
        await stream.catalogue()
        stream.subscribed_pools.add(POOL)
        outcome = await stream.backfill_pending_pools({"number": "0x65"})
        self.assertEqual(outcome["failed"], 1)
        self.assertIsNone((await self.chain.get("pool-stream-backfill", POOL)).get("block"))
        self.assertIsNone(await self.chain.get("pool-stream-coverage", POOL))

    async def test_pool_backfill_due_selection_does_not_starve_later_pool(self):
        low = "0x" + "1" * 40
        high = "0x" + "f" * 40
        for pool in (low, high):
            await self.chain.put("pool", pool, {"pool": pool, "token0": STOCK, "token1": MEME,
                                                 "streamBackfillFromBlock": 93})
        await self.chain.put("pool-stream-backfill", low, {"nextRetryAt": 10**15})
        calls = []
        async def rpc(method, params):
            if method == "eth_getBlockByNumber":
                height = int(params[0], 16)
                return {"number": params[0], "hash": "0x" + f"{height:064x}"}
            if method == "eth_getLogs":
                calls.append(params[0]["address"])
                return []
            raise AssertionError(method)
        stream = ChainPoolStream("56")
        stream.s = self.chain
        stream.rpc = rpc
        stream.wait_for_live = AsyncMock(return_value=True)
        stream.retract_orphans = AsyncMock()
        await stream.catalogue()
        stream.subscribed_pools.add(high)
        await stream.backfill_pending_pools({"number": "0x65"}, limit=1)
        self.assertEqual(calls, [high])
        self.assertIsNotNone(await self.chain.get("pool-stream-coverage", high))
        self.assertIsNone(await self.chain.get("pool-stream-coverage", low))

    async def test_verification_block_reorg_quarantines_pool_and_relation(self):
        candidate = gap()
        await queue_indexed_gap(self.system, candidate, seen_at=self.clock_ms[0])
        ident = (await self.system.all(KIND))[0]["id"]
        await self.system.patch_fact(KIND, ident, {"status": "registered"})
        await self.chain.put("pool", POOL, {
            "pool": POOL, "token0": STOCK, "token1": MEME,
            "streamBackfillFromBlock": 93, "verificationBlock": 94,
            "verificationBlockHash": "0x" + "a" * 64,
            "verificationStatus": "verified", "gapCandidateId": ident,
        })
        relation_id = f"56:{POOL}:{MEME}:{STOCK}"
        await self.chain.put("relation", relation_id, {
            "id": relation_id, "pool": POOL, "status": "verified", "level": "A",
            "token": MEME, "stock": STOCK, "liquidityUsd": 5000,
        })
        async def rpc(method, params):
            self.assertEqual(method, "eth_getBlockByNumber")
            self.assertEqual(params[0], hex(94))
            return {"number": hex(94), "hash": "0x" + "b" * 64}
        stream = ChainPoolStream("56")
        stream.s = self.chain
        stream.rpc = rpc
        with patch("app.collectors.chain_stream.store", AsyncMock(return_value=self.system)):
            await stream.catalogue()
            stream.subscribed_pools.add(POOL)
            outcome = await stream.backfill_pending_pools({"number": "0x65"})
        self.assertEqual(outcome["failed"], 1)
        self.assertEqual((await self.chain.get("pool", POOL))["verificationStatus"], "reorged")
        relation = await self.chain.get("relation", relation_id)
        self.assertEqual((relation["status"], relation["level"]), ("invalid", None))
        self.assertEqual((await self.system.get(KIND, ident))["status"], "retry")
        self.assertIsNone(await self.chain.get("pool-stream-coverage", POOL))
        self.assertTrue(await stream.catalogue())
        self.assertNotIn(POOL, stream.pools)
        self.assertNotIn(POOL, stream.pending_pools)
