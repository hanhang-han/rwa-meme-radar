"""Independent pool OHLCV fallback; never mix its rows with OKX history."""
import asyncio
import time
import httpx

NETWORKS = {'56':'bsc','196':'x-layer','4663':'robinhood'}
INTERVALS = {'1m':('minute',1), '5m':('minute',5), '15m':('minute',15),
             '1H':('hour',1), '4H':('hour',4), '6H':('hour',1), '12H':('hour',12),
             '1D':('day',1), '1W':('day',1)}
_pools = {}
_lock = asyncio.Lock()
_next_at = 0.0

async def _get(path, params=None):
    global _next_at
    await asyncio.wait_for(_lock.acquire(), timeout=20)
    try:
        delay = max(0,_next_at-time.monotonic())
        if delay > 3.1:
            raise RuntimeError("GeckoTerminal cooling down")
        await asyncio.sleep(delay)
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get('https://api.geckoterminal.com/api/v2'+path,params=params,
                                        headers={'accept':'application/json','user-agent':'CliperxRadar/1.0'})
        _next_at = time.monotonic() + (60 if response.status_code == 429 else 3)
        response.raise_for_status()
        return response.json()
    finally:
        _lock.release()

def aggregate_rows(rows, period_ms, weekly=False):
    groups = {}
    offset = 3*86400000 if weekly else 0  # UTC Monday boundary
    for r in sorted(rows,key=lambda r:r['t']):
        t = (r['t']+offset)//period_ms*period_ms-offset
        if t not in groups:
            groups[t] = {**r,'t':t}
        else:
            g=groups[t];g['h']=max(g['h'],r['h']);g['l']=min(g['l'],r['l']);g['c']=r['c'];g['vu']+=r['vu']
    return list(groups.values())

async def fetch_candles(chain, address, bar, limit, store, period_ms):
    network=NETWORKS[chain]
    key=network+':'+address
    hit=_pools.get(key)
    if not hit:
        saved=await store.get('candle-pool',key)
        if isinstance(saved,dict) and time.time()-saved.get('at',0)<3600:
            hit=saved
    if not hit or time.time()-hit['at']>3600:
        hit = None
        data=await _get(f'/networks/{network}/tokens/{address}/pools')
        for pool in data.get('data',[]):
            for side in ('base','quote'):
                token=pool.get('relationships',{}).get(side+'_token',{}).get('data',{}).get('id','')
                pool_address=pool.get('attributes',{}).get('address')
                if token.lower()==key.replace(':','_',1) and pool_address:
                    hit={'pool':pool_address,'side':side,'at':time.time()}
                    break
            if hit:break
        if not hit:raise ValueError('No matching pool')
        _pools[key]=hit
        await store.put('candle-pool',key,hit)
    timeframe,aggregate=INTERVALS[bar]
    factor=6 if bar=='6H' else 7 if bar=='1W' else 1
    body=await _get(f'/networks/{network}/pools/{hit["pool"]}/ohlcv/{timeframe}',
                    {'aggregate':aggregate,'limit':min(1000,(limit+1)*factor),'currency':'usd',
                     'token':hit['side'],'include_empty_intervals':'false'})
    data=body.get('data',{}).get('attributes',{}).get('ohlcv_list',[])
    rows=[{'t':int(r[0])*1000,'o':float(r[1]),'h':float(r[2]),'l':float(r[3]),'c':float(r[4]),
           'v':None,'vu':float(r[5]),'confirmed':False} for r in data if len(r)>=6]
    rows=aggregate_rows(rows,period_ms,bar=='1W') if factor>1 else sorted(rows,key=lambda r:r['t'])
    for r in rows:r['confirmed']=r['t']+period_ms<=time.time()*1000
    if not rows:raise ValueError('No pool candles')
    return rows[-limit:], {'source':'GeckoTerminal','pool':hit['pool'],'tokenSide':hit['side'],
                           'storage':f'gecko:{hit["pool"]}:{address}'}
