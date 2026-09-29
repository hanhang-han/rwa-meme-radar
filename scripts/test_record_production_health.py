import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import urllib.error


MODULE_PATH = Path(__file__).with_name("record-production-health.py")
SPEC = importlib.util.spec_from_file_location("record_production_health", MODULE_PATH)
health_recorder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health_recorder)


class ProductionHealthRecorderTest(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.timezone.utc)

    def test_local_api_is_summarized_without_raw_errors_or_secret(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "research.sqlite-wal").write_bytes(b"w" * 20)
            releases = root / ".releases"
            releases.mkdir()
            backup = releases / "research-20260928T110000Z.sqlite"
            backup.write_bytes(b"b" * 40)
            health = {
                "status": "limited", "issues": [], "warnings": ["CoinGecko-quota-exhausted"],
                "coverage": {
                    "candidates": {"total": 100, "known": 99, "withinTarget": 95, "missing": 1,
                                   "quoteUnavailable": 1, "quotableTotal": 99, "quotableWithinTarget": 95,
                                   "targetMs": 1800000, "ageP95Ms": 1234},
                    "stocks": {"total": 200, "known": 190, "withinTarget": 185, "missing": 10,
                               "targetMs": 3600000, "ageP95Ms": 4567},
                },
                "collector": {"pid": 123, "alive": True, "ok": True, "ageMs": 20,
                              "tasks": {"baseCandidates": {"status": "waiting", "outcome": "data-accepted",
                                                           "lastSuccessAt": 12345,
                                                           "result": {"accepted": 50, "failed": 0},
                                                           "error": "secret-value"}}},
                "projection": {"pid": 124, "alive": True, "ok": True, "ageMs": 30,
                               "tasks": {"comparisons": {"status": "running", "result": {"observations": 12}}}},
                "sources": [{"id": "okx:dex", "status": "ready", "required": True,
                             "error": "secret-value", "budget": {
                                 "daily": 210, "dailyLimit": 8000, "remaining": 7790,
                                 "lanes": [{"name": "base-candidates", "used": 80, "limit": 1728,
                                            "remaining": 1648}]}},
                            {"id": "coingecko:enrichment", "status": "partial", "required": False}],
                "chainStreams": [{"chainId": "4663", "status": "catching-up", "nearTipLagBlocks": 200,
                                  "historicalGapBlocks": 1300000, "queueDepth": 100,
                                  "historicalScan": {"lastSuccessAt": 1234, "lastRangeRpcMs": 250,
                                                     "lastError": "secret-value"}}],
                "disk": {"freeBytes": 123456789, "freePercent": 41.5},
                "assets": 300, "relations": 10, "latestAssetAgeMs": 100,
                "rawData": "secret-value",
            }
            body = json.dumps(health).encode()
            with patch.object(health_recorder.urllib.request, "urlopen", return_value=io.BytesIO(body)) as urlopen:
                sample = health_recorder.record(root, now=self.now)
            self.assertEqual(urlopen.call_args.args[0].full_url, health_recorder.HEALTH_URL)
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 8)
            self.assertTrue(sample["requestOk"])
            self.assertEqual(sample["coverage"]["candidates"]["withinTarget"], 95)
            self.assertEqual(sample["coverage"]["candidates"]["quoteUnavailable"], 1)
            self.assertEqual(sample["coverage"]["candidates"]["quotableTotal"], 99)
            self.assertEqual(sample["coverage"]["stocks"]["ageP95Ms"], 4567)
            self.assertEqual(sample["processes"]["collector"]["pid"], 123)
            self.assertEqual(sample["tasks"]["baseCandidates"]["accepted"], 50)
            self.assertEqual(sample["chains"][0]["historicalGapBlocks"], 1300000)
            self.assertEqual(sample["chains"][0]["historicalScan"]["lastRangeRpcMs"], 250)
            self.assertEqual(sample["okx"]["lanes"][0]["used"], 80)
            self.assertEqual(sample["disk"]["walBytes"], 20)
            self.assertEqual(sample["disk"]["latestResearchBackup"]["bytes"], 40)
            text = (root / "data" / health_recorder.OUTPUT_NAME).read_text()
            self.assertEqual(len(text.splitlines()), 1)
            self.assertNotIn("secret-value", text)
            self.assertNotIn("rawData", text)

    def test_api_error_is_recorded_without_response_details(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            failure = urllib.error.HTTPError(health_recorder.HEALTH_URL, 503, "secret-value", None, None)
            with patch.object(health_recorder.urllib.request, "urlopen", side_effect=failure):
                sample = health_recorder.record(root, now=self.now)
            self.assertFalse(sample["requestOk"])
            self.assertEqual(sample["error"], {"kind": "http", "status": 503})
            text = (root / "data" / health_recorder.OUTPUT_NAME).read_text()
            self.assertNotIn("secret-value", text)
            self.assertEqual(json.loads(text)["error"]["status"], 503)

    def test_rotation_discards_old_and_caps_at_2880_lines(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            path = data / health_recorder.OUTPUT_NAME
            old = self.now - dt.timedelta(hours=49)
            with path.open("w") as output:
                output.write(json.dumps({"schemaVersion": 1, "at": health_recorder.utc_text(old)}) + "\n")
                for index in range(3000):
                    at = self.now - dt.timedelta(minutes=47 * 60) + dt.timedelta(seconds=index * 50)
                    output.write(json.dumps({"schemaVersion": 1, "at": health_recorder.utc_text(at),
                                             "index": index}) + "\n")
                output.write("{malformed\n")
            sample = health_recorder.record(root, fetcher=lambda: {"status": "healthy"}, now=self.now)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), health_recorder.MAX_SAMPLES)
            self.assertEqual(rows[-1], sample)
            self.assertEqual(rows[0]["index"], 121)
            self.assertTrue(all(row["at"] > health_recorder.utc_text(old) for row in rows))
            self.assertFalse(list(data.glob(".health-observations-*")))


if __name__ == "__main__":
    unittest.main()
