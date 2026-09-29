"""OHLCV candles: OKX DEX REST proxy with a phase-aware TTL cache and sqlite
persistence so history extends beyond the API's recent window.

Cache policy: closed bars keep the long per-bar TTL; the open (incomplete)
bar refreshes quickly from the same OKX source — the chart never fabricates
OHLCV from a price snapshot. On upstream failure the stored history is still
served, flagged stale instead of a 502."""
import asyncio
import time
import math
import json
import httpx

from fastapi import APIRouter, HTTPException, Query

from ..db import store, ResearchStore
from ..okx_client import okx_get, QuotaExceeded
from ..gecko_candles import fetch_candles as gecko_candles

router = APIRouter()

BAR_TTL = {
    "1m": 25, "5m": 120, "15m": 300, "1H": 600,
    "4H": 1800, "6H": 2400, "12H": 3600, "1D": 7200, "1W": 86400,
}  # seconds — the TTL for closed history
BAR_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "1H": 3_600_000,
    "4H": 14_400_000, "6H": 21_600_000, "12H": 43_200_000,
    "1D": 86_400_000, "1W": 604_800_000,
}
BARS = list(BAR_TTL)

_mem: dict[str, tuple[float, list, bool]] = {}
_inflight: dict[str, asyncio.Future] = {}
_meta: dict[str, dict] = {}

def result(key, bar, entry):
    at,rows,stale = entry
    meta=_meta.get(key,{})
    return {"bar":bar,"rows":observation_rows(rows,meta),"at":at,"stale":stale,**meta,
            "lastCandleAt":rows[-1]["t"] if rows else None,"timeZone":"UTC"}


def observation_rows(rows,meta):
    """Snapshot watermarks are observations, never invented trade timestamps."""
    history_at=meta.get('lastHistoryRequestAt') or 0
    first=meta.get('lastHistoryFrom')
    tail=meta.get('lastHistoryTailAt')
    live_tail=meta.get('lastRealtimeCandleAt')
    result=[]
    for row in rows:
        observed=history_at if first is not None and tail is not None and first<=row['t']<=tail else 0
        observed=max(observed,(meta.get('rowObservedAt') or {}).get(str(row['t']),0))
        if row['t']==live_tail:
            observed=max(observed,meta.get('lastSourceEventAt') or 0)
        result.append({**row,'observedAt':observed} if observed else row)
    return result


def _num(v):
    try:
        n = float(v)
        return n if math.isfinite(n) and n > 0 else None
    except (TypeError, ValueError):
        return None


def _vol(v):
    try:
        n = float(v)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError):
        return None


def cache_ttl(bar: str, rows: list, _fetched_at: float) -> float:
    if not rows:
        return min(BAR_TTL[bar],30)
    period = BAR_MS[bar]
    # Never retain a cached response across the next bar boundary.
    if int(_fetched_at * 1000 // period) != int(time.time() * 1000 // period):
        return 0
    # The endpoint includes the tail: even daily/weekly tails must refresh.
    return min(BAR_TTL[bar], 30)


async def resolve_market(chain, address, venue, market_id=None, pool=None):
    from ..collectors.market_streams import Market
    if market_id and venue in ('binance','binance-alpha'):
        market_id=market_id.upper()
    if venue == 'binance':
        from ..collectors.binance import BSTOCKS
        entry=next((row for row in BSTOCKS if row['addr'].lower()==address),None)
        if not entry or chain!='56' or market_id and market_id!=entry['symbol']+'USDT':
            return None
        return Market(chain,address,venue,entry['symbol']+'USDT','USDT',entry['symbol'],underlying=entry['underlying'])
    candidates=[]
    for _,row in await (await store(chain)).all_kv('market-registry'):
        definition=row.get('definition') or {}
        if definition.get('venue')!=venue or definition.get('token','').lower()!=address:
            continue
        if market_id and definition.get('market_id')!=market_id:
            continue
        if pool and (definition.get('pool_id') or '').lower()!=pool:
            continue
        candidates.append(Market(**definition))
    return min(candidates,key=lambda m:({'USDT':0,'USDC':1}.get(m.quote_currency,2),m.market_id),default=None)


def series_key(chain,address,bar,venue,market=None):
    return market.candle_key(bar) if market else f'{venue}:{chain}:{address}:{bar}'


def legacy_meta_matches(ident,key,meta,address,venue,instrument=None):
    """Old cache keys ended with a numeric response limit, never a market id.

    A native pool key shares the aggregate token prefix but has different
    price units. Prefix matching alone can show that pool as a USD chart.
    """
    if not ident.startswith(key+':') or not ident[len(key)+1:].isdecimal() or not isinstance(meta,dict):
        return False
    if meta.get('venue') not in (None,venue):
        return False
    if instrument:
        return (meta.get('marketId') in (None,instrument.market_id)
                and meta.get('storage') in (None,instrument.storage)
                and meta.get('priceCurrency') in (None,instrument.quote_currency)
                and not meta.get('pool') and not meta.get('poolId'))
    if meta.get('marketId') or meta.get('poolId') or meta.get('priceCurrency') not in (None,'USD'):
        return False
    storage=meta.get('storage',address)
    # Gecko's explicitly USD-valued fallback is isolated from native pools.
    return storage==address or (meta.get('source')=='GeckoTerminal'
        and isinstance(storage,str) and storage.startswith('gecko:') and storage.endswith(':'+address))


def pool_freshness(health,cursor,meta,now):
    """A quiet, fully scanned pool is different from a disconnected collector."""
    health=health if isinstance(health,dict) else {}
    cursor=cursor if isinstance(cursor,dict) else {}
    head=health.get('lastHead')
    scanned=cursor.get('block')
    scanned_at=cursor.get('updatedAt') or 0
    block_time=cursor.get('blockTime')
    caught_up=(0<=now-int(block_time)<30000 if isinstance(block_time,(int,float)) and block_time>0
               else isinstance(head,int) and isinstance(scanned,int) and head-scanned<=32)
    transport_ready=(health.get('status') in ('live','catching-up')
                     and 0<=now-int(health.get('updatedAt') or 0)<=30000)
    healthy=(transport_ready
             and 0<=now-int(scanned_at)<=20000 and caught_up)
    last_trade=meta.get('lastSourceEventAt')
    # New live trades can be current while the durable historical range is
    # still catching up. Do not call a quiet pool healthy without that scan,
    # or relabel old replayed trades as new merely because they just arrived.
    live_current=(transport_ready and bool(last_trade) and 0<=now-int(last_trade)<=30000
                  and 0<=now-int(health.get('lastReceivedAt') or 0)<=30000
                  and 0<=now-int(health.get('lastProcessedAt') or 0)<=30000
                  and 0<=float(health.get('sourceLagMs') or 0)<=10000
                  and int(health.get('queueDepth') or 0)<=16)
    quiet=healthy and (not last_trade or now-int(last_trade)>60000)
    return {'stale':not (healthy or live_current),'transportStatus':health.get('status','starting'),
            'marketStatus':'quiet' if quiet else 'live' if healthy or live_current else 'recovering',
            'coverageStatus':'current' if healthy else 'backfilling',
            'lastTradeAt':last_trade,'scanThroughBlock':scanned,'scanAt':scanned_at,
            'headBlock':head,'scanBlockTime':block_time,'coverage':'observed'}


async def _put_exchange_history(s,storage,bar,rows,key,request_started):
    if not isinstance(s,ResearchStore):
        # The store protocol remains mockable in provider-contract tests.
        return await s.put_candles(storage,bar,rows)
    async with s._guard_write():
        await s.db.execute('BEGIN IMMEDIATE')
        try:
            latest=await s.get('candle-meta',key) or {}
            if latest.get('lastHistoryRequestAt',0)>request_started:
                await s.db.commit()
                return
            if latest.get('lastSourceEventAt',0)>request_started:
                rows=[row for row in rows if row.get('confirmed')]
            if rows:
                await s.db.executemany('''INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(asset,bar,openTime) DO UPDATE SET
                    open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                    volume=excluded.volume,volumeUsd=excluded.volumeUsd,confirmed=excluded.confirmed
                    WHERE candles.confirmed=0 OR excluded.confirmed=1''',
                    [(s.key(storage),bar,row['t'],row['o'],row['h'],row['l'],row['c'],row.get('v'),row.get('vu'),int(bool(row.get('confirmed')))) for row in rows])
                latest.update(lastHistoryRequestAt=request_started,
                              lastHistoryFrom=min(row['t'] for row in rows),
                              lastHistoryTailAt=max(row['t'] for row in rows),
                              lastObservationAt=max(request_started,latest.get('lastSourceEventAt') or 0))
                await s.db.execute('INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body',
                                   (s.key('candle-meta'),key,json.dumps(latest)))
            await s.db.commit()
        except BaseException:
            await s.db.rollback()
            raise


async def _put_history_meta(s,key,source,request_started):
    if not isinstance(s,ResearchStore):
        await s.put('candle-meta',key,source)
        return source
    async with s._guard_write():
        await s.db.execute('BEGIN IMMEDIATE')
        try:
            latest=await s.get('candle-meta',key) or {}
            if max(latest.get('lastSourceEventAt',0),latest.get('lastHistoryRequestAt',0))>request_started:
                source={**source,**latest}
            else:
                source={**latest,**source}
            await s.db.execute('INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body',
                               (s.key('candle-meta'),key,json.dumps(source)))
            await s.db.commit()
            return source
        except BaseException:
            await s.db.rollback()
            raise


async def candle_series(chain: str, address: str, bar: str, limit: int, venue: str = 'dex', market_id=None, pool=None):
    market = await resolve_market(chain,address,venue,market_id,pool) if venue!='dex' or pool else None
    if (venue!='dex' or pool) and market is None:
        return {'bar':bar,'rows':[],'stale':True,'error':'unmapped-market','venue':venue,'status':'unavailable'}
    key = series_key(chain,address,bar,venue,market)
    limit = 1000
    storage_address = market.storage if market else address
    # Native pool history is built from confirmed protocol logs. USD token
    # aggregator history is a different market and cannot fill its gaps.
    if pool:
        s=await store(chain)
        meta=await s.get('candle-meta',key) or {}
        rows=await s.candle_range(storage_address,bar,limit)
        return {**meta,**market.frame(),'source':meta.get('source','On-chain pool'),
                'bar':bar,'rows':observation_rows(rows,meta),'storage':storage_address,'stale':bool(meta.get('stale')),
                'error':None if rows else 'pool-history-unavailable','timeZone':'UTC'}
    hit = _mem.get(key)
    now = time.time()
    if hit and now - hit[0] < cache_ttl(bar, hit[1], hit[0]):
        return result(key,bar,hit)
    pending = _inflight.get(key)
    if pending:
        rows = await pending
        cached = _mem.get(key)
        return result(key,bar,cached or (now,rows,True))

    async def task():
        stale = False
        exchange=venue in ('binance','binance-alpha')
        source = {'source':('Binance Alpha' if venue=='binance-alpha' else 'Binance') if exchange else 'OKX DEX',
                  'storage':storage_address,'priceCurrency':market.quote_currency if market else 'USD',
                  'volumeCurrency':market.quote_currency if market else 'USD','venue':venue,
                  **(market.frame() if market else {})}
        s = await store(chain)
        previous = await s.get('candle-meta', key)
        previous = previous if isinstance(previous,dict) else {}
        error = None
        request_started=int(time.time()*1000)
        try:
            if exchange:
                endpoint=('https://www.binance.com/bapi/defi/v1/public/alpha-trade/klines'
                          if venue=='binance-alpha' else 'https://api.binance.com/api/v3/klines')
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(endpoint, params={
                        'symbol':market.market_id,'interval':bar.lower(),'limit':min(limit,1000)})
                    response.raise_for_status()
                    data = response.json()
                if venue=='binance-alpha':
                    if not isinstance(data,dict) or data.get('code')!='000000':
                        raise ValueError('Invalid Alpha candles')
                    data=data.get('data')
                if not isinstance(data,list):
                    raise ValueError('Invalid exchange candles')
                parsed=[]
                for row in data:
                    if len(row)<8 or any(_num(v) is None for v in row[1:5]):
                        continue
                    parsed.append({'t':int(row[0]),'o':float(row[1]),'h':float(row[2]),'l':float(row[3]),'c':float(row[4]),
                                   'v':_vol(row[5]),'vu':_vol(row[7]),'confirmed':int(row[6])<time.time()*1000})
            else:
                data = await okx_get('/api/v6/dex/market/candles', {
                    'chainIndex': chain, 'tokenContractAddress': address, 'bar': bar,
                    'limit': str(min(limit, 1000)),
                }, {'skip_round': True, 'priority': 'interactive'})
                if not isinstance(data, list):
                    raise RuntimeError('Invalid candles response')
                parsed = []
                for row in data:
                    t, o, h, l, c, v, vu, confirmed = (list(row) + [None] * 8)[:8]
                    if not t or not o or not c:
                        continue
                    parsed.append({
                        't': int(t), 'o': float(o), 'h': float(h or o), 'l': float(l or o), 'c': float(c),
                        'v': _vol(v), 'vu': _vol(vu), 'confirmed': confirmed == '1',
                    })
            if not parsed:
                raise ValueError('No candles returned')
            # REST can return after WS already persisted a newer open candle.
            # Protect the tail; closed historical bars still backfill normally.
            latest=await s.get('candle-meta',key) or {}
            if isinstance(latest,dict) and latest.get('lastSourceEventAt',0)>request_started:
                parsed=[row for row in parsed if row.get('confirmed')]
            if parsed:
                if exchange:
                    await _put_exchange_history(s,storage_address,bar,parsed,key,request_started)
                else:
                    await s.put_candles(storage_address, bar, parsed)
        except Exception as e:
            stale = True
            error = 'quota-exhausted' if isinstance(e,QuotaExceeded) else 'upstream-unavailable'
            source = previous or source
            if venue == 'dex':
                try:
                    parsed, source = await gecko_candles(chain,address,bar,limit,s,BAR_MS[bar])
                    source.update(venue='dex',scope='pool',priceCurrency='USD',volumeCurrency='USD')
                    await s.put_candles(source['storage'],bar,parsed)
                    source['fallbackReason'] = error
                    stale = False
                except Exception:
                    pass
        attempted = int(time.time()*1000)
        latest=await s.get('candle-meta',key)
        if isinstance(latest,dict) and latest.get('lastSourceEventAt',0)>request_started:
            source={**source,**latest}
            stale=bool(latest.get('stale'))
        elif not stale:
            source['lastSuccessfulAt'] = attempted
        source.update(lastAttemptAt=attempted,stale=stale,error=error if stale else None,
                      nextRefreshAt=attempted+(60000 if exchange else 300000))
        source=await _put_history_meta(s,key,source,request_started)
        stale=bool(source.get('stale'))
        rows = await s.candle_range(source.get('storage',storage_address), bar, min(limit,1000))
        _meta[key] = {**source,'error':error if stale else None}
        _mem[key] = (time.time(), rows, stale)
        if len(_mem) > 600:
            for k in list(_mem)[: len(_mem) - 500]:
                _mem.pop(k, None)
        return rows

    fut = asyncio.ensure_future(task())
    _inflight[key] = fut
    try:
        rows = await fut
        cached = _mem.get(key)
        return result(key,bar,cached or (time.time(),rows,True))
    finally:
        _inflight.pop(key, None)


@router.get('/candles/{chain}/{address}')
async def get_candles(chain: str, address: str, bar: str = Query(default='5m'), limit: int = Query(default=500, ge=30, le=1000),
                      venue: str = Query(default='dex'), market: str | None = Query(default=None), pool: str | None = Query(default=None)):
    address = address.lower()
    # Direct Python callers from tests do not receive FastAPI parameter coercion.
    market = market if isinstance(market,str) else None
    pool = pool.lower() if isinstance(pool,str) else None
    if venue not in ('dex','binance','binance-alpha') or (venue=='binance' and chain!='56'):
        raise HTTPException(status_code=400,detail='unsupported venue')
    if bar not in BARS or chain not in ('196','56','4663') or not (address.startswith('0x') and len(address)==42):
        raise HTTPException(status_code=400,detail='bad request')
    if pool and (venue!='dex' or not pool.startswith('0x') or len(pool)!=42):
        raise HTTPException(status_code=400,detail='bad pool')
    try:
        s=await store(chain)
        instrument=await resolve_market(chain,address,venue,market,pool) if venue!='dex' or pool else None
        key=series_key(chain,address,bar,venue,instrument)
        now=int(time.time()*1000)
        if (venue!='dex' or pool) and instrument is None:
            return {'venue':venue,'bar':bar,'rows':[],'status':'unavailable','stale':True,
                    'error':'unmapped-market','pool':pool,'marketId':market,'timeZone':'UTC'}
        # Persist demand, not process memory, so ten visitors share one job.
        from ..demand_leases import publish_lease
        publish_lease(s,'candle-watch',key,{'chain':chain,'address':address,'bar':bar,'venue':venue,
                    'market':instrument.market_id if instrument else None,'pool':pool,'expiresAt':now+120000})
        meta=await s.get('candle-meta',key)
        if not isinstance(meta,dict):
            legacy=[value for ident,value in await s.all_kv('candle-meta')
                    if legacy_meta_matches(ident,key,value,address,venue,instrument)]
            meta=max(legacy,key=lambda row:row.get('lastSuccessfulAt',0),default={}) if not instrument or venue=='binance' else {}
        storage=meta.get('storage',instrument.storage if instrument else address)
        rows=await s.candle_range(storage,bar,limit)
        exchange=venue in ('binance','binance-alpha')
        age=now-int(meta.get('lastSuccessfulAt') or 0)
        stale=bool(meta.get('stale')) or age>(60000 if exchange or pool else 360000)
        pool_state={}
        if pool:
            health,cursor=await asyncio.gather(s.get('chain-stream','pools'),s.get('chain-stream-cursor','pools'))
            pool_state=pool_freshness(health,cursor,meta,now)
            stale=pool_state['stale']
        status=('stale' if stale else 'current') if rows else ('unavailable' if meta.get('error') else 'collecting')
        return {**meta,**(instrument.frame() if instrument else {}),'bar':bar,'rows':observation_rows(rows,meta),'at':meta.get('lastSuccessfulAt',0)/1000,
                **pool_state,
                'stale':stale,'status':status,'lastCandleAt':rows[-1]['t'] if rows else None,
                'timeZone':'UTC','refreshIntervalMs':1000 if exchange or pool else 300000,
                'nextRefreshAt':meta.get('nextRefreshAt'),'error':meta.get('error'),
                'source':meta.get('source','Binance Alpha' if venue=='binance-alpha' else 'Binance' if venue=='binance' else 'On-chain pool' if pool else 'OKX DEX')}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502,detail=str(e)[:120])
