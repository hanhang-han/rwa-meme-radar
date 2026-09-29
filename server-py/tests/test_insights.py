import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import insights
from app.db import ResearchStore


class InsightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = await ResearchStore(self.tmp.name + "/test.sqlite", "196").connect()
        self.token = "0x" + "1" * 40
        await self.db.put("asset", self.token, {
            "token": self.token, "symbol": "TEST", "updatedAt": 1234,
        })
        self.store_patch = patch.object(insights, "store", AsyncMock(return_value=self.db))
        self.store_patch.start()

    async def asyncTearDown(self):
        self.store_patch.stop()
        await self.db.close()
        self.tmp.cleanup()

    async def test_request_only_queues_and_never_calls_model(self):
        with patch.object(insights, "ai_enabled", return_value=True), \
             patch.object(insights, "ai_narrate", AsyncMock()) as model:
            result = await insights.request_insight("196", self.token, "zh")
        self.assertEqual(result["status"], "queued")
        from app.demand_leases import flush_lease_writer
        await flush_lease_writer()
        self.assertIsNotNone(await self.db.get("insight-demand", self.token + ":zh"))
        model.assert_not_awaited()

    async def test_worker_persists_once_and_followup_reads_cache(self):
        now = int(time.time() * 1000)
        await self.db.put("insight-demand", self.token + ":en", {
            "chainId": "196", "address": self.token, "lang": "en",
            "requestedAt": now, "expiresAt": now + 60_000,
        })
        generated = {"text": "Evidence readout", "at": now}
        with patch.object(insights, "CHAINS", ("196",)), \
             patch.object(insights, "ai_enabled", return_value=True), \
             patch.object(insights, "ai_narrate", AsyncMock(return_value=generated)) as model:
            await insights.refresh_insights()
            await insights.refresh_insights()
            result = await insights.request_insight("196", self.token, "en")
        self.assertEqual(model.await_count, 1)
        self.assertEqual(result["text"], "Evidence readout")
        self.assertEqual(result["status"], "ready")
