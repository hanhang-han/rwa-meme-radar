import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from contextlib import closing
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
import httpx

from app import live_market_store
from app.api import live_market
from app.collectors.market_streams import Market
from app.db import ResearchStore
from app.demand_leases import flush_lease_writer, stop_lease_writer
from app.realtime_schema import enqueue_event

TOKEN = '0x' + '1'*40
POOL = '0x' + '2'*40


def payload(frame):
    return json.loads(next(line[6:] for line in frame.decode().splitlines() if line.startswith('data: ')))


class LiveMarketApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = os.path.join(self.tmp.name, 'research.sqlite')
        self.live_path = os.path.join(self.tmp.name, 'live.sqlite')
        self.env = patch.dict(os.environ, {'LIVE_MARKET_DB': self.live_path, 'LIVE_MARKET_ENABLED': 'true'})
        self.env.start()
        self.research = patch('app.db.DB_PATH', self.original)
        self.research.start()
        self.old = await ResearchStore(self.original, '196').connect()
        self.scoped = await live_market_store.market_store('196')
        self.market = Market('196', TOKEN, 'dex', POOL, 'WBNB', 'MOON', 'pool', POOL,
                             quote_token='0x'+'3'*40)
        now = int(time.time()*1000)
        await self.scoped.put('market-registry', self.market.storage, self.market.record())
        await self.scoped.put('chain-stream', 'pools', {'status': 'live', 'updatedAt': now, 'lastHead': 100})
        await self.scoped.put('chain-stream-cursor', 'pools', {'block': 100, 'updatedAt': now, 'blockTime': now})
        await self.scoped.put('candle-meta', self.market.candle_key('5m'), {
            **self.market.frame(), 'lastSourceEventAt': now, 'lastSuccessfulAt': now,
            'coverage': 'observed', 'source': 'Chain RPC'})
        self.row = {'t': now//300000*300000, 'o': 1, 'h': 2, 'l': 1, 'c': 2, 'v': 4, 'vu': 6, 'confirmed': False}
        await self.scoped.put_candles(self.market.storage, '5m', [self.row])
        self.app = FastAPI()
        self.app.include_router(live_market.router, prefix='/api')

    async def asyncTearDown(self):
        await live_market.stop_market_hubs()
        await stop_lease_writer()
        await live_market_store.close_all()
        await self.old.close()
        self.env.stop()
        self.research.stop()
        self.tmp.cleanup()

    async def append(self, event='candle', **patches):
        packet = {**self.market.frame(), 'bar': '5m', 'row': self.row,
                  'sourceEventAt': int(time.time()*1000), 'receivedAt': int(time.time()*1000), **patches}
        async with self.scoped._guard_write():
            seq = await enqueue_event(self.scoped.db, event, packet)
            await self.scoped.db.commit()
        return seq

    def subscriber(self):
        return live_market.Subscriber('196', TOKEN, POOL, '5m', 'WBNB')

    async def test_open_stream_renews_original_sidecar_demand_without_http_polling(self):
        clock = [100.0]
        wall = time.time()
        fake_time = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: wall+clock[0]-100)
        lease_path = self.original + '.leases.sqlite'
        with patch.dict(os.environ, {'DEMAND_LEASE_DB': lease_path}), \
             patch.object(live_market, 'time', fake_time), \
             patch.object(live_market, 'publish_market_watch', wraps=live_market.publish_market_watch) as renew:
            stream = live_market.market_frames(self.subscriber())
            try:
                await asyncio.wait_for(anext(stream), 1)
                self.assertEqual(renew.call_count, 1)
                clock[0] += 16
                await self.append()
                await asyncio.wait_for(anext(stream), 1)
                self.assertEqual(renew.call_count, 2)
                clock[0] += 1
                await self.append()
                await asyncio.wait_for(anext(stream), 1)
                self.assertEqual(renew.call_count, 2)
                await flush_lease_writer()
                with closing(sqlite3.connect(lease_path)) as leases:
                    rows = leases.execute('SELECT kind,body,expiresAt FROM leases').fetchall()
                self.assertEqual(len(rows), 1)
                kind, body, expires = rows[0]
                self.assertEqual(kind, '196:candle-watch')
                self.assertEqual(json.loads(body)['pool'], POOL)
                self.assertGreaterEqual(expires, int((wall+16)*1000)+120000-1)
            finally:
                await stream.aclose()

    async def test_history_lease_identity_and_current_quiet_are_honest(self):
        response = await live_market.get_candles('196', TOKEN, POOL, '5m', 500)
        self.assertTrue(response['liveMarket'])
        self.assertEqual((response['pool'], response['priceCurrency'], response['coverage']), (POOL, 'WBNB', 'observed'))
        self.assertEqual(response['rows'][0]['c'], 2)
        await flush_lease_writer()
        lease = self.original+'.leases.sqlite'
        self.assertTrue(os.path.isfile(lease))
        self.assertFalse(os.path.exists(self.live_path+'.leases.sqlite'))
        with closing(sqlite3.connect(lease)) as db:
            row = db.execute('SELECT kind,body FROM leases').fetchone()
        self.assertEqual(row[0], '196:candle-watch')
        self.assertEqual(json.loads(row[1])['pool'], POOL)
        await self.scoped.put('candle-meta', self.market.candle_key('5m'), {'lastSourceEventAt': int(time.time()*1000)-120000})
        quiet = await live_market.get_candles('196', TOKEN, POOL, '5m', 500)
        self.assertEqual((quiet['stale'], quiet['marketStatus']), (False, 'quiet'))
        await self.scoped.put('chain-stream', 'pools', {'status': 'disconnected', 'updatedAt': int(time.time()*1000)})
        stale = await live_market.get_candles('196', TOKEN, POOL, '5m', 500)
        self.assertTrue(stale['stale'])

    async def test_inactive_registered_pool_wakes_without_claiming_selected_coverage(self):
        await self.scoped.put('live-selection', 'pools', {'poolIds': [], 'tokenIds': [], 'basesByPool': {}})
        with patch.object(live_market, 'publish_market_watch', wraps=live_market.publish_market_watch) as watch:
            pending = await live_market.get_markets('196', TOKEN, POOL)
            self.assertEqual((pending['selectionStatus'], pending['watchPool']), ('pending', POOL))
            self.assertFalse(pending['liveMarket'])
            self.assertEqual(pending['markets'], [])
            self.assertTrue(pending['watchRequested'])
            watch.assert_called_once_with('196', TOKEN, POOL, '5m')
            # History and stream eligibility do not change until the worker
            # commits actual selection; old registry alone is insufficient.
            self.assertFalse((await live_market.get_candles('196', TOKEN, POOL))['liveMarket'])
            with self.assertRaises(HTTPException) as refused:
                await live_market.live_stream('196', TOKEN, POOL)
            self.assertEqual(refused.exception.status_code, 404)
        await flush_lease_writer()
        with closing(sqlite3.connect(self.original+'.leases.sqlite')) as db:
            rows = db.execute('SELECT body FROM leases').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0])['pool'], POOL)
        await self.scoped.put('live-selection', 'pools', {'poolIds': [POOL], 'tokenIds': [TOKEN], 'basesByPool': {POOL: [TOKEN]}})
        selected = await live_market.get_markets('196', TOKEN, POOL)
        self.assertEqual(selected['selectionStatus'], 'selected')
        self.assertTrue(selected['markets'][0]['liveMarket'])

    async def test_ordinary_asset_link_wakes_one_real_pool_and_unknown_identity_does_not(self):
        other_pool = '0x'+'4'*40
        other = Market('196', TOKEN, 'dex', other_pool, 'USDT', 'MOON', 'pool', other_pool)
        await self.scoped.put('market-registry', other.storage, other.record())
        await self.scoped.put('live-selection', 'pools', {'poolIds': [], 'tokenIds': [], 'basesByPool': {}})
        with patch.object(live_market, 'publish_market_watch', wraps=live_market.publish_market_watch) as watch:
            pending = await live_market.get_markets('196', TOKEN)
            self.assertEqual(pending['selectionStatus'], 'pending')
            self.assertIn(pending['watchPool'], (POOL, other_pool))
            self.assertEqual(watch.call_count, 1)
            self.assertFalse(pending['liveMarket'])
            for address, pool in ((TOKEN, '0x'+'8'*40), ('0x'+'9'*40, POOL)):
                absent = await live_market.get_markets('196', address, pool)
                self.assertEqual(absent['selectionStatus'], 'unregistered')
                self.assertFalse(absent['watchRequested'])
            self.assertEqual(watch.call_count, 1)

    async def test_old_writer_held_ten_seconds_does_not_block_history_commit_or_sse(self):
        # Both the SQLite external lock and the in-process research writer
        # lock remain held for this entire interval. Live I/O must finish
        # while they are held, rather than merely retry after their release.
        old_connection = sqlite3.connect(self.original, timeout=.1)
        old_connection.execute('BEGIN IMMEDIATE')
        await self.old._write_lock.acquire()
        generator = live_market.market_frames(self.subscriber(), 0)
        try:
            started = time.monotonic()
            watched = await asyncio.wait_for(live_market.get_markets('196', TOKEN, POOL), 1)
            self.assertEqual(watched['selectionStatus'], 'selected')
            await asyncio.wait_for(live_market.get_candles('196', TOKEN, POOL, '5m', 10), 1)
            self.assertEqual(payload(await asyncio.wait_for(anext(generator), 1))['liveMarket'], True)
            seq = await asyncio.wait_for(self.append(), 1)
            observed = payload(await asyncio.wait_for(anext(generator), 1))
            self.assertEqual(observed['row']['c'], 2)
            self.assertEqual(seq, 1)
            self.assertLess(time.monotonic()-started, 2)
            await asyncio.sleep(max(0, 10-(time.monotonic()-started)))
            self.assertTrue(old_connection.in_transaction)
        finally:
            await generator.aclose()
            self.old._write_lock.release()
            old_connection.rollback()
            old_connection.close()

    async def test_replay_reorg_filter_and_cursor_realms(self):
        await self.append(priceCurrency='USD')
        await self.append(poolId='0x'+'4'*40, pool='0x'+'4'*40)
        match = await self.append()
        reset = await self.append('candle-reset', reason='chain-reorg')
        epoch = await live_market.stream_epoch(self.scoped)
        stream = live_market.market_frames(self.subscriber(), 0, epoch)
        try:
            await anext(stream)
            frame = await asyncio.wait_for(anext(stream), 1)
            self.assertIn(f'id: {match}\n'.encode(), frame)
            frame = await asyncio.wait_for(anext(stream), 1)
            self.assertIn(b'event: market.reset', frame)
            self.assertIn(f'id: {reset}\n'.encode(), frame)
        finally:
            await stream.aclose()
        async with self.scoped._guard_write():
            await self.scoped.db.execute('DELETE FROM realtime_events WHERE id<?', (match,))
            await self.scoped.db.commit()
        for after, realm, reason in ((0, epoch, 'cursor-expired'), (reset+100, epoch, 'cursor-ahead'), (reset, 'old-research-epoch', 'epoch-changed')):
            stream = live_market.market_frames(self.subscriber(), after, realm)
            try:
                await anext(stream)
                self.assertEqual(payload(await anext(stream))['reason'], reason)
            finally:
                await stream.aclose()

    async def test_http_validation_unregistered_and_selected_coverage(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
            response = await client.get(f'/api/live-market/candles/196/{TOKEN}?pool={POOL}')
            self.assertEqual(response.status_code, 200)
            for url in (f'/api/live-market/candles/196/{TOKEN}?pool=0xbad', f'/api/live-market/candles/196/{TOKEN}?pool={POOL}&bar=2m', f'/api/live-market/markets/1/{TOKEN}'):
                self.assertEqual((await client.get(url)).status_code, 400)
            other = await client.get(f'/api/live-market/candles/196/{TOKEN}?pool=0x'+ '5'*40)
            self.assertFalse(other.json()['liveMarket'])
            await self.scoped.put('live-selection', 'pools', {'poolIds': [POOL], 'tokenIds': [TOKEN],
                                                             'basesByPool': {POOL: ['0x'+'9'*40]}})
            response = await client.get(f'/api/live-market/markets/196/{TOKEN}')
            self.assertEqual(response.json()['markets'], [])
            await self.scoped.put('live-selection', 'pools', {'poolIds': [], 'tokenIds': []})
            response = await client.get(f'/api/live-market/markets/196/{TOKEN}')
            self.assertFalse(response.json()['liveMarket'])
            self.assertEqual(response.json()['markets'], [])

    async def test_single_reader_filters_before_queue_and_slow_consumer_resets(self):
        path = self.scoped.path
        hub = live_market.MarketHub(path)
        sub = self.subscriber()
        hub.clients.add(sub)
        hub.start()
        try:
            await asyncio.wait_for(hub.ready.wait(), 1)
            await self.append(priceCurrency='USD')
            await asyncio.sleep(.15)
            self.assertTrue(sub.queue.empty())
            for _ in range(live_market.QUEUE_LIMIT+1):
                await self.append()
            for _ in range(20):
                if sub.closed:
                    break
                await asyncio.sleep(.05)
            self.assertTrue(sub.closed)
            self.assertEqual(sub.queue.qsize(), 1)
            self.assertEqual(sub.queue.get_nowait()[2]['reason'], 'slow-consumer')
        finally:
            await hub.stop()
