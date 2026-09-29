"""Market-scoped stream primitives. No quote is silently promoted to a DEX USD price.

Quotes/candles are latest-value observations; trades retain their source identity.
The buffered writer coalesces replaceable observations, never individual trades.
"""
import asyncio
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import json
import math
import time

from ..db import ResearchStore, store

BAR_MS = {'1m':60000,'5m':300000,'15m':900000,'1H':3600000,'4H':14400000,
          '6H':21600000,'12H':43200000,'1D':86400000,'1W':604800000}


def now_ms():
    return int(time.time()*1000)


def market_storage_key(market):
    return market.storage


def finite(value, *, positive=False):
    try:
        number = float(value)
        return number if math.isfinite(number) and (number > 0 if positive else number >= 0) else None
    except (TypeError, ValueError):
        return None


def _integer(value):
    if isinstance(value,str) and value.lower().startswith('0x'):
        return int(value,16)
    return int(value)


def trade_order(trade):
    """Numeric source ordering; lexical ids put trade 100 before trade 99."""
    supplied=trade.get('eventOrder')
    if supplied is not None:
        return tuple(_integer(v) for v in (supplied if isinstance(supplied,(list,tuple)) else [supplied]))
    if trade.get('blockNumber') is not None:
        return tuple(_integer(trade.get(k) or 0) for k in ('blockNumber','transactionIndex','logIndex'))
    if trade.get('sourceId') is not None:
        return (_integer(trade['sourceId']),)
    try:
        return (_integer(str(trade.get('id','')).rsplit(':',1)[-1]),)
    except (TypeError,ValueError):
        return (0,)


def merge_quote(old,new):
    """Price ticks and rolling statistics have independent source clocks."""
    old_at=old.get('sourceEventAt',old.get('marketAt',0))
    new_at=new.get('sourceEventAt',new.get('marketAt',0))
    merged={**old,**new} if new_at>=old_at else dict(old)
    stats=('volume24h','exchangeTrades24h','change24h','statisticsAt')
    if new.get('statisticsAt',0)>=old.get('statisticsAt',0):
        merged.update({key:new[key] for key in stats if key in new})
        if new.get('statisticsAt'):
            merged['receivedAt']=new.get('receivedAt',merged.get('receivedAt'))
    else:
        merged.update({key:old[key] for key in stats if key in old})
    return merged


@dataclass(frozen=True)
class Market:
    chain_id: str
    token: str
    venue: str
    market_id: str
    quote_currency: str
    base_symbol: str = ''
    kind: str = 'exchange-token'
    pool_id: str | None = None
    underlying: str | None = None
    quote_token: str | None = None

    @property
    def storage(self):
        # Preserve the already deployed bStocks history key. Alpha and each
        # native DEX pool have separate units and must never share that series.
        if self.venue == 'binance':
            return 'binance:' + self.token.lower()
        unit=self.quote_token.lower() if self.pool_id and self.quote_token else self.quote_currency
        return ':'.join((self.venue, self.market_id, unit, self.token.lower()))

    def candle_key(self, bar):
        key = f'{self.venue}:{self.chain_id}:{self.token.lower()}:{bar}'
        return key if self.venue == 'binance' else key + ':' + self.market_id

    def frame(self):
        return {'chainId':self.chain_id,'token':self.token.lower(),'venue':self.venue,
                'marketId':self.market_id,'symbol':self.base_symbol,'quoteType':self.kind,
                'priceCurrency':self.quote_currency,'volumeCurrency':self.quote_currency,
                'priceScope':'pool' if self.pool_id else 'exchange',
                'volumeScope':'pool' if self.pool_id else 'exchange',
                **({'poolId':self.pool_id,'pool':self.pool_id} if self.pool_id else {}),
                **({'quoteToken':self.quote_token.lower()} if self.quote_token else {}),
                **({'underlying':self.underlying} if self.underlying else {})}

    def record(self):
        return {**self.frame(),'storage':self.storage,'definition':asdict(self)}


def trade_to_candle(previous, trade, bar_ms):
    """Fold ONE already deduplicated trade, using event time, in native quote units.

    Preserve the returned first/last metadata when persisting the accumulator.
    This function neither deduplicates nor fabricates candles without a trade.
    Weeks use Monday 00:00 UTC, matching exchange kline boundaries.
    """
    try:
        at = int(trade.get('t') or trade.get('sourceEventAt'))
        price = Decimal(str(trade['price']))
        size = Decimal(str(trade.get('quantity', trade.get('v', 0))))
        quote_size = Decimal(str(trade.get('quoteQuantity', price * size)))
        if at <= 0 or not price.is_finite() or price <= 0 or not size.is_finite() or size < 0 or not quote_size.is_finite() or quote_size < 0:
            return None
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    offset = 4 * 86400000 if bar_ms == 604800000 else 0
    opened = (at-offset)//bar_ms*bar_ms+offset
    order = (at, trade_order(trade))
    trade_id=str(trade.get('id',''))
    if not previous or previous.get('t') != opened:
        return {'t':opened,'o':float(price),'h':float(price),'l':float(price),'c':float(price),
                'v':float(size),'vu':float(quote_size),'confirmed':False,
                'firstEventAt':at,'lastEventAt':at,'firstTradeId':trade_id,'lastTradeId':trade_id,
                'firstEventOrder':list(order[1]),'lastEventOrder':list(order[1]),
                '_vExact':str(size),'_vuExact':str(quote_size)}
    row = dict(previous)
    first = (row.get('firstEventAt',at), tuple(row.get('firstEventOrder') or trade_order({'id':row.get('firstTradeId')})))
    last = (row.get('lastEventAt',at), tuple(row.get('lastEventOrder') or trade_order({'id':row.get('lastTradeId')})))
    row.update(h=float(max(Decimal(str(row['h'])),price)),l=float(min(Decimal(str(row['l'])),price)),
               _vExact=str(Decimal(str(row.get('_vExact',row.get('v',0))))+size),
               _vuExact=str(Decimal(str(row.get('_vuExact',row.get('vu',0))))+quote_size))
    row.update(v=float(Decimal(row['_vExact'])),vu=float(Decimal(row['_vuExact'])))
    if order < first:
        row.update(o=float(price),firstEventAt=at,firstTradeId=trade_id,firstEventOrder=list(order[1]))
    if order >= last:
        row.update(c=float(price),lastEventAt=at,lastTradeId=trade_id,lastEventOrder=list(order[1]))
    return row


def normalize_binance_trade(data):
    """The aggregation id and raw trade id occupy separate namespaces."""
    aggregate = data.get('e') == 'aggTrade' or 'a' in data and 't' not in data
    ident = data.get('a') if aggregate else data.get('t')
    price, quantity = finite(data.get('p'),positive=True), finite(data.get('q'))
    at = int(data.get('T') or 0)
    if ident is None or not at or price is None or quantity is None:
        return None
    return {'id':('agg:' if aggregate else 'trade:')+str(ident),'sourceId':int(ident),
            't':at,'price':price,'quantity':quantity,
            'quoteQuantity':float(Decimal(str(data['p']))*Decimal(str(data['q']))),
            'type':'sell' if data.get('m') else 'buy','isBuyerMaker':bool(data.get('m')),
            'sourceEventAt':int(data.get('E') or at),'aggregation':'aggregate' if aggregate else 'individual'}


def normalize_binance_candle(data):
    k = data.get('k') or {}
    bar = {'1h':'1H','4h':'4H','6h':'6H','12h':'12H','1d':'1D','1w':'1W'}.get(k.get('i'),k.get('i'))
    if bar not in BAR_MS:
        return None
    prices = [finite(k.get(x),positive=True) for x in ('o','h','l','c')]
    volume,quote_volume = finite(k.get('v')),finite(k.get('q'))
    if any(v is None for v in prices) or volume is None or quote_volume is None:
        return None
    try:
        at = int(k['t'])
    except (KeyError,ValueError,TypeError):
        return None
    return bar, {'t':at,**dict(zip(('o','h','l','c'),prices)), 'v':volume,'vu':quote_volume,'confirmed':bool(k.get('x'))}


class MarketStreamProcessor:
    """A bounded 250ms batch writer, shared by a collector's connections.

    Start/stop are explicit worker lifecycle hooks. submit blocks at capacity,
    allowing socket backpressure to disconnect and subsequently trigger replay.
    No unbounded raw quote history is written.
    """
    def __init__(self, source='Binance', flush_interval=.25, capacity=4096):
        self.source = source
        self.flush_interval = flush_interval
        self.queue = asyncio.Queue(maxsize=capacity)
        self.task = None
        self.error = None
        self.last_persisted_at = None
        self._last_prune = {}
        self.on_quote = None
        self._writers = {}
        self._writer_init_lock = asyncio.Lock()

    async def _writer_store(self, chain):
        """Keep live writes off the shared reader/collector connection queue.

        The process-wide SQLite writer lock is still shared. Only the
        aiosqlite worker thread is private, so a long read on store(chain)
        cannot run between BEGIN and COMMIT while this task holds that lock.
        """
        if chain not in self._writers:
            async with self._writer_init_lock:
                if chain not in self._writers:
                    shared = await store(chain)
                    self._writers[chain] = (shared if shared.path == ':memory:' else
                                            await ResearchStore(shared.path, chain).connect())
        return self._writers[chain]

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(),name=self.source+'-market-writer')
        return self.task

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
            self.task = None
        pending=[]
        while not self.queue.empty():
            pending.append(self.queue.get_nowait())
        try:
            if pending:
                await self.flush(pending)
        finally:
            writers, self._writers = self._writers, {}
            for writer in writers.values():
                if writer.path != ':memory:':
                    await writer.close()

    async def quote(self, market, data):
        await self.queue.put(('quote',market,dict(data)))

    async def trade(self, market, data):
        await self.queue.put(('trade',market,dict(data)))

    async def candle(self, market, bar, row, source_at=None, received_at=None):
        await self.queue.put(('candle',market,{'bar':bar,'row':dict(row),'sourceEventAt':source_at or now_ms(),
                                            'receivedAt':received_at or now_ms()}))

    async def _run(self):
        pending=[]
        try:
            while True:
                if not pending:
                    pending.append(await self.queue.get())
                await asyncio.sleep(self.flush_interval)
                while len(pending)<1000 and not self.queue.empty():
                    pending.append(self.queue.get_nowait())
                try:
                    await self.flush(pending)
                    pending=[]
                    self.error=None
                except Exception as error:
                    self.error=type(error).__name__
                    await asyncio.sleep(1)
        finally:
            if pending:
                await self.flush(pending)

    async def flush(self, messages):
        from ..realtime_schema import enqueue_events
        # Replay pages contain up to 1,000 trades. A 128-event transaction
        # held SQLite's only writer for over 45 seconds in production. Keep
        # each commit short so the projection and quote collectors can write.
        if len(messages)>16:
            for offset in range(0,len(messages),16):
                await self.flush(messages[offset:offset+16])
                await asyncio.sleep(0)
            return
        # Coalesce only replaceable observations, choosing newest source time.
        latest, trades = {}, []
        for typ, market, data in messages:
            if typ == 'trade':
                trades.append((typ,market,data))
            else:
                ident=(typ,market.chain_id,market.storage,data.get('bar'),(data.get('row') or {}).get('t'))
                prior=latest.get(ident)
                if typ=='quote' and prior is not None:
                    latest[ident]=(typ,market,merge_quote(prior[2],data))
                elif prior is None or data.get('sourceEventAt',data.get('marketAt',0)) >= prior[2].get('sourceEventAt',prior[2].get('marketAt',0)):
                    latest[ident]=(typ,market,data)
        grouped={}
        for item in [*trades,*latest.values()]:
            grouped.setdefault(item[1].chain_id,[]).append(item)
        for chain, items in grouped.items():
            s = await self._writer_store(chain)
            frames=[]
            quotes=[]
            async with s._guard_write():
                await s.db.execute('BEGIN IMMEDIATE')
                try:
                    # SQLite's RETURNING reports only trades this transaction
                    # inserted. A separate SELECT of these composite keys can
                    # stall under production I/O while holding the only write
                    # lock; this single statement keeps exact replay dedup and
                    # ensures events exist only for newly committed trades.
                    unique_trades = []
                    seen = set()
                    for typ, market, data in items:
                        if typ != 'trade':
                            continue
                        key = (s.key(market.storage), str(data['id']))
                        if key not in seen:
                            seen.add(key)
                            unique_trades.append((market, data, key))
                    if unique_trades:
                        trade_rows = []
                        trade_bodies = {}
                        for market, data, key in unique_trades:
                            persisted = now_ms()
                            body = {**market.frame(), 'source': self.source, 'provider': self.source,
                                    **data, 'receivedAt': data.get('receivedAt') or persisted,
                                    'persistedAt': persisted}
                            body['marketAt'] = body.get('marketAt') or body.get('sourceEventAt') or body.get('t') or persisted
                            body['sourceEventAt'] = body.get('sourceEventAt') or body['marketAt']
                            body['volume'] = body.get('quoteQuantity') if market.quote_currency == 'USD' else None
                            body['scope'] = 'pool' if market.pool_id else 'exchange'
                            trade_rows.append((key[0], key[1], data['t'], json.dumps(body)))
                            trade_bodies[key] = body
                        slots = ','.join('(?,?,?,?)' for _ in trade_rows)
                        params = tuple(value for row in trade_rows for value in row)
                        inserted = await s.fetchall(
                            f'INSERT OR IGNORE INTO trades(asset,id,t,body) VALUES {slots} RETURNING asset,id', params)
                        inserted_keys = {(row[0], row[1]) for row in inserted}
                        for _, _, key in unique_trades:
                            if key in inserted_keys:
                                frames.append(('trade', trade_bodies[key]))
                    for typ,market,data in items:
                        if typ == 'trade':
                            continue
                        persisted=now_ms()
                        body={**market.frame(),'source':self.source,'provider':self.source,
                              **data,'receivedAt':data.get('receivedAt') or persisted,'persistedAt':persisted}
                        body['marketAt']=body.get('marketAt') or body.get('sourceEventAt') or body.get('t') or persisted
                        body['sourceEventAt']=body.get('sourceEventAt') or body['marketAt']
                        if typ == 'quote':
                            if finite(body.get('price'),positive=True) is None:
                                continue
                            hit=await s.fetchone('SELECT body FROM facts WHERE kind=? AND id=?',
                                (s.key('market-quote'),market.storage))
                            old=json.loads(hit[0]) if hit else {}
                            if old.get('sourceEventAt',0)>body['sourceEventAt'] and old.get('statisticsAt',0)>=body.get('statisticsAt',0):
                                continue
                            body=merge_quote(old,body)
                            body['persistedAt']=persisted
                            body.update(timeKind='market',quoteAt=body['marketAt'],
                                priceProvenance={'timeKind':'market','marketAt':body['marketAt'],
                                  'venue':market.venue,'dependencies':[],'dependenciesComplete':True})
                            await self._fact(s,'market-quote',market.storage,body)
                            quotes.append((market,body))
                            event='stock-quote' if market.underlying else 'price'
                        else:
                            bar,row=data['bar'],data['row']
                            if bar not in BAR_MS:
                                continue
                            hit=await s.fetchone('SELECT body FROM facts WHERE kind=? AND id=?',
                                (s.key('market-candle'),market.storage+':'+bar))
                            old=json.loads(hit[0]) if hit else {}
                            meta=await s.get('candle-meta',market.candle_key(bar)) or {}
                            history_from=meta.get('lastHistoryFrom')
                            history_tail=meta.get('lastHistoryTailAt')
                            if (history_from is not None and history_tail is not None
                                    and history_from<=row['t']<=history_tail
                                    and body['sourceEventAt']<=meta.get('lastHistoryRequestAt',0)):
                                continue
                            previous_bar=await s.fetchone('SELECT confirmed FROM candles WHERE asset=? AND bar=? AND openTime=?',
                                                       (s.key(market.storage),bar,row['t']))
                            if previous_bar and previous_bar[0] and not row.get('confirmed'):
                                continue
                            if old.get('row',{}).get('t')==row['t'] and (old.get('sourceEventAt',0)>body['sourceEventAt'] or (old.get('row',{}).get('confirmed') and not row.get('confirmed'))):
                                continue
                            await s.db.execute('''INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)
                                ON CONFLICT(asset,bar,openTime) DO UPDATE SET open=excluded.open,high=excluded.high,
                                low=excluded.low,close=excluded.close,volume=excluded.volume,volumeUsd=excluded.volumeUsd,
                                confirmed=excluded.confirmed''',(s.key(market.storage),bar,row['t'],row['o'],row['h'],row['l'],row['c'],row.get('v'),row.get('vu'),int(bool(row.get('confirmed')))))
                            if row['t']>=old.get('row',{}).get('t',0):
                                await self._fact(s,'market-candle',market.storage+':'+bar,body)
                                await self._fact(s,'candle-meta',market.candle_key(bar),{
                                    **meta,**market.frame(),'source':self.source,'storage':market.storage,
                                    'lastSuccessfulAt':persisted,'lastSourceEventAt':max(body['sourceEventAt'],meta.get('lastSourceEventAt',0)),
                                    'lastObservationAt':max(body['sourceEventAt'],meta.get('lastObservationAt',0)),
                                    'lastRealtimeCandleAt':row['t'],
                                    'stale':False,'error':None,'transport':'websocket','nextRefreshAt':persisted+60000})
                            event='candle'
                        frames.append((event,body))
                    await enqueue_events(s.db, frames)
                    await s.db.commit()
                except BaseException:
                    await s.db.rollback()
                    raise
            self.last_persisted_at=now_ms()
            if self.on_quote:
                for market,body in quotes:
                    self.on_quote(market,body)
            if self.last_persisted_at-self._last_prune.get(chain,0)>60000:
                # Preserve a bounded recent tape, independently per market. An
                # outage exceeding retention stays explicitly incomplete.
                async with s._guard_write():
                    cutoff=self.last_persisted_at-86400000
                    for storage in {item[1].storage for item in items}:
                        await s.db.execute('DELETE FROM trades WHERE rowid IN (SELECT rowid FROM trades WHERE asset=? AND t<? LIMIT 1000)',(s.key(storage),cutoff))
                        await s.db.execute('''DELETE FROM trades WHERE asset=? AND rowid IN
                            (SELECT rowid FROM trades WHERE asset=? ORDER BY t DESC LIMIT 1000 OFFSET 10000)''',
                            (s.key(storage),s.key(storage)))
                    await s.db.commit()
                self._last_prune[chain]=self.last_persisted_at

    @staticmethod
    async def _fact(s,kind,key,value):
        await s.db.execute('INSERT INTO facts VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body',
                           (s.key(kind),key,json.dumps(value,allow_nan=False)))
