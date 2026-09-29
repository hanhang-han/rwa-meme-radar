"""Single-owner demand collection; query handlers only read the shared cache."""
import time

from ..db import store
from ..okx_client import request_lane


async def refresh_watched_candles():
    from ..api.candles import candle_series, BARS
    now = int(time.time()*1000)
    work = []
    for chain in ('196','56','4663'):
        s = await store(chain)
        for key, lease in await s.all_kv('candle-watch'):
            if lease.get('expiresAt',0)<=now or lease.get('bar') not in BARS:
                continue
            if lease.get('pool'):
                # Native pool bars are maintained by the chain-log collector;
                # querying an unrelated token/USD REST feed would mix units.
                continue
            meta = await s.get('candle-meta',key) or {}
            if meta.get('nextRefreshAt',0)>now:
                continue
            work.append((meta.get('lastAttemptAt',0),chain,lease))
    # Two global attempts per tick; aged jobs get the next slots. Demand does
    # not bypass the shared day budget or the five-minute DEX cooldown.
    result = {'requested':0,'accepted':0,'failed':0,'quotaBlocked':0}
    for _,chain,lease in sorted(work,key=lambda item:item[0])[:2]:
        result['requested']+=1
        with request_lane('candles',800):
            response = await candle_series(chain,lease['address'],lease['bar'],1000,lease['venue'],
                                           lease.get('market'),lease.get('pool'))
        if response.get('stale'):
            result['failed']+=1
            result['quotaBlocked']+=int(response.get('error')=='quota-exhausted')
        else:
            result['accepted']+=len(response.get('rows') or [])
    return result
