"""Cursor-consistent SSE with bounded replay and one shared process tailer."""
import asyncio
import json
import time
import zlib
from functools import lru_cache

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from ..stream_hub import clients, cursor, hello, replay_batch, replay_page, _frame

router = APIRouter()


@lru_cache(maxsize=2)
def _projection_frame(frame):
    """Decode once per published frame, shared by all page subscribers."""
    lines = frame.split(b'\n', 3)
    seq = int(lines[0][3:]) if lines[0].startswith(b'id:') else None
    data = json.loads(next(line[5:].strip() for line in lines if line.startswith(b'data:')))
    return seq, data, {}


def _matches_asset(row, identity):
    return (str(row.get('chainId') or '196') == identity[0] and identity[1] in
            {str(row.get(key) or '').lower() for key in ('token', 'tokenContractAddress', 'stock')})


def scoped_projection_frame(frame, scope=None, identity=None):
    seq, data, encoded = _projection_frame(frame)
    # Old clients retain precisely their original protocol, without paying for
    # the additional durable page payloads stored in the journal record.
    if scope is None:
        if 'views' not in data:
            return frame
        if 'legacy' not in encoded:
            encoded['legacy'] = _frame('projection.delta', {k: v for k, v in data.items() if k != 'views'}, seq)
        return encoded['legacy']
    if scope in ('overview', 'market'):
        if scope not in encoded:
            packet = (data.get('views') or {}).get(scope)
            if packet is None:
                # An old journal segment cannot safely be patched onto a new
                # page DTO. Recover once from the current published snapshot.
                return _frame('reset', {'cursor': seq, 'reason': 'scoped-projection-unavailable'}, seq)
            invalidations = data.get('invalidations') or []
            if scope == 'overview':
                invalidations = [r for r in invalidations if r.get('kind') in ('feed', 'briefing')]
            encoded[scope] = _frame('projection.delta', {**packet, 'invalidations': invalidations}, seq)
        return encoded[scope]
    # Asset subscribers receive complete replacements for their own rows. The
    # detail summary has its own HTTP lifecycle, so it needs no global catalogue
    # or partial-row base to keep its canonical headline price current.
    upserts = {group: [row for row in rows if _matches_asset(row, identity)]
               for group, rows in (data.get('upserts') or {}).items() if group != 'sectors'}
    invalidations = [r for r in data.get('invalidations', [])
                     if (not r.get('chainId') or str(r['chainId']) == identity[0])
                     and (not r.get('token') or str(r['token']).lower() == identity[1])
                     and r.get('kind') in ('detail', 'comparison', 'insight')]
    # Removed relation keys do not encode a token; trigger the bounded summary
    # reconciliation rather than guessing which selected asset they belonged to.
    if (data.get('removes') or {}).get('relations'):
        invalidations.append({'kind': 'detail', 'chainId': identity[0], 'token': identity[1]})
    removed = {group: [key for key in (data.get('removes') or {}).get(group, [])
                       if key == ':'.join(identity)] for group in ('assets', 'stockTokens')}
    removed['relations'] = (data.get('removes') or {}).get('relations', [])
    if not any(upserts.values()) and not any(removed.values()) and not invalidations:
        return None
    from ..dashboard_projection import ASSET_FIELDS, STOCK_FIELDS
    common = {'fieldTimes', 'fieldSources', 'fieldScopes', 'fieldTimeKinds', 'fieldStatus', 'priceProvenance', 'projectionKey'}
    fields = {}
    if upserts.get('assets'):
        fields['assets'] = sorted(ASSET_FIELDS | common | {'dataQuality', 'riskAssessment'})
    if upserts.get('stockTokens'):
        fields['stockTokens'] = sorted(STOCK_FIELDS | common | {'issuerIdentity', 'stockIdentity', 'premium'})
    return _frame('projection.delta', {'schema': 1, 'scope': 'asset', 'asset': ':'.join(identity),
                  'revision': data.get('revision'), 'now': data.get('now'),
                  'upserts': upserts, 'removes': removed, 'canonicalFields': fields,
                  'invalidations': invalidations}, seq)


def scope_frame(frame, protocol=0, scope=None, identity=None):
    lines = frame.split(b'\n', 3)
    event_line = lines[1] if lines[0].startswith(b'id:') else lines[0]
    event = event_line.partition(b':')[2].strip().decode()
    if event == 'projection.delta':
        return scoped_projection_frame(frame, scope if protocol == 2 else None, identity)
    if protocol != 2:
        return frame
    if event in ('price', 'stock-quote'):
        return None
    if event == 'comparison':
        if scope != 'asset':
            return None
        data = json.loads(next(line[5:].strip() for line in lines if line.startswith(b'data:')))
        return frame if _matches_asset(data, identity) else None
    if scope == 'asset' and event in ('relationship', 'discovery'):
        data = json.loads(next(line[5:].strip() for line in lines if line.startswith(b'data:')))
        row = data.get('relation') or data.get('asset') or data
        return frame if _matches_asset({**row, 'chainId': data.get('chainId', row.get('chainId'))}, identity) else None
    return frame


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
                     scope: str | None = None,
                     accept_encoding: str | None = Header(default=None, alias='Accept-Encoding')):
    q: asyncio.Queue = asyncio.Queue(maxsize=1024)
    # Browser reconnect headers take precedence over the original URL cursor.
    try:
        last_id = int(last_event_id if isinstance(last_event_id, str) else after)
    except (TypeError, ValueError):
        last_id = None
    candle_keys = set(candles.split(',')) if isinstance(candles, str) and candles != 'all' else None
    trade_scope = parse_trade_scope(trades)
    if protocol == 2:
        scope = scope or 'overview'
        if scope not in ('overview', 'market', 'asset'):
            raise HTTPException(status_code=422, detail='scope must be overview, market or asset')
        if scope == 'asset' and (trade_scope is None or trade_scope == ('feed', '')):
            raise HTTPException(status_code=422, detail='asset scope requires trades=chainId:token')
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
                _, data, _ = _projection_frame(frame)
                for row in (data.get('upserts') or {}).get('assets', []):
                    identity = (str(row.get('chainId')), str(row.get('token') or '').lower())
                    if row.get('kind') == 'candidate':
                        feed_tokens.add(identity)
                for key in (data.get('removes') or {}).get('assets', []):
                    chain, _, token = str(key).partition(':')
                    feed_tokens.discard((chain, token.lower()))
            except (ValueError, StopIteration, TypeError):
                pass
        if not frame_visible(frame, protocol=protocol, candle_keys=candle_keys, trade_scope=trade_scope,
                             feed_tokens=feed_tokens):
            return None
        return scope_frame(frame, protocol, scope, trade_scope)

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
                        selected = visible(frame)
                        if selected is not None:
                            yield selected
                            if b'event: reset\n' in selected:
                                return
                        else:
                            skipped = True
                    if skipped:
                        yield _frame('checkpoint', {'cursor': seen}, seen)
                    if done:
                        break
                    await asyncio.sleep(0)
            if snapshot and protocol not in (1, 2):
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
                    selected = visible(frame)
                    if selected is None:
                        pending_checkpoint = True
                        if time.monotonic() - last_checkpoint >= 5:
                            yield _frame('checkpoint', {'cursor': seen}, seen)
                            pending_checkpoint = False
                            last_checkpoint = time.monotonic()
                        continue
                    yield selected
                    if b'event: reset\n' in selected:
                        return
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
    if protocol in (1, 2) and event in ('price', 'stock-quote'):
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
