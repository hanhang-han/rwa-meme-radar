"""Bounded native-market fast lane, independent of research writers and scans.

Catalogue reads run in a background task. A trade only touches the small
market journal; native prices never wait for USD conversion or projections.
"""
import asyncio
import json
import math
import os
import time

import httpx

from ..config import bounded_env_int
from ..demand_leases import all_leases
from ..live_market_store import NativeMarketBatchConnection, catalogue_store, market_store
from .chain_stream import (ChainPoolStream, FACT_UPSERT, TOPICS, SWAP_V2, SWAP_V3,
                          address, event_key, now_ms, number)


def positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def choose_markets(pools, assets, watches, capacity, meme_limit=20, stock_limit=10):
    """Choose watched identities first, then one verified pool per popular token.

    No ticker inference: stock/candidate classification comes from catalogue
    facts. A viewer's explicit pool is preserved, subject to bounded capacity.
    """
    valid = {}
    for row in pools:
        pool, t0, t1 = (address(row.get(k)) for k in ('pool', 'token0', 'token1'))
        if (not pool or not t0 or not t1 or t0 == t1
                or row.get('creationStatus') == 'orphaned'
                or row.get('verificationStatus') == 'reorged'):
            continue
        valid[pool] = {**row, 'pool': pool, 'token0': t0, 'token1': t1}
    selected, bases = {}, {}

    def add(pool, token):
        if pool not in valid or token not in (valid[pool]['token0'], valid[pool]['token1']):
            return False
        if pool not in selected and len(selected) >= capacity:
            return False
        selected[pool] = valid[pool]
        bases.setdefault(pool, set()).add(token)
        return True

    for watch in sorted(watches, key=lambda w: w.get('expiresAt') or 0, reverse=True):
        if (watch.get('expiresAt') or 0) > now_ms():
            add(address(watch.get('pool')), address(watch.get('address')))
    by_token = {}
    for pool, row in valid.items():
        for token in (row['token0'], row['token1']):
            asset = assets.get(token) or {}
            if asset.get('kind') not in ('stock', 'candidate'):
                continue
            score = (positive(row.get('volume24h')) or positive(row.get('volumeUsd24h')),
                     positive(row.get('liquidityUsd')), row.get('updatedAt') or row.get('at') or 0, pool)
            old = by_token.get(token)
            if old is None or score > old[0]:
                by_token[token] = (score, pool)
    for kind, limit in (('stock', stock_limit), ('candidate', meme_limit)):
        tokens = sorted((token for token in by_token if assets[token].get('kind') == kind),
                        key=lambda token: (positive(assets[token].get('volume24h')),
                                           positive(assets[token].get('liquidityUsd')),
                                           positive(assets[token].get('marketCap')), token), reverse=True)
        for token in tokens[:limit]:
            add(by_token[token][1], token)
    return selected, bases


class HotMarketStream(ChainPoolStream):
    write_derived_quotes = False
    owns_retention = True

    def __init__(self, chain):
        super().__init__(chain)
        self.capacity = bounded_env_int('LIVE_MARKET_POOL_LIMIT', 32, 1, 64)
        self.meme_limit = bounded_env_int('LIVE_MARKET_MEME_LIMIT', 20, 0, 40)
        self.stock_limit = bounded_env_int('LIVE_MARKET_STOCK_LIMIT', 10, 0, 30)
        self.selected_bases = {}
        self.catalogue_task = None
        self.catalogue_changed = False
        self.catalogue_started = False
        self.catalogue_ready = False
        self.catalogue_error = None
        self.catalogue_attempt_at = 0
        self.retention_task = None
        self.last_hot_prune = 0
        self.selection_generation = 0
        self.recent_scan_metrics = {}
        self.recent_scan_task = None
        self.recent_reorg_generation = 0
        self.fallback_http_verified = set()
        # Only native trade bars are produced here. Cold quote/relation
        # derivations still have their original research collector owner.
        self.relations = {}

    async def open_store(self):
        scoped = await market_store(self.chain)
        async with scoped._guard_write():
            await scoped.db.execute('CREATE INDEX IF NOT EXISTS candles_open_time ON candles(openTime)')
            await scoped.db.execute("CREATE INDEX IF NOT EXISTS live_acc_time ON facts(kind,json_extract(body,'$.t'))")
            await scoped.db.commit()
        return scoped

    def bases(self, pool):
        return sorted(self.selected_bases.get(pool['pool'], set()))

    async def _open_trade_db(self):
        await super()._open_trade_db()
        if self.trade_db is not None and not isinstance(self.trade_db, NativeMarketBatchConnection):
            self.trade_db = NativeMarketBatchConnection(self.trade_db)
            self.trade_store.db = self.trade_db

    async def rpc(self, method, params):
        try:
            return await super().rpc(method, params)
        except (httpx.HTTPStatusError, httpx.RequestError) as error:
            status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            if (self.chain != '56' or method != 'eth_getLogs' or not self.http_reads_enabled
                    or self.http is None or status not in (None, 403, 404, 408, 500, 502, 503, 504)):
                raise
            # PublicNode refuses some old BNB ranges even when recent logs
            # succeed. Use one separately chain-verified HTTP attempt; never
            # push log replay onto the live subscription socket.
            fallback = os.environ.get('BSC_LIVE_HTTP_FALLBACK_URL', 'https://1rpc.io/bnb').strip()
            if not fallback.startswith('https://'):
                raise RuntimeError('bnb-http-replay-unavailable') from None
            async with self.rpc_gate:
                async def call(call_method, call_params):
                    deadline = max(self.rpc_cooldown_until,
                        (self.last_rpc + self.rpc_interval) if self.last_rpc is not None else 0)
                    if deadline > time.monotonic():
                        await asyncio.sleep(deadline - time.monotonic())
                    self.last_rpc = time.monotonic()
                    response = await self.http.post(fallback, timeout=4, json={
                        'jsonrpc': '2.0', 'id': 1, 'method': call_method, 'params': call_params})
                    if response.status_code == 429:
                        self.rpc_rate_failures += 1
                        delay = min(30, 2 ** min(self.rpc_rate_failures, 5))
                        self.rpc_rate_limited_at = now_ms()
                        self.rpc_cooldown_until = time.monotonic() + delay
                        self.rpc_cooldown_wall = self.rpc_rate_limited_at + delay * 1000
                    response.raise_for_status()
                    body = response.json()
                    if body.get('error') or 'result' not in body:
                        raise RuntimeError('bnb-fallback-rpc-rejected')
                    return body['result']
                try:
                    if fallback not in self.fallback_http_verified:
                        if number(await call('eth_chainId', [])) != 56:
                            raise RuntimeError('wrong-fallback-chain-id')
                        self.fallback_http_verified.add(fallback)
                    return await call(method, params)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.fallback_http_verified.discard(fallback)
                    # Endpoint strings can contain credentials. Diagnostic
                    # facts keep a fixed category, never an HTTP/driver URL.
                    raise RuntimeError('bnb-http-replay-unavailable') from None

    async def retraction_pool(self, pool_id):
        return self.pools.get(pool_id) or await self.s.get('pool', pool_id)

    def retraction_bases(self, pool):
        tokens = set(self.bases(pool))
        for record in self.market_registry.values():
            definition = record.get('definition') or {}
            if definition.get('pool_id') == pool['pool']:
                tokens.add(definition['token'])
        return sorted(tokens)

    async def catalogue(self):
        if not self.catalogue_started:
            self.catalogue_started = True
            await self.refresh_catalogue()
        elif now_ms() - self.last_catalogue >= 60_000 and now_ms() - self.catalogue_attempt_at >= 15_000:
            self.request_catalogue_refresh()
        changed, self.catalogue_changed = self.catalogue_changed, False
        return changed

    def request_catalogue_refresh(self):
        if self.catalogue_task is not None and not self.catalogue_task.done():
            return
        self.catalogue_attempt_at = now_ms()

        async def refresh():
            try:
                await asyncio.wait_for(self.refresh_catalogue(), 20)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.catalogue_error = type(error).__name__
                print('[live-market] catalogue refresh failed for ' + self.chain + ': ' + type(error).__name__, flush=True)
        self.catalogue_task = asyncio.create_task(refresh())

    async def refresh_catalogue(self):
        catalogue = await catalogue_store(self.chain)
        if not self.market_registry:
            self.market_registry = dict(await self.s.all_kv('market-registry'))
        # Snapshot selection outside both trade mutation and market writer
        # locks. Disk-heavy catalogue work can never hold the live writer.
        assets = {address(row.get('token')): row for row in await catalogue.all('asset')
                  if address(row.get('token'))}
        # Select the real trading side of stock/Meme pools. A wrapper keeps
        # its own address and units, never its underlying native price.
        from ..stock_identity import token_identity
        for token, row in assets.items():
            if row.get('kind') == 'wrapped_stock' and token_identity(self.chain, token).get('eligibleForPair'):
                assets[token] = {**row, 'kind': 'stock', 'tokenKind': 'wrapper-current'}
        rows = await catalogue.all('pool')
        watches = await all_leases(self.s, 'candle-watch')
        pools, bases = choose_markets(rows, assets, watches, self.capacity, self.meme_limit, self.stock_limit)
        wanted_tokens = {token for row in pools.values() for token in (row['token0'], row['token1'])}
        kept_assets = {token: assets[token] for token in wanted_tokens if token in assets}
        quotes = [row for row in await catalogue.all('pool-quote') if address(row.get('pool')) in pools]
        facts = [('asset', token, row) for token, row in kept_assets.items()]
        facts += [('pool', pool, row) for pool, row in pools.items()]
        # Copy only dated recent oracle evidence. Every live USD lookup is
        # performed on this file; absent/stale evidence leaves USD unknown.
        if self.chain == '56':
            oracle_rows = await catalogue.fetchall(
                "SELECT id,body FROM facts WHERE kind=? AND json_extract(body,'$.updatedAt')>=? LIMIT 1000",
                (catalogue.key('oracle-feed'), now_ms() - 900_000))
            facts += [('oracle-feed', key, json.loads(body)) for key, body in oracle_rows]
        encoded = [self._fact_params(kind, key, value) for kind, key, value in facts]
        # Bounded batches with byte-identical no-ops skipped. The existing
        # selected set is replaced only after all necessary copies commit.
        for offset in range(0, len(encoded), 32):
            async with self.s._guard_write():
                await self.s.db.executemany(FACT_UPSERT + ' WHERE facts.body IS NOT excluded.body', encoded[offset:offset+32])
                await self.s.db.commit()
            await asyncio.sleep(0)
        changed = set(pools) != set(self.pools)
        async with self.lock:
            added = {pool for pool, tokens in bases.items() if tokens - self.selected_bases.get(pool, set())}
            selection_changed = bases != self.selected_bases
            self.pools, self.selected_bases, self.assets = pools, bases, kept_assets
            # A newly selected pool/base gets a bounded overlap, never the
            # original factory history or another pool's coverage identity.
            for pool in pools.values():
                pool.pop('streamBackfillFromBlock', None)
            self.pending_pools.intersection_update(pools)
            self.pending_pools.update(added)
            if selection_changed:
                self.selection_generation += 1
            for row in quotes:
                for side in (0, 1):
                    decimals = row.get('decimals' + str(side))
                    if isinstance(decimals, int) and 0 <= decimals <= 36:
                        self.decimals[pools[address(row['pool'])]['token' + str(side)]] = decimals
            records = []
            for pool in pools.values():
                for token in self.bases(pool):
                    market = self.market(pool, token)
                    record = market.record()
                    records.append(self._fact_params('market-registry', market.storage, record))
                    self.market_registry[market.storage] = record
            selection = {'poolIds': sorted(pools), 'tokenIds': sorted({token for tokens in bases.values() for token in tokens}),
                         'basesByPool': {pool: sorted(tokens) for pool, tokens in bases.items()},
                         'updatedAt': now_ms(), 'capacity': self.capacity, 'scope': 'popular-and-watched',
                         'generation': self.selection_generation,
                         'historyPolicy': 'observed-from-selection', 'usdPolicy': 'dated-evidence-only'}
            records.append(self._fact_params('live-selection', 'pools', selection))
            async with self.s._guard_write():
                for pool in added:
                    await self.s.db.execute('UPDATE chain_stream_logs SET processed=0 WHERE chain=? AND pool=? AND block>=?',
                        (self.chain, pool, max(0, self.latest_head-32)))
                await self.s.db.executemany(FACT_UPSERT + ' WHERE facts.body IS NOT excluded.body', records)
                await self.s.db.commit()
        self.last_catalogue = now_ms()
        self.catalogue_error = None
        self.catalogue_ready = True
        self.catalogue_changed |= changed

    async def refresh_watches(self):
        await super().refresh_watches()
        # A pool can already be selected in the opposite token direction.
        # Route missing bases through the same bounded catalogue selection.
        missing_base = any(token not in self.selected_bases.get(pool, set())
                           for pool, token in self.watched_bars)
        if self.watched_pools - set(self.pools) or missing_base:
            self.request_catalogue_refresh()

    async def recover_live_queue(self, reader):
        # Subscribe immediately on a verified connection. Retry head and the
        # old bounded queue are retained and consumed by the normal consumer;
        # durable range replay repairs any disconnected/overflow interval.
        if reader.done():
            await reader
            raise RuntimeError('chain-stream-recovery-connection-closed')
        self.recovery_needed = False

    async def _recent_pending_logs(self, logs):
        """Resume a dense block without truncating its durable coverage."""
        relevant = [log for log in logs if not self.irrelevant_sync(log)]
        processed = set()
        for offset in range(0, len(relevant), 128):
            keys = [event_key(log) for log in relevant[offset:offset + 128]]
            if keys:
                rows = await self.s.fetchall('SELECT id FROM chain_stream_logs WHERE chain=? '
                    'AND processed=1 AND id IN (' + ','.join('?' for _ in keys) + ')',
                    (self.chain, *keys))
                processed.update(row[0] for row in rows)
        return [log for log in relevant if event_key(log) not in processed]

    async def _verify_recent_trades(self, pools, logs):
        expected = []
        for log in logs:
            if str((log.get('topics') or [''])[0]).lower() not in (SWAP_V2.lower(), SWAP_V3.lower()):
                continue
            pool = self.pools[address(log['address'])]
            for token in self.bases(pool):
                expected.append((self.s.key(self.market(pool, token).storage),
                                 'chain:' + self.chain + ':' + event_key(log)))
        for offset in range(0, len(expected), 128):
            batch = expected[offset:offset + 128]
            by_asset = {}
            for asset, ident in batch:
                by_asset.setdefault(asset, []).append(ident)
            selectors, parameters = [], []
            for asset, ids in by_asset.items():
                selectors.append('(asset=? AND id IN (' + ','.join('?' for _ in ids) + '))')
                parameters.extend((asset, *ids))
            # Keep this an ordinary indexed table read. The SQLite-compatible
            # adapter intentionally rejects implicit-star VALUES CTE sources.
            rows = await self.s.fetchall('SELECT asset,id FROM trades WHERE ' + ' OR '.join(selectors),
                                         tuple(parameters))
            if set(batch) != {(row[0], row[1]) for row in rows}:
                raise RuntimeError('recent-swap-not-persisted')

    async def _persist_recent_proofs(self, pools, current, start, end_header, generation, head_height, reorg_generation):
        """Merge exact-pool proof without touching trade/candle source clocks."""
        end, stamp = number(end_header['number']), now_ms()
        async with self.lock:
            if generation != self.selection_generation or any(pool not in self.pools for pool in pools):
                raise RuntimeError('market-selection-changed-during-replay')
            if reorg_generation != self.recent_reorg_generation:
                raise RuntimeError('chain-reorg-during-recent-range')
            async with self.s._guard_write():
                await self.s.db.execute('BEGIN IMMEDIATE')
                try:
                    patches = []
                    for pool_id in pools:
                        old = current.get(pool_id) or {}
                        previous = old.get('block')
                        contiguous = (old.get('canonical') is True and isinstance(previous, int)
                                      and start <= previous + 1
                                      and old.get('selectedBases') == self.bases(self.pools[pool_id]))
                        coverage_from = old.get('coverageFrom', start) if contiguous else start
                        anchors = [anchor for anchor in old.get('anchors') or [] if anchor['block'] < end]
                        anchors.append({'block': end, 'hash': end_header['hash'].lower()})
                        proof = {'scope': 'exact-pool', 'pool': pool_id, 'chainId': self.chain,
                            'canonical': True, 'decoded': True, 'fromBlock': start, 'throughBlock': end,
                            'coverageFromBlock': coverage_from, 'blockTime': number(end_header['timestamp']) * 1000,
                            'verifiedAt': stamp, 'hash': end_header['hash'].lower(),
                            'headBlockAtScan': head_height, 'selectionGeneration': generation,
                            'transport': 'http' if self.chain == '56' and self.http_reads_enabled else 'rpc'}
                        record = {'pool': pool_id, 'block': end, 'hash': proof['hash'],
                            'coverageFrom': coverage_from, 'blockTime': proof['blockTime'],
                            'updatedAt': stamp, 'canonical': True, 'anchors': anchors[-64:],
                            'selectedBases': self.bases(self.pools[pool_id])}
                        for key in ('rebasedAt', 'rebasedFromBlock', 'previousCoverageFrom', 'reorgAt'):
                            if key in old:
                                record[key] = old[key]
                        if isinstance(previous, int) and start > previous + 1:
                            gap = {'pool': pool_id, 'fromBlock': previous + 1, 'throughBlock': start - 1,
                                   'recordedAt': stamp, 'status': 'historical-backfill-pending'}
                            patches.append(self._fact_params('pool-live-gap', pool_id + ':' + str(previous + 1), gap))
                            record.update(rebasedAt=stamp, rebasedFromBlock=previous,
                                          previousCoverageFrom=old.get('coverageFrom'))
                        patches.append(self._fact_params('pool-live-cursor', pool_id, record))
                        for token in self.bases(self.pools[pool_id]):
                            market = self.market(self.pools[pool_id], token)
                            for bar in self.bars(market):
                                key = market.candle_key(bar)
                                meta = await self.s.get('candle-meta', key) or {}
                                patches.append(self._fact_params('candle-meta', key, {
                                    **market.frame(), **meta, 'storage': market.storage,
                                    'source': 'Chain RPC', 'coverage': 'observed', 'poolScan': proof}))
                    await NativeMarketBatchConnection(self.s.db).executemany(FACT_UPSERT, patches)
                    await self.s.db.commit()
                except BaseException:
                    await self.s.db.rollback()
                    raise

    async def _invalidate_recent_proofs(self, pool_ids, *, reorg_at=None):
        stamp = reorg_at or now_ms()
        async with self.s._guard_write():
            await self.s.db.execute('BEGIN IMMEDIATE')
            try:
                patches = []
                for pool_id in pool_ids:
                    cursor = await self.s.get('pool-live-cursor', pool_id)
                    if cursor:
                        patches.append(self._fact_params('pool-live-cursor', pool_id, {
                            **cursor, 'canonical': False, 'reorgAt': stamp}))
                    pool = self.pools.get(pool_id)
                    if not pool:
                        continue
                    for token in self.bases(pool):
                        market = self.market(pool, token)
                        for bar in self.bars(market):
                            key = market.candle_key(bar)
                            meta = await self.s.get('candle-meta', key)
                            if meta and isinstance(meta.get('poolScan'), dict):
                                patches.append(self._fact_params('candle-meta', key, {
                                    **meta, 'poolScan': {**meta['poolScan'], 'canonical': False, 'reorgAt': stamp}}))
                await NativeMarketBatchConnection(self.s.db).executemany(FACT_UPSERT, patches)
                await self.s.db.commit()
            except BaseException:
                await self.s.db.rollback()
                raise

    async def retract_block(self, block_hash):
        self.recent_reorg_generation += 1
        known = await self.s.fetchone('SELECT MIN(block) FROM chain_stream_logs WHERE chain=? AND hash=?',
                                     (self.chain, block_hash))
        await super().retract_block(block_hash)
        if known and isinstance(known[0], int):
            # A removal invalidates descendant empty-range proofs as well as
            # bars containing that block. Do not wait for their age to expire.
            cursors = await self.s.all_kv('pool-live-cursor')
            affected = [pool for pool, row in cursors if (row.get('block') or 0) >= known[0]]
            await self._invalidate_recent_proofs(affected)

    async def scan_recent_pools(self, head, *, max_pages=4, event_budget=64, budget_seconds=8):
        """Give every selected pool bounded recent turns independent of old replay.

        A recent rebase is explicitly recorded as a gap. The original history
        cursor never moves here. Dense blocks can commit a bounded prefix of
        logs, but cannot advance or certify coverage until every log is durable.
        """
        self.remember_header(head)
        height, began = number(head['number']), time.monotonic()
        generation = self.selection_generation
        current = dict(await self.s.all_kv('pool-live-cursor'))
        candidates = sorted(self.pools, key=lambda pool: (
            (current.get(pool) or {}).get('updatedAt') or 0, pool not in self.watched_pools, pool))
        result = {'verifiedPools': 0, 'processedLogs': 0, 'partialPages': 0, 'head': height}
        canonical = {}
        for offset in range(0, min(len(candidates), max_pages * 8), 8):
            if time.monotonic() - began >= budget_seconds or generation != self.selection_generation:
                break
            pools = candidates[offset:offset + 8]
            reorg_generation = self.recent_reorg_generation
            starts = []
            for pool in pools:
                previous = current.get(pool) or {}
                block = previous.get('block')
                if isinstance(block, int) and block > height:
                    raise RuntimeError('recent-checkpoint-ahead-of-head')
                if isinstance(block, int) and previous.get('hash') and previous.get('canonical') is True:
                    if block not in canonical:
                        canonical[block] = await self.rpc('eth_getBlockByNumber', [hex(block), False])
                    anchor = canonical[block]
                    if not anchor or anchor['hash'].lower() != previous['hash'].lower():
                        # Retain the old checkpoint and historical cursor. A
                        # fresh bounded interval is observed on the next turn,
                        # while canonical historical replay repairs older bars.
                        await self._invalidate_recent_proofs([pool])
                        raise RuntimeError('recent-checkpoint-reorged')
                same_bases = (previous.get('canonical') is True
                              and previous.get('selectedBases') == self.bases(self.pools[pool]))
                start = block + 1 if isinstance(block, int) and same_bases else max(0, height - 31)
                # An interrupted recent lane must not become another all-day
                # replay queue. Record the skipped interval when it is rebased.
                if height - start > 299:
                    start = max(0, height - 31)
                starts.append(min(start, height))
            start = min(starts)
            end = min(height, start + 299)
            before = await self.rpc('eth_getBlockByNumber', [hex(end), False])
            if not before:
                raise RuntimeError('recent-range-anchor-unavailable')
            while True:
                logs = await self.rpc('eth_getLogs', [{'fromBlock': hex(start), 'toBlock': hex(end),
                    'address': pools, 'topics': [TOPICS]}])
                if not isinstance(logs, list):
                    raise RuntimeError('invalid-recent-log-range')
                if any(address(log.get('address')) not in pools or log.get('removed')
                       or str((log.get('topics') or [''])[0]).lower() not in {topic.lower() for topic in TOPICS}
                       or not start <= number(log.get('blockNumber')) <= end for log in logs):
                    raise RuntimeError('invalid-recent-log-scope')
                pending = await self._recent_pending_logs(logs)
                remaining = max(0, event_budget - result['processedLogs'])
                if len(pending) <= remaining or start == end:
                    break
                end = start + (end - start) // 2
                before = await self.rpc('eth_getBlockByNumber', [hex(end), False])
                if not before:
                    raise RuntimeError('recent-range-anchor-unavailable')
            ordered = sorted(pending, key=lambda log: (
                number(log['blockNumber']), number(log.get('transactionIndex', '0x0')), number(log['logIndex'])))
            received = now_ms()
            for log in ordered[:remaining]:
                if time.monotonic() - began >= budget_seconds:
                    break
                await self.process_log({**log, '_receivedAt': received})
                result['processedLogs'] += 1
                await asyncio.sleep(0)
            if await self._recent_pending_logs(logs):
                result['partialPages'] += 1
                continue
            await self._verify_recent_trades(pools, logs)
            checked = await self.rpc('eth_getBlockByNumber', [hex(end), False])
            if not checked or checked['hash'].lower() != before['hash'].lower():
                raise RuntimeError('chain-reorg-during-recent-range')
            hashes = {}
            for log in logs:
                block, block_hash = number(log['blockNumber']), str(log['blockHash']).lower()
                if block in hashes and hashes[block] != block_hash:
                    raise RuntimeError('chain-reorg-during-recent-range')
                hashes[block] = block_hash
            rows = await self.s.fetchall('SELECT DISTINCT block,hash FROM chain_stream_logs '
                'WHERE chain=? AND pool IN (' + ','.join('?' for _ in pools) + ') AND block>=? AND block<=?',
                (self.chain, *pools, start, end))
            for block, block_hash in rows:
                if block not in hashes:
                    anchor = await self.rpc('eth_getBlockByNumber', [hex(block), False])
                    if not anchor:
                        raise RuntimeError('recent-canonical-block-unavailable')
                    hashes[block] = anchor['hash'].lower()
                if block_hash.lower() != hashes[block]:
                    async with self.lock:
                        await self.retract_block(block_hash)
            # Recheck after any correction and immediately before publication.
            checked = await self.rpc('eth_getBlockByNumber', [hex(end), False])
            if not checked or checked['hash'].lower() != before['hash'].lower():
                raise RuntimeError('chain-reorg-during-recent-range')
            await self._persist_recent_proofs(pools, current, start, checked, generation, height, reorg_generation)
            result['verifiedPools'] += len(pools)
        self.recent_scan_metrics = {**result, 'updatedAt': now_ms(),
            'durationMs': round((time.monotonic() - began) * 1000)}
        return result

    async def reconcile_step(self):
        current = await self.s.get('chain-stream-cursor', 'pools') or {}
        if self.replay_under_pressure():
            return {'caughtUp': False, 'head': self.latest_head, 'processedBlock': current.get('block')}
        head = await self.rpc('eth_getBlockByNumber', ['latest', False])
        # One bounded canonical range per turn. No imported research cursor
        # or unbounded factory backfill can hold this lane behind old history.
        return await self.catch_up('pools', max_ranges=1, initial_depth=2, head=head)

    async def observe_recent_pools(self):
        while True:
            if self.initialized and self.http is not None:
                attempt_at = now_ms()
                try:
                    head = await self.rpc('eth_getBlockByNumber', ['latest', False])
                    await self.scan_recent_pools(head)
                    self.recent_scan_metrics.update(lastAttemptAt=attempt_at, lastError=None)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.recent_scan_metrics.update(lastAttemptAt=attempt_at,
                        lastError=type(error).__name__, lastErrorAt=now_ms())
            await asyncio.sleep(5)

    async def run(self):
        # Old replay may wait on its queue indefinitely. Its waiting cursor
        # cannot hold the independent bounded current-pool observation task.
        task = self.recent_scan_task = asyncio.create_task(self.observe_recent_pools())
        try:
            await super().run()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self.recent_scan_task is task:
                self.recent_scan_task = None

    def schedule_housekeeping(self):
        super().schedule_housekeeping()
        if now_ms() - self.last_hot_prune > 10_000 and (self.retention_task is None or self.retention_task.done()):
            self.retention_task = asyncio.create_task(self.prune_journal())

    async def prune_journal(self):
        # All selections share one event tape, so one chain owns its global
        # retention. Per-chain raw/trade/accumulator evidence has an identical
        # eight-day window for reorg reconstruction. Every write is small and
        # an unfinished pass gets another bounded turn.
        self.last_hot_prune = now_ms()
        began = time.monotonic()
        budget = 1.0
        batch = 200
        removed = {}
        try:
            cutoff = now_ms() - 8 * 86_400_000
            # Index seeks do not decode historical JSON tape bodies before
            # reserving SQLite's sole live-market writer.
            jobs = [
                ('rawLogs', '''SELECT chain,id FROM chain_stream_logs
                    INDEXED BY chain_stream_logs_retention
                    WHERE chain=? AND at<? AND processed=1 ORDER BY at LIMIT ?''',
                 (self.chain, cutoff, batch),
                 'DELETE FROM chain_stream_logs WHERE chain=? AND id=? AND at<? AND processed=1', cutoff),
                ('trades', '''SELECT asset,id FROM trades INDEXED BY trades_scope_time
                    WHERE substr(asset,1,instr(asset,':')-1)=? AND t<? AND asset LIKE ?
                    ORDER BY t LIMIT ?''',
                 (self.chain, cutoff, self.s.key('dex:') + '%', batch),
                 'DELETE FROM trades WHERE asset=? AND id=? AND t<?', cutoff),
                ('accumulators', '''SELECT kind,id FROM facts INDEXED BY live_acc_time
                    WHERE kind=? AND json_extract(body,'$.t')<? LIMIT ?''',
                 (self.s.key('pool-candle-acc'), cutoff, batch),
                 "DELETE FROM facts WHERE kind=? AND id=? AND json_extract(body,'$.t')<?", cutoff),
            ]
            if self.chain == '196':
                latest = await self.s.fetchone('SELECT id FROM realtime_events ORDER BY id DESC LIMIT 1')
                # Sequence gaps only reduce the number retained below this
                # bound; COUNT(*) would traverse the whole event tape.
                sequence_cutoff = max(0, (latest[0] if latest else 0) - 200_000)
                age_cutoff = now_ms() - 3_600_000
                candle_cutoff = now_ms() - 30 * 86_400_000
                jobs = [
                    ('eventsCap', 'SELECT id FROM realtime_events WHERE id<=? ORDER BY id LIMIT ?',
                     (sequence_cutoff, batch),
                     'DELETE FROM realtime_events WHERE id=? AND id<=?', sequence_cutoff),
                    ('eventsAge', '''SELECT id FROM realtime_events INDEXED BY realtime_events_time
                        WHERE at<? ORDER BY at LIMIT ?''', (age_cutoff, batch),
                     'DELETE FROM realtime_events WHERE id=? AND at<?', age_cutoff),
                    *jobs,
                    ('candles', '''SELECT rowid FROM candles INDEXED BY candles_open_time
                        WHERE openTime<? ORDER BY openTime LIMIT ?''', (candle_cutoff, batch),
                     'DELETE FROM candles WHERE rowid=? AND openTime<?', candle_cutoff),
                ]
            while True:
                changed = 0
                for name, select, params, delete, boundary in jobs:
                    if time.monotonic() - began >= budget:
                        # Schedule another short pass; a large backlog must
                        # not remain behind until a one-minute timer expires.
                        self.last_hot_prune = 0
                        return {'removed': removed, 'pending': True}
                    rows = await self.s.fetchall(select, params)
                    if not rows:
                        continue
                    async with self.s._guard_write():
                        await self.s.db.executemany(delete, [(*row, boundary) for row in rows])
                        await self.s.db.commit()
                    removed[name] = removed.get(name, 0) + len(rows)
                    changed += len(rows)
                    await asyncio.sleep(.005)
                if not changed:
                    return {'removed': removed, 'pending': False}
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print('[live-market] retention failed: ' + type(error).__name__, flush=True)
            return {'removed': removed, 'pending': True, 'error': type(error).__name__}

    async def close(self):
        tasks = [task for task in (self.catalogue_task, self.retention_task, self.recent_scan_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.catalogue_task = self.retention_task = self.recent_scan_task = None
        await super().close()
