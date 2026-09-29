"""Official Alpha token/chain mapping plus shared market streams.

The directory is joined through alphaId == exchangeInfo.baseAsset, never by
human-readable symbol. USDT, USDC and U remain different quote currencies.
"""
import asyncio
import os
import re

import httpx

from ..db import store
from .exchange_stream import BinanceMarketFeed
from .market_streams import Market, now_ms

TOKEN_LIST='https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list'
EXCHANGE_INFO='https://www.binance.com/bapi/defi/v1/public/alpha-trade/get-exchange-info'
CHAINS=('196','56','4663')
_feed=None
_directory_task=None


def map_markets(tokens,exchange_info):
    """Return a deterministic full market catalogue and mapping diagnostics."""
    if not isinstance(tokens,list) or not isinstance(exchange_info,dict) or not isinstance(exchange_info.get('symbols'),list):
        raise ValueError('Invalid Alpha catalogue')
    candidates={}
    for token in tokens:
        if not isinstance(token,dict):
            continue
        chain, address = str(token.get('chainId','')),str(token.get('contractAddress','')).lower()
        alpha=str(token.get('alphaId','')).upper()
        if chain not in CHAINS or not re.fullmatch(r'0x[0-9a-f]{40}',address) or not re.fullmatch(r'ALPHA_\d+',alpha) or token.get('offline'):
            continue
        candidates.setdefault(alpha,{})[(chain,address)]=token
    result=[]
    ambiguous=sum(len(v)>1 for v in candidates.values())
    seen=set()
    for symbol in exchange_info['symbols']:
        if not isinstance(symbol,dict) or symbol.get('status')!='TRADING':
            continue
        matches=candidates.get(str(symbol.get('baseAsset','')).upper(),{})
        # Ambiguous bridged representations need an explicit reviewed mapping.
        if len(matches)!=1:
            continue
        (chain,address),token=next(iter(matches.items()))
        quote=str(symbol.get('quoteAsset','')).upper()
        pair=str(symbol.get('symbol','')).upper()
        if not re.fullmatch(r'[A-Z0-9_]+',quote) or pair!=str(symbol['baseAsset']).upper()+quote:
            continue
        identity=(chain,address,pair)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(Market(chain,address,'binance-alpha',pair,quote,str(token.get('symbol') or ''),kind='exchange-token'))
    result.sort(key=lambda m:(m.chain_id,m.token,{'USDT':0,'USDC':1}.get(m.quote_currency,2),m.market_id))
    return result,{'directoryTokens':len(tokens),'mappedMarkets':len(result),'ambiguousAlphaIds':ambiguous,
                   'mappedAssets':len({(m.chain_id,m.token) for m in result})}


async def refresh_directory():
    try:
        async with httpx.AsyncClient(timeout=20,headers={'User-Agent':'CliperX/1.0'}) as client:
            responses=await asyncio.gather(client.get(TOKEN_LIST),client.get(EXCHANGE_INFO))
        bodies=[]
        for response in responses:
            response.raise_for_status()
            body=response.json()
            if not isinstance(body,dict) or body.get('code')!='000000' or body.get('success') is False:
                raise ValueError('Alpha catalogue response failed')
            bodies.append(body.get('data'))
        markets,counts=map_markets(*bodies)
        if not markets:
            raise ValueError('Empty Alpha market mapping')
        by_chain={chain:[] for chain in CHAINS}
        for market in markets:
            by_chain[market.chain_id].append(market)
        # One transaction per chain prevents partially published directories.
        for chain,rows in by_chain.items():
            s=await store(chain)
            async with s._guard_write():
                await s.db.execute('BEGIN IMMEDIATE')
                try:
                    await s.db.execute('DELETE FROM facts WHERE kind=? AND id LIKE ?',
                                       (s.key('market-registry'),'binance-alpha:%'))
                    for market in rows:
                        await _feed_fact(s,market)
                    await s.db.commit()
                except BaseException:
                    await s.db.rollback()
                    raise
        await (await store('56')).put('market-directory','binance-alpha',{
            **counts,'status':'ready','updatedAt':now_ms(),'error':None})
        return {'requested':2,'accepted':len(markets),'failed':0,**counts}
    except Exception as error:
        s=await store('56')
        previous=await s.get('market-directory','binance-alpha') or {}
        await s.put('market-directory','binance-alpha',{
            **previous,'status':'stale' if previous.get('updatedAt') else 'error',
            'error':type(error).__name__,'lastAttemptAt':now_ms()})
        return {'requested':2,'accepted':0,'failed':1}


async def _feed_fact(s,market):
    from .market_streams import MarketStreamProcessor
    await MarketStreamProcessor._fact(s,'market-registry',market.storage,market.record())


async def markets():
    selected=[]
    for chain in CHAINS:
        s=await store(chain)
        assets=set()
        for kind in ('asset','stock'):
            for row in await s.all(kind):
                address=str(row.get('token') or row.get('tokenContractAddress') or row.get('address') or '').lower()
                if address:
                    assets.add(address)
        watched={(r.get('address'),r.get('market')) for _,r in await s.all_kv('candle-watch')
                 if r.get('venue')=='binance-alpha' and r.get('expiresAt',0)>now_ms()}
        definitions=[]
        for _,row in await s.all_kv('market-registry'):
            definition=row.get('definition') or {}
            if definition.get('venue')=='binance-alpha':
                definitions.append(Market(**definition))
        definitions.sort(key=lambda m:({'USDT':0,'USDC':1}.get(m.quote_currency,2),m.market_id))
        chosen=set()
        for market in definitions:
            baseline=market.token in assets and market.token not in chosen
            demand=(market.token,market.market_id) in watched or (market.token,None) in watched and market.token not in chosen
            if baseline or demand:
                selected.append(market)
                chosen.add(market.token)
    return sorted(selected,key=lambda m:(m.chain_id,m.token,m.market_id))


def start_stream():
    global _feed,_directory_task
    if os.environ.get('NODE_ENV')=='test':
        return
    if _feed is None:
        _feed=BinanceMarketFeed('binance-alpha','wss://nbstream.binance.com/w3w/wsa/stream/stream',
                                markets,source='Binance Alpha')
    if _directory_task is None or _directory_task.done():
        async def refresh_loop():
            while True:
                await refresh_directory()
                await asyncio.sleep(600)
        _directory_task=asyncio.create_task(refresh_loop(),name='binance-alpha-directory')
    return _feed.start()


async def stop_stream():
    global _feed,_directory_task
    if _directory_task:
        _directory_task.cancel()
        await asyncio.gather(_directory_task,return_exceptions=True)
        _directory_task=None
    if _feed:
        await _feed.stop()
        _feed=None
