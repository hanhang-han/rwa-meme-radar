"""Read-only V3/V4 Quoter calls for operator-verified pool mappings.

No swap, approval, private key or user-provided destination is accepted.
An absent verified Quoter, token denomination or current USD observation
withholds the estimate rather than falling back to a V2 formula.
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import time
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
from eth_abi import encode, decode
from eth_abi.exceptions import DecodingError, EncodingError
from eth_utils import keccak

ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')
_slots = asyncio.Semaphore(2)


def registry():
    try:
        rows = json.loads(os.environ.get('PRODUCT_QUOTER_REGISTRY', '[]'))
    except (ValueError, TypeError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def verified_mapping(chain, pool, token, rows=None):
    for row in registry() if rows is None else rows:
        if (str(row.get('chainId')) != str(chain) or str(row.get('pool') or '').lower() != str(pool).lower()
                or str(row.get('tokenIn') or '').lower() != str(token).lower()):
            continue
        if row.get('verified') is not True or row.get('version') not in ('v3-v2', 'v4'):
            return None
        if not re.fullmatch(r'0x[0-9a-fA-F]{40}' if row['version'] == 'v3-v2' else r'0x[0-9a-fA-F]{64}', str(row.get('pool') or '')):
            return None
        if any(not ADDRESS.fullmatch(str(row.get(k) or '')) for k in ('quoter', 'tokenIn', 'tokenOut')):
            return None
        if not str(row.get('verificationUrl') or '').startswith('https://'):
            return None
        for key in ('decimalsIn', 'decimalsOut'):
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 36:
                return None
        fee = row.get('fee')
        if isinstance(fee, bool) or not isinstance(fee, int) or not 0 <= fee < 2**24:
            return None
        if row['version'] == 'v4':
            if not ADDRESS.fullmatch(str(row.get('hooks') or '')) or isinstance(row.get('tickSpacing'), bool) or not isinstance(row.get('tickSpacing'), int) or not 0 < row['tickSpacing'] < 2**23:
                return None
            # Hook behavior and permission requirements need explicit review.
            if row['hooks'].lower() != '0x'+'0'*40 and row.get('hooksReviewed') is not True:
                return None
            currencies = sorted((row['tokenIn'], row['tokenOut']), key=lambda address: int(address, 16))
            pool_key = encode(['address', 'address', 'uint24', 'int24', 'address'], [*currencies, fee, row['tickSpacing'], row['hooks']])
            if '0x'+keccak(pool_key).hex() != row['pool'].lower():
                return None
        return row
    return None


def calldata(mapping, amount):
    if not isinstance(amount, int) or amount <= 0 or amount >= 2**128:
        raise ValueError('unsupported-input-amount')
    token_in, token_out = mapping['tokenIn'], mapping['tokenOut']
    if mapping['version'] == 'v3-v2':
        signature = 'quoteExactInputSingle((address,address,uint256,uint24,uint160))'
        encoded = encode(['(address,address,uint256,uint24,uint160)'], [(token_in, token_out, amount, mapping['fee'], 0)])
    else:
        token0, token1 = sorted((token_in, token_out), key=lambda a: int(a, 16))
        signature = 'quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))'
        key = (token0, token1, mapping['fee'], mapping['tickSpacing'], mapping['hooks'])
        encoded = encode(['((address,address,uint24,int24,address),bool,uint128,bytes)'], [(key, token_in.lower() == token0.lower(), amount, b'')])
    return '0x'+(keccak(text=signature)[:4]+encoded).hex()


def current_usd_price(asset, now):
    at = (asset.get('fieldTimes') or {}).get('price')
    price = asset.get('price')
    if (asset.get('priceCurrency') != 'USD' or isinstance(price, bool) or not isinstance(price, (int, float))
            or not 0 < price < 1e12 or not isinstance(at, (int, float)) or not 0 <= now-at <= 180_000
            or not (asset.get('fieldSources') or {}).get('price')):
        return None
    return Decimal(str(price))


def rpc_url(chain):
    aliases = {'196': ('XLAYER_RPC_URL', 'XLAYER_RPC'), '56': ('BSC_RPC_URL', 'BNB_RPC_URL', 'BSC_RPC'),
               '4663': ('ROBINHOOD_RPC_URL', 'ROBINHOOD_RPC'), '5042': ('ARC_RPC_URL',)}
    for name in (f'PRODUCT_QUOTER_RPC_{chain}', *aliases.get(str(chain), ())):
        url = os.environ.get(name)
        if url and urlsplit(url).scheme == 'https' and urlsplit(url).hostname:
            return url
    return None


async def quote(chain, token, pool, sell_usd, assets, relations):
    now = int(time.time()*1000)
    base = {'sellUsd': sell_usd, 'pool': pool, 'status': 'unavailable', 'valuePercent': None,
            'executable': False, 'excludesTokenTax': True, 'at': now}
    if sell_usd not in (100, 1000, 10000):
        return {**base, 'reason': 'unsupported-sale-amount'}
    relation = next((r for r in relations if str(r.get('chainId')) == str(chain) and str(r.get('pool')).lower() == str(pool).lower()
                     and str(r.get('token')).lower() == str(token).lower() and r.get('status') == 'verified'), None)
    mapping = verified_mapping(chain, pool, token)
    url = rpc_url(chain)
    if os.environ.get('PRODUCT_QUOTER_READ_ENABLED') != '1' or relation is None or mapping is None or url is None:
        return {**base, 'reason': 'verified-quoter-unconfigured'}
    source = next((a for a in assets if str(a.get('chainId')) == str(chain) and str(a.get('token')).lower() == token.lower()), {})
    destination = next((a for a in assets if str(a.get('chainId')) == str(chain) and str(a.get('token') or a.get('tokenContractAddress')).lower() == mapping['tokenOut'].lower()), {})
    price_in, price_out = current_usd_price(source, now), current_usd_price(destination, now)
    if price_in is None or price_out is None:
        return {**base, 'reason': 'current-usd-prices-unavailable'}
    if abs(source['fieldTimes']['price']-destination['fieldTimes']['price']) > 60_000:
        return {**base, 'reason': 'price-times-unaligned'}
    amount_in = int(Decimal(sell_usd)/price_in*10**mapping['decimalsIn'])
    try:
        data = calldata(mapping, amount_in)
        async with _slots, httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            async def call(method, params):
                response = await client.post(url, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
                response.raise_for_status()
                value = response.json()
                if value.get('error') or 'result' not in value:
                    raise ValueError('rpc-error')
                return value['result']
            rpc_chain = await call('eth_chainId', [])
            if int(rpc_chain, 16) != int(chain):
                raise ValueError('rpc-chain-mismatch')
            block = await call('eth_getBlockByNumber', ['latest', False])
            if not block or not block.get('hash') or not block.get('number'):
                raise ValueError('block-unavailable')
            block_time = int(block.get('timestamp') or '0x0', 16)*1000
            if not -15_000 <= now-block_time <= 180_000:
                raise ValueError('block-stale')
            encoded = await call('eth_call', [{'to': mapping['quoter'], 'data': data}, block['number']])
            result = bytes.fromhex(encoded[2:])
            output = decode(['uint256'], result[:32])[0]
            canonical = await call('eth_getBlockByNumber', [block['number'], False])
            if not canonical or canonical.get('hash') != block['hash']:
                raise ValueError('quote-block-reorg')
            value_usd = float(Decimal(output)/10**mapping['decimalsOut']*price_out)
            if not 0 <= value_usd < 1e20:
                raise ValueError('invalid-quote-output')
        return {**base, 'status': 'current', 'reason': None, 'outputUsd': value_usd,
            'valuePercent': 100*(1-value_usd/sell_usd), 'method': mapping['version']+'-quoter',
            'source': mapping['quoter'], 'verificationUrl': mapping['verificationUrl'],
            'blockNumber': int(block['number'], 16), 'blockHash': block['hash'],
            'validUntil': now+30_000, 'priceAt': {'input': source['fieldTimes']['price'], 'output': destination['fieldTimes']['price']}}
    except (httpx.HTTPError, ValueError, TypeError, OverflowError, DecodingError, EncodingError):
        return {**base, 'reason': 'quoter-call-unavailable'}
