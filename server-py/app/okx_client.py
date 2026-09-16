"""OKX DEX API client: HMAC signing, local quota ledger, throttling and
backoff — a port of the Node okx-client. The round budget guards scan-type
bursts; price-info batches may skip it (skip_round) but never the daily cap."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx

BASE = "https://web3.okx.com"
USAGE_FILE = "data/okx-usage.json"


def _positive(env: str | None, fallback: int) -> int:
    try:
        v = int(os.environ.get(env, "") or 0)
        return v if v > 0 else fallback
    except ValueError:
        return fallback


class Quota:
    def __init__(self):
        self.day = ""
        self.daily = 0
        self.round = 0
        self.last_persist = 0.0
        self._loaded = False
        self._persist_task: asyncio.Task | None = None

    def limits(self):
        return {
            "daily": _positive("OKX_DAILY_REQUEST_LIMIT", 8000),
            "round": _positive("OKX_ROUND_REQUEST_LIMIT", 70),
        }

    def rollover(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day != today:
            self.day = today
            self.daily = 0

    def load(self):
        if self._loaded:
            return
        self._loaded = True
        if os.environ.get("NODE_ENV") == "test" or not os.path.exists(USAGE_FILE):
            return
        try:
            saved = json.load(open(USAGE_FILE))
            self.day = saved.get("day", "")
            self.daily = saved.get("daily", 0)
        except Exception:
            pass

    def persist_soon(self):
        if os.environ.get("NODE_ENV") == "test":
            return

        def write():
            try:
                os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
                tmp = USAGE_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"day": self.day, "daily": self.daily}, f)
                os.replace(tmp, USAGE_FILE)
            except Exception:
                pass

        now = time.time()
        if now - self.last_persist >= 30:
            self.last_persist = now
            write()
            return
        if not self._persist_task or self._persist_task.done():
            async def later():
                await asyncio.sleep(30)
                self.last_persist = time.time()
                write()
            try:
                self._persist_task = asyncio.create_task(later())
            except RuntimeError:
                pass


USAGE = Quota()

# Allowance contexts mirror the Node AsyncLocalStorage budgets.
_allowance_set: set[asyncio.Task] = set()


@contextmanager
def request_allowance(limit: int):
    task = asyncio.current_task()
    token = (task, limit)
    _allowance_set.add(token)
    try:
        yield
    finally:
        _allowance_set.discard(token)


def _remaining_allowance() -> int | None:
    task = asyncio.current_task()
    vals = [limit for (t, limit) in _allowance_set if t is task]
    return min(vals) if vals else None


class QuotaExceeded(Exception):
    pass


def charge(skip_round: bool = False):
    USAGE.load()
    USAGE.rollover()
    limits = USAGE.limits()
    remaining = _remaining_allowance()
    if remaining is not None and remaining <= 0:
        raise QuotaExceeded("OKX network request allowance exhausted")
    if USAGE.daily >= limits["daily"] or (not skip_round and USAGE.round >= limits["round"]):
        raise QuotaExceeded("OKX local request budget exhausted")
    USAGE.daily += 1
    if not skip_round:
        USAGE.round += 1
    USAGE.persist_soon()


def reset_round():
    USAGE.round = 0


_serial_lock = asyncio.Lock()
_throttle = {"next_at": 0.0}


async def okx_get(endpoint: str, params: dict[str, str]):
    path = endpoint + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return await _request(path, "GET", "")


async def okx_post(endpoint: str, data, opts: dict | None = None):
    opts = opts or {}
    body = json.dumps(data, separators=(",", ":"))
    return await _request(endpoint, "POST", body, opts)


async def _request(path: str, method: str, body: str, opts: dict | None = None):
    opts = opts or {}
    key = os.environ.get("OKX_API_KEY")
    secret = os.environ.get("OKX_SECRET_KEY")
    passphrase = os.environ.get("OKX_PASSPHRASE")
    if not (key and secret and passphrase):
        raise RuntimeError("OKX credentials not configured")
    if not path.startswith("/api/v6/dex/"):
        raise RuntimeError("Invalid OKX endpoint")

    skip = bool(opts.get("skip_round"))
    urgent = bool(opts.get("urgent"))

    async def attempt():
        await asyncio.sleep(max(0.0, _throttle["next_at"] - time.time()))
        charge(skip)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        message = (timestamp + method + path.split("?")[0] + (body if method == "POST" else "")).encode()
        if method == "GET" and path.split("?", 1)[1:]:  # signed query string included for GET
            message = (timestamp + method + path).encode()
        signature = base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).decode()
        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": key,
            "OK-ACCESS-PASSPHRASE": passphrase,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-SIGN": signature,
            "User-Agent": "curl/8.7.1",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            if method == "GET":
                resp = await client.get(BASE + path, headers=headers)
            else:
                resp = await client.post(BASE + path, content=body, headers=headers)
        _throttle["next_at"] = time.time() + 0.4
        payload = {}
        try:
            payload = resp.json()
        except Exception:
            pass
        if resp.status_code == 429 or payload.get("code") == "50011":
            raise Retryable()
        if resp.status_code >= 400 or payload.get("code") not in (None, "0"):
            detail = str(payload.get("code", resp.status_code))[:30]
            raise RuntimeError(f"OKX HTTP {resp.status_code} · {detail}")
        return payload.get("data")

    class Retryable(Exception):
        pass

    async def guarded():
        # The serial lock preserves the Node promise-queue semantics except
        # for urgent batched price calls, which bypass it.
        if urgent:
            return await attempt()
        async with _serial_lock:
            return await attempt()

    backoff = 1.0
    for i in range(4):
        try:
            return await guarded()
        except Retryable:
            if i == 3:
                raise RuntimeError("OKX rate limit")
            _throttle["next_at"] = time.time() + backoff
            backoff *= 2
            await asyncio.sleep(backoff)
        except QuotaExceeded:
            raise
