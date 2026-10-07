"""Durable, read-only factory-log discovery for pinned X Layer / BNB factories.

The registry is deliberately small. The Uniswap V3 address is published at
https://developers.uniswap.org/docs/protocols/v3/deployments/v3-xlayer-deployments
and the V2 address at
https://developers.uniswap.org/docs/protocols/v2/deployments . Both are
independently checked on X Layer mainnet: known pools return the factories
from factory(), and the V2 factory getPair(token0, token1) returns the pool.

This module records creation evidence, not a verified stock relationship. A
caller must still verify the pool's tokens and official stock identity before
adding it to the product's candidate/relationship views. No transactions are
sent and no address from an unverified discovery feed is subscribed.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiosqlite

from ..storage_runtime import async_connect
import httpx
from web3 import Web3

from ..stock_identity import assess_pool_relation, manifest_status, token_identity


Rpc = Callable[[str, list], Awaitable[object]]
CHAIN_ID = "196"
V3_FACTORY = "0x4b2ab38dbf28d31d467aa8993f6c2585981d6804"
V2_FACTORY = "0xdf38f24fe153761634be942f9d859f3dba857e95"
V3_PROOF_POOL = "0x081845418865c536c013570303f617711b3cc39c"
V2_PROOF_POOL = "0x02f335a407688cad2d22fe16878474dae04b920c"
V2_PROOF_TOKEN0 = "0x21d6359cab338ea54705e7d04efbaadd9020eeee"
V2_PROOF_TOKEN1 = "0xa8ddb5cd96b5222afe198316e9a57caa642850d5"
FACTORY_SELECTOR = "0xc45a0155"
GET_PAIR_SELECTOR = "0xe6a43905"
GET_POOL_SELECTOR = "0x1698ee82"
PAIR_CREATED = Web3.to_hex(Web3.keccak(text="PairCreated(address,address,address,uint256)"))
POOL_CREATED = Web3.to_hex(Web3.keccak(text="PoolCreated(address,address,uint24,int24,address)"))
HEX_32 = re.compile(r"^0x[0-9a-fA-F]{64}$")
HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
DEFAULT_BATCH_BLOCKS = 80  # X Layer public RPC currently rejects >100.
# RPC range limits do not bound SQLite work. Commit complete blocks in small
# event batches; a single unusually large block still remains atomic.
COMMIT_EVENT_TARGET = 64
CONFIRM_EVENT_LIMIT = 128
QUOTE_SYMBOLS = frozenset({
    "USDT", "USDC", "USDG", "DAI", "WOKB", "OKB", "WETH", "ETH", "WBTC",
    "BTC", "XBTC", "WBNB", "BTCB", "USD1", "XUSD",
})


@dataclass(frozen=True)
class Factory:
    address: str
    kind: str
    topic: str
    proof_pool: str | None = None
    proof_tokens: tuple[str, str] | None = None
    proof_fee: int | None = None
    label: str | None = None
    source_url: str | None = None


FACTORIES = (
    Factory(V3_FACTORY, "uniswap_v3", POOL_CREATED, V3_PROOF_POOL),
    Factory(V2_FACTORY, "uniswap_v2", PAIR_CREATED, V2_PROOF_POOL,
            (V2_PROOF_TOKEN0, V2_PROOF_TOKEN1)),
)
# Published addresses, independently verified with eth_chainId, getCode,
# factory.getPair/getPool, pool.factory and both pool token calls on 2026-09-30.
# Every worker repeats the proof before its first range; a mismatch fails closed.
BSC_PROOF_TOKENS = ("0x55d398326f99059ff775485246999027b3197955",
                    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c")
BSC_FACTORIES = (
    Factory("0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865", "uniswap_v3", POOL_CREATED,
            "0x172fcd41e0913e95784454622d1c3724f546f849", BSC_PROOF_TOKENS, 100,
            "PancakeSwap V3", "https://developer.pancakeswap.finance/contracts/v3/addresses"),
    Factory("0xca143ce32fe78f1f7019d7d551a6402fc5350c73", "uniswap_v2", PAIR_CREATED,
            "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae", BSC_PROOF_TOKENS, None,
            "PancakeSwap V2", "https://developer.pancakeswap.finance/contracts/v2/addresses"),
)
FACTORIES_BY_CHAIN = {"196": FACTORIES, "56": BSC_FACTORIES}


def _number(value):
    if isinstance(value, bool):
        raise ValueError("invalid-integer")
    return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)


def _word_address(value):
    if not isinstance(value, str) or not HEX_32.fullmatch(value) or int(value[2:26], 16):
        raise ValueError("invalid-address-word")
    address = "0x" + value[-40:].lower()
    if int(address, 16) == 0:
        raise ValueError("zero-address")
    return address


def _address_word(value):
    if not isinstance(value, str) or not HEX_ADDRESS.fullmatch(value):
        raise ValueError("invalid-address")
    return value[2:].lower().rjust(64, "0")


def _header(header, expected=None):
    if not isinstance(header, dict) or not HEX_32.fullmatch(str(header.get("hash") or "")):
        raise ValueError("block-header-unavailable")
    block = _number(header.get("number"))
    if expected is not None and block != expected:
        raise ValueError("block-number-mismatch")
    timestamp = _number(header.get("timestamp"))
    if timestamp <= 0:
        raise ValueError("block-time-unavailable")
    return {"block": block, "hash": header["hash"].lower(), "timestamp": timestamp}


def _abi_text(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value):
        return None
    raw = bytes.fromhex(value[2:])
    if len(raw) == 32:
        raw = raw.rstrip(b"\0")
    elif len(raw) >= 64 and int.from_bytes(raw[:32], "big") == 32:
        length = int.from_bytes(raw[32:64], "big")
        if length > 256 or len(raw) < 64 + length:
            return None
        raw = raw[64:64 + length]
    else:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return text[:128] or None


def decode_factory_log(factory: Factory, raw: dict, header: dict, head: int, confirmations: int, discovered_at: int,
                       chain_id: str = CHAIN_ID):
    """Decode only canonical, standard V2/V3 factory events with full provenance."""
    if not isinstance(raw, dict) or str(raw.get("address") or "").lower() != factory.address:
        raise ValueError("wrong-factory")
    if raw.get("removed"):
        raise ValueError("removed-log-in-range")
    topics = raw.get("topics") or []
    expected_count = 4 if factory.kind == "uniswap_v3" else 3
    if len(topics) != expected_count or str(topics[0]).lower() != factory.topic:
        raise ValueError("invalid-creation-topic")
    token0, token1 = _word_address(topics[1]), _word_address(topics[2])
    if int(token0, 16) >= int(token1, 16):
        raise ValueError("invalid-token-order")
    data = raw.get("data")
    if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]{128}", data):
        raise ValueError("invalid-creation-data")
    words = ["0x" + data[2 + i * 64:2 + (i + 1) * 64] for i in range(2)]
    fee = tick_spacing = None
    if factory.kind == "uniswap_v3":
        fee = _number(topics[3])
        if not 0 < fee < 1_000_000:
            raise ValueError("invalid-fee")
        tick_word = int(words[0], 16)
        # Solidity int24 is ABI sign-extended to a 256-bit word.
        tick_spacing = tick_word - (1 << 256) if tick_word & (1 << 255) else tick_word
        if not -2**23 <= tick_spacing < 2**23 or tick_spacing == 0:
            raise ValueError("invalid-tick-spacing")
        pool = _word_address(words[1])
    else:
        pool = _word_address(words[0])
        if int(words[1], 16) <= 0:
            raise ValueError("invalid-pair-index")
    block = _number(raw.get("blockNumber"))
    block_hash = str(raw.get("blockHash") or "").lower()
    tx_hash = str(raw.get("transactionHash") or "").lower()
    log_index = _number(raw.get("logIndex"))
    if block != header["block"] or block_hash != header["hash"] or not HEX_32.fullmatch(tx_hash) or log_index < 0:
        raise ValueError("creation-log-not-canonical")
    if chain_id not in FACTORIES_BY_CHAIN or factory not in FACTORIES_BY_CHAIN[chain_id]:
        raise ValueError("factory-not-registered-for-chain")
    event_id = ":".join((chain_id, factory.address, block_hash, tx_hash, str(log_index)))
    return {
        "id": event_id, "chainId": chain_id, "factory": factory.address,
        "dex": factory.kind, "pool": pool, "token0": token0, "token1": token1,
        "fee": fee, "tickSpacing": tick_spacing,
        "creationBlock": block, "creationBlockHash": block_hash,
        "creationTx": tx_hash, "creationLogIndex": log_index,
        "poolCreatedAt": header["timestamp"] * 1000, "discoveredAt": discovered_at,
        "confirmationStatus": "confirmed" if head - block >= confirmations else "provisional",
        "source": "factory_log",
    }


class FactoryDiscovery:
    """One bounded scan per call; schedule repeatedly and share the chain RPC gate."""

    def __init__(self, store, rpc: Rpc, *, start_block=None, initial_lookback_blocks=2048,
                 batch_blocks=DEFAULT_BATCH_BLOCKS, confirmations=6, enabled=True):
        self.chain = str(store.scope)
        self.factories = FACTORIES_BY_CHAIN.get(self.chain)
        if not self.factories:
            raise ValueError("factory-discovery-chain-unconfigured")
        if batch_blocks < 1 or batch_blocks > 100 or confirmations < 1:
            raise ValueError("invalid-factory-scan-limits")
        self.store = store
        self.rpc = rpc
        self.start_block = start_block
        self.initial_lookback_blocks = initial_lookback_blocks
        self.batch_blocks = batch_blocks
        self.confirmations = confirmations
        self.enabled = enabled
        self.initialized = False
        self.verified = False

    async def _init(self):
        if self.initialized:
            return
        if getattr(self.store.db, 'backend', 'sqlite') == 'postgres':
            from ..storage_schema import verify_tables
            await verify_tables(self.store.db, ('factory_discovery_events', 'factory_discovery_cursors'))
            self.initialized = True
            return
        async with self.store._guard_write():
            await self.store.db.executescript("""
                CREATE TABLE IF NOT EXISTS factory_discovery_events (
                    id TEXT PRIMARY KEY, chain TEXT NOT NULL, factory TEXT NOT NULL,
                    pool TEXT NOT NULL, block INTEGER NOT NULL, block_hash TEXT NOT NULL,
                    tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, discovered_at INTEGER NOT NULL,
                    status TEXT NOT NULL, body TEXT NOT NULL,
                    processing_status TEXT NOT NULL DEFAULT 'pending',
                    processing_reason TEXT, next_attempt_at INTEGER NOT NULL DEFAULT 0,
                    processed_at INTEGER, relation_id TEXT, retracted_at INTEGER);
                CREATE INDEX IF NOT EXISTS factory_discovery_chain_block
                    ON factory_discovery_events(chain,block);
                CREATE INDEX IF NOT EXISTS factory_discovery_chain_pool
                    ON factory_discovery_events(chain,pool,status);
                CREATE TABLE IF NOT EXISTS factory_discovery_cursors (
                    chain TEXT PRIMARY KEY, block INTEGER NOT NULL, hash TEXT,
                    coverage_from INTEGER NOT NULL, anchors TEXT NOT NULL,
                    batch_blocks INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            """)
            # An interrupted rollout can leave the first discovery schema on
            # disk. Add processing columns without discarding its evidence.
            columns = {row[1] for row in await self.store.db.execute_fetchall("PRAGMA table_info(factory_discovery_events)")}
            for name, definition in (
                ("processing_status", "TEXT NOT NULL DEFAULT 'pending'"),
                ("processing_reason", "TEXT"),
                ("next_attempt_at", "INTEGER NOT NULL DEFAULT 0"),
                ("processed_at", "INTEGER"),
                ("relation_id", "TEXT"),
                ("retracted_at", "INTEGER"),
            ):
                if name not in columns:
                    await self.store.db.execute(f"ALTER TABLE factory_discovery_events ADD COLUMN {name} {definition}")
            await self.store.db.execute("""CREATE INDEX IF NOT EXISTS factory_discovery_pending
                ON factory_discovery_events(chain,status,processing_status,next_attempt_at,block)""")
            await self.store.db.execute("""CREATE INDEX IF NOT EXISTS factory_discovery_confirmation
                ON factory_discovery_events(chain,block) WHERE status='provisional'""")
            await self.store.db.commit()
        self.initialized = True

    async def _verify_factories(self):
        if self.verified:
            return
        if _number(await self.rpc("eth_chainId", [])) != int(self.chain):
            raise RuntimeError("factory-chain-id-mismatch")
        for factory in self.factories:
            code = await self.rpc("eth_getCode", [factory.address, "latest"])
            if not isinstance(code, str) or not re.fullmatch(r"0x[0-9a-fA-F]{32,}", code):
                raise RuntimeError("factory-code-unavailable:" + factory.address)
            actual = await self.rpc("eth_call", [{"to": factory.proof_pool, "data": FACTORY_SELECTOR}, "latest"])
            if _word_address(actual) != factory.address:
                raise RuntimeError("factory-onchain-proof-mismatch")
            if factory.proof_tokens:
                selector = GET_POOL_SELECTOR if factory.kind == "uniswap_v3" else GET_PAIR_SELECTOR
                calldata = selector + "".join(_address_word(token) for token in factory.proof_tokens)
                if factory.kind == "uniswap_v3":
                    calldata += f"{factory.proof_fee:064x}"
                actual = await self.rpc("eth_call", [{"to": factory.address, "data": calldata}, "latest"])
                if _word_address(actual) != factory.proof_pool:
                    raise RuntimeError("factory-onchain-proof-mismatch")
                if self.chain == "56":
                    for token_selector, expected in zip(("0x0dfe1681", "0xd21220a7"), factory.proof_tokens):
                        actual = await self.rpc("eth_call", [{"to": factory.proof_pool, "data": token_selector}, "latest"])
                        if _word_address(actual) != expected:
                            raise RuntimeError("factory-proof-token-mismatch")
        self.verified = True

    async def _cursor(self):
        row = await self.store.fetchone("SELECT block,hash,coverage_from,anchors,batch_blocks FROM factory_discovery_cursors WHERE chain=?", (self.chain,))
        if row is None:
            return None
        return {"block": row[0], "hash": row[1], "coverageFrom": row[2],
                "anchors": json.loads(row[3]), "batchBlocks": row[4]}

    async def _bootstrap(self, head):
        coverage = self.start_block if self.start_block is not None else max(0, head["block"] - self.initial_lookback_blocks)
        if not isinstance(coverage, int) or coverage < 0 or self.initial_lookback_blocks < 0:
            raise ValueError("invalid-bootstrap-block")
        if coverage > head["block"] + 1:
            raise ValueError("start-block-ahead-of-chain")
        previous = coverage - 1
        anchor = None
        if previous >= 0:
            anchor = _header(await self.rpc("eth_getBlockByNumber", [hex(previous), False]), previous)
        now = int(time.time() * 1000)
        anchors = [{"block": previous, "hash": anchor["hash"]}] if anchor else []
        async with self.store._guard_write():
            await self.store.db.execute("INSERT OR IGNORE INTO factory_discovery_cursors VALUES (?,?,?,?,?,?,?)",
                (self.chain, previous, anchor["hash"] if anchor else None, coverage,
                 json.dumps(anchors), self.batch_blocks, now))
            await self.store.db.commit()
        return await self._cursor()

    async def _canonical(self, block):
        return _header(await self.rpc("eth_getBlockByNumber", [hex(block), False]), block)

    async def _rewind_if_needed(self, cursor):
        if cursor["block"] < 0:
            return cursor
        try:
            current = await self._canonical(cursor["block"])
        except ValueError:
            current = None  # A short reorg may temporarily lower the head.
        if current and current["hash"] == cursor["hash"]:
            return cursor
        ancestor = None
        for saved in reversed(cursor["anchors"][:-1]):
            try:
                canonical = await self._canonical(saved["block"])
            except ValueError:
                continue
            if canonical["hash"] == saved["hash"]:
                ancestor = saved
                break
        if ancestor is None:
            raise RuntimeError("factory-reorg-beyond-retained-anchors")
        anchors = [a for a in cursor["anchors"] if a["block"] <= ancestor["block"]]
        async with self.store._guard_write():
            await self.store.db.execute("BEGIN IMMEDIATE")
            try:
                await self.store.db.execute("UPDATE factory_discovery_events SET status='orphaned',body=json_set(body,'$.confirmationStatus','orphaned') WHERE chain=? AND block>?",
                                            (self.chain, ancestor["block"]))
                await self.store.db.execute("UPDATE factory_discovery_cursors SET block=?,hash=?,anchors=?,updated_at=? WHERE chain=?",
                    (ancestor["block"], ancestor["hash"], json.dumps(anchors), int(time.time() * 1000), self.chain))
                await self.store.db.commit()
            except BaseException:
                await self.store.db.rollback()
                raise
        return await self._cursor()

    async def _fetch_range(self, start, end, head, cursor):
        before = await self._canonical(end)
        raw = []
        for factory in self.factories:
            logs = await self.rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                "address": factory.address, "topics": [factory.topic]}])
            if not isinstance(logs, list):
                raise ValueError("invalid-factory-log-range")
            raw.extend((factory, log) for log in logs)
        headers = {end: before}
        for _, log in raw:
            block = _number(log.get("blockNumber"))
            if not start <= block <= end:
                raise ValueError("factory-log-outside-range")
            if block not in headers:
                headers[block] = await self._canonical(block)
        found = [decode_factory_log(factory, log, headers[_number(log["blockNumber"])],
                                    head["block"], self.confirmations, int(time.time() * 1000), self.chain)
                 for factory, log in raw]
        ids = [item["id"] for item in found]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate-factory-logs-in-response")
        # A reorganization during a slow paged read must not commit a false
        # watermark. The preceding cursor anchor must still be canonical too.
        if (await self._canonical(end))["hash"] != before["hash"]:
            raise RuntimeError("factory-reorg-during-range")
        if cursor["block"] >= 0 and (await self._canonical(cursor["block"]))["hash"] != cursor["hash"]:
            raise RuntimeError("factory-reorg-during-range")
        return found, before

    async def _commit_range(self, found, end_header, start, head, cursor, batch_blocks):
        # A cursor may advance only past complete blocks. Split a fetched RPC
        # range at event-bearing block boundaries, never through one block.
        # If a later transaction fails, retry starts immediately after the
        # last durable batch; committed evidence and its cursor stay aligned.
        groups = []
        for item in sorted(found, key=lambda row: (row["creationBlock"], row["creationLogIndex"], row["id"])):
            block = item["creationBlock"]
            if not start <= block <= end_header["block"]:
                raise ValueError("factory-log-outside-range")
            if not groups or groups[-1][0] != block:
                groups.append((block, []))
            groups[-1][1].append(item)
        batches, pending = [], []
        for _, items in groups:
            if pending and len(pending) + len(items) > COMMIT_EVENT_TARGET:
                batches.append(pending)
                pending = []
            pending.extend(items)
        if pending or not batches:
            batches.append(pending)
        prepared = [[(dict(item), json.dumps(item, separators=(",", ":")))
                     for item in batch] for batch in batches]
        created = []
        # The shared X Layer connection serves unrelated reads. Each awaited
        # statement on it can sit behind those reads while holding the one
        # process-wide writer lock. A short dedicated connection keeps this
        # evidence + watermark transaction atomic without that queue.
        async with async_connect(self.store.path, timeout=2) as db:
            for index, batch in enumerate(prepared):
                boundary = end_header if index == len(prepared) - 1 else {
                    "block": batch[-1][0]["creationBlock"],
                    "hash": batch[-1][0]["creationBlockHash"],
                }
                anchor = {"block": boundary["block"], "hash": boundary["hash"]}
                anchors = (cursor["anchors"] + [anchor])[-128:]
                now = int(time.time() * 1000)
                values = [(item["id"], self.chain, item["factory"], item["pool"], item["creationBlock"],
                           item["creationBlockHash"], item["creationTx"], item["creationLogIndex"],
                           item["poolCreatedAt"], item["discoveredAt"], item["confirmationStatus"], body)
                          for item, body in batch]
                # Rollback must affect this dedicated connection, never a
                # caller's read transaction on store.db.
                async with self.store._guard_write(db):
                    await db.execute("BEGIN IMMEDIATE")
                    actual = await db.execute_fetchall(
                        "SELECT block,hash FROM factory_discovery_cursors WHERE chain=?", (self.chain,))
                    if not actual or tuple(actual[0]) != (cursor["block"], cursor["hash"]):
                        raise RuntimeError("factory-cursor-changed")
                    previous = {}
                    # Keep bind counts bounded even when one complete block
                    # contains more events than the normal batch target.
                    for offset in range(0, len(batch), COMMIT_EVENT_TARGET):
                        ids = [item["id"] for item, _ in batch[offset:offset + COMMIT_EVENT_TARGET]]
                        rows = await db.execute_fetchall(
                            "SELECT id,status,discovered_at FROM factory_discovery_events WHERE id IN (" +
                            ",".join("?" for _ in ids) + ")", ids)
                        previous.update({row[0]: (row[1], row[2]) for row in rows})
                    await db.executemany("""INSERT INTO factory_discovery_events
                            (id,chain,factory,pool,block,block_hash,tx_hash,log_index,created_at,discovered_at,status,body)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                                body=json_set(excluded.body,'$.discoveredAt',factory_discovery_events.discovered_at),
                                processing_status=CASE WHEN factory_discovery_events.status='orphaned'
                                    THEN 'pending' ELSE factory_discovery_events.processing_status END,
                                relation_id=CASE WHEN factory_discovery_events.status='orphaned'
                                    THEN NULL ELSE factory_discovery_events.relation_id END,
                                retracted_at=CASE WHEN factory_discovery_events.status='orphaned'
                                    THEN NULL ELSE factory_discovery_events.retracted_at END
                            WHERE factory_discovery_events.status='orphaned'
                                OR (factory_discovery_events.status='provisional' AND excluded.status='confirmed')""",
                            values)
                    await db.execute("""UPDATE factory_discovery_cursors
                        SET block=?,hash=?,anchors=?,batch_blocks=?,updated_at=? WHERE chain=?""",
                        (boundary["block"], boundary["hash"], json.dumps(anchors), batch_blocks, now, self.chain))
                    await db.commit()
                for item, _ in batch:
                    old = previous.get(item["id"])
                    if old is None or old[0] == "orphaned":
                        if old:
                            item["discoveredAt"] = old[1]
                        created.append(item)
                cursor = {**cursor, "block": boundary["block"], "hash": boundary["hash"], "anchors": anchors}
                # Explicitly give live writers a chance between independently
                # durable batches; RPC range size must not dictate lock time.
                await asyncio.sleep(0)
        return created

    async def _confirm_existing(self, head):
        cutoff = head["block"] - self.confirmations
        # Confirmation is recoverable housekeeping, not part of the fetched
        # evidence watermark. Bound it and avoid taking any writer lock when
        # there are no due rows. The partial index skips years of final rows.
        async with async_connect(self.store.path, timeout=2) as db:
            rows = await db.execute_fetchall("""SELECT id FROM factory_discovery_events
                INDEXED BY factory_discovery_confirmation
                WHERE chain=? AND status='provisional' AND block<=?
                ORDER BY block LIMIT ?""", (self.chain, cutoff, CONFIRM_EVENT_LIMIT))
            if not rows:
                return 0
            async with self.store._guard_write(db):
                await db.executemany("""UPDATE factory_discovery_events
                    SET status='confirmed',body=json_set(body,'$.confirmationStatus','confirmed')
                    WHERE id=? AND chain=? AND status='provisional' AND block<=?""",
                    [(row[0], self.chain, cutoff) for row in rows])
                await db.commit()
            return len(rows)

    async def run_once(self, *, max_ranges=3):
        if not self.enabled:
            return {"status": "disabled", "reason": "factory-discovery-disabled"}
        if max_ranges < 1:
            raise ValueError("invalid-range-limit")
        await self._init()
        await self._verify_factories()
        head = _header(await self.rpc("eth_getBlockByNumber", ["latest", False]))
        cursor = await self._cursor() or await self._bootstrap(head)
        cursor = await self._rewind_if_needed(cursor)
        scanned = 0
        new_events = []
        while scanned < max_ranges and cursor["block"] < head["block"]:
            start = cursor["block"] + 1
            batch = min(self.batch_blocks, cursor["batchBlocks"])
            while True:
                end = min(head["block"], start + batch - 1)
                try:
                    found, end_header = await self._fetch_range(start, end, head, cursor)
                    break
                except Exception as exc:
                    # Only log-provider range/size failures should shrink the
                    # interval; malformed evidence and reorgs fail closed.
                    detail = str(exc).lower()
                    range_limited = isinstance(exc, httpx.TimeoutException) or any(marker in detail for marker in
                        ("block range", "too many", "response size", "limit exceeded", "timeout", "query returned"))
                    if batch <= 1 or not range_limited:
                        raise
                    batch = max(1, batch // 2)
            # A smaller successful range is a temporary provider ceiling, not
            # a permanent one-block mode after the provider recovers.
            next_batch = min(self.batch_blocks, batch + 1) if end - start + 1 == batch else batch
            new_events.extend(await self._commit_range(found, end_header, start, head, cursor, next_batch))
            cursor = await self._cursor()
            scanned += 1
        # Previous provisional events can be confirmed without re-fetching
        # their creation logs or holding a range transaction open longer.
        await self._confirm_existing(head)
        return {"status": "ok", "head": head["block"], "cursor": cursor["block"],
                "coverageFrom": cursor["coverageFrom"], "scannedRanges": scanned,
                "caughtUp": cursor["block"] >= head["block"], "newEvents": new_events,
                "confirmationBlocks": self.confirmations, "factories": [f.address for f in self.factories]}

    async def recent(self, *, limit=100, status=None):
        """Canonical pool-creation evidence for integration with pool verification."""
        if not 1 <= limit <= 1000 or status not in (None, "provisional", "confirmed"):
            raise ValueError("invalid-recent-query")
        await self._init()
        if status is None:
            rows = await self.store.fetchall("""SELECT body FROM factory_discovery_events
                WHERE chain=? AND status!='orphaned' ORDER BY block DESC,log_index DESC LIMIT ?""", (self.chain, limit))
        else:
            rows = await self.store.fetchall("""SELECT body FROM factory_discovery_events
                WHERE chain=? AND status=? ORDER BY block DESC,log_index DESC LIMIT ?""", (self.chain, status, limit))
        return [json.loads(row[0]) for row in rows]

    async def _mark_processing(self, event_id, status, reason=None, *, relation_id=None, retry_ms=0):
        now = int(time.time() * 1000)
        async with self.store._guard_write():
            await self.store.db.execute("""UPDATE factory_discovery_events SET
                processing_status=?,processing_reason=?,next_attempt_at=?,processed_at=?,
                relation_id=COALESCE(?,relation_id)
                WHERE id=? AND status='confirmed'""",
                (status, reason, now + retry_ms if retry_ms else 0,
                 now if status in ("matched", "irrelevant") else None,
                 relation_id, event_id))
            await self.store.db.commit()

    async def _retract_orphans(self):
        """Invalidate a relation from an abandoned creation block, even after restart."""
        from .main_round import emit_relation_event

        rows = await self.store.fetchall("""SELECT id,pool,relation_id FROM factory_discovery_events
            WHERE chain=? AND status='orphaned' AND relation_id IS NOT NULL AND retracted_at IS NULL
            ORDER BY block LIMIT 100""", (self.chain,))
        count = 0
        for event_id, pool, relation_id in rows:
            relation = await self.store.get("relation", relation_id)
            if relation and relation.get("factoryEventId") == event_id:
                updated = await self.store.patch_fact("relation", relation_id, {
                    "status": "invalid", "level": None, "evidenceStatus": "creation-orphaned",
                    "confirmationStatus": "orphaned", "error": "Factory creation block reorganized",
                    "checkedAt": int(time.time() * 1000),
                })
                await emit_relation_event(self.store, updated, "invalidated", "建池区块已重组，配对证据失效")
                count += 1
            pool_row = await self.store.get("pool", pool)
            if pool_row and pool_row.get("factoryEventId") == event_id:
                await self.store.patch_fact("pool", pool, {"creationStatus": "orphaned"})
            async with self.store._guard_write():
                await self.store.db.execute("UPDATE factory_discovery_events SET retracted_at=? WHERE id=?",
                                            (int(time.time() * 1000), event_id))
                await self.store.db.commit()
        return count

    async def _metadata(self, token):
        result = {}
        for key, selector in (("symbol", "0x95d89b41"), ("name", "0x06fdde03")):
            try:
                text = _abi_text(await self.rpc("eth_call", [{"to": token, "data": selector}, "latest"]))
                if text:
                    result[key] = text
            except Exception:
                # ERC-20 metadata is optional. The contract address remains
                # the identity; a later collector may enrich the display.
                pass
        return result

    async def _verify_created_pool(self, item):
        pool = item["pool"]
        code = await self.rpc("eth_getCode", [pool, "latest"])
        if not isinstance(code, str) or not re.fullmatch(r"0x[0-9a-fA-F]{32,}", code):
            raise ValueError("created-pool-code-unavailable")
        for selector, expected in (("0x0dfe1681", item["token0"]),
                                   ("0xd21220a7", item["token1"]),
                                   (FACTORY_SELECTOR, item["factory"])):
            actual = _word_address(await self.rpc("eth_call", [{"to": pool, "data": selector}, "latest"]))
            if actual != expected:
                raise ValueError("created-pool-identity-mismatch")
        if (await self._canonical(item["creationBlock"]))["hash"] != item["creationBlockHash"]:
            raise RuntimeError("creation-block-reorg")

    async def _stock_catalogue(self):
        result = {}
        for stock in await self.store.all("stock"):
            address = str(stock.get("tokenContractAddress") or "").lower()
            identity = token_identity(self.chain, address, stock.get("stockCode"))
            if identity["eligibleForPair"]:
                result.setdefault(identity["underlyingId"], []).append(stock)
        return result

    async def _persist_match(self, item, stock, stock_side, meme, metadata):
        """Preserve concurrent quotes/liquidity while adding creation evidence."""
        now = int(time.time() * 1000)
        asset = await self.store.get("asset", meme)
        asset_patch = {
            "poolCreatedAt": min(int(asset.get("poolCreatedAt") or item["poolCreatedAt"]), item["poolCreatedAt"]) if asset else item["poolCreatedAt"],
            "discoveredAt": min(int(asset.get("discoveredAt") or item["discoveredAt"]), item["discoveredAt"]) if asset else item["discoveredAt"],
        }
        if not asset:
            asset_patch.update({"token": meme, "chain": self.chain, "chainId": self.chain,
                                "kind": "candidate", "firstSeen": item["discoveredAt"],
                                "discoveryEventPending": False})
        if metadata.get("symbol") and not (asset or {}).get("symbol"):
            asset_patch["symbol"] = metadata["symbol"]
        if metadata.get("name") and not (asset or {}).get("name"):
            asset_patch["name"] = metadata["name"]
        await self.store.patch_fact("asset", meme, asset_patch)
        pool_patch = {
            "pool": item["pool"], "token0": item["token0"], "token1": item["token1"],
            "queryToken": meme, "protocol": next((factory.label for factory in self.factories
                if factory.address == item["factory"] and factory.label),
                "Uniswap V3" if item["dex"] == "uniswap_v3" else "Uniswap V2"),
            "poolCreatedAt": item["poolCreatedAt"], "discoveredAt": item["discoveredAt"],
            "creationTx": item["creationTx"], "creationBlock": item["creationBlock"],
            "creationBlockHash": item["creationBlockHash"], "creationLogIndex": item["creationLogIndex"],
            "creationStatus": "confirmed", "factoryEventId": item["id"], "factory": item["factory"],
            "checkedAt": now,
        }
        await self.store.patch_fact("pool", item["pool"], pool_patch)
        stock_address = str(stock["tokenContractAddress"]).lower()
        relation_id = f"{self.chain}:{item['pool']}:{meme}:{stock_address}"
        stock_identity = token_identity(self.chain, stock_address, stock.get("stockCode"))
        async with self.store._guard_write():
            await self.store.db.execute("BEGIN IMMEDIATE")
            try:
                row = await self.store.fetchone("SELECT body FROM facts WHERE kind=? AND id=?",
                                               (self.store.key("relation"), relation_id))
                previous = json.loads(row[0]) if row else None
                checked_at = (previous or {}).get("checkedAt") if (previous or {}).get("factoryEventId") == item["id"] else None
                checked_at = int(checked_at or now)
                relation = {**(previous or {}),
                    "id": relation_id, "chainId": self.chain, "token": meme,
                    "stock": stock_address, "stockSide": stock_side,
                    "ticker": stock_identity.get("ticker") or stock.get("stockCode"),
                    "pool": item["pool"], "token0": item["token0"], "token1": item["token1"],
                    "wrapper": stock_address != stock_side,
                    "protocol": pool_patch["protocol"], "factory": item["factory"], "factoryVerified": True,
                    "poolType": item["dex"], "feeTier": item.get("fee"), "firstSeen": (previous or {}).get("firstSeen") or item["discoveredAt"],
                    "checkedAt": checked_at, "block": item["creationBlock"],
                    "status": "verified", "error": None,
                    "poolCreatedAt": item["poolCreatedAt"], "discoveredAt": item["discoveredAt"],
                    "creationTx": item["creationTx"], "creationBlock": item["creationBlock"],
                    "creationBlockHash": item["creationBlockHash"], "creationLogIndex": item["creationLogIndex"],
                    "confirmationStatus": "confirmed", "factoryEventId": item["id"],
                    "creationAnnouncementPending": ((not previous) and 0 <= now - item["poolCreatedAt"] <= 120_000)
                        or bool((previous or {}).get("creationAnnouncementPending")),
                }
                relation.update(assess_pool_relation(relation, now))
                await self.store.db.execute("""INSERT INTO facts(kind,id,body) VALUES (?,?,?)
                    ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body""",
                    (self.store.key("relation"), relation_id, json.dumps(relation, ensure_ascii=False)))
                await self.store.db.commit()
            except BaseException:
                await self.store.db.rollback()
                raise
        if relation.get("creationAnnouncementPending"):
            from .main_round import emit_relation_event
            await emit_relation_event(self.store, relation, "pair-observed", "官方股票配对池已创建，流动性待核验",
                                      metadata.get("symbol"))
            await self.store.patch_fact("relation", relation_id, {"creationAnnouncementPending": False})
        return relation_id

    async def process_confirmed(self, *, limit=4):
        """Verify and attach confirmed creations without losing work on restart."""
        if not 1 <= limit <= 100:
            raise ValueError("invalid-processing-limit")
        await self._init()
        retracted = await self._retract_orphans()
        now = int(time.time() * 1000)
        rows = await self.store.fetchall("""SELECT body FROM factory_discovery_events
            WHERE chain=? AND status='confirmed' AND processing_status IN ('pending','retry')
              AND next_attempt_at<=? ORDER BY block DESC,log_index DESC LIMIT ?""", (self.chain, now, limit))
        catalogue = await self._stock_catalogue() if rows else {}
        result = {"requested": len(rows), "accepted": 0, "updated": retracted,
                  "failed": 0, "unsupported": 0, "waitingCatalogue": 0}
        for row in rows:
            item = json.loads(row[0])
            try:
                if manifest_status()["status"] != "ready":
                    await self._mark_processing(item["id"], "retry", "official-manifest-unavailable", retry_ms=60_000)
                    result["failed"] += 1
                    continue
                sides = [(token, token_identity(self.chain, token)) for token in (item["token0"], item["token1"])]
                official = [(token, identity) for token, identity in sides if identity["eligibleForPair"]]
                if len(official) != 1:
                    await self._mark_processing(item["id"], "irrelevant", "no-single-official-stock-side")
                    result["unsupported"] += 1
                    continue
                stock_side, identity = official[0]
                meme = item["token1"] if stock_side == item["token0"] else item["token0"]
                candidates = catalogue.get(identity["underlyingId"]) or []
                if not candidates:
                    await self._mark_processing(item["id"], "retry", "official-stock-catalogue-missing", retry_ms=300_000)
                    result["waitingCatalogue"] += 1
                    continue
                stock = next((row for row in candidates if str(row.get("tokenContractAddress") or "").lower() == stock_side), None)
                if not stock:
                    stock = next((row for row in candidates if token_identity(self.chain, row.get("tokenContractAddress")).get("tokenKind") == "native"), candidates[0])
                await self._verify_created_pool(item)
                metadata = await self._metadata(meme)
                if str(metadata.get("symbol") or "").upper() in QUOTE_SYMBOLS:
                    await self._mark_processing(item["id"], "irrelevant", "quote-token-not-meme")
                    result["unsupported"] += 1
                    continue
                relation_id = await self._persist_match(item, stock, stock_side, meme, metadata)
                await self._mark_processing(item["id"], "matched", relation_id=relation_id)
                result["accepted"] += 1
                result["updated"] += 1
            except Exception as exc:
                await self._mark_processing(item["id"], "retry", type(exc).__name__, retry_ms=30_000)
                result["failed"] += 1
        return result
