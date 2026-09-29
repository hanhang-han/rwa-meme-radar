import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from web3.exceptions import ContractCustomError, ContractLogicError

os.environ["NODE_ENV"] = "test"

from app.collectors import main_round as m
from app.db import ResearchStore


MEME = "0x" + "1" * 40
STOCK = "0x" + "2" * 40
POOL = "0x" + "3" * 40
UNSUPPORTED = "0x" + "4" * 40


def word(address):
    return bytes.fromhex(address[2:].rjust(64, "0"))


class DiscoveryCapabilitiesTest(unittest.IsolatedAsyncioTestCase):
    def test_unsupported_pool_is_coverage_gap_but_rpc_failure_is_task_failure(self):
        self.assertEqual(m.discovery_scan_quality({
            "status": "partial", "unsupportedPools": 4, "failedPools": 0,
        }), (True, 4))
        self.assertEqual(m.discovery_scan_quality({
            "status": "partial", "unsupportedPools": 1, "failedPools": 1,
        }), (False, 1))

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = await ResearchStore(self.temp.name + "/research.sqlite", "196").connect()
        self.chain_handle = m.CHAIN.set("196")

    async def asyncTearDown(self):
        m.CHAIN.reset(self.chain_handle)
        await self.store.close()
        self.temp.cleanup()

    async def test_rpc_preserves_plain_and_hex_only_contract_reverts(self):
        for original in (ContractLogicError("execution reverted"),
                         ContractCustomError("0x800ab12c", data="0x800ab12c")):
            with self.subTest(error=type(original).__name__):
                client = SimpleNamespace(eth=SimpleNamespace(call=Mock(side_effect=original)))
                with patch.object(m, "chain_web3", return_value=client):
                    with self.assertRaises(m.ContractReverted) as raised:
                        await m.rpc_call(MEME, "0x38d52e0f")
                self.assertIsInstance(raised.exception, RuntimeError)
                self.assertIs(raised.exception.__cause__, original)
                self.assertEqual(raised.exception.selector, "0x38d52e0f")
                self.assertEqual(raised.exception.address, MEME)

    async def test_rpc_timeout_is_not_a_contract_revert(self):
        original = TimeoutError("RPC timed out")
        client = SimpleNamespace(eth=SimpleNamespace(call=Mock(side_effect=original)))
        with patch.object(m, "chain_web3", return_value=client):
            with self.assertRaises(RuntimeError) as raised:
                await m.rpc_call(MEME, "0x38d52e0f")
        self.assertNotIsInstance(raised.exception, m.ContractReverted)
        self.assertIsInstance(raised.exception.__cause__, TimeoutError)
        self.assertIn("RPC timed out", str(raised.exception.__cause__))

    async def test_optional_wrapper_custom_reverts_do_not_reject_stock_meme_pair(self):
        async def rpc(address, selector, block="latest"):
            if address == POOL:
                return word(MEME if selector == "0x0dfe1681" else STOCK)
            raise m.ContractReverted("('0x800ab12c', '0x800ab12c')", address=address, selector=selector)

        with patch.object(m, "rpc_call", rpc):
            checked = await m.verify_pool(POOL, [{"tokenContractAddress": STOCK}])
        self.assertEqual(checked["relation"]["token"].lower(), MEME)
        self.assertFalse(checked["relation"]["wrapper"])

    async def test_optional_wrapper_can_resolve_second_method_after_revert(self):
        fetch = AsyncMock(side_effect=[m.ContractReverted("0x800ab12c"), word(STOCK)])
        with patch.object(m, "rpc_call", fetch):
            resolved = await m.resolve_stock(MEME, [{"tokenContractAddress": STOCK}])
        self.assertTrue(resolved["wrapped"])

    async def test_optional_wrapper_network_failure_remains_incomplete(self):
        for error in (RuntimeError("HTTP 403 Forbidden"), RuntimeError("HTTP 429"), TimeoutError("RPC timed out")):
            with self.subTest(error=str(error)):
                fetch = AsyncMock(side_effect=[error, m.ContractReverted("0x800ab12c")])
                with patch.object(m, "rpc_call", fetch):
                    with self.assertRaisesRegex(RuntimeError, "Wrapper identity lookup incomplete"):
                        await m.resolve_stock(MEME, [{"tokenContractAddress": STOCK}])

    async def test_pool_identity_revert_is_classified_but_network_error_is_not(self):
        with patch.object(m, "rpc_call", AsyncMock(side_effect=m.ContractReverted(
                "execution reverted", address=POOL, selector="0x0dfe1681"))):
            with self.assertRaises(m.PoolInterfaceUnsupported) as raised:
                await m.verify_pool(POOL, [])
        self.assertEqual(raised.exception.pool, POOL)
        self.assertEqual(raised.exception.selector, "0x0dfe1681")
        with patch.object(m, "rpc_call", AsyncMock(side_effect=RuntimeError("HTTP 429"))):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429") as raised:
                await m.verify_pool(POOL, [])
        self.assertNotIsInstance(raised.exception, m.PoolInterfaceUnsupported)

    async def test_unsupported_pool_does_not_block_supported_pool_and_keeps_evidence(self):
        async def verify(pool, stocks):
            if pool == UNSUPPORTED:
                raise m.PoolInterfaceUnsupported(pool, "0x0dfe1681")
            return {"token0": MEME, "token1": STOCK, "relation": None}

        rows = [{"poolAddress": UNSUPPORTED, "protocolName": "Nonstandard"}, {"poolAddress": POOL}]
        with patch.object(m, "okx_get", AsyncMock(return_value=rows)), patch.object(m, "verify_pool", verify):
            scan = await m.scan_pools(self.store, MEME, [], "0x1")
        self.assertEqual(scan["status"], "partial")
        self.assertEqual(scan["unsupportedPools"], 1)
        self.assertEqual(scan["failedPools"], 0)
        self.assertEqual(scan["verifiedPools"], 1)
        self.assertIsNotNone(await self.store.get("pool", POOL))
        reason = await self.store.get("pool-error", UNSUPPORTED)
        self.assertEqual(reason["classification"], "unsupported")
        self.assertEqual(reason["reason"], "pool-token-interface-reverted")
        self.assertEqual(reason["selector"], "0x0dfe1681")
        self.assertEqual(reason["queryToken"], MEME)

    async def test_pool_identifier_and_transport_error_have_distinct_persisted_reasons(self):
        pool_id = "0x" + "a" * 64
        rows = [{"poolAddress": pool_id, "protocolName": "PoolManager"}, {"poolAddress": POOL}]
        verify = AsyncMock(side_effect=RuntimeError("RPC timed out"))
        with patch.object(m, "okx_get", AsyncMock(return_value=rows)), patch.object(m, "verify_pool", verify):
            scan = await m.scan_pools(self.store, MEME, [], "0x1")
        verify.assert_awaited_once_with(POOL, [])
        self.assertEqual(scan["unsupportedPools"], 1)
        self.assertEqual(scan["failedPools"], 1)
        self.assertEqual((await self.store.get("pool-error", pool_id))["reason"], "unsupported-pool-reference")
        self.assertEqual((await self.store.get("pool-error", POOL))["classification"], "failed")

    async def test_contract_revert_still_falls_back_from_v2_reserves_to_v3_slot0(self):
        async def rpc(address, selector, block="latest"):
            if selector == "0x0902f1ac":
                raise m.ContractReverted("execution reverted", address=address, selector=selector)
            self.assertEqual(selector, "0x3850c7bd")
            return (2 ** 96).to_bytes(32, "big")

        rel = {"id": "r", "pool": POOL, "token": MEME, "stock": STOCK,
               "token0": MEME, "token1": STOCK}
        with patch.object(m, "rpc_call", rpc), patch.object(m, "cached_decimals", AsyncMock(return_value=18)):
            await m._pool_quote(self.store, rel, 100, 1_000_000)
        quote = await self.store.get("pool-quote", POOL)
        self.assertEqual(quote["method"], "v3-slot0")
        self.assertEqual(quote["memePerStock"], 1)


if __name__ == "__main__":
    unittest.main()
