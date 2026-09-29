import asyncio
import unittest
from contextlib import ExitStack
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.collectors.chain_stream import ChainPoolStream


class Clock:
    def __init__(self):
        self.now = 100.0
        self.epoch = 1_790_300_000.0
        self.real_sleep = asyncio.sleep
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.epoch + self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await self.real_sleep(0)

    def patch(self):
        context = ExitStack()
        context.enter_context(patch('app.collectors.chain_stream.time', self))
        context.enter_context(patch('app.collectors.chain_stream.asyncio.sleep', self.sleep))
        return context


class ChainRpcBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_concurrent_requests_share_conservative_chain_interval(self):
        for chain, interval in [('196', .5), ('56', .25), ('4663', .25)]:
            with self.subTest(chain=chain):
                clock = Clock()
                calls = []

                def respond(request):
                    calls.append(clock.now)
                    return httpx.Response(200, json={'jsonrpc': '2.0', 'result': '0x1'})

                collector = ChainPoolStream(chain)
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
                    with clock.patch():
                        results = await asyncio.gather(*(collector.rpc('eth_blockNumber', []) for _ in range(5)))
                self.assertEqual(results, ['0x1'] * 5)
                self.assertEqual(calls, [100 + interval * n for n in range(5)])

    async def test_http_retry_after_and_exponential_cooldown_precede_every_retry(self):
        clock = Clock()
        calls = []
        replies = [(429, {'Retry-After': '3'}), (429, {}), (200, {}), (200, {})]

        def respond(request):
            calls.append(clock.now)
            status, headers = replies.pop(0)
            return httpx.Response(status, headers=headers, json={'result': '0x1'})

        collector = ChainPoolStream('196')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
            with clock.patch():
                results = await asyncio.gather(collector.rpc('eth_blockNumber', []), collector.rpc('eth_chainId', []))
        self.assertEqual(results, ['0x1', '0x1'])
        self.assertEqual(calls, [100, 103, 107, 109])
        self.assertEqual(collector.rpc_interval, 2)
        self.assertEqual(collector.rpc_rate_failures, 2)
        self.assertIsNone(collector.rpc_cooldown_wall)
        self.assertEqual(collector.rpc_rate_limited_at, int((clock.epoch + 103) * 1000))

    async def test_http_date_retry_after_is_respected(self):
        clock = Clock()
        calls = []

        def respond(request):
            calls.append(clock.now)
            if len(calls) == 1:
                return httpx.Response(429, headers={'Retry-After': formatdate(clock.time() + 9, usegmt=True)})
            return httpx.Response(200, json={'result': '0x1'})

        collector = ChainPoolStream('196')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
            with clock.patch():
                await collector.rpc('eth_blockNumber', [])
        self.assertEqual(calls, [100, 109])

    async def test_failed_retry_budget_keeps_cooldown_for_next_caller_and_reports_it(self):
        clock = Clock()
        calls = []

        def respond(request):
            calls.append(clock.now)
            return httpx.Response(429 if len(calls) <= 3 else 200, json={'result': '0x1'})

        collector = ChainPoolStream('196')
        collector.s = SimpleNamespace(get=AsyncMock(return_value={}), put=AsyncMock())
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
            with clock.patch():
                with self.assertRaises(httpx.HTTPStatusError):
                    await collector.rpc('eth_blockNumber', [])
                await collector.status_fact(force=True)
                health = collector.s.put.await_args.args[2]
                self.assertEqual(health['rateLimitedAt'], int((clock.epoch + 106) * 1000))
                self.assertEqual(health['cooldownUntil'], int((clock.epoch + 114) * 1000))
                self.assertEqual(health['rpcIntervalMs'], 2000)
                await collector.rpc('eth_blockNumber', [])
        self.assertEqual(calls, [100, 102, 106, 114])
        self.assertEqual(collector.rpc_interval, 2)

    async def test_ws_rate_limit_retries_but_range_too_wide_does_not(self):
        clock = Clock()
        collector = ChainPoolStream('4663')
        collector.ws = object()
        calls = []
        replies = [RuntimeError('rpc-error:-32005:block range too wide'),
                   RuntimeError('rpc-error:-32005:request rate limit exceeded'), '0x1']

        async def ws_call(*args):
            calls.append(clock.now)
            response = replies.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        collector.ws_call = ws_call
        with clock.patch():
            with self.assertRaisesRegex(RuntimeError, 'range too wide'):
                await collector.rpc('eth_getLogs', [{}])
            self.assertIsNone(collector.rpc_rate_limited_at)
            self.assertEqual(collector.rpc_rate_failures, 0)
            self.assertEqual(await collector.rpc('eth_blockNumber', []), '0x1')
        self.assertEqual(calls, [100, 100.25, 102.25])
        self.assertEqual(collector.rpc_interval, .5)

    async def test_single_success_retains_reduced_rate_until_sixty_seconds_without_limit(self):
        clock = Clock()
        calls = []

        def respond(request):
            calls.append(clock.now)
            return httpx.Response(429 if len(calls) == 1 else 200, json={'result': '0x1'})

        collector = ChainPoolStream('56')
        collector.s = SimpleNamespace(get=AsyncMock(return_value={}), put=AsyncMock())
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
            with clock.patch():
                await collector.rpc('eth_blockNumber', [])  # 429 then success at t=102.
                self.assertEqual(collector.rpc_interval, .5)
                await collector.rpc('eth_blockNumber', [])
                self.assertEqual(calls, [100, 102, 102.5])
                clock.now = 159.999
                await collector.rpc('eth_blockNumber', [])
                await collector.status_fact(force=True)
                self.assertEqual(collector.s.put.await_args.args[2]['rpcIntervalMs'], 500)
                self.assertEqual(collector.rpc_rate_failures, 1)
                clock.now = 160.5
                await collector.rpc('eth_blockNumber', [])
                await collector.status_fact(force=True)
                self.assertEqual(collector.s.put.await_args.args[2]['rpcIntervalMs'], 250)
                self.assertEqual(collector.rpc_rate_failures, 0)

    async def test_http_json_rpc_rate_message_is_preserved_and_budgeted(self):
        clock = Clock()
        calls = []

        def respond(request):
            calls.append(clock.now)
            body = {'error': {'code': -32005, 'message': 'too many requests'}} if len(calls) == 1 else {'result': []}
            return httpx.Response(200, json=body)

        collector = ChainPoolStream('196')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
            with clock.patch():
                self.assertEqual(await collector.rpc('eth_getLogs', [{}]), [])
        self.assertEqual(calls, [100, 102])

    async def test_cancellation_during_cooldown_never_sends_retry(self):
        clock = Clock()
        calls = []

        def respond(request):
            calls.append(clock.now)
            return httpx.Response(429)

        collector = ChainPoolStream('196')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as collector.http:
            with clock.patch(), patch('app.collectors.chain_stream.asyncio.sleep', AsyncMock(side_effect=asyncio.CancelledError())):
                with self.assertRaises(asyncio.CancelledError):
                    await collector.rpc('eth_blockNumber', [])
        self.assertEqual(calls, [100])
        self.assertFalse(collector.rpc_gate.locked())
        self.assertEqual(collector.rpc_cooldown_until, 102)
