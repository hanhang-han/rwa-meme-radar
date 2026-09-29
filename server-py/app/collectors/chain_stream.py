"""Read-only V2/V3 pool event streams with durable replay watermarks.

Only previously verified pool identities are subscribed. Native pool prices
never become USD simply because their counter-token looks like a stablecoin.
"""
import asyncio
import json
import math
import os
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from decimal import Decimal, localcontext

import aiosqlite
import httpx
import websockets
from web3 import Web3

from ..db import ResearchStore, store
from ..pool_quotes import pool_ratio

SWAP_V2 = Web3.to_hex(Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)"))
SWAP_V3 = Web3.to_hex(Web3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)"))
SYNC_V2 = Web3.to_hex(Web3.keccak(text="Sync(uint112,uint112)"))
TOPICS = [SWAP_V2, SWAP_V3, SYNC_V2]
FACT_UPSERT = "INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body"
CANDLE_UPSERT = """INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(asset,bar,openTime) DO UPDATE SET open=excluded.open,high=excluded.high,
    low=excluded.low,close=excluded.close,volume=excluded.volume,volumeUsd=excluded.volumeUsd,
    confirmed=excluded.confirmed"""
# The recent lane must be able to reach the tip before another long outage
# makes its backlog grow again. Older blocks remain assigned to the durable
# historical lane when we start a new, explicitly bounded recent interval.
RECENT_REBASE_LAG_BLOCKS = 1800
RECENT_REBASE_DEPTH_BLOCKS = 300
REPLAY_PAUSE_QUEUE_FRACTION = .5
REPLAY_RESUME_QUEUE_FRACTION = .25
# A 4,096-entry queue can represent minutes of lag long before it is half
# full. Give subscribed logs the shared RPC/SQLite budget after a small
# backlog; the unfinished replay range retains its unadvanced cursor.
REPLAY_PAUSE_QUEUE_CAP = 32
REPLAY_RESUME_QUEUE_CAP = 8
WATCHED_POOL_BURST = 8
LIVE_DIAGNOSTIC_WINDOW = 256
_live_log_collector = ContextVar("chain_stream_live_log_collector", default=None)
DEFAULTS = {
    "196": (["wss://ws.xlayer.tech", "wss://xlayerws.okx.com"], "https://xlayerrpc.okx.com"),
    "56": (["wss://bsc-rpc.publicnode.com"], "https://bsc-rpc.publicnode.com"),
    "4663": (["wss://robinhood-rpc.publicnode.com"], "https://robinhood-rpc.publicnode.com"),
}
ENV_NAMES = {"196": "XLAYER", "56": "BSC", "4663": "ROBINHOOD"}
_tasks = []
_collectors = {}


def now_ms():
    return int(time.time() * 1000)


def number(value):
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def address(value):
    value = str(value or "").lower()
    return value if Web3.is_address(value) else None


def words(data, count):
    try:
        raw = bytes.fromhex(str(data).removeprefix("0x"))
    except (ValueError, TypeError):
        return None
    if len(raw) != 32 * count:
        return None
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw), 32)]


def signed(value, bits=256):
    return value - (1 << bits) if value >= (1 << (bits - 1)) else value


def decode_swap(log, token0, base, decimals0, decimals1):
    """Native execution price and quantities; no FX or ticker assumptions."""
    if not all(isinstance(d, int) and 0 <= d <= 36 for d in (decimals0, decimals1)):
        return None
    topics = log.get("topics") or []
    if not topics:
        return None
    topic = str(topics[0]).lower()
    sqrt = None
    if topic == SWAP_V3.lower():
        values = words(log.get("data"), 5)
        if not values:
            return None
        a0, a1 = signed(values[0]), signed(values[1])
        sqrt = values[2]
        # Standard swaps exchange opposite-signed token balances.
        if a0 == 0 or a1 == 0 or (a0 > 0) == (a1 > 0) or sqrt <= 0:
            return None
    elif topic == SWAP_V2.lower():
        values = words(log.get("data"), 4)
        if not values:
            return None
        a0, a1 = values[0] - values[2], values[1] - values[3]
        if a0 == 0 or a1 == 0 or (a0 > 0) == (a1 > 0):
            return None
    else:
        return None
    is0 = str(token0).lower() == str(base).lower()
    base_raw, quote_raw = (a0, a1) if is0 else (a1, a0)
    db, dq = (decimals0, decimals1) if is0 else (decimals1, decimals0)
    with localcontext() as ctx:
        ctx.prec = 90
        size = abs(Decimal(base_raw)) / Decimal(10) ** db
        notional = abs(Decimal(quote_raw)) / Decimal(10) ** dq
        price = notional / size
        result = {"price": float(price), "size": float(size), "quoteVolume": float(notional),
                  "type": "buy" if base_raw < 0 else "sell",
                  "sqrtPriceX96": str(sqrt) if sqrt else None}
    if not all(math.isfinite(result[k]) and result[k] > 0 for k in ("price", "size", "quoteVolume")):
        return None
    return result


def event_key(log):
    return ":".join(str(log.get(k) or "").lower() for k in ("blockHash", "transactionHash", "logIndex"))


class _StreamWriteStore(ResearchStore):
    @asynccontextmanager
    async def _guard_write(self):
        # The shared store shields rollback, which can leave a dedicated
        # rollback running after cancellation releases the global write lock.
        async with self._write_lock:
            try:
                yield
            except BaseException as exc:
                if isinstance(exc, aiosqlite.OperationalError) and 'locked' in str(exc).lower():
                    print('[research-db] locked ' + json.dumps({**self._write_lock.snapshot(),
                        'sqliteErrorCode': getattr(exc, 'sqlite_errorcode', None),
                        'sqliteErrorName': getattr(exc, 'sqlite_errorname', None)}), flush=True)
                if self.db is not None:
                    await self.db.rollback()
                raise


class ChainPoolStream:
    def __init__(self, chain):
        self.chain = str(chain)
        prefix = ENV_NAMES[self.chain]
        urls, rpc = DEFAULTS[self.chain]
        self.urls = [u.strip() for u in os.environ.get(prefix + "_WS_URLS", ",".join(urls)).split(",") if u.strip()]
        self.rpc_url = os.environ.get(prefix + "_STREAM_RPC", rpc)
        # PublicNode HTTP accepts BSC's 300-block / 8-pool replay pages.
        # Robinhood HTTP rejects even 100-block / 8-pool pages (403); its
        # supported 50-block pages cannot keep up with the current catalogue.
        self.http_reads_enabled = (self.chain == "56" or
                                   self.chain == "4663" and os.environ.get(
                                       "ROBINHOOD_STREAM_HTTP_READS", "false").lower() == "true")
        self.http = None
        self.trade_db = None
        self.trade_store = None
        self.status_db = None
        self.status_store = None
        self.status_task = None
        self.status_pending = None
        self.status_lock = asyncio.Lock()
        self.watch_task = None
        self.bar_close_task = None
        self.pools = {}
        self.assets = {}
        self.relations = {}
        self.headers = OrderedDict()
        self.live_header_lookups = deque(maxlen=LIVE_DIAGNOSTIC_WINDOW)
        self.live_log_durations_ms = deque(maxlen=LIVE_DIAGNOSTIC_WINDOW)
        self.decimals = {}
        self.queue = asyncio.Queue(maxsize=4096)
        self.retry_event = None
        self.recovery_needed = False
        self.pending = {}
        self.request_id = 0
        self.lock = asyncio.Lock()
        self.rpc_gate = asyncio.Lock()
        self.rpc_base_interval = .5 if self.chain == "196" else .25
        self.rpc_interval = self.rpc_base_interval
        self.last_rpc = None
        self.rpc_cooldown_until = 0.0
        self.rpc_cooldown_wall = None
        self.rpc_rate_limited_at = None
        self.rpc_rate_failures = 0
        self.rpc_recovery_after = 0.0
        self.last_catalogue = 0
        self.last_status = 0
        self.last_event = 0
        self.last_received = 0
        self.last_source_event = 0
        self.live_processing = False
        self.replay_paused = False
        self.removed_revisions = OrderedDict()
        self.processed = 0
        self.unsupported = 0
        self.reconnects = 0
        self.status = "starting"
        self.latest_head = 0
        self.coverage_from = None
        self.processor = None
        self.s = None
        self.last_prune = 0
        self.last_watches = 0
        self.watched_bars = {}
        self.watched_pools = set()
        self.watched_burst = 0
        self.ws = None
        self.http_chain_verified = False
        self.http_fallback_until = 0.0
        self.pending_pools = set()
        self.scan_page_metrics = {}
        self.last_closed = 0
        self.initialized = False

    async def init(self):
        if self.initialized:
            return
        self.s = await store(self.chain)
        async with self.s._guard_write():
            await self.s.db.executescript("""
                CREATE TABLE IF NOT EXISTS chain_stream_logs (
                  chain TEXT NOT NULL, id TEXT NOT NULL, pool TEXT NOT NULL,
                  block INTEGER NOT NULL, hash TEXT NOT NULL, at INTEGER NOT NULL,
                  body TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(chain,id));
                CREATE INDEX IF NOT EXISTS chain_stream_logs_block ON chain_stream_logs(chain,block);
                CREATE INDEX IF NOT EXISTS chain_stream_logs_retention ON chain_stream_logs(chain,at,processed);
                CREATE INDEX IF NOT EXISTS candles_unconfirmed ON candles(confirmed,openTime);
            """)
            await self.s.db.commit()
        await self.catalogue()
        await self._open_trade_db()
        await self._open_status_db()
        self.initialized = True

    async def _open_trade_db(self):
        """Keep trade transactions off the shared connection's read queue."""
        if self.trade_db is not None or self.s.path == ":memory:":
            return
        connection = await aiosqlite.connect(self.s.path, timeout=15)
        try:
            await connection.execute("PRAGMA busy_timeout=15000")
        except BaseException:
            await connection.close()
            raise
        self.trade_db = connection
        self.trade_store = _StreamWriteStore(self.s.path, self.s.scope,
                                             write_lock=self.s._write_lock,
                                             busy_timeout_ms=self.s.busy_timeout_ms)
        self.trade_store.db = connection

    async def _open_status_db(self):
        """Keep telemetry away from both shared reads and live trade writes."""
        if self.status_db is not None or self.s.path == ":memory:":
            return
        connection = await aiosqlite.connect(self.s.path, timeout=2)
        try:
            await connection.execute("PRAGMA busy_timeout=2000")
        except BaseException:
            await connection.close()
            raise
        self.status_db = connection
        self.status_store = _StreamWriteStore(self.s.path, self.s.scope,
                                              write_lock=self.s._write_lock,
                                              busy_timeout_ms=2000)
        self.status_store.db = connection

    async def close(self):
        task, self.status_task = self.status_task, None
        self.status_pending = None
        watch_task, self.watch_task = self.watch_task, None
        bar_task, self.bar_close_task = self.bar_close_task, None
        tasks = [work for work in (task, watch_task, bar_task) if work is not None]
        for work in tasks:
            work.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        status_connection, self.status_db = self.status_db, None
        self.status_store = None
        connection, self.trade_db = self.trade_db, None
        self.trade_store = None
        self.initialized = False
        if status_connection is not None:
            await status_connection.close()
        if connection is not None:
            await connection.close()

    async def _write_stream_log(self, query, params):
        """Commit one raw-log state change without waiting on shared reads."""
        db = self.trade_db or self.s.db
        guard = self.s._write_lock if db is not self.s.db else self.s._guard_write()
        async with guard:
            try:
                await db.execute(query, params)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def catalogue(self):
        rows = await self.s.all("pool")
        relations = await self.s.all("relation")
        self.relations = {}
        for row in relations:
            self.relations.setdefault(address(row.get("pool")), []).append(row)
        assets = await self.s.all("asset")
        self.assets = {address(a.get("token")): a for a in assets if address(a.get("token"))}
        known = {}
        for row in rows:
            pool, t0, t1 = (address(row.get(k)) for k in ("pool", "token0", "token1"))
            if not pool or not t0 or not t1 or t0 == t1:
                continue
            # The pool record was populated by token0/token1 RPC verification.
            known[pool] = {**row, "pool": pool, "token0": t0, "token1": t1}
        for row in await self.s.all("pool-quote"):
            pool = known.get(address(row.get("pool")))
            if not pool:
                continue
            for side in (0, 1):
                d = row.get("decimals" + str(side))
                if isinstance(d, int) and 0 <= d <= 36:
                    self.decimals[pool["token" + str(side)]] = d
        changed = set(known) != set(self.pools)
        coverage = {key for key, _ in await self.s.all_kv("pool-stream-coverage")}
        self.pending_pools.update(set(known) - coverage)
        self.pools = known
        self.last_catalogue = now_ms()
        registry = {key: body for key, body in await self.s.all_kv("market-registry")}
        for pool in self.pools.values():
            for token in self.bases(pool):
                market = self.market(pool, token)
                if registry.get(market.storage) != market.record():
                    await self.s.put("market-registry", market.storage, market.record())
        return changed

    async def refresh_watches(self):
        from .market_streams import BAR_MS
        if now_ms() - self.last_watches < 5_000:
            return
        watched_bars = {}
        for watch in await (self.status_store or self.s).all("candle-watch"):
            if watch.get("pool") and (watch.get("expiresAt") or 0) > now_ms() and watch.get("bar") in BAR_MS:
                key = (watch["pool"].lower(), watch.get("address", "").lower())
                watched_bars.setdefault(key, set()).add(watch["bar"])
        self.watched_bars = watched_bars
        self.watched_pools = {pool for pool, _ in watched_bars}
        self.last_watches = now_ms()

    def schedule_housekeeping(self):
        """Run slow watch reads and candle closure outside the live loop."""
        if now_ms() - self.last_watches >= 5_000:
            self.watch_task = self._schedule_housekeeping_task(
                self.watch_task, self.refresh_watches, 'watches')
        if now_ms() - self.last_closed >= 5_000:
            self.bar_close_task = self._schedule_housekeeping_task(
                self.bar_close_task, self.close_elapsed_bars, 'bars')

    def _schedule_housekeeping_task(self, task, operation, label):
        if task is not None and not task.done():
            return task

        async def run():
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f'[chain-stream] {label} update failed for {self.chain}: {exc}', flush=True)

        return asyncio.create_task(run())

    def bars(self, market):
        return sorted({"1m", "5m", "1H"} | self.watched_bars.get((market.pool_id, market.token), set()))

    async def rpc(self, method, params):
        # Public-node traffic, including retries and cooldown, shares one gate
        # per chain. Viewer count and the number of pools cannot multiply it.
        import re
        from email.utils import parsedate_to_datetime

        async def budget_slot():
            deadline = max(self.rpc_cooldown_until,
                           self.last_rpc + self.rpc_interval if self.last_rpc is not None else 0)
            while deadline > time.monotonic():
                await asyncio.sleep(deadline - time.monotonic())
            self.last_rpc = time.monotonic()

        async with self.rpc_gate:
            for attempt in range(3):
                if self.rpc_recovery_after and time.monotonic() >= self.rpc_recovery_after:
                    self.rpc_interval = self.rpc_base_interval
                    self.rpc_rate_failures = 0
                    self.rpc_recovery_after = 0.0
                await budget_slot()
                response = None

                async def http_call(call_method, call_params):
                    nonlocal response
                    response = await self.http.post(self.rpc_url, json={
                        "jsonrpc": "2.0", "id": 1, "method": call_method, "params": call_params})
                    response.raise_for_status()
                    body = response.json()
                    if body.get("error") or "result" not in body:
                        detail = body.get("error") or {}
                        raise RuntimeError("RPC " + str(detail.get("code", "invalid-response"))
                                           + ":" + str(detail.get("message") or "")[:180])
                    return body["result"]

                try:
                    # Keep chain identity and subscriptions on WSS. Read RPC
                    # uses HTTP so replay cannot flood the live socket.
                    public_ws = self.ws is not None and self.chain in ("56", "4663")
                    if public_ws and (method == "eth_chainId" or not self.http_reads_enabled
                                      or self.http is None
                                      or time.monotonic() < self.http_fallback_until):
                        result = await self.ws_call(self.ws, method, params)
                    else:
                        try:
                            verify_http = (self.chain in ("56", "4663")
                                           and method != "eth_chainId")
                            if verify_http and not self.http_chain_verified:
                                actual = await http_call("eth_chainId", [])
                                try:
                                    same_chain = number(actual) == int(self.chain)
                                except (TypeError, ValueError):
                                    same_chain = False
                                if not same_chain:
                                    raise ValueError("wrong-http-chain-id")
                                self.http_chain_verified = True
                                await budget_slot()
                            result = await http_call(method, params)
                        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as http_error:
                            # An unavailable or wrong-chain HTTP endpoint must
                            # not stall durable replay. The already validated
                            # live WSS connection is a temporary read fallback.
                            status = (http_error.response.status_code
                                      if isinstance(http_error, httpx.HTTPStatusError) else None)
                            fallback = (isinstance(http_error, httpx.RequestError)
                                        or status in (403, 404, 408)
                                        or (status is not None and 500 <= status < 600)
                                        or str(http_error) == "wrong-http-chain-id")
                            if not public_ws or not fallback:
                                raise
                            self.http_chain_verified = False
                            self.http_fallback_until = time.monotonic() + 60
                            await budget_slot()
                            response = None
                            result = await self.ws_call(self.ws, method, params)
                except Exception as error:
                    limited = (isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429)
                    limited = limited or bool(re.search(
                        r'\brate[\s_-]*limit|\brequests?[\s_-]*limit|\btoo many requests\b|\brate exceeded\b',
                        str(error), flags=re.IGNORECASE))
                    if not limited:
                        raise
                    self.rpc_rate_failures += 1
                    delay = min(60, 2 ** min(self.rpc_rate_failures, 6))
                    retry_after = response.headers.get('Retry-After') if response is not None else None
                    if retry_after:
                        try:
                            seconds = float(retry_after)
                        except ValueError:
                            try:
                                seconds = parsedate_to_datetime(retry_after).timestamp() - time.time()
                            except (ValueError, TypeError, OverflowError):
                                seconds = 0
                        if math.isfinite(seconds):
                            delay = max(delay, seconds)
                    self.rpc_rate_limited_at = now_ms()
                    self.rpc_cooldown_until = time.monotonic() + delay
                    self.rpc_cooldown_wall = self.rpc_rate_limited_at + math.ceil(delay * 1000)
                    self.rpc_interval = min(2, self.rpc_interval * 2)
                    self.rpc_recovery_after = time.monotonic() + 60
                    if attempt == 2:
                        raise
                    continue
                # One accepted response does not prove the original rate is
                # safe. Keep the reduced rate until a full quiet minute passes.
                self.rpc_cooldown_until = 0.0
                self.rpc_cooldown_wall = None
                return result

    async def token_decimals(self, token):
        if token not in self.decimals:
            raw = await self.rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
            result = words(raw, 1)
            if not result or not 0 <= result[0] <= 36:
                raise ValueError("unsupported-token-decimals")
            self.decimals[token] = result[0]
        return self.decimals[token]

    async def block(self, block_hash):
        if block_hash not in self.headers:
            header = await self.rpc("eth_getBlockByHash", [block_hash, False])
            if not header:
                raise ValueError("block-not-available")
            self.remember_header(header)
        else:
            self.headers.move_to_end(block_hash)
        return self.headers[block_hash]

    def remember_header(self, header):
        key = str(header.get("hash") or "").lower()
        if not key:
            return
        self.headers[key] = header
        self.headers.move_to_end(key)
        while len(self.headers) > 8192:
            self.headers.popitem(last=False)
        self.latest_head = max(self.latest_head, number(header["number"]))

    async def status_fact(self, error=None, force=False):
        async with self.status_lock:
            await self._write_status_fact(error, force)

    def schedule_status_fact(self, error=None, force=False):
        """Coalesce status refreshes without delaying a live queue consumer."""
        if not force and now_ms() - self.last_status < 10_000:
            return
        # A reconnect can request a normal snapshot before the preceding
        # failure snapshot starts. Keep the failure so lastError is durable.
        if self.status_pending is None or error is not None or self.status_pending[0] is None:
            self.status_pending = (error, force)
        if self.status_task is None or self.status_task.done():
            self.status_task = asyncio.create_task(self._drain_status_facts())

    async def _drain_status_facts(self):
        while self.status_pending is not None:
            error, force = self.status_pending
            self.status_pending = None
            try:
                await self.status_fact(error=error, force=force)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f'[chain-stream] status update failed for {self.chain}: {exc}', flush=True)

    async def _write_status_fact(self, error=None, force=False):
        if not force and now_ms() - self.last_status < 10_000:
            return
        self.last_status = now_ms()
        telemetry = self.status_store or self.s
        checkpoint = await telemetry.get("chain-stream-cursor", "pools") or {}
        near_tip = await telemetry.get("chain-stream-cursor", "pools-live") or {}
        historical_scan = await telemetry.get("chain-stream-scan", "pools") or {}
        near_tip_scan = await telemetry.get("chain-stream-scan", "pools-live") or {}
        previous_status = await telemetry.get("chain-stream", "pools") or {}
        historical_block = checkpoint.get("block")
        near_tip_block = near_tip.get("block")
        near_tip_from = near_tip.get("coverageFrom")
        last_error = type(error).__name__ + ":" + str(error)[:180] if isinstance(error, Exception) else error
        header_hits = sum(self.live_header_lookups)
        durations = sorted(self.live_log_durations_ms)
        await telemetry.put("chain-stream", "pools", {
            "chainId": self.chain, "provider": "Chain RPC", "status": self.status,
            "poolCount": len(self.pools), "decodedEvents": self.processed,
            "unsupportedEvents": self.unsupported, "lastEventAt": self.last_event or None,
            "lastProcessedAt": self.last_event or None, "lastReceivedAt": self.last_received or None,
            "lastSourceEventAt": self.last_source_event or None,
            "sourceLagMs": max(0, now_ms() - self.last_source_event) if self.last_source_event else None,
            "queueDepth": self.queue.qsize(), "liveProcessing": self.live_processing,
            "liveHeaderCacheHitsRecent": header_hits,
            "liveHeaderRpcMissesRecent": len(self.live_header_lookups) - header_hits,
            "liveLogProcessingSamplesRecent": len(durations),
            "liveLogProcessingLastMs": (self.live_log_durations_ms[-1]
                                        if self.live_log_durations_ms else None),
            "liveLogProcessingP95Ms": (durations[math.ceil(.95 * len(durations)) - 1]
                                       if durations else None),
            "replayPausedForLive": self.replay_under_pressure(),
            "rateLimitedAt": self.rpc_rate_limited_at, "cooldownUntil": self.rpc_cooldown_wall,
            "rpcIntervalMs": round(self.rpc_interval * 1000),
            "lastHead": self.latest_head, "lastProcessedBlock": historical_block,
            # The original cursor is the contiguous historical watermark.
            # Recent verification is a separate lane and never asserts that
            # the intervening historical interval has been scanned.
            "lastNearTipBlock": near_tip_block,
            "nearTipCoverageFrom": near_tip_from,
            "nearTipVerifiedAt": near_tip.get("updatedAt"),
            "nearTipRebasedAt": near_tip.get("rebasedAt"),
            "nearTipRebasedFromBlock": near_tip.get("rebasedFromBlock"),
            "nearTipLagBlocks": (max(0, self.latest_head - near_tip_block)
                                 if isinstance(near_tip_block, int) else None),
            # Only the interval before the recent lane's observed start is
            # unscanned. Its own scanned blocks and the tail lag are separate.
            "historicalGapBlocks": (max(0, near_tip_from - historical_block - 1)
                                    if isinstance(historical_block, int) and isinstance(near_tip_from, int)
                                    else None),
            "historicalScan": historical_scan, "nearTipScan": near_tip_scan,
            "coverageFrom": checkpoint.get("coverageFrom", self.coverage_from),
            "updatedAt": now_ms(), "error": error,
            "lastError": last_error or previous_status.get("lastError"),
            "lastErrorAt": now_ms() if error else previous_status.get("lastErrorAt"),
            "reconnects": self.reconnects, "protocols": ["v2-swap", "v3-swap", "v2-sync"],
        })

    def market(self, pool, token):
        from .market_streams import Market
        other = pool["token1"] if token == pool["token0"] else pool["token0"]
        symbol = (self.assets.get(other) or {}).get("symbol")
        if not symbol:
            for item in pool.get("amounts") or []:
                if address(item.get("tokenContractAddress")) == other:
                    symbol = item.get("tokenSymbol")
                    break
        return Market(chain_id=self.chain, token=token, venue="dex", market_id=pool["pool"],
                      quote_currency=symbol or other, base_symbol=(self.assets.get(token) or {}).get("symbol") or token[:8],
                      kind="pool", pool_id=pool["pool"], quote_token=other)

    def bases(self, pool):
        # Both known sides can have their own independent chart; never combine pools.
        result = {t for t in (pool["token0"], pool["token1"]) if t in self.assets}
        query = address(pool.get("queryToken"))
        if query in (pool["token0"], pool["token1"]):
            result.add(query)
        for rel in self.relations.get(pool["pool"], []):
            if rel.get("token") in (pool["token0"], pool["token1"]):
                result.add(rel["token"])
        return sorted(result)

    def irrelevant_sync(self, log):
        """A V2 Sync without a known relation cannot update user-facing data.

        Keep removed logs: any removed event can signal that swaps from the
        same block need to be retracted.
        """
        topics = log.get("topics") or []
        return (not log.get("removed") and bool(topics)
                and str(topics[0]).lower() == SYNC_V2.lower()
                and not self.relations.get(address(log.get("address"))))

    async def wait_for_live(self, max_wait=.25):
        """Give a shallow live queue a bounded head start over replay.

        A permanently nonempty but shallow queue should not stop history from
        progressing. A high queue is handled by replay_under_pressure, which
        pauses the range without advancing its cursor.
        """
        deadline = time.monotonic() + max_wait
        while not self.queue.empty() or self.live_processing:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await asyncio.sleep(0)
                return False
            await asyncio.sleep(min(.05, remaining))
        return True

    def replay_under_pressure(self):
        """Leave the shared RPC and SQLite budget to live logs until backlog drains."""
        capacity = self.queue.maxsize
        if capacity <= 0:
            self.replay_paused = False
            return False
        pending = self.queue.qsize() + int(self.retry_event is not None) + int(self.live_processing)
        pause_at = max(1, min(int(capacity * REPLAY_PAUSE_QUEUE_FRACTION),
                              REPLAY_PAUSE_QUEUE_CAP))
        resume_above = max(1, min(int(capacity * REPLAY_RESUME_QUEUE_FRACTION),
                                  REPLAY_RESUME_QUEUE_CAP))
        if self.replay_paused:
            self.replay_paused = pending > resume_above
        else:
            self.replay_paused = pending >= pause_at
        return self.replay_paused

    async def log_header(self, log):
        """Public nodes can publish a log before hash lookup is indexed.

        Retry the same block briefly without restarting the subscription. A
        canonical height with another hash identifies a reverted queued log.
        """
        block_hash = str(log['blockHash']).lower()
        if _live_log_collector.get() is self:
            self.live_header_lookups.append(block_hash in self.headers)
        try:
            return await self.block(block_hash)
        except ValueError as error:
            if str(error) != 'block-not-available':
                raise
        for delay in (.2, .5, 1):
            await asyncio.sleep(delay)
            if block_hash in self.headers:
                self.headers.move_to_end(block_hash)
                return self.headers[block_hash]
            header = await self.rpc('eth_getBlockByNumber', [log['blockNumber'], False])
            if not header:
                continue
            if header.get('hash', '').lower() != block_hash:
                async with self.lock:
                    await self.retract_block(block_hash)
                return None
            self.remember_header(header)
            return header
        raise ValueError('block-not-available')

    async def process_log(self, log):
        pool = self.pools.get(address(log.get("address")))
        if not pool:
            return
        ident = event_key(log)
        block_hash = str(log.get("blockHash") or "").lower()
        if not block_hash or not log.get("transactionHash"):
            return
        if log.get("removed"):
            async with self.lock:
                await self.retract_block(block_hash)
            return
        if self.irrelevant_sync(log):
            return
        revision = self.removed_revisions.get(block_hash, 0)
        existing = await (self.trade_store or self.s).fetchone(
            "SELECT processed FROM chain_stream_logs WHERE chain=? AND id=?", (self.chain, ident))
        if existing and existing[0]:
            return
        # Public-node metadata may take seconds. It must not monopolize the
        # per-chain mutation lock while live logs or a removal are waiting.
        header = await self.log_header(log)
        if header is None:
            return
        at, height = number(header["timestamp"]) * 1000, number(log["blockNumber"])
        if revision:
            # A previously removed block can be accepted again only if it
            # really rejoined the canonical chain; old queued logs are ignored.
            canonical = await self.rpc("eth_getBlockByNumber", [hex(height), False])
            if not canonical or canonical.get("hash", "").lower() != block_hash:
                return
        unsupported = False
        try:
            d0, d1 = await asyncio.gather(self.token_decimals(pool["token0"]), self.token_decimals(pool["token1"]))
        except ValueError as error:
            if str(error) != "unsupported-token-decimals":
                raise
            unsupported = True
        async with self.lock:
            # Another consumer may have committed or removed this event while
            # the metadata awaits ran. Never restore an in-flight orphan.
            if self.removed_revisions.get(block_hash, 0) != revision:
                return
            existing = await (self.trade_store or self.s).fetchone(
                "SELECT processed FROM chain_stream_logs WHERE chain=? AND id=?", (self.chain, ident))
            if existing and existing[0]:
                return
            if unsupported:
                # A permanently unsupported ABI must not hold back every
                # other pool on this chain. Transport errors remain retryable.
                await self._write_stream_log("INSERT OR REPLACE INTO chain_stream_logs VALUES (?,?,?,?,?,?,?,1)",
                    (self.chain, ident, pool["pool"], height, block_hash, at, json.dumps(log)))
                self.unsupported += 1
                return
            # Store unprocessed input first; a crash is safely retried by the watermark.
            await self._write_stream_log("INSERT OR IGNORE INTO chain_stream_logs VALUES (?,?,?,?,?,?,?,0)",
                (self.chain, ident, pool["pool"], height, block_hash, at, json.dumps(log)))
            topic = str((log.get("topics") or [""])[0]).lower()
            reserves = words(log.get("data"), 2) if topic == SYNC_V2.lower() else None
            sqrt = None
            accepted = False
            for token in self.bases(pool):
                decoded = decode_swap(log, pool["token0"], token, d0, d1)
                if not decoded:
                    continue
                market = self.market(pool, token)
                sqrt = int(decoded["sqrtPriceX96"]) if decoded["sqrtPriceX96"] else None
                await self.commit_trade(market, {
                    **decoded, "quantity": decoded["size"], "quoteQuantity": decoded["quoteVolume"],
                    "id": "chain:" + self.chain + ":" + ident, "t": at,
                    "hash": log["transactionHash"], "blockHash": block_hash, "blockNumber": height,
                    "transactionIndex": number(log.get("transactionIndex", "0x0")),
                    "logIndex": number(log.get("logIndex", "0x0")), "pool": pool["pool"],
                    "source": "Chain RPC", "finality": "provisional", "provider": "Chain RPC",
                    "receivedAt": log.get('_receivedAt') or now_ms(),
                })
                await self.pool_asset_quote(pool, token, decoded, at, height, ident)
                accepted = True
            if reserves or sqrt:
                for rel in self.relations.get(pool["pool"], []):
                    ratio = pool_ratio(pool["token0"], rel["token"], d0, d1,
                                       reserves=reserves, sqrt_price_x96=sqrt)
                    old = await (self.trade_store or self.s).get("pool-quote", pool["pool"]) or {}
                    order = [height, number(log.get("logIndex", "0x0"))]
                    old_order = old.get("eventOrder") or [old.get("block") or 0, -1]
                    if ratio and at >= (old.get("at") or 0) and order >= old_order:
                        await (self.trade_store or self.s).put("pool-quote", pool["pool"], {
                            "chainId": self.chain, "pool": pool["pool"], "token": rel["token"],
                            "stockSide": rel.get("stockSide") or rel.get("stock"), "memePerStock": ratio,
                            "at": at, "block": height, "blockHash": block_hash, "timeKind": "market",
                            "method": "v2-sync-stream" if reserves else "v3-swap-stream",
                            "decimals0": d0, "decimals1": d1, "finality": "provisional",
                            "eventOrder": order,
                        })
                if reserves:
                    await self.reserve_valuation(pool, reserves, d0, d1, at, height, block_hash,
                                                 number(log.get("logIndex", "0x0")))
                accepted = True
            if not accepted:
                self.unsupported += 1
            await self._write_stream_log("UPDATE chain_stream_logs SET processed=1 WHERE chain=? AND id=?",
                                         (self.chain, ident))
            self.processed += 1
            self.last_event = now_ms()
            self.last_source_event = max(self.last_source_event, at)

    async def pool_asset_quote(self, pool, token, decoded, at, height, ident):
        other = pool["token1"] if token == pool["token0"] else pool["token0"]
        base = await (self.trade_store or self.s).get("asset", token)
        anchor = await (self.trade_store or self.s).get("asset", other) or {}
        if not base:
            return
        # Stable, largest verified pool selection prevents thin pools randomly
        # replacing a token's headline quote from one trade to the next.
        candidates = [p for p in self.pools.values() if token in (p["token0"], p["token1"])]
        chosen = max(candidates, key=lambda p: (p.get("liquidityUsd") or 0, p["pool"]), default=None)
        if not chosen or chosen["pool"] != pool["pool"]:
            return
        if at < (base.get("fieldTimes") or {}).get("price", 0):
            return
        previous = base.get("priceProvenance") or {}
        event_order = [height, number(ident.split(":")[-1])]
        if previous.get("eventOrder") and tuple(previous["eventOrder"]) > tuple(event_order):
            return
        anchor_at = (anchor.get("fieldTimes") or {}).get("price") or 0
        anchor_price = anchor.get("price")
        provenance = anchor.get("priceProvenance") or {}
        source = (anchor.get("fieldSources") or {}).get("price") or anchor.get("provider")
        if (not isinstance(anchor_price, (int, float)) or not math.isfinite(anchor_price) or anchor_price <= 0
                or not 0 <= at - anchor_at <= 900_000
                or provenance.get("pool") == pool["pool"]
                or source == "Chain RPC" or anchor.get("priceCurrency", "USD") != "USD"):
            return
        price = decoded["price"] * anchor_price
        if not math.isfinite(price) or price <= 0:
            return
        # The price is a current native execution multiplied by an explicitly
        # dated USD anchor; never label its conversion as an independent quote.
        await (self.trade_store or self.s).merge_asset_observation(token, {
            "priceCurrency": "USD", "priceProvenance": {
                "provider": "Chain RPC", "venue": "dex", "scope": "pool", "pool": pool["pool"],
                "quoteType": "pool-derived", "quoteToken": other, "quoteAt": anchor_at,
                "timeKind": "market", "block": height, "eventId": ident, "eventOrder": event_order,
                "method": "execution-times-dated-usd-anchor"},
            "fieldSources": {"price": "Chain RPC"},
            "fieldScopes": {"price": "pool:" + pool["pool"]},
            "fieldTimeKinds": {"price": "market"},
            "fieldObservations": {"price": {
                "provider": "Chain RPC", "scope": "pool", "venue": "dex", "currency": "USD",
                "marketAt": at, "receivedAt": now_ms(), "observedAt": at, "timeKind": "market",
                "conversionAt": anchor_at, "pool": pool["pool"], "independent": False,
                "quoteType": "pool-derived", "verified": True}},
        }, {"price": (price, at)}, sample=(price, base.get("marketCap"), at))

    async def reserve_valuation(self, pool, reserves, d0, d1, at, height, block_hash=None, log_index=0):
        values, times = [], []
        for token, raw, decimals in zip((pool["token0"], pool["token1"]), reserves, (d0, d1)):
            row = await (self.trade_store or self.s).get("asset", token) or {}
            stamp = (row.get("fieldTimes") or {}).get("price") or 0
            price = row.get("price")
            if not isinstance(price, (int, float)) or price <= 0 or not 0 <= at - stamp <= 900_000 or row.get("priceCurrency", "USD") != "USD":
                return
            values.append(float(Decimal(raw) / Decimal(10) ** decimals) * price)
            times.append(stamp)
        total = sum(values)
        if not math.isfinite(total) or not 0 <= total < 1e10:
            return
        for rel in self.relations.get(pool["pool"], []):
            current = await (self.trade_store or self.s).get("relation", rel["id"]) or {}
            order = [height, log_index]
            if at < (current.get("reservesAt") or 0) or order < (current.get("valuationOrder") or [current.get("valuationBlock") or 0, -1]):
                continue
            await (self.trade_store or self.s).patch_fact("relation", rel["id"], {
                "liquidityUsd": total, "liquidityAt": min(at, *times),
                "reservesAt": at, "priceAt": min(times), "valuationAt": now_ms(),
                "liquidityMethod": "streamed-v2-two-sided-reserves", "valuationBlock": height,
                "valuationBlockHash": block_hash,
                "valuationOrder": order,
            })

    async def commit_trade(self, market, trade):
        from .market_streams import BAR_MS, trade_to_candle
        from ..realtime_schema import enqueue_events
        s = self.s
        # Direct unit-test collectors may not have run init(). Production uses
        # a dedicated connection so unrelated reads cannot queue inside this
        # global-write-lock transaction.
        db = self.trade_db or s.db
        # _guard_write rolls back s.db on failure. A dedicated transaction must
        # hold the same lock without touching an unrelated shared transaction.
        write_guard = s._write_lock if db is not s.db else s._guard_write()
        received = trade.get('receivedAt') or now_ms()
        async with write_guard:
            stamp = now_ms()
            market_frame = market.frame()
            body = {**market_frame, **trade, "provider": "Chain RPC", "source": "Chain RPC",
                    "receivedAt": received, "sourceEventAt": trade["t"], "persistedAt": stamp,
                    "volume": trade["quoteQuantity"] if market.quote_currency == "USD" else None}
            try:
                await db.execute("BEGIN IMMEDIATE")
                insert = await db.execute("INSERT OR IGNORE INTO trades VALUES (?,?,?,?)",
                    (s.key(market.storage), trade["id"], trade["t"], json.dumps(body)))
                if not insert.rowcount:
                    await db.rollback()
                    return False
                fact_rows = [self._fact_params("market-registry", market.storage, market.record())]
                candle_rows = []
                events = []
                bars = self.bars(market)
                bar_keys = {}
                for bar in bars:
                    width = BAR_MS[bar]
                    offset = 4 * 86400000 if width == 604800000 else 0
                    opened = (trade["t"] - offset) // width * width + offset
                    bar_keys[bar] = (opened, market.storage + ":" + bar + ":" + str(opened))

                selectors = {
                    "pool-candle-acc": [bar_keys[bar][1] for bar in bars],
                    "market-candle": [market.storage + ":" + bar for bar in bars],
                    "candle-meta": [market.candle_key(bar) for bar in bars],
                }
                where, args = [], []
                for kind, keys in selectors.items():
                    where.append("(kind=? AND id IN (" + ",".join("?" for _ in keys) + "))")
                    args.extend((s.key(kind), *keys))
                rows = await db.execute_fetchall(
                    "SELECT kind,id,body FROM facts WHERE " + " OR ".join(where), tuple(args))
                current = {kind: {} for kind in selectors}
                kind_names = {s.key(kind): kind for kind in selectors}
                for kind, key, value in rows:
                    current[kind_names[kind]][key] = json.loads(value)
                accumulators = current["pool-candle-acc"]
                current_candles = current["market-candle"]
                current_meta = current["candle-meta"]
                for bar in bars:
                    width = BAR_MS[bar]
                    opened, key = bar_keys[bar]
                    previous = accumulators.get(key)
                    if previous:
                        candle = trade_to_candle(previous, body, width)
                    else:
                        # Rebuild if the accumulator was pruned or this is a
                        # historical correction. The new trade is already in
                        # this transaction, so it must not be counted twice.
                        cur = await db.execute_fetchall("SELECT body FROM trades WHERE asset=? AND t>=? AND t<? ORDER BY t,id",
                            (s.key(market.storage), opened, opened + width))
                        candle = None
                        for row in cur:
                            candle = trade_to_candle(candle, json.loads(row[0]), width)
                    if not candle:
                        raise ValueError("invalid-chain-candle")
                    candle["confirmed"] = opened + width <= stamp
                    candle["observedAt"] = stamp
                    fact_rows.append(self._fact_params("pool-candle-acc", key, candle))
                    candle_rows.append(self._candle_params(market, bar, candle))
                    frame = {**market_frame, "bar": bar, "row": candle, "source": "Chain RPC",
                             "sourceEventAt": trade["t"], "receivedAt": received, "persistedAt": stamp,
                             "quality": "observed", "coverageFromBlock": self.coverage_from}
                    events.append(("candle", frame))
                    old = current_candles.get(market.storage + ":" + bar) or {}
                    meta = current_meta.get(market.candle_key(bar)) or {}
                    if opened >= (old.get("row") or {}).get("t", 0):
                        fact_rows.append(self._fact_params("market-candle", market.storage + ":" + bar, frame))
                        meta = {**meta, **market_frame, "source": "Chain RPC", "storage": market.storage,
                            "lastSuccessfulAt": stamp, "lastSourceEventAt": max(trade["t"], old.get("sourceEventAt") or 0),
                            "stale": False, "error": None, "transport": "websocket",
                            "coverage": "observed", "nextRefreshAt": stamp + 60000}
                    marks = dict(meta.get("rowObservedAt") or {})
                    marks[str(opened)] = stamp
                    if len(marks) > 1000:
                        marks = dict(sorted(marks.items(), key=lambda item: int(item[0]), reverse=True)[:1000])
                    fact_rows.append(self._fact_params("candle-meta", market.candle_key(bar), {
                        **meta, "rowObservedAt": marks, "lastObservationAt": stamp}))
                # All rows are derived from one transaction snapshot. Grouping
                # the writes avoids a worker-thread dispatch for every bar.
                await db.executemany(FACT_UPSERT, fact_rows)
                await db.executemany(CANDLE_UPSERT, candle_rows)
                # Keep the original candle-then-trade event order, with one
                # aiosqlite worker dispatch and the same transaction boundary.
                events.append(("trade", body))
                await enqueue_events(db, events)
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def _fact(self, kind, key, value):
        await self.s.db.execute(FACT_UPSERT, self._fact_params(kind, key, value))

    def _fact_params(self, kind, key, value):
        return self.s.key(kind), key, json.dumps(value, allow_nan=False)

    async def _observe_bar(self, market, bar, opened, stamp):
        meta = await self.s.get("candle-meta",market.candle_key(bar)) or {}
        marks = dict(meta.get("rowObservedAt") or {})
        marks[str(opened)] = stamp
        if len(marks)>1000:
            marks=dict(sorted(marks.items(),key=lambda item:int(item[0]),reverse=True)[:1000])
        await self._fact("candle-meta",market.candle_key(bar),{
            **meta,"rowObservedAt":marks,"lastObservationAt":stamp})

    async def close_elapsed_bars(self):
        """Close elapsed observed bars; never invent zero-volume price bars."""
        from .market_streams import BAR_MS, Market
        from ..realtime_schema import enqueue_event
        stamp = now_ms()
        if stamp - self.last_closed < 5_000:
            return
        widths = "CASE c.bar " + " ".join("WHEN '"+bar+"' THEN "+str(width) for bar,width in BAR_MS.items()) + " ELSE 0 END"
        # Start with the small unconfirmed set, then look up its exact market
        # primary key. The inverse join multiplied all markets by all open
        # bars on every pass, even when no bar was due.
        prefix = self.s.key('dex:')
        cur = await self.s.db.execute_fetchall(
            "SELECT m.body,c.bar,c.openTime FROM candles c CROSS JOIN facts m "
            "WHERE c.confirmed=0 AND c.asset>=? AND c.asset<? "
            "AND c.openTime+("+widths+")<=? AND m.kind=? AND m.id=substr(c.asset,?) LIMIT 250",
            (prefix, prefix[:-1]+';', stamp, self.s.key("market-registry"), len(self.s.scope)+2))
        rows = cur
        if not rows:
            self.last_closed = stamp
            return
        # Each candle, accumulator and event stay atomic while other chains
        # can write between bounded groups of elapsed bars.
        for offset in range(0, len(rows), 25):
            async with self.s._guard_write():
                await self.s.db.execute("BEGIN IMMEDIATE")
                try:
                    for record,bar,opened in rows[offset:offset+25]:
                        market = Market(**json.loads(record)["definition"])
                        asset = self.s.key(market.storage)
                        current_bar = await self.s.fetchone(
                            "SELECT confirmed FROM candles WHERE asset=? AND bar=? AND openTime=?",
                            (asset, bar, opened))
                        # A live trade may have confirmed the bar between
                        # batches; do not emit a duplicate close event.
                        if not current_bar or current_bar[0]:
                            continue
                        key = market.storage+":"+bar+":"+str(opened)
                        cur = await self.s.db.execute_fetchall("SELECT body FROM facts WHERE kind=? AND id=?", (self.s.key("pool-candle-acc"),key))
                        hit = (cur[0] if cur else None)
                        if not hit:
                            continue
                        candle=json.loads(hit[0]); candle["confirmed"]=True; candle["observedAt"]=stamp
                        await self._fact("pool-candle-acc",key,candle)
                        await self._write_candle(market,bar,candle,stamp)
                        await self._observe_bar(market,bar,opened,stamp)
                        frame={**market.frame(),"bar":bar,"row":candle,"source":"Chain RPC",
                               "sourceEventAt":candle.get("lastEventAt"),"receivedAt":stamp,"quality":"observed"}
                        cur=await self.s.db.execute_fetchall("SELECT body FROM facts WHERE kind=? AND id=?",(self.s.key("market-candle"),market.storage+":"+bar))
                        current=(cur[0] if cur else None)
                        if current and json.loads(current[0]).get("row",{}).get("t")==opened:
                            await self._fact("market-candle",market.storage+":"+bar,frame)
                        await enqueue_event(self.s.db,"candle",frame)
                    await self.s.db.commit()
                except BaseException:
                    await self.s.db.rollback()
                    raise
            await asyncio.sleep(0)
        self.last_closed=stamp

    async def _write_candle(self, market, bar, row, stamp):
        await self.s.db.execute(CANDLE_UPSERT, self._candle_params(market, bar, row))

    def _candle_params(self, market, bar, row):
        return (self.s.key(market.storage), bar, row["t"], row["o"], row["h"], row["l"], row["c"],
                row["v"], row["vu"], int(bool(row.get("confirmed"))))

    async def retract_trade(self, market, ident):
        from .market_streams import BAR_MS, trade_to_candle
        from ..realtime_schema import enqueue_event
        s = self.s
        async with s._guard_write():
            await s.db.execute("BEGIN IMMEDIATE")
            try:
                cur = await s.db.execute_fetchall("SELECT t FROM trades WHERE asset=? AND id=?", (s.key(market.storage), ident))
                row = (cur[0] if cur else None)
                if not row:
                    await s.db.rollback()
                    return
                at = row[0]
                await s.db.execute("DELETE FROM trades WHERE asset=? AND id=?", (s.key(market.storage), ident))
                latest = await s.db.execute_fetchall("SELECT MAX(t) FROM trades WHERE asset=?", (s.key(market.storage),))
                last_trade = ((latest[0] if latest else None))[0]
                # Retraction must include previously watched bars even if
                # their page lease has since expired.
                for bar, width in BAR_MS.items():
                    offset = 4 * 86400000 if width == 604800000 else 0
                    opened = (at - offset) // width * width + offset
                    cur = await s.db.execute_fetchall("SELECT 1 FROM candles WHERE asset=? AND bar=? AND openTime=?", (s.key(market.storage), bar, opened))
                    if not (cur[0] if cur else None):
                        continue
                    cur = await s.db.execute_fetchall("SELECT body FROM trades WHERE asset=? AND t>=? AND t<? ORDER BY t,id",
                        (s.key(market.storage), opened, opened + width))
                    candle = None
                    for item in cur:
                        candle = trade_to_candle(candle, json.loads(item[0]), width)
                    key = market.storage + ":" + bar + ":" + str(opened)
                    if candle:
                        candle["confirmed"] = opened + width <= now_ms()
                        candle["observedAt"] = now_ms()
                        await self._write_candle(market, bar, candle, now_ms())
                        await self._fact("pool-candle-acc", key, candle)
                        await enqueue_event(s.db, "candle", {**market.frame(), "bar": bar, "row": candle,
                            "source": "Chain RPC", "sourceEventAt": candle.get("lastEventAt"),
                            "persistedAt":now_ms(),"correction": True})
                    else:
                        await s.db.execute("DELETE FROM candles WHERE asset=? AND bar=? AND openTime=?", (s.key(market.storage), bar, opened))
                        await s.db.execute("DELETE FROM facts WHERE kind=? AND id=?", (s.key("pool-candle-acc"), key))
                    # Force a matching scoped chart to reload if its last
                    # candle vanished, and refresh persistent current metadata.
                    cur = await s.db.execute_fetchall("SELECT body FROM facts WHERE kind=? AND id=?",
                        (s.key("market-candle"), market.storage + ":" + bar))
                    current = (cur[0] if cur else None)
                    if current and json.loads(current[0]).get("row", {}).get("t") == opened:
                        if candle:
                            await self._fact("market-candle", market.storage + ":" + bar,
                                {**market.frame(), "row": candle, "bar": bar, "source": "Chain RPC",
                                 "sourceEventAt":candle.get("lastEventAt"),"persistedAt":now_ms()})
                        else:
                            await s.db.execute("DELETE FROM facts WHERE kind=? AND id=?", (s.key("market-candle"), market.storage + ":" + bar))
                    meta_cursor = await s.db.execute_fetchall("SELECT body FROM facts WHERE kind=? AND id=?",
                        (s.key("candle-meta"),market.candle_key(bar)))
                    meta_row = (meta_cursor[0] if meta_cursor else None)
                    if meta_row:
                        await self._fact("candle-meta",market.candle_key(bar),{
                            **json.loads(meta_row[0]),"lastSourceEventAt":last_trade,"reorgAt":now_ms()})
                    await self._observe_bar(market,bar,opened,now_ms())
                    await enqueue_event(s.db, "candle-reset", {**market.frame(), "bar": bar, "reason": "chain-reorg"})
                await enqueue_event(s.db, "trade-remove", {**market.frame(), "ids": [ident], "reason": "chain-reorg"})
                await s.db.commit()
            except BaseException:
                await s.db.rollback()
                raise

    async def retract_block(self, block_hash):
        self.removed_revisions[block_hash] = self.removed_revisions.get(block_hash, 0) + 1
        self.removed_revisions.move_to_end(block_hash)
        while len(self.removed_revisions) > 1024:
            self.removed_revisions.popitem(last=False)
        cur = await self.s.db.execute_fetchall("SELECT id,pool,body FROM chain_stream_logs WHERE chain=? AND hash=?", (self.chain, block_hash))
        rows = cur
        for row in rows:
            pool = self.pools.get(row[1])
            if not pool:
                continue
            for token in self.bases(pool):
                await self.retract_trade(self.market(pool, token), "chain:" + self.chain + ":" + row[0])
                base = await self.s.get("asset", token) or {}
                if (base.get("priceProvenance") or {}).get("eventId") == row[0]:
                    await self.s.patch_fact("asset", token, {"price": None, "quoteReason": "chain-reorg",
                                                          "priceProvenance": None})
            quote = await self.s.get("pool-quote", pool["pool"]) or {}
            if quote.get("blockHash") == block_hash:
                await self.s.put("pool-quote", pool["pool"], {**quote, "memePerStock": None,
                    "at":0,"eventOrder":[0,-1],"reason": "chain-reorg", "finality": "reverted"})
            for rel in self.relations.get(pool["pool"], []):
                current = await self.s.get("relation", rel["id"]) or {}
                if current.get("valuationBlockHash") == block_hash:
                    await self.s.patch_fact("relation", rel["id"], {
                        "liquidityUsd": None, "liquidityAt": None,
                        "liquidityMethod": "chain-reorg", "valuationBlockHash": None,
                        "reservesAt":0,"valuationOrder":[0,-1],
                    })
        async with self.s._guard_write():
            await self.s.db.execute("DELETE FROM chain_stream_logs WHERE chain=? AND hash=?", (self.chain, block_hash))
            await self.s.db.commit()

    async def retract_orphans(self, start, end, canonical_hashes=None):
        cur = await self.s.db.execute_fetchall(
            "SELECT DISTINCT block,hash FROM chain_stream_logs WHERE chain=? AND block>=? AND block<=?",
            (self.chain, start, end))
        # A complete, anchored getLogs range already identifies the canonical
        # hashes for blocks containing logs. Only orphan-only/empty blocks
        # need another RPC; do not query every historical block twice.
        headers = dict(canonical_hashes or {})
        for row in cur:
            if row[0] not in headers:
                header = await self.rpc("eth_getBlockByNumber", [hex(row[0]), False])
                if not header:
                    raise ValueError("canonical-block-unavailable")
                headers[row[0]] = header["hash"].lower()
            if row[1].lower() != headers[row[0]]:
                async with self.lock:
                    await self.retract_block(row[1])

    async def catch_up(self, lane="pools", max_ranges=3, initial_depth=2, head=None):
        """Persist lane attempts and failures without changing scan priority."""
        telemetry = self.status_store or self.s
        diagnostic = await telemetry.get("chain-stream-scan", lane) or {}
        diagnostic = {**diagnostic, "lastAttemptAt": now_ms()}
        await telemetry.put("chain-stream-scan", lane, diagnostic)
        self.scan_page_metrics[lane] = {}
        try:
            result = await self._catch_up(lane, max_ranges, initial_depth, head)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await telemetry.put("chain-stream-scan", lane, {
                **diagnostic, **self.scan_page_metrics[lane],
                "lastErrorAt": now_ms(),
                "lastError": type(error).__name__ + ":" + str(error)[:180],
            })
            raise
        else:
            await telemetry.put("chain-stream-scan", lane, {
                **diagnostic, **self.scan_page_metrics[lane], "lastCompletedAt": now_ms(),
            })
            return result

    def _paused_replay_result(self, lane, head, processed_block):
        self.scan_page_metrics[lane]["pausedForLiveAt"] = now_ms()
        return {'caughtUp': False, 'head': head, 'processedBlock': processed_block}

    async def _catch_up(self, lane="pools", max_ranges=3, initial_depth=2, head=None):
        """Only a successful full getLogs interval advances its own durable cursor.

        The pools cursor proves contiguous historical coverage. pools-live
        proves only its explicitly recorded recent interval; neither lane may
        advance the other when a provider fails or the process restarts.
        """
        current = await self.s.get("chain-stream-cursor", lane) or {}
        if self.replay_under_pressure():
            return self._paused_replay_result(
                lane, number(head['number']) if head else self.latest_head, current.get('block'))
        if head is None:
            head = await self.rpc("eth_getBlockByNumber", ["latest", False])
        self.remember_header(head)
        height = number(head["number"])
        # Confirm that the previous processed block is still canonical.
        previous = current.get("block")
        anchors = list(current.get("anchors") or [])
        if isinstance(previous, int) and previous > height:
            raise RuntimeError("checkpoint-ahead-of-head")
        if previous and current.get("hash"):
            canonical = await self.rpc("eth_getBlockByNumber", [hex(previous), False])
            if canonical and canonical.get("hash", "").lower() != current["hash"].lower():
                rewind = None
                for anchor in reversed(anchors[:-1]):
                    check = await self.rpc("eth_getBlockByNumber", [hex(anchor["block"]), False])
                    if check and check.get("hash", "").lower() == anchor["hash"].lower():
                        rewind = anchor["block"]
                        break
                if rewind is None:
                    # Never silently assert coverage across a reorg deeper
                    # than our persisted canonical checkpoints.
                    raise RuntimeError("reorg-beyond-retained-checkpoints")
                cur = await self.s.db.execute_fetchall("SELECT DISTINCT hash FROM chain_stream_logs WHERE chain=? AND block>?", (self.chain, rewind))
                async with self.lock:
                    for row in cur:
                        await self.retract_block(row[0])
                previous = rewind
                anchors = [a for a in anchors if a["block"] <= rewind]
        start = int(previous) + 1 if previous is not None else max(0, height - initial_depth)
        pending = set(self.pending_pools) if lane == "pools" else set()
        if pending:
            # A newly discovered pool gets an explicit observed-history start.
            # Re-scan a short overlap; existing market/log keys are idempotent.
            start = min(start, max(0, height - 32))
        lane_coverage_from = current.get("coverageFrom")
        if lane_coverage_from is None:
            lane_coverage_from = start
        if lane == "pools" and self.coverage_from is None:
            self.coverage_from = lane_coverage_from
        addresses = sorted(self.pools)
        processed_block = current.get("block")
        if not addresses:
            return {'caughtUp': True, 'head': height, 'processedBlock': previous}
        if not anchors and start > 0:
            first = await self.rpc("eth_getBlockByNumber", [hex(start - 1), False])
            if not first:
                raise ValueError("initial-checkpoint-unavailable")
            anchors.append({"block": start - 1, "hash": first["hash"]})
        # A bounded pass yields to live ingestion; the durable cursor retains
        # any backlog rather than claiming that skipped blocks were covered.
        for _ in range(max_ranges):
            if start > height:
                break
            if self.replay_under_pressure():
                return self._paused_replay_result(lane, height, processed_block)
            range_started = time.monotonic()
            range_rpc_ms = 0
            # Both PublicNode chains accept 300-block / 8-address filters.
            # Amortize catalogue pages so replay can outpace their fast blocks
            # while respecting the shared conservative RPC budget.
            range_blocks = 300 if self.chain in ('56', '4663') else 100
            end = min(height, start + range_blocks - 1)
            before = await self.rpc("eth_getBlockByNumber", [hex(end), False])
            if not before:
                raise ValueError("range-anchor-unavailable")
            all_logs = []
            # PublicNode rejects >=10 addresses even though subscriptions
            # accept larger filters. Verified 8-address getLogs batches.
            page_size = 8 if self.chain in ("56", "4663") else 64
            for offset in range(0, len(addresses), page_size):
                if self.replay_under_pressure():
                    return self._paused_replay_result(lane, height, processed_block)
                page_started = time.monotonic()
                await self.wait_for_live()
                if self.replay_under_pressure():
                    return self._paused_replay_result(lane, height, processed_block)
                rpc_started = time.monotonic()
                logs = None
                try:
                    logs = await self.rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                        "address": addresses[offset:offset + page_size], "topics": [TOPICS]}])
                finally:
                    rpc_ms = round((time.monotonic() - rpc_started) * 1000)
                    range_rpc_ms += rpc_ms
                    self.scan_page_metrics[lane].update({
                        "lastPageAt": now_ms(), "lastPageFromBlock": start, "lastPageToBlock": end,
                        "lastPageAddressCount": len(addresses[offset:offset + page_size]),
                        "lastPageDurationMs": round((time.monotonic() - page_started) * 1000),
                        "lastPageRpcMs": rpc_ms,
                        "lastPageLogs": len(logs) if isinstance(logs, list) else None,
                    })
                if not isinstance(logs, list):
                    raise ValueError("invalid-log-range")
                received = now_ms()
                all_logs.extend({**log, '_receivedAt': received} for log in logs)
            canonical_hashes = {}
            for log in all_logs:
                block = number(log['blockNumber'])
                block_hash = str(log['blockHash']).lower()
                if block in canonical_hashes and canonical_hashes[block] != block_hash:
                    raise RuntimeError('chain-reorg-during-range')
                canonical_hashes[block] = block_hash
            # Metadata failures abort this interval and leave the cursor intact.
            ordered_logs = sorted(all_logs, key=lambda x: (
                number(x["blockNumber"]), number(x.get("transactionIndex", "0x0")), number(x["logIndex"])))
            for index, log in enumerate(ordered_logs):
                if self.replay_under_pressure():
                    return self._paused_replay_result(lane, height, processed_block)
                # One bounded live head start per eight replayed logs keeps
                # high-volume ranges moving; the intervening yields and the
                # mutation lock still let subscribed events run between them.
                if index % 8 == 0:
                    await self.wait_for_live()
                else:
                    await asyncio.sleep(0)
                if self.replay_under_pressure():
                    return self._paused_replay_result(lane, height, processed_block)
                await self.process_log(log)
            if self.replay_under_pressure():
                return self._paused_replay_result(lane, height, processed_block)
            # WebSocket events may have arrived from an abandoned fork while
            # the paged HTTP/WS range was in progress. Remove them before the
            # durable watermark can certify this interval.
            checked = await self.rpc('eth_getBlockByNumber', [hex(end), False])
            if not checked or checked['hash'].lower() != before['hash'].lower():
                raise RuntimeError('chain-reorg-during-range')
            await self.retract_orphans(start, end, canonical_hashes)
            end_header = await self.rpc("eth_getBlockByNumber", [hex(end), False])
            if not end_header:
                raise ValueError("checkpoint-block-unavailable")
            if end_header["hash"].lower() != before["hash"].lower():
                raise RuntimeError("chain-reorg-during-range")
            if self.replay_under_pressure():
                return self._paused_replay_result(lane, height, processed_block)
            anchors.append({"block": end, "hash": end_header["hash"]})
            anchors = anchors[-128:]
            await self.s.put("chain-stream-cursor", lane, {
                "block": end, "hash": end_header["hash"], "coverageFrom": lane_coverage_from,
                "updatedAt": now_ms(), "blockTime": number(end_header["timestamp"])*1000,
                "poolCount": len(addresses), "anchors": anchors,
                **({key: current[key] for key in ("rebasedAt", "rebasedFromBlock", "previousCoverageFrom")
                    if key in current} if lane == "pools-live" else {})})
            processed_block = end
            self.scan_page_metrics[lane].update({
                "lastSuccessAt": now_ms(), "lastSuccessfulBlock": end,
                "lastRangeFromBlock": start, "lastRangeToBlock": end,
                "lastRangeDurationMs": round((time.monotonic() - range_started) * 1000),
                "lastRangeRpcMs": range_rpc_ms, "lastRangeLogs": len(all_logs),
                "lastRangePages": (len(addresses) + page_size - 1) // page_size,
            })
            for pool_id in pending:
                await self.s.put("pool-stream-coverage", pool_id, {
                    "pool": pool_id, "fromBlock": start, "observedAt": now_ms(), "coverage": "observed"})
            self.pending_pools.difference_update(pending)
            pending.clear()
            start = end + 1
        if lane == "pools" and now_ms() - self.last_prune > 60_000:
            tape_cutoff = now_ms() - 8 * 86_400_000
            await self.s.prune_market_trades('dex:', tape_cutoff)
            cutoff = now_ms() - 2 * 86_400_000
            old_logs = await self.s.fetchall('''SELECT id FROM chain_stream_logs
                WHERE chain=? AND at<? AND processed=1 LIMIT 5000''', (self.chain, cutoff))
            old_bars = await self.s.fetchall('''SELECT id FROM facts
                WHERE kind=? AND json_extract(body,'$.t')<? LIMIT 5000''',
                (self.s.key('pool-candle-acc'), tape_cutoff))
            # Selection is read-only; empty retention passes must never hold
            # back a live trade. Each actual deletion batch is bounded too.
            for offset in range(0, max(len(old_logs), len(old_bars)), 250):
                async with self.s._guard_write():
                    await self.s.db.executemany('DELETE FROM chain_stream_logs WHERE chain=? AND id=? AND processed=1 AND at<?',
                        [(self.chain, row[0], cutoff) for row in old_logs[offset:offset+250]])
                    await self.s.db.executemany('DELETE FROM facts WHERE kind=? AND id=?',
                        [(self.s.key('pool-candle-acc'), row[0]) for row in old_bars[offset:offset+250]])
                    await self.s.db.commit()
                await asyncio.sleep(0)
            self.last_prune = now_ms()
        return {'caughtUp': start > height, 'head': height, 'processedBlock': start - 1}

    async def rebase_recent_if_far_behind(self, head):
        """Restore a near-tip lane without certifying the skipped history.

        A saved recent cursor can fall so far behind after an outage that the
        three bounded replay ranges cannot catch the moving head. Its former
        interval remains in the historical lane's explicit gap and is replayed
        there. Existing trades remain persisted and replay is idempotent.
        """
        recent = await self.s.get("chain-stream-cursor", "pools-live") or {}
        previous = recent.get("block")
        height = number(head["number"])
        if not isinstance(previous, int) or height - previous <= RECENT_REBASE_LAG_BLOCKS:
            return False
        historical = await self.s.get("chain-stream-cursor", "pools") or {}
        if not isinstance(historical.get("block"), int):
            return False
        first = max(0, height - RECENT_REBASE_DEPTH_BLOCKS + 1)
        anchor = await self.rpc("eth_getBlockByNumber", [hex(first - 1), False])
        if not anchor or not anchor.get("hash"):
            raise ValueError("recent-rebase-anchor-unavailable")
        if self.replay_under_pressure():
            return False
        await self.s.put("chain-stream-cursor", "pools-live", {
            "block": first - 1, "hash": anchor["hash"],
            "coverageFrom": first, "updatedAt": now_ms(),
            "blockTime": number(anchor["timestamp"]) * 1000,
            "poolCount": len(self.pools),
            "anchors": [{"block": first - 1, "hash": anchor["hash"]}],
            "rebasedAt": now_ms(), "rebasedFromBlock": previous,
            "previousCoverageFrom": recent.get("coverageFrom"),
        })
        return True

    async def reconcile_step(self):
        """Verify the current tip before spending the shared RPC budget on old history."""
        historical = await self.s.get("chain-stream-cursor", "pools") or {}
        if self.replay_under_pressure():
            return {'caughtUp': False, 'head': self.latest_head,
                    'processedBlock': historical.get('block')}
        head = await self.rpc("eth_getBlockByNumber", ["latest", False])
        height = number(head["number"])
        historical_block = historical.get("block")
        if self.replay_under_pressure():
            return {'caughtUp': False, 'head': height, 'processedBlock': historical_block}
        if isinstance(historical_block, int) and height - historical_block > 32:
            await self.rebase_recent_if_far_behind(head)
            # On the first dual-lane run, explicitly cover the last 300
            # blocks. On later runs the saved lane replays every missed block
            # after a disconnect, while the older gap stays on pools.
            recent = await self.catch_up("pools-live", max_ranges=3, initial_depth=299, head=head)
            if not recent['caughtUp']:
                # A prolonged outage may leave more than three near-tip
                # ranges. Spend the next RPC slot there before old history.
                return {'caughtUp': False, 'head': height, 'processedBlock': historical_block}
            return await self.catch_up("pools", max_ranges=1, head=head)
        return await self.catch_up(head=head)

    async def ws_call(self, ws, method, params):
        self.request_id += 1
        ident = self.request_id
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}))
            return await asyncio.wait_for(future, 15)
        finally:
            self.pending.pop(ident, None)

    async def reader(self, ws):
        async for raw in ws:
            message = json.loads(raw)
            if message.get("id") in self.pending:
                future = self.pending[message["id"]]
                if future.done():
                    continue
                if message.get("error"):
                    detail=message["error"]
                    future.set_exception(RuntimeError("rpc-error:"+str(detail.get("code"))+":"+str(detail.get("message") or "")[:160]))
                else:
                    future.set_result(message.get("result"))
                continue
            data = (message.get("params") or {}).get("result")
            if isinstance(data, dict):
                if "parentHash" in data:
                    self.remember_header(data)
                else:
                    # Overflow closes the connection; catch-up replays from
                    # the durable range cursor. No silent dropped logs.
                    self.last_received = now_ms()
                    if self.irrelevant_sync(data):
                        continue
                    data['_receivedAt'] = self.last_received
                    try:
                        self.queue.put_nowait(data)
                    except asyncio.QueueFull:
                        self.recovery_needed = True
                        await ws.close()
                        raise RuntimeError("chain-stream-backpressure")

    async def process_queued(self, event):
        """Keep the removed queue head until its durable processing succeeds."""
        started = time.perf_counter()
        live_token = _live_log_collector.set(self)
        self.live_processing = True
        try:
            await self.process_log(event)
        except BaseException:
            # Requeueing into the bounded queue could block forever if the
            # reader filled the freed slot. This single pending head preserves
            # FIFO order across reconnects and is replay-safe after a restart.
            self.retry_event = event
            self.recovery_needed = True
            raise
        finally:
            self.live_log_durations_ms.append(round(max(0, time.perf_counter() - started) * 1000, 1))
            _live_log_collector.reset(live_token)
            self.live_processing = False

    def take_live_event(self):
        """Favor viewed pools without changing each pool's arrival order.

        Keep the single bounded queue: overflow/reconnect and durable replay
        still cover every log. Rotate its deque only within this synchronous
        step, then let Queue.get_nowait wake a waiting producer normally.
        """
        pending = self.queue._queue
        if not pending:
            raise asyncio.QueueEmpty
        if not self.watched_pools:
            self.watched_burst = 0
            return self.queue.get_nowait()
        first_hot = first_cold = first_removed = None
        seen_pools = set()
        for index, event in enumerate(pending):
            pool = str(event.get("address") or "").lower() if isinstance(event, dict) else ""
            if pool in seen_pools:
                continue
            seen_pools.add(pool)
            hot = pool in self.watched_pools
            if hot and first_hot is None:
                first_hot = index
            if not hot and first_cold is None:
                first_cold = index
            if isinstance(event, dict) and event.get("removed") and first_removed is None:
                first_removed = (index, hot)
        if first_removed and (not first_removed[1] or self.watched_burst < WATCHED_POOL_BURST
                              or first_cold is None):
            selected = first_removed[0]
        elif self.watched_burst >= WATCHED_POOL_BURST and first_cold is not None:
            selected = first_cold
        else:
            selected = first_hot if first_hot is not None else 0
        if selected:
            pending.rotate(-selected)
        try:
            event = self.queue.get_nowait()
        finally:
            if selected:
                pending.rotate(selected)
        self.record_live_event(event)
        return event

    def record_live_event(self, event):
        pool = str(event.get("address") or "").lower() if isinstance(event, dict) else ""
        self.watched_burst = self.watched_burst + 1 if pool in self.watched_pools else 0

    async def recover_live_queue(self, reader):
        """Drain an old queue before opening another log subscription.

        After an overflow, a new log subscription would immediately fill the
        old queue again. The unadvanced recent cursor covers the disconnected
        interval; normal reconcile scans it after the new subscription starts.
        """
        if not self.recovery_needed and self.retry_event is None and self.queue.empty():
            return
        self.status = "catching-up"
        self.schedule_status_fact(force=True)
        await self.refresh_watches()
        drained = 0
        while self.retry_event is not None or not self.queue.empty():
            if reader.done():
                await reader
                raise RuntimeError("chain-stream-recovery-connection-closed")
            if self.retry_event is not None:
                event = self.retry_event
                self.retry_event = None
            else:
                event = self.take_live_event()
            await self.process_queued(event)
            drained += 1
            if drained % 64 == 0:
                await self.refresh_watches()
                self.schedule_status_fact(force=True)
            await asyncio.sleep(0)
        if not self.recovery_needed:
            return
        # Replay can take minutes when a range spans many pool pages. The
        # durable cursor has not advanced, so the normal reconcile task can
        # safely scan the gap alongside newly subscribed live logs.
        if reader.done():
            await reader
            raise RuntimeError("chain-stream-recovery-connection-closed")
        self.recovery_needed = False
        self.schedule_status_fact(force=True)

    async def reconcile(self):
        while True:
            try:
                progress = await self.reconcile_step()
                self.status = "live" if progress['caughtUp'] else "catching-up"
                await self.status_fact()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.status = "catching-up"
                await self.status_fact(type(error).__name__+":"+str(error)[:180], force=True)
            await asyncio.sleep(5)

    async def run(self):
        await self.init()
        attempt = 0
        async with httpx.AsyncClient(timeout=12) as self.http:
            while True:
                reader = replay = None
                try:
                    await self.catalogue()
                    async with websockets.connect(self.urls[attempt % len(self.urls)], open_timeout=15,
                                                  close_timeout=3, ping_interval=20, ping_timeout=20,
                                                  max_queue=128, max_size=4 * 1024 * 1024) as ws:
                        reader = asyncio.create_task(self.reader(ws))
                        actual = number(await self.ws_call(ws, "eth_chainId", []))
                        if actual != int(self.chain):
                            raise ValueError("wrong-chain-id")
                        self.ws = ws
                        await self.recover_live_queue(reader)
                        await self.ws_call(ws, "eth_subscribe", ["newHeads"])
                        pools = sorted(self.pools)
                        for offset in range(0, len(pools), 64):
                            await self.ws_call(ws, "eth_subscribe", ["logs", {"address": pools[offset:offset + 64], "topics": [TOPICS]}])
                        self.status = "catching-up"
                        self.schedule_status_fact(force=True)
                        replay = asyncio.create_task(self.reconcile())
                        while not reader.done():
                            try:
                                if self.queue.empty():
                                    event = await asyncio.wait_for(self.queue.get(), 1)
                                    self.record_live_event(event)
                                else:
                                    event = self.take_live_event()
                                await self.process_queued(event)
                            except asyncio.TimeoutError:
                                pass
                            if now_ms() - self.last_catalogue > 60_000 and await self.catalogue():
                                break  # reconnect with the full updated address filter
                            self.schedule_housekeeping()
                            self.schedule_status_fact()
                        if reader.done():
                            await reader
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.status = "reconnecting"
                    self.reconnects += 1
                    self.schedule_status_fact(type(error).__name__+":"+str(error)[:180], force=True)
                finally:
                    if self.retry_event is not None or not self.queue.empty():
                        self.recovery_needed = True
                    self.ws = None
                    self.http_chain_verified = False
                    for task in (reader, replay):
                        if task:
                            task.cancel()
                    await asyncio.gather(*(t for t in (reader, replay) if t), return_exceptions=True)
                attempt += 1
                await asyncio.sleep(min(30, 2 ** min(attempt, 5)))


async def prepare_streams():
    if os.environ.get("CHAIN_STREAM_ENABLED", "true").lower() == "false":
        return
    for chain in ("196", "56", "4663"):
        collector = _collectors.setdefault(chain, ChainPoolStream(chain))
        await collector.init()


def stream_for(chain):
    """Share a prepared stream's RPC gate with read-only discovery workers."""
    return _collectors.get(str(chain))


def start_streams():
    if _tasks or os.environ.get("CHAIN_STREAM_ENABLED", "true").lower() == "false":
        return
    for chain in ("196", "56", "4663"):
        collector = _collectors.setdefault(chain, ChainPoolStream(chain))
        async def supervised(instance):
            while True:
                try:
                    await instance.run()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    print("[chain-stream-" + instance.chain + "] retry " + type(error).__name__, flush=True)
                    await asyncio.sleep(10)
        _tasks.append(asyncio.create_task(supervised(collector), name="chain-stream-" + chain))


async def stop_streams():
    for task in _tasks:
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    await asyncio.gather(*(collector.close() for collector in _collectors.values()))
    _collectors.clear()
