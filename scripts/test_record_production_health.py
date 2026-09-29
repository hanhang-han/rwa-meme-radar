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

    @staticmethod
    def fake_process(proc, pid, command, rss_kb):
        directory = proc / str(pid)
        directory.mkdir()
        (directory / "cmdline").write_bytes(command + b"\0")
        (directory / "status").write_bytes(f"Name:\tsecret-value\nVmRSS:\t{rss_kb} kB\n".encode())

    def test_resources_use_fixed_host_fields_and_known_processes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            pm2 = root / ".pm2" / "pids"
            pressure = proc / "pressure"
            pm2.mkdir(parents=True)
            pressure.mkdir(parents=True)
            (proc / "meminfo").write_bytes(
                b"MemTotal: 8192 kB\nMemAvailable: 2048 kB\n"
                b"SwapTotal: 4096 kB\nSwapFree: 3072 kB\nSecret: secret-value\n")
            (proc / "loadavg").write_bytes(b"1.25 2.50 3.75 1/100 123\n")
            (pressure / "cpu").write_bytes(b"some avg10=10.25 avg60=9.00 avg300=8.00 total=100\n")
            (pressure / "memory").write_bytes(
                b"some avg10=1.50 avg60=2.00 avg300=3.00 total=100\n"
                b"full avg10=0.25 avg60=0.00 avg300=0.00 total=10\n")
            (pressure / "io").write_bytes(b"some avg10=3.00 avg60=2.00 avg300=1.00 total=100\n")
            self.fake_process(proc, 101, b"node\0/opt/memedashboard/src/server.ts", 1000)
            self.fake_process(proc, 102, b"uvicorn\0app.main:app", 2000)
            self.fake_process(proc, 103, b"python\0-m\0app.other", 3000)
            self.fake_process(proc, 104, b"python\0-m\0app.worker", 4000)
            self.fake_process(proc, 105, b"python\0-m\0app.projection_worker", 5000)
            self.fake_process(proc, 106, b"node\0/opt/memedashboard/src/server.ts", 9000)
            for service, pm2_id, pid in (("memedashboard", 0, 106), ("memedashboard", 2, 101),
                                         ("pyradar", 3, 102), ("pyradar-worker", 1, 103)):
                (pm2 / f"{service}-{pm2_id}.pid").write_text(f"{pid}\n")
            health = {"collector": {"pid": 104}, "projection": {"pid": 105}}
            resources = health_recorder.resource_summary(health, proc_root=proc, pm2_home=pm2.parent)
            self.assertEqual(resources["memAvailableBytes"], 2048 * 1024)
            self.assertEqual(resources["swapUsedBytes"], 1024 * 1024)
            self.assertEqual((resources["load1"], resources["load5"], resources["load15"]),
                             (1.25, 2.5, 3.75))
            self.assertEqual(resources["pressureAvg10"], {
                "cpuSome": 10.25, "memorySome": 1.5, "memoryFull": 0.25,
                "ioSome": 3.0, "ioFull": None,
            })
            self.assertEqual(resources["processRssBytes"], {
                "memedashboard": 1000 * 1024,
                "pyradar": 2000 * 1024,
                "pyradar-worker": 4000 * 1024,
                "pyradar-projection": 5000 * 1024,
            })
            self.assertNotIn("secret-value", json.dumps(resources))

    def test_pm2_discovery_caps_numeric_ids_and_skips_untrusted_names(self):
        with TemporaryDirectory() as tmp:
            pids = Path(tmp) / "pids"
            pids.mkdir()
            for pm2_id in range(24):
                (pids / f"pyradar-worker-{pm2_id}.pid").write_text(f"{1000 + pm2_id}\n")
            (pids / "pyradar-worker-not-an-id.pid").write_text("9999\n")
            (pids / "unknown-service-25.pid").write_text("9999\n")
            (pids / "memedashboard-2.pid").symlink_to(pids / "pyradar-worker-2.pid")
            with patch.object(health_recorder, "read_bounded", wraps=health_recorder.read_bounded) as read:
                candidates = health_recorder.pm2_pids(Path(tmp))
            self.assertEqual(candidates["pyradar-worker"], tuple(range(1023, 1007, -1)))
            self.assertEqual(candidates["memedashboard"], ())
            self.assertEqual(candidates["pyradar"], ())
            self.assertEqual(read.call_count, health_recorder.MAX_PM2_PIDS_PER_SERVICE)

    def test_resources_reject_oversized_and_invalid_proc_values(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            pm2 = root / ".pm2" / "pids"
            proc.mkdir()
            pm2.mkdir(parents=True)
            (proc / "meminfo").write_bytes(b"M" * (health_recorder.MAX_PROC_BYTES + 1))
            (proc / "loadavg").write_bytes(b"nan 1e309 -1 1/100 123\n")
            (pm2 / "pyradar-worker-0.pid").write_bytes(b"1" * 33)
            resources = health_recorder.resource_summary(
                {"collector": {"pid": True}, "projection": {"pid": -2}},
                proc_root=proc, pm2_home=pm2.parent)
            self.assertIsNone(resources["memAvailableBytes"])
            self.assertIsNone(resources["swapUsedBytes"])
            self.assertIsNone(resources["load1"])
            self.assertIsNone(resources["load5"])
            self.assertIsNone(resources["load15"])
            self.assertTrue(all(value is None for value in resources["processRssBytes"].values()))

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
            self.assertEqual(set(sample["resources"]["processRssBytes"]),
                             set(health_recorder.SERVICE_MARKERS))
            text = (root / "data" / health_recorder.OUTPUT_NAME).read_text()
            self.assertEqual(len(text.splitlines()), 1)
            self.assertNotIn("secret-value", text)
            self.assertNotIn("rawData", text)

    def test_api_error_is_recorded_without_response_details(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            failure = urllib.error.HTTPError(health_recorder.HEALTH_URL, 503, "secret-value", None, None)
            with patch.object(health_recorder.urllib.request, "urlopen", side_effect=failure):
                sample = health_recorder.record(
                    root, now=self.now, resource_sampler=lambda _: {"memAvailableBytes": 4096})
            self.assertFalse(sample["requestOk"])
            self.assertEqual(sample["error"], {"kind": "http", "status": 503})
            self.assertEqual(sample["resources"], {"memAvailableBytes": 4096})
            text = (root / "data" / health_recorder.OUTPUT_NAME).read_text()
            self.assertNotIn("secret-value", text)
            self.assertEqual(json.loads(text)["error"]["status"], 503)

    def test_resource_read_failure_does_not_hide_a_healthy_api_response(self):
        with TemporaryDirectory() as tmp:
            def failed_resources(_):
                raise OSError("secret-value")

            sample = health_recorder.record(
                Path(tmp), fetcher=lambda: {"status": "healthy"},
                resource_sampler=failed_resources, now=self.now)
            self.assertTrue(sample["requestOk"])
            self.assertEqual(sample["status"], "healthy")
            self.assertIsNone(sample["resources"]["memAvailableBytes"])
            self.assertNotIn("secret-value", json.dumps(sample))

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
