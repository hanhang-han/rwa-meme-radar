"""Large market catalogues exercise the API's real SQLite/PG tape queries."""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.api import misc
from app.db import ResearchStore, WriterLock
from app.market_history import TRADE_ORDER, _bounded_trade_rows, market_trades, token_trades
from test_postgres_core_storage import core_pg


@pytest.fixture(params=['sqlite', 'postgres'])
def tape_source(request, tmp_path):
    if request.param == 'postgres':
        sources, _ = request.getfixturevalue('core_pg')
        return str(sources['research']), request.param
    return str(tmp_path / 'tape.sqlite'), request.param


def test_large_market_heads_keep_numeric_ties_and_page_offsets(tape_source):
    path, backend = tape_source

    async def check():
        store = await ResearchStore(path, '56', write_lock=WriterLock()).connect()
        token = '0x' + '1' * 40
        storages = ['dex:market:' + str(n) for n in range(205)]
        try:
            assert getattr(store.db, 'backend', 'sqlite') == backend
            for n, storage in enumerate(storages):
                await store.put('market-registry', storage, {'storage': storage, 'token': token, 'venue': 'dex'})
                await store.put_trades(storage, [
                    {'id': 'chain:' + str(999 - i), 't': 1000, 'token': token, 'venue': 'dex',
                     'blockNumber': 10, 'transactionIndex': n, 'logIndex': i}
                    for i in reversed(range(3))] + [{'id': 'old', 't': 100, 'token': token, 'venue': 'dex'}])
            requested = [*map(store.key, storages), store.key('empty'), store.key(storages[0])]
            for offset in (0, 29, 117):
                expected = await store.fetchall('SELECT asset,id,t,body FROM trades ORDER BY '+TRADE_ORDER+' LIMIT ? OFFSET ?',
                                                (17, offset))
                actual = await _bounded_trade_rows(store, requested, 17, offset)
                project = lambda rows: [(row['asset'], row['id'], row['t'], row['body']) for row in rows]
                assert project(actual) == project(expected)
                markets = await market_trades(store, limit=17, offset=offset, dex_only=True, tokens=[token])
                assert markets == [json.loads(row['body']) for row in expected]
        finally:
            try:
                await misc.stop_feed_reads()
            finally:
                await store.close()
    asyncio.run(check())


def test_token_trades_and_feed_endpoint_read_more_than_sixteen_markets(tape_source):
    path, backend = tape_source

    async def check():
        store = await ResearchStore(path, '56', write_lock=WriterLock()).connect()
        tokens = ['0x' + f'{n + 1:040x}' for n in range(24)]
        try:
            assert getattr(store.db, 'backend', 'sqlite') == backend
            for n, token in enumerate(tokens):
                await store.put_trades(token, [{'id': 'legacy:' + str(n), 't': 2000 + n, 'token': token, 'venue': 'dex'}])
                storage = 'dex:pool:' + str(n)
                await store.put('market-registry', storage, {'storage': storage, 'token': token, 'venue': 'dex'})
                await store.put_trades(storage, [{'id': 'swap:' + str(n), 't': 3000 + n, 'token': token,
                                                  'venue': 'dex', 'blockNumber': 100, 'logIndex': n}])
            legacy = await token_trades(store, tokens)
            assert [row['token'] for row in legacy] == list(reversed(tokens))
            # The feed must still exclude exchange tapes and untracked assets.
            await store.put('market-registry', 'exchange', {'storage': 'binance:listed', 'token': tokens[0], 'venue': 'binance'})
            await store.put_trades('binance:listed', [{'id': 'exchange', 't': 9999, 'token': tokens[0], 'venue': 'binance'}])
            await store.put('market-registry', 'unlisted', {'storage': 'dex:unlisted', 'token': 'unlisted', 'venue': 'dex'})
            await store.put_trades('dex:unlisted', [{'id': 'unlisted', 't': 9999, 'token': 'unlisted', 'venue': 'dex'}])
            assets = [{'chainId': '56', 'token': token, 'symbol': 'TOKEN' + str(n)} for n, token in enumerate(tokens)]
            snapshot = json.dumps({'assets': assets, 'trackedAssets': assets, 'signals': []})
            application = FastAPI()
            application.include_router(misc.router, prefix='/api')
            with patch.object(misc, 'store', AsyncMock(return_value=store)), \
                 patch.object(misc, 'read_projection_json', AsyncMock(return_value=snapshot)), \
                 patch.object(misc, '_feed_cache', {}), patch.object(misc, '_feed_locks', {}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://test') as client:
                    response = await client.get('/api/feed', params={'chain': '56'})
            assert response.status_code == 200
            payload = response.json()
            assert payload['scope'] == 'indexed-assets-dex-swaps'
            assert [row['id'] for row in payload['trades']] == ([f'swap:{n}' for n in reversed(range(24))]
                                                             + [f'legacy:{n}' for n in reversed(range(24))])
            assert {row['token'] for row in payload['trades']} == set(tokens)
            assert all(row['chainId'] == '56' and row['venue'] == 'dex' for row in payload['trades'])
        finally:
            try:
                await misc.stop_feed_reads()
            finally:
                await store.close()
    asyncio.run(check())
