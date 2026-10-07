"""BNB log replay uses one bounded, chain-verified HTTP fallback."""
import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.collectors.live_market import HotMarketStream


@pytest.mark.parametrize('fallback_chain', ['0x38', '0x1'])
def test_log_fallback_checks_chain_and_never_uses_subscription_socket(fallback_chain):
    async def check():
        calls = []
        def handle(request):
            body = json.loads(request.content)
            calls.append((request.url.host, body['method']))
            if body['method'] == 'eth_chainId':
                return httpx.Response(200, json={'result': '0x38' if request.url.host == 'primary.test' else fallback_chain})
            if request.url.host == 'primary.test':
                return httpx.Response(403)
            return httpx.Response(200, json={'result': []})
        with patch.dict(os.environ, {'BSC_LIVE_HTTP_FALLBACK_URL': 'https://fallback.test'}):
            stream = HotMarketStream('56')
            stream.rpc_url = 'https://primary.test'
            stream.rpc_interval = stream.rpc_base_interval = 0
            stream.ws = object()
            stream.ws_call = AsyncMock(side_effect=AssertionError('replay on subscription socket'))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                stream.http = client
                try:
                    if fallback_chain == '0x38':
                        assert await stream.rpc('eth_getLogs', [{}]) == []
                        assert await stream.rpc('eth_getLogs', [{}]) == []
                        assert calls.count(('fallback.test', 'eth_chainId')) == 1
                        assert calls.count(('fallback.test', 'eth_getLogs')) == 2
                    else:
                        with pytest.raises(RuntimeError, match='^bnb-http-replay-unavailable$'):
                            await stream.rpc('eth_getLogs', [{}])
                        assert ('fallback.test', 'eth_getLogs') not in calls
                    stream.ws_call.assert_not_awaited()
                finally:
                    stream.http = None
                    await stream.close()
    asyncio.run(check())


@pytest.mark.parametrize('outcome', ['timeout', 'limited', 'rpc-error'])
def test_log_fallback_failure_is_bounded_and_redacts_endpoint(outcome):
    async def check():
        calls = []
        def handle(request):
            body = json.loads(request.content)
            calls.append((request.url.host, body['method']))
            if body['method'] == 'eth_chainId':
                return httpx.Response(200, json={'result': '0x38'})
            if request.url.host == 'primary.test':
                return httpx.Response(403)
            if outcome == 'timeout':
                raise httpx.ReadTimeout('private-endpoint-details', request=request)
            if outcome == 'limited':
                return httpx.Response(429)
            return httpx.Response(200, json={'error': {'code': -32000, 'message': 'private-endpoint-details'}})
        with patch.dict(os.environ, {'BSC_LIVE_HTTP_FALLBACK_URL': 'https://fallback.test/key/private-endpoint-details'}):
            stream = HotMarketStream('56')
            stream.rpc_url = 'https://primary.test'
            stream.rpc_interval = stream.rpc_base_interval = 0
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                stream.http = client
                try:
                    with pytest.raises(RuntimeError) as error:
                        await stream.rpc('eth_getLogs', [{}])
                    assert str(error.value) == 'bnb-http-replay-unavailable'
                    assert calls.count(('fallback.test', 'eth_getLogs')) == 1
                    assert not stream.fallback_http_verified
                    if outcome == 'limited':
                        assert stream.rpc_cooldown_until > time.monotonic()
                finally:
                    stream.http = None
                    await stream.close()
    asyncio.run(check())
