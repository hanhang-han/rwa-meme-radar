import asyncio
import json
import os
import tempfile
import threading
import unittest

from app.db import ResearchStore
from app.collectors.factory_discovery import (
    FACTORIES, FACTORY_SELECTOR, GET_PAIR_SELECTOR, V2_FACTORY,
    V2_PROOF_POOL, V3_FACTORY, V3_PROOF_POOL, FactoryDiscovery,
    BSC_FACTORIES, BSC_PROOF_TOKENS, GET_POOL_SELECTOR, decode_factory_log,
)


def word(value):
    return "0x" + (value[2:] if value.startswith("0x") else value).rjust(64, "0")


def data(*words):
    return "0x" + "".join(f"{value % 2**256:064x}" for value in words)


TOKEN0 = "0x" + "1" * 40
TOKEN1 = "0x" + "2" * 40
POOL_V2 = "0x" + "3" * 40
POOL_V3 = "0x" + "4" * 40
OFFICIAL_STOCK = "0xe7fd74df6c9c32e34af6774aebb240c990f5008c"


class FakeXLayer:
    def __init__(self, head=14):
        self.head = head
        self.hashes = {n: "0x" + f"{n:064x}" for n in range(0, 30)}
        self.logs = []
        self.calls = []
        self.max_log_span = None
        self.invalid_block = None
        self.pool_tokens = {}

    def header(self, block):
        return {"number": hex(block), "hash": self.hashes[block],
                "timestamp": hex(1_790_000_000 + block * 2)}

    def log(self, factory, block, pool, tx=1, index=0, token0=TOKEN0, token1=TOKEN1):
        self.pool_tokens[pool] = (token0, token1, factory.address)
        base = {"address": factory.address, "topics": [factory.topic, word(token0), word(token1)],
                "blockNumber": hex(block), "blockHash": self.hashes[block],
                "transactionHash": "0x" + f"{tx:064x}", "logIndex": hex(index)}
        if factory.kind == "uniswap_v3":
            return {**base, "topics": base["topics"] + [word("0x" + f"{3000:x}")],
                    "data": data(60, int(pool, 16))}
        return {**base, "data": data(int(pool, 16), 3)}

    async def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return "0xc4"
        if method == "eth_getCode":
            return "0x" + "aa" * 16
        if method == "eth_call":
            call = params[0]
            pool = self.pool_tokens.get(call["to"].lower())
            if pool:
                selectors = {"0x0dfe1681": pool[0], "0xd21220a7": pool[1], FACTORY_SELECTOR: pool[2]}
                if call["data"] in selectors:
                    return word(selectors[call["data"]])
            if call["data"] == "0x95d89b41":
                return "0x" + "MOON".encode().hex().ljust(64, "0")
            if call["data"] == "0x06fdde03":
                return "0x" + "Moon Candidate".encode().hex().ljust(64, "0")
            if call["to"].lower() == V3_PROOF_POOL and call["data"] == FACTORY_SELECTOR:
                return word(V3_FACTORY)
            if call["to"].lower() == V2_PROOF_POOL and call["data"] == FACTORY_SELECTOR:
                return word(V2_FACTORY)
            if call["to"].lower() == V2_FACTORY and call["data"].startswith(GET_PAIR_SELECTOR):
                return word(V2_PROOF_POOL)
            raise AssertionError(call)
        if method == "eth_getBlockByNumber":
            block = self.head if params[0] == "latest" else int(params[0], 16)
            if block == self.invalid_block:
                return None
            return self.header(block) if block <= self.head else None
        if method == "eth_getLogs":
            query = params[0]
            start, end = int(query["fromBlock"], 16), int(query["toBlock"], 16)
            if self.max_log_span and end - start + 1 > self.max_log_span:
                raise RuntimeError(f"block range greater than {self.max_log_span} max")
            return [log for log in self.logs if log["address"].lower() == query["address"].lower()
                    and start <= int(log["blockNumber"], 16) <= end]
        raise AssertionError(method)


class DecodeFactoryTests(unittest.TestCase):
    def test_v2_v3_creation_evidence_and_wrong_factory(self):
        fake = FakeXLayer()
        v2, v3 = FACTORIES[1], FACTORIES[0]
        for factory, pool in ((v2, POOL_V2), (v3, POOL_V3)):
            log = fake.log(factory, 14, pool)
            header = {"block": 14, "hash": fake.hashes[14], "timestamp": 1_790_000_028}
            row = decode_factory_log(factory, log, header, 14, 6, 1234)
            self.assertEqual((row["pool"], row["creationTx"], row["poolCreatedAt"]),
                             (pool, log["transactionHash"], 1_790_000_028_000))
            self.assertEqual(row["confirmationStatus"], "provisional")
            self.assertEqual(row["fee"], 3000 if factory == v3 else None)
            with self.assertRaises(ValueError):
                decode_factory_log(factory, {**log, "address": TOKEN0}, header, 14, 6, 1234)
            with self.assertRaises(ValueError):
                decode_factory_log(factory, {**log, "blockHash": "0x" + "f" * 64}, header, 14, 6, 1234)

    def test_wrong_chain_factory_cannot_be_accepted(self):
        fake = FakeXLayer()
        log = fake.log(BSC_FACTORIES[0], 14, POOL_V3)
        header = {"block": 14, "hash": fake.hashes[14], "timestamp": 1_790_000_028}
        with self.assertRaisesRegex(ValueError, 'factory-not-registered-for-chain'):
            decode_factory_log(BSC_FACTORIES[0], log, header, 14, 6, 1234)
        row = decode_factory_log(BSC_FACTORIES[0], log, header, 14, 6, 1234, '56')
        self.assertEqual(row['chainId'], '56')
        self.assertTrue(row['id'].startswith('56:'))


class FakeBsc(FakeXLayer):
    def __init__(self):
        super().__init__()
        self.bad_token = False

    async def __call__(self, method, params):
        if method == 'eth_chainId':
            return '0x38'
        if method == 'eth_call':
            call = params[0]
            for factory in BSC_FACTORIES:
                if call['to'] == factory.proof_pool:
                    if call['data'] == FACTORY_SELECTOR:
                        return word(factory.address)
                    if call['data'] in ('0x0dfe1681', '0xd21220a7'):
                        return word(TOKEN0 if self.bad_token else BSC_PROOF_TOKENS[call['data'] == '0xd21220a7'])
                if call['to'] == factory.address and call['data'].startswith((GET_PAIR_SELECTOR, GET_POOL_SELECTOR)):
                    return word(factory.proof_pool)
        return await super().__call__(method, params)


class BscFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, 'research.sqlite')
        self.bsc = await ResearchStore(path, '56').connect()
        self.xlayer = await ResearchStore(path, '196').connect()

    async def asyncTearDown(self):
        await self.bsc.close()
        await self.xlayer.close()
        self.tmp.cleanup()

    async def test_independent_cursors_confirmation_and_reorg(self):
        bsc, xlayer = FakeBsc(), FakeXLayer()
        bsc.logs = [bsc.log(BSC_FACTORIES[1], 12, POOL_V2)]
        xlayer.logs = [xlayer.log(FACTORIES[1], 12, POOL_V2)]
        b = FactoryDiscovery(self.bsc, bsc, start_block=10, batch_blocks=3, confirmations=2)
        x = FactoryDiscovery(self.xlayer, xlayer, start_block=10, batch_blocks=3, confirmations=2)
        await b.run_once(max_ranges=2)
        await x.run_once(max_ranges=2)
        self.assertEqual((await b.recent())[0]['chainId'], '56')
        self.assertEqual((await x.recent())[0]['chainId'], '196')
        self.assertNotEqual((await b.recent())[0]['id'], (await x.recent())[0]['id'])
        bsc.hashes[14] = '0x' + 'f' * 64
        await b.run_once(max_ranges=2)
        self.assertEqual((await x._cursor())['hash'], xlayer.hashes[14])
        self.assertEqual((await b._cursor())['hash'], bsc.hashes[14])

    async def test_bsc_identity_mismatch_stops_before_any_log_scan(self):
        rpc = FakeBsc()
        rpc.bad_token = True
        watcher = FactoryDiscovery(self.bsc, rpc, start_block=10)
        with self.assertRaisesRegex(RuntimeError, 'factory-proof-token-mismatch'):
            await watcher.run_once()
        self.assertFalse(watcher.verified)
        self.assertFalse(any(method == 'eth_getLogs' for method, _ in rpc.calls))
        self.assertIsNone(await watcher._cursor())

    async def test_bsc_match_keeps_chain_and_pancake_protocol(self):
        rpc = FakeBsc()
        rpc.logs = [rpc.log(BSC_FACTORIES[1], 12, POOL_V2, token1=OFFICIAL_STOCK)]
        await self.bsc.put('stock', OFFICIAL_STOCK, {
            'tokenContractAddress': OFFICIAL_STOCK, 'stockCode': 'AAFL'})
        watcher = FactoryDiscovery(self.bsc, rpc, start_block=10, batch_blocks=3, confirmations=2)
        await watcher.run_once(max_ranges=2)
        result = await watcher.process_confirmed()
        self.assertEqual(result['accepted'], 1)
        rows = await self.bsc.all('relation')
        self.assertEqual(rows[0]['chainId'], '56')
        self.assertEqual(rows[0]['protocol'], 'PancakeSwap V2')
        self.assertIsNone(rows[0]['level'])  # Liquidity still needs an observation.
        self.assertEqual(await self.xlayer.all('relation'), [])


class DurableFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = await ResearchStore(os.path.join(self.tmp.name, "research.sqlite"), "196").connect()
        self.rpc = FakeXLayer()

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    def collector(self, **kwargs):
        return FactoryDiscovery(self.store, self.rpc, start_block=10, batch_blocks=3,
                                confirmations=2, **kwargs)

    async def test_bounded_backfill_idempotence_real_creation_time_and_confirmation(self):
        self.rpc.logs = [self.rpc.log(FACTORIES[0], 12, POOL_V3),
                         self.rpc.log(FACTORIES[1], 14, POOL_V2, tx=2)]
        watcher = self.collector()
        first = await watcher.run_once(max_ranges=1)
        self.assertEqual((first["cursor"], first["caughtUp"], first["scannedRanges"]), (12, False, 1))
        second = await watcher.run_once(max_ranges=1)
        self.assertEqual((second["cursor"], second["caughtUp"], second["scannedRanges"]), (14, True, 1))
        self.assertEqual(len(first["newEvents"] + second["newEvents"]), 2)
        rows = await watcher.recent()
        self.assertEqual({row["confirmationStatus"] for row in rows}, {"confirmed", "provisional"})
        self.assertEqual(next(row for row in rows if row["pool"] == POOL_V2)["poolCreatedAt"],
                         (1_790_000_000 + 14 * 2) * 1000)
        self.assertEqual((await watcher.run_once())["newEvents"], [])
        self.assertEqual((await self.store.fetchone("SELECT COUNT(*) FROM factory_discovery_events"))[0], 2)
        self.rpc.head = 16
        await watcher.run_once(max_ranges=1)
        self.assertTrue(all(row["confirmationStatus"] == "confirmed" for row in await watcher.recent()))

    async def test_provider_range_limit_adapts_without_skipping(self):
        self.rpc.max_log_span = 2
        self.rpc.logs = [self.rpc.log(FACTORIES[0], 11, POOL_V3)]
        watcher = self.collector()
        result = await watcher.run_once(max_ranges=6)
        self.assertEqual((result["cursor"], result["caughtUp"], len(result["newEvents"])), (14, True, 1))
        cursor = await watcher._cursor()
        self.assertEqual(cursor["coverageFrom"], 10)
        self.assertGreaterEqual(cursor["batchBlocks"], 1)
        self.assertLessEqual(cursor["batchBlocks"], 3)
        spans = [int(params[0]["toBlock"], 16) - int(params[0]["fromBlock"], 16) + 1
                 for method, params in self.rpc.calls if method == "eth_getLogs"]
        self.assertIn(3, spans)  # The provider rejects the first attempt.
        self.assertIn(1, spans)  # The retry covers every block in smaller intervals.

    async def test_metadata_failure_keeps_cursor_and_retry_recovers(self):
        self.rpc.logs = [self.rpc.log(FACTORIES[0], 12, POOL_V3)]
        self.rpc.invalid_block = 12
        watcher = self.collector()
        with self.assertRaises(ValueError):
            await watcher.run_once(max_ranges=1)
        self.assertEqual((await watcher._cursor())["block"], 9)
        self.assertEqual((await self.store.fetchone("SELECT COUNT(*) FROM factory_discovery_events"))[0], 0)
        self.rpc.invalid_block = None
        result = await watcher.run_once(max_ranges=1)
        self.assertEqual((result["cursor"], len(result["newEvents"])), (12, 1))

    async def test_range_commit_does_not_wait_for_shared_connection_reader(self):
        watcher = self.collector()
        await watcher._init()
        cursor = await watcher._bootstrap({"block": 14})
        started, release = threading.Event(), threading.Event()

        def hold_read():
            started.set()
            release.wait(5)
            return 1

        await self.store.db.create_function("hold_read", 0, hold_read)
        reader = asyncio.create_task(self.store.fetchall("SELECT hold_read()"))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            await asyncio.wait_for(watcher._commit_range(
                [], {"block": 12, "hash": self.rpc.hashes[12]}, 10,
                {"block": 14}, cursor, 3), 2)
        finally:
            release.set()
            await reader
        self.assertEqual((await watcher._cursor())["block"], 12)

    async def test_cursor_write_failure_rolls_back_creation_evidence(self):
        watcher = self.collector()
        await watcher._init()
        cursor = await watcher._bootstrap({"block": 14})
        log = self.rpc.log(FACTORIES[0], 12, POOL_V3)
        item = decode_factory_log(FACTORIES[0], log, {
            "block": 12, "hash": self.rpc.hashes[12],
            "timestamp": 1_790_000_024}, 14, 2, 1234)
        await self.store.db.execute("""CREATE TRIGGER reject_factory_cursor
            BEFORE UPDATE ON factory_discovery_cursors
            BEGIN SELECT RAISE(ABORT, 'cursor-write-failed'); END""")
        await self.store.db.commit()
        # A read transaction on the shared connection belongs to its caller;
        # the dedicated writer's failure must not roll it back.
        await self.store.db.execute("BEGIN")
        try:
            with self.assertRaisesRegex(Exception, "cursor-write-failed"):
                await watcher._commit_range([item], {"block": 12, "hash": self.rpc.hashes[12]},
                                            10, {"block": 14}, cursor, 3)
            self.assertTrue(self.store.db.in_transaction)
        finally:
            await self.store.db.rollback()
        self.assertEqual((await watcher._cursor())["block"], 9)
        self.assertEqual((await self.store.fetchone("SELECT COUNT(*) FROM factory_discovery_events"))[0], 0)
        await self.store.db.execute("DROP TRIGGER reject_factory_cursor")
        await self.store.db.commit()
        self.assertEqual(len(await watcher._commit_range(
            [item], {"block": 12, "hash": self.rpc.hashes[12]},
            10, {"block": 14}, cursor, 3)), 1)
        self.assertEqual((await watcher._cursor())["block"], 12)

    async def test_reorg_orphans_old_log_and_replays_canonical_event(self):
        old = self.rpc.log(FACTORIES[1], 14, POOL_V2, tx=2)
        self.rpc.logs = [old]
        watcher = self.collector()
        await watcher.run_once(max_ranges=3)
        self.assertEqual((await watcher._cursor())["block"], 14)
        self.rpc.hashes[14] = "0x" + "f" * 64
        replacement = self.rpc.log(FACTORIES[1], 14, POOL_V3, tx=3)
        self.rpc.logs = [replacement]
        result = await watcher.run_once(max_ranges=1)
        self.assertEqual((result["cursor"], len(result["newEvents"])), (14, 1))
        self.assertEqual([row["pool"] for row in await watcher.recent()], [POOL_V3])
        rows = await self.store.fetchall("SELECT pool,status FROM factory_discovery_events ORDER BY pool")
        self.assertEqual([(r[0], r[1]) for r in rows], [(POOL_V2, "orphaned"), (POOL_V3, "provisional")])

    async def test_deep_reorg_fails_closed(self):
        watcher = self.collector()
        await watcher.run_once(max_ranges=3)
        for block in (9, 12, 14):
            self.rpc.hashes[block] = "0x" + f"{1000 + block:064x}"
        with self.assertRaisesRegex(RuntimeError, "beyond-retained-anchors"):
            await watcher.run_once(max_ranges=1)
        self.assertEqual((await watcher._cursor())["block"], 14)

    async def test_explicit_disable_has_no_rpc_or_db_side_effect(self):
        watcher = self.collector(enabled=False)
        self.assertEqual(await watcher.run_once(), {"status": "disabled", "reason": "factory-discovery-disabled"})
        self.assertEqual(self.rpc.calls, [])
        self.assertIsNone(await self.store.fetchone("SELECT name FROM sqlite_master WHERE name='factory_discovery_events'"))

    async def test_confirmed_official_side_creates_candidate_and_unqualified_relation(self):
        await self.store.put("stock", OFFICIAL_STOCK, {
            "tokenContractAddress": OFFICIAL_STOCK, "stockCode": "AAFL", "chainId": "196"})
        self.rpc.logs = [self.rpc.log(FACTORIES[0], 11, POOL_V3,
                                      token0=TOKEN0, token1=OFFICIAL_STOCK)]
        watcher = self.collector()
        await watcher.run_once(max_ranges=3)
        processed = await watcher.process_confirmed()
        self.assertEqual((processed["accepted"], processed["failed"]), (1, 0))
        asset = await self.store.get("asset", TOKEN0)
        self.assertEqual((asset["kind"], asset["symbol"], asset["discoveryEventPending"]),
                         ("candidate", "MOON", False))
        relation_id = f"196:{POOL_V3}:{TOKEN0}:{OFFICIAL_STOCK}"
        relation = await self.store.get("relation", relation_id)
        self.assertEqual((relation["status"], relation["ticker"], relation["level"]),
                         ("verified", "AAFL", None))
        self.assertEqual(relation["evidenceStatus"], "liquidity-unknown")
        self.assertIsNone(relation.get("liquidityUsd"))
        self.assertEqual((relation["poolCreatedAt"], relation["creationTx"]),
                         ((1_790_000_000 + 11 * 2) * 1000, self.rpc.logs[0]["transactionHash"]))
        self.assertEqual((await watcher.process_confirmed())["requested"], 0)
        self.assertEqual((await self.store.fetchone("SELECT processing_status FROM factory_discovery_events"))[0], "matched")
        self.assertEqual((await self.store.fetchone("SELECT COUNT(*) FROM events"))[0], 0)

    async def test_unrelated_factory_pool_does_not_become_meme_candidate(self):
        self.rpc.logs = [self.rpc.log(FACTORIES[1], 11, POOL_V2)]
        watcher = self.collector()
        await watcher.run_once(max_ranges=3)
        result = await watcher.process_confirmed()
        self.assertEqual((result["accepted"], result["unsupported"]), (0, 1))
        self.assertIsNone(await self.store.get("asset", TOKEN0))
        self.assertEqual((await self.store.fetchone("SELECT processing_status FROM factory_discovery_events"))[0], "irrelevant")

    async def test_official_side_waits_for_catalogue_without_false_relation(self):
        self.rpc.logs = [self.rpc.log(FACTORIES[0], 11, POOL_V3,
                                      token0=TOKEN0, token1=OFFICIAL_STOCK)]
        watcher = self.collector()
        await watcher.run_once(max_ranges=3)
        result = await watcher.process_confirmed()
        self.assertEqual((result["accepted"], result["waitingCatalogue"]), (0, 1))
        self.assertIsNone(await self.store.get("asset", TOKEN0))
        self.assertEqual((await self.store.fetchone("SELECT processing_status FROM factory_discovery_events"))[0], "retry")

    async def test_reorg_retracts_previously_matched_relation(self):
        await self.store.put("stock", OFFICIAL_STOCK, {
            "tokenContractAddress": OFFICIAL_STOCK, "stockCode": "AAFL", "chainId": "196"})
        self.rpc.head = 16
        self.rpc.logs = [self.rpc.log(FACTORIES[0], 14, POOL_V3,
                                      token0=TOKEN0, token1=OFFICIAL_STOCK)]
        watcher = self.collector()
        await watcher.run_once(max_ranges=4)
        self.assertEqual((await watcher.process_confirmed())["accepted"], 1)
        relation_id = f"196:{POOL_V3}:{TOKEN0}:{OFFICIAL_STOCK}"
        for block in (14, 15, 16):
            self.rpc.hashes[block] = "0x" + f"{1000 + block:064x}"
        self.rpc.logs = []
        await watcher.run_once(max_ranges=2)
        repaired = await watcher.process_confirmed()
        self.assertEqual(repaired["updated"], 1)
        relation = await self.store.get("relation", relation_id)
        self.assertEqual((relation["status"], relation["level"], relation["evidenceStatus"]),
                         ("invalid", None, "creation-orphaned"))
