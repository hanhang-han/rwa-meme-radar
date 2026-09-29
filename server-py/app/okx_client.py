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
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

BASE = "https://web3.okx.com"
USAGE_FILE = "data/okx-usage.json"


def _positive(env: str | None, fallback: int) -> int:
    try:
        v = int(os.environ.get(env, "") or 0)
        return v if v > 0 else fallback
    except ValueError:
        return fallback


def _reserve(env: str, daily: int, fallback: int, ratio: float) -> int:
    raw = os.environ.get(env)
    if raw not in (None, ""):
        return min(daily, _positive(env, fallback))
    return min(fallback, max(0, int(daily * ratio)))


class Quota:
    def __init__(self):
        self.day = ""
        self.daily = 0
        self.round = 0
        self.round_slot: int | None = None
        self.last_persist = 0.0
        self._loaded = False
        self._persist_task: asyncio.Task | None = None
        # daily value at the last persist: the delta-merge ledger adds only
        # this process's new charges to the shared file, never overwrites it.
        self._persisted_daily = 0

    def limits(self):
        daily = _positive("OKX_DAILY_REQUEST_LIMIT", 8000)
        background_reserve = _reserve("OKX_BACKGROUND_RESERVE", daily, 1200, 0.15)
        critical_reserve = min(background_reserve, _reserve("OKX_CRITICAL_RESERVE", daily, 400, 0.05))
        return {
            "daily": daily,
            "round": _positive("OKX_ROUND_REQUEST_LIMIT", 70),
            "backgroundCap": max(0, daily - background_reserve),
            "interactiveCap": max(0, daily - critical_reserve),
            "criticalReserve": critical_reserve,
            "backgroundReserve": background_reserve,
        }

    def rollover(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day != today:
            self.day = today
            self.daily = 0
            self._persisted_daily = 0
            self.round = 0
            self.round_slot = None

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
            self._persisted_daily = self.daily
        except Exception:
            pass

    def sync(self):
        self.rollover()
        if os.environ.get("NODE_ENV") != "test":
            from .request_ledger import shared_usage
            self.daily = shared_usage(self.day)


USAGE = Quota()

# Allowance contexts mirror the Node AsyncLocalStorage budgets.
_allowances = ContextVar("okx_allowances", default=())
_lane = ContextVar("okx_lane", default=None)

@contextmanager
def request_lane(name: str, daily_limit: int):
    token = _lane.set((name, daily_limit))
    try:
        yield
    finally:
        _lane.reset(token)

@contextmanager
def request_allowance(limit: int):
    token = _allowances.set((*_allowances.get(), {"remaining": limit}))
    try:
        yield
    finally:
        _allowances.reset(token)

def _remaining_allowance():
    return min((b["remaining"] for b in _allowances.get()), default=None)


class QuotaExceeded(Exception):
    pass


def charge(skip_round: bool = False, priority: str = "background"):
    USAGE.load()
    USAGE.rollover()
    if not skip_round:
        reset_round()
    limits = USAGE.limits()
    remaining = _remaining_allowance()
    if remaining is not None and remaining <= 0:
        raise QuotaExceeded("OKX network request allowance exhausted")
    caps = {
        "background": limits["backgroundCap"],
        "interactive": limits["interactiveCap"],
        "critical": limits["daily"],
    }
    effective_limit = caps.get(priority, caps["background"])
    if USAGE.daily >= effective_limit or (not skip_round and USAGE.round >= limits["round"]):
        raise QuotaExceeded("OKX local request budget exhausted")
    if os.environ.get("NODE_ENV") == "test":
        USAGE.daily += 1
    else:
        from .request_ledger import shared_usage
        try:
            lane = _lane.get()
            USAGE.daily = shared_usage(USAGE.day, effective_limit, *(lane or (None,None)))
        except RuntimeError as e:
            raise QuotaExceeded(str(e)) from e
    for budget in _allowances.get():
        budget["remaining"] -= 1
    if not skip_round:
        USAGE.round += 1



def reset_round():
    """Open one bounded scan budget per UTC five-minute window.

    Discovery runs every five minutes, but its worker can run for days. A
    process-lifetime counter eventually blocks all scans. The time slot lets
    a later round resume without permitting a second invocation in the same
    slot to refill its 70-request allowance. Daily and lane budgets remain in
    the shared SQLite ledger and are never reset here.
    """
    slot = int(time.time() // 300)
    if USAGE.round_slot is None or slot > USAGE.round_slot:
        USAGE.round_slot = slot
        USAGE.round = 0


_serial_lock = asyncio.Lock()
_throttle = {"next_at": 0.0}


async def okx_get(endpoint: str, params: dict[str, str], opts: dict | None = None):
    path = endpoint + "?" + urlencode(params)
    return await _request(path, "GET", "", opts)


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
    priority = str(opts.get("priority") or "background")

    async def attempt():
        await asyncio.sleep(max(0.0, _throttle["next_at"] - time.time()))
        charge(skip, priority)
        if os.environ.get("NODE_ENV") != "test":
            from .request_ledger import reserve_request_slot
            delay = await asyncio.to_thread(reserve_request_slot, _positive("OKX_REQUEST_INTERVAL_MS",500))
            await asyncio.sleep(delay)
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
