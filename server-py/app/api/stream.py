"""Cursor-consistent SSE with bounded replay and one shared process tailer."""
import asyncio
import json
import time
import zlib

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from ..stream_hub import clients, cursor, hello, replay_batch, replay_page, _frame

router = APIRouter()


def accepts_gzip(value):
    """Use gzip only when the client explicitly accepts a positive quality."""
    if not isinstance(value, str):
        return False
    for entry in value.split(','):
        encoding, *parameters = entry.strip().lower().split(';')
        if encoding.strip() != 'gzip':
            continue
        quality = 1.0
        for parameter in parameters:
            name, separator, raw = parameter.strip().partition('=')
            if name.strip() == 'q':
                try:
                    quality = float(raw.strip()) if separator else 0
                except ValueError:
                    quality = 0
        if 0 < quality <= 1:
            return True
    return False


async def gzip_frames(source):
    """Flush each SSE frame immediately while retaining the gzip dictionary."""
    compressor = zlib.compressobj(wbits=31)
    try:
        async for frame in source:
            yield compressor.compress(frame) + compressor.flush(zlib.Z_SYNC_FLUSH)
        trailer = compressor.flush(zlib.Z_FINISH)
        if trailer:
            yield trailer
    finally:
        # Closing the response must reach frames()'s subscriber cleanup even
        # when cancellation arrives while this wrapper is suspended at yield.
        await source.aclose()


@router.get('/stream')
async def get_stream(last_event_id: str | None = Header(default=None, alias='Last-Event-ID'),
                     snapshot: bool = True, after: str | None = None,
                     protocol: int = 0, candles: str | None = None,
                     trades: str | None = None,
                     accept_encoding: str | None = Header(default=None, alias='Accept-Encoding')):
    q: asyncio.Queue = asyncio.Queue(maxsize=1024)
    # Browser reconnect headers take precedence over the original URL cursor.
    try:
        last_id = int(last_event_id if isinstance(last_event_id, str) else after)
    except (TypeError, ValueError):
        last_id = None
    candle_keys = set(candles.split(',')) if isinstance(candles, str) and candles != 'all' else None
    trade_scope = parse_trade_scope(trades)
    feed_tokens = set()
    if trade_scope == ('feed', ''):
        from ..realtime_projection import read_projection_json, ProjectionUnavailable
        try:
            feed = json.loads(await read_projection_json('feed'))
        except ProjectionUnavailable as exc:
            raise HTTPException(status_code=503, detail='snapshot-not-ready', headers={'Retry-After': '3'}) from exc
        feed_tokens = {(str(row.get('chainId')), str(row.get('token') or '').lower())
                       for row in feed.get('trackedAssets', feed.get('assets', []))}

    def visible(frame):
        if trade_scope == ('feed', '') and b'event: projection.delta\n' in frame:
            # The full delta still reaches clients and advances their version.
            # Update membership in memory; no database query per trade/client.
            try:
                data = json.loads(next(line[5:].strip() for line in frame.split(b'\n') if line.startswith(b'data:')))
                for row in (data.get('upserts') or {}).get('assets', []):
                    identity = (str(row.get('chainId')), str(row.get('token') or '').lower())
                    if row.get('kind') == 'candidate':
                        feed_tokens.add(identity)
                for key in (data.get('removes') or {}).get('assets', []):
                    chain, _, token = str(key).partition(':')
                    feed_tokens.discard((chain, token.lower()))
            except (ValueError, StopIteration, TypeError):
                pass
        return frame_visible(frame, protocol=protocol, candle_keys=candle_keys, trade_scope=trade_scope,
                             feed_tokens=feed_tokens)

    async def frames():
        # StreamingResponse may be abandoned before its body is iterated. Only
        # register once the generator starts, so such requests cannot retain a
        # subscriber queue for the lifetime of the API process.
        subscribers = clients()
        subscribers.add(q)
        try:
            hello(q)
            upper = cursor()
            seen = upper if last_id is None else last_id
            if last_id is not None:
                while True:
                    page, seen, done = replay_page(seen, upper)
                    skipped = False
                    for frame in page:
                        if visible(frame):
                            yield frame
                        else:
                            skipped = True
                    if skipped:
                        yield _frame('checkpoint', {'cursor': seen}, seen)
                    if done:
                        break
                    await asyncio.sleep(0)
            if snapshot and protocol != 1:
                latest, _ = replay_batch(None, include_latest=True)
                for frame in latest:
                    yield frame
            last_checkpoint = time.monotonic()
            pending_checkpoint = False
            while True:
                try:
                    queued = await asyncio.wait_for(q.get(), timeout=25)
                    if queued is None:
                        break
                    seq, frame = queued
                    if seq is not None and seq <= seen:
                        continue
                    if seq is not None:
                        seen = seq
                    if not visible(frame):
                        pending_checkpoint = True
                        if time.monotonic() - last_checkpoint >= 5:
                            yield _frame('checkpoint', {'cursor': seen}, seen)
                            pending_checkpoint = False
                            last_checkpoint = time.monotonic()
                        continue
                    yield frame
                    if seq is not None:
                        pending_checkpoint = False
                        last_checkpoint = time.monotonic()
                except asyncio.TimeoutError:
                    # No database work per connected browser. Checkpoints also
                    # advance through intentionally filtered quote/candle ids.
                    if pending_checkpoint:
                        yield _frame('checkpoint', {'cursor': seen}, seen)
                        pending_checkpoint = False
                        last_checkpoint = time.monotonic()
                    yield _frame('heartbeat', {'at': int(time.time()*1000)})
        finally:
            subscribers.discard(q)

    headers = {'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
               'Vary': 'Accept-Encoding'}
    body = frames()
    if accepts_gzip(accept_encoding):
        headers['Content-Encoding'] = 'gzip'
        body = gzip_frames(body)
    return StreamingResponse(body, media_type='text/event-stream', headers=headers)


def parse_trade_scope(value):
    """An omitted scope keeps the existing global trade stream."""
    if value is None or value == 'all':
        return None
    if value == 'feed':
        return ('feed', '')
    chain, separator, token = value.partition(':')
    if not separator or not chain.isdecimal() or not token or len(token) > 200:
        raise HTTPException(status_code=422, detail='trades must be chainId:token, feed or all')
    return chain, token.lower()


def frame_visible(frame, protocol=0, candle_keys=None, trade_scope=None, feed_tokens=None):
    lines = frame.split(b'\n', 3)
    event_line = lines[1] if lines[0].startswith(b'id:') else lines[0]
    event = event_line.partition(b':')[2].strip().decode()
    if protocol == 1 and event in ('price', 'stock-quote'):
        return False
    if trade_scope is not None and event in ('trade', 'trade-remove', 'market.trade'):
        try:
            data_line = next(line for line in lines if line.startswith(b'data:'))
            data = json.loads(data_line.partition(b':')[2])
            if not isinstance(data, dict):
                return False
            identity = (str(data.get('chainId')), str(data.get('token') or '').lower())
            if trade_scope == ('feed', ''):
                dex = data.get('venue') == 'dex' or (not data.get('venue') and data.get('source') == 'OKX trades')
                return dex and data.get('priceScope') != 'exchange' and identity in (feed_tokens or set())
            return identity == trade_scope
        except (ValueError, StopIteration):
            return False  # unknown trade identity cannot be assigned to the watched asset
    if candle_keys is not None and event in ('candle', 'candle.upsert', 'candle.close', 'candle.correct'):
        if 'none' in candle_keys:
            return False
        try:
            data_line = next(line for line in lines if line.startswith(b'data:'))
            data = json.loads(data_line.partition(b':')[2])
            key = ':'.join((str(data.get('chainId') or ''), str(data.get('token') or '').lower(),
                            str(data.get('venue') or '').lower(),
                            str(data.get('poolId') or data.get('marketId') or '').lower(), str(data.get('bar') or '')))
            return key in candle_keys
        except (ValueError, StopIteration):
            return True  # do not silently hide an unknown new event shape
    return True
