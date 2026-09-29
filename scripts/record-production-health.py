#!/usr/bin/env python3
"""Record one bounded, sanitized production health observation.

Run once per minute from the deployment root. Only the loopback health API and
local file metadata are read; the research database is never opened.
"""

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.request


HEALTH_URL = "http://127.0.0.1:8010/api/health/data"
OUTPUT_NAME = "health-observations.jsonl"
SCHEMA_VERSION = 1
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SAMPLES = 2880
RETENTION = dt.timedelta(hours=48)
TASK_NAMES = (
    "mainRound", "liveQuotes", "baseCandidates", "baseStocks", "discovery",
    "factoryDiscovery", "hotTrades", "binance", "binanceApply", "candles",
    "liquidityRefresh", "comparisonInputs", "comparisons", "baskets",
    "realtimeProjection", "realtimeDerived", "aiInsights", "aiBriefing",
    "maintenance",
)
SAFE_LABEL = re.compile(r"[A-Za-z0-9:._-]{1,96}\Z")


def utc_text(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def number(value):
    """Keep only finite numbers/bools; never copy arbitrary response objects."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not (-float("inf") < value < float("inf")):
        return None
    return value


def label(value):
    return value if isinstance(value, str) and SAFE_LABEL.fullmatch(value) else None


def mapping(value):
    return value if isinstance(value, dict) else {}


def fetch_health():
    request = urllib.request.Request(HEALTH_URL, method="GET", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=8) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("health response too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("health response is not an object")
    return data


def process_summary(value):
    process = mapping(value)
    return {
        "pid": number(process.get("pid")),
        "alive": process.get("alive") is True,
        "ok": process.get("ok") is True,
        "ageMs": number(process.get("ageMs")),
    }


def coverage_summary(value):
    coverage = mapping(value)
    return {
        name: {key: number(mapping(coverage.get(name)).get(key)) for key in
               ("total", "known", "withinTarget", "missing", "quoteUnavailable",
                "quotableTotal", "quotableWithinTarget", "pending", "targetMs", "ageP95Ms")}
        for name in ("candidates", "stocks")
    }


def tasks_summary(health):
    collector = mapping(mapping(health.get("collector")).get("tasks"))
    projection = mapping(mapping(health.get("projection")).get("tasks"))
    tasks = {**collector, **projection}
    result = {}
    for name in TASK_NAMES:
        if name not in tasks:
            continue
        task = mapping(tasks[name])
        counts = mapping(task.get("result"))
        result[name] = {
            "status": label(task.get("status")),
            "outcome": label(task.get("outcome")),
            "startedAt": number(task.get("startedAt")),
            "lastCompletedAt": number(task.get("lastCompletedAt")),
            "lastSuccessAt": number(task.get("lastSuccessAt")),
            "lastDataAt": number(task.get("lastDataAt")),
            "durationMs": number(task.get("durationMs")),
            "accepted": number(counts.get("accepted")),
            "observations": number(counts.get("observations")),
            "failed": number(counts.get("failed")),
            "quotaBlocked": number(counts.get("quotaBlocked")),
        }
    return result


def sources_summary(value):
    if not isinstance(value, list):
        return []
    result = []
    for source in value:
        source = mapping(source)
        source_id = label(source.get("id"))
        if source_id:
            result.append({
                "id": source_id,
                "status": label(source.get("status")),
                "required": source.get("required") is True,
                "updatedAt": number(source.get("updatedAt")),
            })
    return result


def chains_summary(value):
    if not isinstance(value, list):
        return []
    result = []
    for chain in value:
        chain = mapping(chain)
        chain_id = label(chain.get("chainId"))
        if chain_id:
            scan_fields = (
                "lastAttemptAt", "lastCompletedAt", "lastSuccessAt", "lastSuccessfulBlock",
                "lastErrorAt", "lastPageDurationMs", "lastPageRpcMs", "lastPageLogs",
                "lastRangeDurationMs", "lastRangeRpcMs", "lastRangeLogs", "lastRangePages",
            )
            result.append({
                "chainId": chain_id,
                "status": label(chain.get("status")),
                **{key: number(chain.get(key)) for key in (
                    "sourceLagMs", "queueDepth", "decodedEvents", "reconnects",
                    "lastEventAt", "lastReceivedAt", "lastHead", "lastProcessedBlock",
                    "lastNearTipBlock", "nearTipLagBlocks", "historicalGapBlocks",
                    "nearTipVerifiedAt",
                )},
                "historicalScan": {key: number(mapping(chain.get("historicalScan")).get(key)) for key in scan_fields},
                "nearTipScan": {key: number(mapping(chain.get("nearTipScan")).get(key)) for key in scan_fields},
            })
    return result


def okx_summary(sources):
    source = next((item for item in sources if mapping(item).get("id") == "okx:dex"), {})
    budget = mapping(mapping(source).get("budget"))
    lanes = budget.get("lanes") if isinstance(budget.get("lanes"), list) else []
    return {
        "used": number(budget.get("daily")),
        "limit": number(budget.get("dailyLimit")),
        "backgroundCap": number(budget.get("backgroundCap")),
        "interactiveCap": number(budget.get("interactiveCap")),
        "remaining": number(budget.get("remaining")),
        "lanes": [
            {"name": label(lane.get("name")),
             "used": number(lane.get("used")), "limit": number(lane.get("limit")),
             "remaining": number(lane.get("remaining"))}
            for item in lanes if (lane := mapping(item)) and label(lane.get("name"))
        ],
    }


def latest_backup(root: Path):
    directory = root / ".releases"
    candidates = []
    for pattern in ("research-*.sqlite", "before-*.tar.gz.sqlite"):
        for path in directory.glob(pattern):
            if path.is_file() and not path.is_symlink():
                try:
                    info = path.stat()
                    candidates.append((info.st_mtime_ns, path.name, info.st_size))
                except OSError:
                    pass
    if not candidates:
        return None
    mtime_ns, name, size = max(candidates)
    return {
        "name": name,
        "mtime": utc_text(dt.datetime.fromtimestamp(mtime_ns / 1_000_000_000, dt.timezone.utc)),
        "bytes": size,
    }


def local_files_summary(root: Path):
    wal = root / "data" / "research.sqlite-wal"
    try:
        wal_bytes = wal.stat().st_size
    except FileNotFoundError:
        wal_bytes = 0
    except OSError:
        wal_bytes = None
    return {"walBytes": wal_bytes, "latestResearchBackup": latest_backup(root)}


def project_health(health, root: Path, now: dt.datetime):
    sources = health.get("sources") if isinstance(health.get("sources"), list) else []
    disk = mapping(health.get("disk"))
    return {
        "schemaVersion": SCHEMA_VERSION,
        "at": utc_text(now),
        "requestOk": True,
        "status": label(health.get("status")),
        "issues": [safe for item in health.get("issues", []) if (safe := label(item))]
                  if isinstance(health.get("issues"), list) else [],
        "warnings": [safe for item in health.get("warnings", []) if (safe := label(item))]
                    if isinstance(health.get("warnings"), list) else [],
        "coverage": coverage_summary(health.get("coverage")),
        "processes": {name: process_summary(health.get(name)) for name in ("collector", "projection")},
        "tasks": tasks_summary(health),
        "sources": sources_summary(sources),
        "chains": chains_summary(health.get("chainStreams")),
        "okx": okx_summary(sources),
        "disk": {"freeBytes": number(disk.get("freeBytes")),
                 "freePercent": number(disk.get("freePercent")),
                 **local_files_summary(root)},
        "assets": number(health.get("assets")),
        "relations": number(health.get("relations")),
        "latestAssetAgeMs": number(health.get("latestAssetAgeMs")),
    }


def fetch_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return {"kind": "http", "status": number(exc.code)}
    if isinstance(exc, (TimeoutError,)):
        return {"kind": "timeout"}
    if isinstance(exc, (ValueError, UnicodeError, json.JSONDecodeError)):
        return {"kind": "invalid-response"}
    return {"kind": "request-failed"}


def retain_and_write(path: Path, sample: dict, now: dt.datetime):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ".health-observations.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        cutoff = now - RETENTION
        retained = []
        try:
            with path.open("r", encoding="utf-8") as old:
                for line in old:
                    try:
                        record = json.loads(line)
                        if record.get("schemaVersion") != SCHEMA_VERSION:
                            continue
                        stamp = dt.datetime.fromisoformat(record["at"].replace("Z", "+00:00"))
                        if stamp.tzinfo and cutoff <= stamp <= now:
                            retained.append(record)
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
        except FileNotFoundError:
            pass
        retained.append(sample)
        retained = retained[-MAX_SAMPLES:]
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                             prefix=".health-observations-", delete=False) as output:
                temporary = Path(output.name)
                for row in retained:
                    output.write(json.dumps(row, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def record(root: Path, *, fetcher=fetch_health, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        sample = project_health(fetcher(), root, now)
    except Exception as exc:
        sample = {
            "schemaVersion": SCHEMA_VERSION,
            "at": utc_text(now),
            "requestOk": False,
            "error": fetch_error(exc),
            "disk": local_files_summary(root),
        }
    retain_and_write(root / "data" / OUTPUT_NAME, sample, now)
    return sample


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    sample = record(args.root)
    print(f"{sample['at']} {sample.get('status') or sample.get('error', {}).get('kind')}")
    return 0 if sample["requestOk"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
