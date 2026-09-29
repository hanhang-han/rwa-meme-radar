#!/usr/bin/env python3
"""Record one bounded, sanitized production health observation.

Run once per minute from the deployment root. Read the loopback health API,
local file metadata, and small Linux procfs snapshots; never open the research
database.
"""

import argparse
import datetime as dt
import fcntl
import json
import math
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
MAX_PROC_BYTES = 64 * 1024
MAX_PROC_FIELD_BYTES = 4096
MAX_COUNTER = (1 << 63) - 1
MAX_PID = 10_000_000
MAX_PM2_DIRECTORY_ENTRIES = 256
MAX_PM2_PIDS_PER_SERVICE = 16
SERVICE_MARKERS = {
    "memedashboard": b"src/server.ts",
    "pyradar": b"app.main:app",
    "pyradar-worker": b"app.worker",
    "pyradar-projection": b"app.projection_worker",
}


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


def read_bounded(path: Path, limit: int = MAX_PROC_FIELD_BYTES):
    """Return a small procfs/PID-file snapshot, or None if it is unavailable."""
    try:
        with path.open("rb") as source:
            value = source.read(limit + 1)
        return value if len(value) <= limit else None
    except OSError:
        return None


def bounded_integer(value):
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if 0 <= value <= MAX_COUNTER else None


def kilobytes(value):
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return bounded_integer(count * 1024)


def meminfo_summary(proc_root: Path):
    names = (b"MemTotal", b"MemAvailable", b"SwapTotal", b"SwapFree")
    values = {name: None for name in names}
    raw = read_bounded(proc_root / "meminfo", MAX_PROC_BYTES)
    if raw is not None:
        for line in raw.splitlines():
            key, separator, value = line.partition(b":")
            if separator and key in values:
                match = re.fullmatch(rb"\s*([0-9]+)\s+kB\s*", value)
                values[key] = kilobytes(match.group(1)) if match else None
    swap_total, swap_free = values[b"SwapTotal"], values[b"SwapFree"]
    return {
        "memTotalBytes": values[b"MemTotal"],
        "memAvailableBytes": values[b"MemAvailable"],
        "swapTotalBytes": swap_total,
        "swapUsedBytes": swap_total - swap_free
                         if swap_total is not None and swap_free is not None and swap_free <= swap_total else None,
    }


def bounded_float(value, maximum):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return round(result, 3) if math.isfinite(result) and 0 <= result <= maximum else None


def load_summary(proc_root: Path):
    raw = read_bounded(proc_root / "loadavg")
    fields = raw.split() if raw is not None else []
    return {name: bounded_float(fields[index], 1_000_000) if len(fields) > index else None
            for index, name in enumerate(("load1", "load5", "load15"))}


def pressure_values(proc_root: Path, kind: str):
    values = {b"some": None, b"full": None}
    raw = read_bounded(proc_root / "pressure" / kind)
    for line in raw.splitlines() if raw is not None else ():
        category, separator, _ = line.partition(b" ")
        if separator and category in values:
            match = re.search(rb"(?:^|\s)avg10=([^\s]+)", line)
            values[category] = bounded_float(match.group(1), 100) if match else None
    return values


def valid_pid(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_PID else None


def pm2_pids(pm2_home: Path):
    """Find a few numeric PM2 IDs for only the services this recorder knows."""
    paths = {service: [] for service in SERVICE_MARKERS}
    try:
        with os.scandir(pm2_home / "pids") as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_PM2_DIRECTORY_ENTRIES:
                    break
                if not entry.name.endswith(".pid"):
                    continue
                service, separator, pm2_id = entry.name[:-4].rpartition("-")
                if not separator or service not in paths or not re.fullmatch(r"[0-9]{1,6}", pm2_id):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        paths[service].append((int(pm2_id), Path(entry.path)))
                except OSError:
                    continue
    except OSError:
        pass
    result = {}
    for service, matches in paths.items():
        pids = []
        for _, path in sorted(matches, key=lambda item: item[0], reverse=True)[:MAX_PM2_PIDS_PER_SERVICE]:
            raw = read_bounded(path, 32)
            if raw is not None and re.fullmatch(rb"[0-9]{1,8}\s*", raw):
                if (pid := valid_pid(int(raw))) is not None:
                    pids.append(pid)
        result[service] = tuple(pids)
    return result


def process_rss_bytes(proc_root: Path, pid: int, marker: bytes):
    process = proc_root / str(pid)
    command = read_bounded(process / "cmdline", MAX_PROC_FIELD_BYTES)
    if command is None or not any(argument == marker or argument.endswith(b"/" + marker)
                                  for argument in command.split(b"\0")):
        return None
    status = read_bounded(process / "status", MAX_PROC_BYTES)
    if status is None:
        return None
    for line in status.splitlines():
        if line.startswith(b"VmRSS:"):
            match = re.fullmatch(rb"VmRSS:\s*([0-9]+)\s+kB\s*", line)
            return kilobytes(match.group(1)) if match else None
    return None


def resource_summary(health=None, *, proc_root=Path("/proc"), pm2_home=None):
    """Sample only fixed procfs files and the four known PM2 service PIDs."""
    if pm2_home is None:
        pm2_home = Path(os.environ.get("PM2_HOME") or Path.home() / ".pm2")
    health = mapping(health)
    api_pids = {
        "pyradar-worker": valid_pid(mapping(health.get("collector")).get("pid")),
        "pyradar-projection": valid_pid(mapping(health.get("projection")).get("pid")),
    }
    known_pm2_pids = pm2_pids(pm2_home)
    rss = {}
    for service, marker in SERVICE_MARKERS.items():
        rss[service] = None
        for pid in (*known_pm2_pids[service], api_pids.get(service)):
            if pid is not None and (size := process_rss_bytes(proc_root, pid, marker)) is not None:
                rss[service] = size
                break
    cpu_pressure = pressure_values(proc_root, "cpu")
    memory_pressure = pressure_values(proc_root, "memory")
    io_pressure = pressure_values(proc_root, "io")
    return {
        **meminfo_summary(proc_root),
        **load_summary(proc_root),
        "pressureAvg10": {
            "cpuSome": cpu_pressure[b"some"],
            "memorySome": memory_pressure[b"some"],
            "memoryFull": memory_pressure[b"full"],
            "ioSome": io_pressure[b"some"],
            "ioFull": io_pressure[b"full"],
        },
        "processRssBytes": rss,
    }


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


def record(root: Path, *, fetcher=fetch_health, resource_sampler=resource_summary, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    health = {}
    try:
        health = fetcher()
        sample = project_health(health, root, now)
    except Exception as exc:
        sample = {
            "schemaVersion": SCHEMA_VERSION,
            "at": utc_text(now),
            "requestOk": False,
            "error": fetch_error(exc),
            "disk": local_files_summary(root),
        }
    try:
        sample["resources"] = resource_sampler(health)
    except Exception:
        # A local telemetry read must not turn a good API observation into an error.
        sample["resources"] = resource_summary({}, proc_root=Path("/nonexistent"), pm2_home=Path("/nonexistent"))
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
