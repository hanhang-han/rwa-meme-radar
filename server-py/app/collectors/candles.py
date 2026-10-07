"""Single-owner demand collection; query handlers only read the shared cache."""
import asyncio
import time

from ..db import ResearchStore, quote_store_scope, store
from ..demand_leases import all_lease_kv
from ..okx_client import request_lane

_attempts = {}
MAX_EXCHANGE_REFRESHES = 12
EXCHANGE_CONCURRENCY = 4


async def refresh_watched_candles():
    from ..api.candles import candle_series, BARS
    now = int(time.time()*1000)
    work = {'exchange': [], 'dex': []}
    active = set()
    with quote_store_scope():
        for chain in ('196','56','4663'):
            s = await store(chain)
            for key, lease in await all_lease_kv(s, 'candle-watch'):
                if lease.get('expiresAt',0)<=now or lease.get('bar') not in BARS:
                    continue
                if lease.get('pool'):
                    # Native protocol history has separate units and owner.
                    continue
                active.add((chain,key))
                meta = await s.get('candle-meta',key) or {}
                if meta.get('nextRefreshAt',0)>now:
                    continue
                lane = 'exchange' if lease.get('venue') in ('binance','binance-alpha') else 'dex'
                work[lane].append((max(meta.get('lastAttemptAt',0),_attempts.get((chain,key),0)),
                                   chain,key,lease,s))
    for identity in list(_attempts):
        if identity not in active:
            _attempts.pop(identity,None)
    # Exchange demand never queues behind DEX quotas. Both lanes remain
    # finite; older attempts receive the next slots independently of viewers.
    result = {'requested':0,'accepted':0,'failed':0,'quotaBlocked':0}
    async def one(job):
        _,chain,key,lease,shared = job
        _attempts[(chain,key)] = now
        private = None
        try:
            # Parallel HTTP refreshes must never share a transaction connection
            # with another market or a cold catalogue job. Memory test stores
            # have only one connection and retain their common writer guard.
            if isinstance(shared,ResearchStore) and shared.path != ':memory:':
                private = await ResearchStore(shared.path,chain,write_lock=shared._write_lock,
                                              busy_timeout_ms=shared.busy_timeout_ms).connect()
            with request_lane('candles',800):
                response = await candle_series(chain,lease['address'],lease['bar'],1000,lease['venue'],
                    lease.get('market'),lease.get('pool'),candle_store=private or shared)
            if response.get('error') == 'unmapped-market':
                await (private or shared).put('candle-meta',key,{
                    'lastAttemptAt':now,'nextRefreshAt':now+60_000,'stale':True,'error':'unmapped-market'})
            return response
        finally:
            if private is not None:
                await private.close()

    selected = sorted(work['exchange'],key=lambda item:item[0])[:MAX_EXCHANGE_REFRESHES]
    responses = []
    for start in range(0,len(selected),EXCHANGE_CONCURRENCY):
        responses.extend(await asyncio.gather(*(one(job) for job in selected[start:start+EXCHANGE_CONCURRENCY]),
                                              return_exceptions=True))
    for job in sorted(work['dex'],key=lambda item:item[0])[:2]:
        try:
            responses.append(await one(job))
        except Exception as error:
            responses.append(error)
    for response in responses:
        if isinstance(response,asyncio.CancelledError):
            raise response
        result['requested']+=1
        if isinstance(response,Exception) or response.get('stale'):
            result['failed']+=1
            result['quotaBlocked']+=int(not isinstance(response,Exception)
                                       and response.get('error')=='quota-exhausted')
        else:
            result['accepted']+=len(response.get('rows') or [])
    return result
