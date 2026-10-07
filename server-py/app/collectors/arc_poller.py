"""Verified, configuration-gated Arc chain health and issuer catalogue.

No mainnet endpoint, stock contract, factory or wrapper is guessed. Operator
configuration supplies the RPC; the issuer manifest supplies verified chain
and contract identities. Without either, coverage stays visibly unavailable.
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import httpx

from ..db import store
from ..stock_identity import token_identity
from .discovery_coverage import official_contracts

CHAIN = '5042'


def configured():
    url = os.getenv('ARC_RPC_URL') or os.getenv('ARC_STREAM_RPC') or ''
    parsed = urlparse(url)
    return url if parsed.scheme == 'https' and parsed.hostname else None


async def refresh_arc_poller():
    now = int(time.time()*1000)
    system = await store('system')
    url = configured()
    official = official_contracts(CHAIN)
    base = {'chainId': CHAIN, 'at': now, 'provider': 'Chain RPC', 'stockContracts': len(official),
            'coverage': 'configured-verified-contracts', 'factoryCoverage': 'unconfigured', 'complete': False}
    if not url:
        await system.put('collector', 'arc-poller', {**base, 'status': 'unconfigured', 'reason': 'rpc-not-configured'})
        return {'requested': 0, 'accepted': 0, 'skipped': 1}
    result = {'requested': 0, 'accepted': 0, 'updated': 0, 'failed': 0}
    async with httpx.AsyncClient(timeout=10) as client:
        async def rpc(method, params):
            result['requested'] += 1
            response = await client.post(url, json={'jsonrpc': '2.0', 'id': result['requested'], 'method': method, 'params': params})
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get('error') or 'result' not in body:
                raise ValueError('arc-rpc-unavailable')
            return body['result']
        try:
            raw = await rpc('eth_chainId', [])
            chain_id = int(raw, 16) if isinstance(raw, str) and raw.startswith('0x') else int(raw)
            if chain_id != int(CHAIN):
                raise ValueError('arc-rpc-chain-mismatch')
            head = await rpc('eth_getBlockByNumber', ['latest', False])
            if not isinstance(head, dict) or not head.get('hash') or not head.get('timestamp'):
                raise ValueError('arc-head-unavailable')
            height = int(head['number'], 16)
            at = int(head['timestamp'], 16)*1000
            if not 0 <= now-at <= 300_000:
                raise ValueError('arc-head-delayed')
            s = await store(CHAIN)
            # Catalogue creates only manifest-verified identities; metadata is
            # read later by normal quote lanes, not inferred from a ticker.
            for address in official:
                identity = token_identity(CHAIN, address)
                if identity.get('eligibleForPair') is not True:
                    continue
                ticker = identity.get('ticker')
                await s.patch_fact('stock', address, {'chainId': CHAIN, 'chain': CHAIN,
                    'tokenContractAddress': address, 'stockCode': ticker, 'tokenSymbol': ticker,
                    'issuerIdentity': identity, 'active': True, 'discoverySource': 'verified-issuer-manifest'})
                await s.patch_fact('asset', address, {'chainId': CHAIN, 'chain': CHAIN, 'token': address,
                    'kind': 'stock', 'symbol': ticker, 'issuerIdentity': identity})
                result['updated'] += 1
            result['accepted'] = 1
            await system.put('collector', 'arc-poller', {**base, 'status': 'partial' if not official else 'ready',
                'reason': 'official-stock-manifest-unavailable' if not official else None,
                'chainIdVerified': True, 'head': height, 'headHash': head['hash'], 'sourceEventAt': at,
                'sourceLagMs': now-at, 'explorerConfigured': bool(os.getenv('ARC_EXPLORER_URL'))})
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            result['failed'] += 1
            await system.put('collector', 'arc-poller', {**base, 'status': 'unavailable', 'reason': 'rpc-verification-unavailable'})
    return result
