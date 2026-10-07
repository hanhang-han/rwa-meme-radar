"""Bounded visible-asset delivery cannot leak another market's event tape."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import stream_hub as hub
from app.api import stream as stream_api
from app.api.stream import get_stream, parse_quote_tokens, scope_frame
from app.collectors.live_quotes import quote_packet
from app.dashboard_projection import asset_summary, stock_summary, _market_asset, _market_stock
from app.api.product_v2 import directory_stock, _stock_directory


TOKEN = '0x' + 'ab' * 20
OTHER = '0x' + 'cd' * 20
KEYS = {('196', TOKEN)}


def data(frame):
    return json.loads(frame.split(b'data: ', 1)[1])


class VisibleQuoteFilterTests(unittest.TestCase):
    def test_exact_identities_are_case_normalized_and_deduplicated(self):
        self.assertEqual(parse_quote_tokens(f'196:{TOKEN},196:{TOKEN.upper().replace("0X", "0x")},56:{TOKEN}'),
                         {('196', TOKEN), ('56', TOKEN)})
        eighty = ','.join(f'{i}:{TOKEN}' for i in range(1, 81))
        self.assertEqual(len(parse_quote_tokens(eighty)), 80)

    def test_missing_invalid_or_unbounded_subscriptions_are_rejected(self):
        invalid = [None, '', 'all', '196:0xabc', f'0:{TOKEN}', f'-1:{TOKEN}',
                   f'0196:{TOKEN}', f'１９６:{TOKEN}', f'196:{TOKEN},',
                   f'196:{TOKEN} ', f'196:{TOKEN}:1m', f'196:{OTHER[:-1]}g',
                   ','.join([f'196:{TOKEN}'] * 81), '1' * 6000]
        for value in invalid:
            with self.subTest(value=str(value)[:80]), self.assertRaises(HTTPException) as caught:
                parse_quote_tokens(value)
            self.assertEqual(caught.exception.status_code, 422)

    def test_http_route_rejects_invalid_subscription_before_streaming(self):
        app = FastAPI()
        app.include_router(stream_api.router, prefix='/api')
        with TestClient(app) as client:
            for tokens in (None, 'all', f'196:{TOKEN},', ','.join([f'196:{TOKEN}'] * 81)):
                query = {'protocol': 2, 'scope': 'quotes', 'trades': 'none', 'candles': 'none'}
                if tokens is not None:
                    query['tokens'] = tokens
                response = client.get('/api/stream', params=query)
                self.assertEqual(response.status_code, 422)
                self.assertIn('tokens', response.json()['detail'])

    def test_prices_keep_exact_chain_contract_and_original_unit_provenance(self):
        for event in ('price', 'stock-quote'):
            payload = {'chainId': 196, 'token': TOKEN.upper().replace('0X', '0x'),
                       'price': 3, 'priceCurrency': 'wTCENTx', 'venue': 'dex',
                       'poolId': OTHER, 'priceScope': 'pool', 'marketAt': 123,
                       'priceProvenance': {'method': 'native-pool'}}
            frame = hub._frame(event, payload, 7)
            self.assertIs(scope_frame(frame, 2, 'quotes', quote_keys=KEYS), frame)
            self.assertEqual(data(frame), payload)
            for wrong in ({**payload, 'chainId': '56'}, {**payload, 'token': OTHER},
                          {key: value for key, value in payload.items() if key != 'chainId'},
                          ['not-a-quote']):
                self.assertIsNone(scope_frame(hub._frame(event, wrong, 8), 2, 'quotes', quote_keys=KEYS))

    def test_only_quote_and_transport_events_cross_the_subscription(self):
        for event in ('hello', 'heartbeat', 'reset', 'checkpoint'):
            frame = hub._frame(event, {'cursor': 5})
            self.assertIs(scope_frame(frame, 2, 'quotes', quote_keys=KEYS), frame)
        for event in ('projection.delta', 'directory.invalidate', 'trade', 'trade-remove',
                      'market.trade', 'candle', 'candle.upsert', 'candle-reset',
                      'comparison', 'relationship', 'discovery', 'unknown'):
            # An invalid JSON body proves discarded event kinds are not even
            # decoded, including large projection/candle packets.
            frame = f'id: 9\nevent: {event}\ndata: invalid JSON\n\n'.encode()
            self.assertIsNone(scope_frame(frame, 2, 'quotes', quote_keys=KEYS))
        malformed = b'id: 9\nevent: price\ndata: invalid JSON\n\n'
        self.assertIsNone(scope_frame(malformed, 2, 'quotes', quote_keys=KEYS))

    def test_existing_protocols_still_filter_direct_quotes(self):
        frame = hub._frame('price', {'chainId': '196', 'token': TOKEN, 'price': 2}, 1)
        self.assertIs(scope_frame(frame, 0), frame)
        for scope in ('overview', 'market', 'asset', 'memes'):
            self.assertIsNone(scope_frame(frame, 2, scope, ('196', TOKEN)))


class VisibleQuoteStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if hub._publisher is not None and hub._publisher.cancelled():
            hub._publisher = None
            hub._pending.clear()
        await hub.stop_hub()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, STREAM_LEDGER_PATH=self.tmp.name + '/events.sqlite')
        self.env.start()

    async def asyncTearDown(self):
        await hub.stop_hub()
        self.env.stop()
        self.tmp.cleanup()

    def append(self, events):
        ledger = hub._ledger()
        ledger.executemany('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
                           [(name, json.dumps(body), int(time.time()*1000)) for name, body in events])
        ledger.commit()

    async def test_actual_request_validation_has_no_feed_or_database_work(self):
        with patch.object(stream_api, 'cursor') as cursor, patch.object(stream_api, 'clients') as clients:
            with self.assertRaises(HTTPException) as caught:
                await get_stream(protocol=2, scope='quotes', tokens=None, trades='feed')
            self.assertEqual(caught.exception.status_code, 422)
            cursor.assert_not_called()
            clients.assert_not_called()

    async def test_live_queue_filters_other_assets_and_updates_preserve_cursor(self):
        response = await get_stream(protocol=2, scope='quotes', tokens=f'196:{TOKEN}',
                                    trades='none', candles='none', snapshot=False)
        iterator = response.body_iterator
        self.assertFalse(hub.clients())
        self.assertIn(b'event: hello', await anext(iterator))
        queue = next(iter(hub.clients()))
        queue.put_nowait((1, hub._frame('projection.delta', {'trace': 'large' * 20000}, 1)))
        queue.put_nowait((2, hub._frame('trade', {'chainId': '196', 'token': TOKEN, 'trace': 'unwanted'}, 2)))
        queue.put_nowait((3, hub._frame('price', {'chainId': '56', 'token': TOKEN, 'price': 99}, 3)))
        queue.put_nowait((4, hub._frame('stock-quote', {'chainId': '196', 'token': TOKEN, 'price': 4,
                                                    'priceCurrency': 'USDT', 'marketAt': 123}, 4)))
        frame = await asyncio.wait_for(anext(iterator), 1)
        self.assertIn(b'id: 4', frame)
        self.assertEqual(data(frame)['priceCurrency'], 'USDT')
        self.assertNotIn(b'trace', frame)
        await iterator.aclose()
        self.assertFalse(hub.clients())

    async def test_replay_pages_checkpoint_filtered_ids_and_keep_only_selected_quotes(self):
        self.append([
            ('trade', {'chainId': '196', 'token': TOKEN, 'trace': 'ignored'}),
            ('price', {'chainId': '196', 'token': TOKEN, 'price': 2, 'marketAt': 100}),
            ('price', {'chainId': '56', 'token': TOKEN, 'price': 3}),
            ('stock-quote', {'chainId': '196', 'token': TOKEN, 'price': 4, 'marketAt': 200}),
            ('projection.delta', {'trace': 'global' * 20000}),
            ('price', {'chainId': '196', 'token': OTHER, 'price': 6}),
            ('price', {'chainId': '196', 'token': TOKEN, 'price': 7, 'marketAt': 300}),
        ])
        with patch.object(stream_api, 'replay_page', side_effect=lambda after, upper:
                          hub.replay_page(after, upper, limit=2)):
            response = await get_stream(protocol=2, scope='quotes', tokens=f'196:{TOKEN}',
                                        trades='none', candles='none', snapshot=False, after='0')
            frames = []
            while True:
                frame = await asyncio.wait_for(anext(response.body_iterator), 1)
                frames.append(frame)
                if b'event: hello\n' in frame:
                    break
            await response.body_iterator.aclose()
        quotes = [data(frame)['price'] for frame in frames
                  if b'event: price\n' in frame or b'event: stock-quote\n' in frame]
        self.assertEqual(quotes, [2, 4, 7])
        checkpoints = [data(frame)['cursor'] for frame in frames if b'event: checkpoint\n' in frame]
        self.assertEqual(checkpoints, [2, 4, 6])
        self.assertNotIn(b'trace', b''.join(frames))
        self.assertFalse(hub.clients())

    async def test_reconnect_header_takes_precedence_over_original_after(self):
        self.append([('price', {'chainId': '196', 'token': TOKEN, 'price': value}) for value in (1, 2)])
        response = await get_stream(protocol=2, scope='quotes', tokens=f'196:{TOKEN}',
                                    snapshot=False, last_event_id='1', after='0')
        frame = await anext(response.body_iterator)
        self.assertEqual(data(frame)['price'], 2)
        await response.body_iterator.aclose()

    async def test_expired_cursor_resets_instead_of_inventing_a_quote(self):
        self.append([('price', {'chainId': '196', 'token': TOKEN, 'price': 2})])
        response = await get_stream(protocol=2, scope='quotes', tokens=f'196:{TOKEN}',
                                    snapshot=False, after='99')
        frame = await anext(response.body_iterator)
        self.assertIn(b'event: reset', frame)
        self.assertEqual(data(frame)['reason'], 'cursor-expired')
        with self.assertRaises(StopAsyncIteration):
            await anext(response.body_iterator)
        self.assertFalse(hub.clients())

    async def test_cancelled_wait_does_not_keep_a_subscriber(self):
        response = await get_stream(protocol=2, scope='quotes', tokens=f'196:{TOKEN}', snapshot=False)
        await anext(response.body_iterator)
        waiter = asyncio.create_task(anext(response.body_iterator))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(hub.clients())


class VisibleQuoteMetadataTests(unittest.TestCase):
    def test_stock_directory_returns_the_exact_published_cursor_and_revision(self):
        payload = {'now': 1234, 'realtime': {'cursor': 567, 'revision': 89}, 'unified': {}}
        result = _stock_directory(payload, 'all', '', 'volume24h', 'all', 20, 0)
        self.assertEqual(result['now'], 1234)
        self.assertEqual(result['realtime'], {'cursor': 567, 'revision': 89})
        self.assertIsNot(result['realtime'], payload['realtime'])
        missing = _stock_directory({'now': 1234, 'unified': {}}, 'all', '', 'volume24h', 'all', 20, 0)
        self.assertEqual(missing['realtime'], {})

    def test_okx_packet_retains_independent_change_clock_and_unit(self):
        asset = {'price': 2, 'change24h': 4,
                 'fieldTimes': {'price': 123, 'change24h': 100},
                 'fieldSources': {'price': 'OKX', 'change24h': 'OKX'},
                 'fieldScopes': {'price': 'token', 'change24h': 'token'},
                 'fieldTimeKinds': {'price': 'received', 'change24h': 'market'},
                 'fieldObservations': {'price': {'provider': 'OKX', 'scope': 'token', 'venue': 'dex',
                                               'currency': 'USD', 'timeKind': 'received',
                                               'marketAt': None, 'receivedAt': 123}}}
        packet = quote_packet('196', TOKEN, asset)
        self.assertEqual(packet['priceCurrency'], 'USD')
        self.assertEqual(packet['provider'], 'OKX')
        self.assertEqual(packet['quoteType'], 'dex')
        self.assertEqual(packet['timeKind'], 'received')
        self.assertNotIn('marketAt', packet)
        self.assertEqual(packet['fieldTimes'], {'price': 123, 'change24h': 100})
        self.assertEqual(packet['fieldObservations']['price']['marketAt'], None)

    def test_native_or_unknown_price_never_acquires_usd_by_provider_name(self):
        asset = {'price': 2, 'fieldTimes': {'price': 123},
                 'fieldObservations': {'price': {'provider': 'OKX', 'currency': 'wTCENTx'}}}
        self.assertEqual(quote_packet('196', TOKEN, asset)['priceCurrency'], 'wTCENTx')
        packet = quote_packet('196', TOKEN, {'price': 2, 'provider': 'OKX'})
        self.assertNotIn('priceCurrency', packet)
        self.assertNotIn('timeKind', packet)

    def test_new_okx_quote_does_not_reuse_stale_chain_pool_provenance(self):
        asset = {'price': 2, 'fieldTimes': {'price': 123},
                 'priceProvenance': {'provider': 'Chain RPC', 'scope': 'pool', 'pool': OTHER},
                 'fieldObservations': {'price': {'provider': 'OKX', 'currency': 'USD',
                                               'scope': 'token', 'venue': 'dex', 'timeKind': 'market'}}}
        packet = quote_packet('196', TOKEN, asset)
        self.assertNotIn('priceProvenance', packet)
        self.assertNotIn('pool', packet)
        self.assertEqual(packet['provider'], 'OKX')

    def test_stock_and_meme_directory_keep_compact_market_identity(self):
        metadata = {'venue': 'binance', 'quoteType': 'exchange-token',
                    'marketId': 'AAAUSDT', 'priceScope': 'exchange', 'priceCurrency': 'USDT',
                    'priceProvenance': {'timeKind': 'market', 'venue': 'binance', 'marketId': 'AAAUSDT'},
                    'fieldScopes': {'price': 'exchange'}, 'fieldTimeKinds': {'price': 'market'}}
        stock = {'chainId': '56', 'tokenContractAddress': TOKEN, 'price': 3, **metadata}
        card = directory_stock(stock_summary(stock))
        for field in metadata:
            self.assertEqual(card[field], metadata[field])
        market = _market_stock(stock_summary(stock))
        self.assertEqual(market['marketId'], 'AAAUSDT')
        self.assertEqual(market['priceProvenance']['venue'], 'binance')
        asset = {'chainId': '56', 'token': TOKEN, 'price': 3, 'poolId': OTHER, **metadata}
        market = _market_asset(asset_summary(asset))
        self.assertEqual(market['poolId'], OTHER)
        self.assertEqual(market['marketId'], 'AAAUSDT')
        self.assertEqual(market['fieldScopes']['price'], 'exchange')


if __name__ == '__main__':
    unittest.main()
