"""SSE compression must deliver a complete event before the stream closes."""
import asyncio
import unittest
import zlib
from unittest.mock import patch

from app.api import stream


FRAMES = (
    b'id: 101\nevent: candle\ndata: {"c":1.25}\n\n',
    b'id: 102\nevent: projection.delta\ndata: {"name":"' + '测试'.encode() * 1000 + b'"}\n\n',
    b'id: 103\nevent: checkpoint\ndata: {"cursor":103}\n\n',
)


class StreamCompressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_frame_is_decodable_before_stream_finishes(self):
        closed = False

        async def source():
            nonlocal closed
            try:
                yield FRAMES[0]
                yield FRAMES[1]
                await asyncio.Event().wait()
            finally:
                closed = True

        compressed = stream.gzip_frames(source())
        decoder = zlib.decompressobj(wbits=31)
        try:
            self.assertEqual(decoder.decompress(await anext(compressed)), FRAMES[0])
            self.assertFalse(closed)
            self.assertFalse(decoder.eof)
            self.assertEqual(decoder.decompress(await anext(compressed)), FRAMES[1])
            self.assertFalse(closed)
            self.assertFalse(decoder.eof)
        finally:
            await compressed.aclose()
        self.assertTrue(closed)

    async def test_all_frames_and_gzip_trailer_are_preserved(self):
        async def source():
            for frame in FRAMES:
                yield frame

        decoder = zlib.decompressobj(wbits=31)
        restored = b''.join([decoder.decompress(chunk) async for chunk in stream.gzip_frames(source())])
        self.assertEqual(restored, b''.join(FRAMES))
        self.assertTrue(decoder.eof)
        self.assertEqual(decoder.unused_data, b'')

    async def test_headers_select_compression_without_changing_frame(self):
        cases = [(None, False), ('br', False), ('gzip;q=0', False),
                 ('gzip;q=0, br;q=1', False), ('*', False),
                 ('gzip;q=invalid', False), ('gzip;q=nan', False),
                 ('gzip;q=2', False), ('br, gzip;q=0.5', True),
                 ('GZIP; q=1.0', True), ('gzip, deflate, br', True)]
        for header, expected in cases:
            with self.subTest(header=header):
                subscribers = set()
                with patch.object(stream, 'clients', return_value=subscribers), \
                     patch.object(stream, 'cursor', return_value=0), \
                     patch.object(stream, 'hello', side_effect=lambda queue: queue.put_nowait((None, FRAMES[0]))):
                    response = await stream.get_stream(last_event_id=None, snapshot=False, accept_encoding=header)
                    self.assertEqual(response.headers.get('content-encoding'), 'gzip' if expected else None)
                    self.assertEqual(response.headers['vary'], 'Accept-Encoding')
                    self.assertEqual(response.headers['x-accel-buffering'], 'no')
                    self.assertFalse(subscribers)
                    try:
                        chunk = await anext(response.body_iterator)
                        frame = zlib.decompressobj(wbits=31).decompress(chunk) if expected else chunk
                        self.assertEqual(frame, FRAMES[0])
                        self.assertEqual(len(subscribers), 1)
                    finally:
                        await response.body_iterator.aclose()
                    self.assertFalse(subscribers)

    async def test_unstarted_response_does_not_retain_subscriber(self):
        subscribers = set()
        with patch.object(stream, 'clients', return_value=subscribers), \
             patch.object(stream, 'cursor', return_value=0), \
             patch.object(stream, 'hello', side_effect=lambda queue: queue.put_nowait((None, FRAMES[0]))):
            response = await stream.get_stream(last_event_id=None, snapshot=False)
            self.assertFalse(subscribers)
            await response.body_iterator.aclose()
            self.assertFalse(subscribers)

    async def test_cancellation_removes_underlying_subscriber(self):
        subscribers = set()
        with patch.object(stream, 'clients', return_value=subscribers), \
             patch.object(stream, 'cursor', return_value=0), \
             patch.object(stream, 'hello', side_effect=lambda queue: queue.put_nowait((None, FRAMES[0]))):
            response = await stream.get_stream(last_event_id=None, snapshot=False, accept_encoding='gzip')
            await anext(response.body_iterator)
            waiting = asyncio.create_task(anext(response.body_iterator))
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertFalse(subscribers)
            await response.body_iterator.aclose()

    async def test_existing_gzip_middleware_does_not_compress_twice(self):
        import httpx
        from fastapi import FastAPI
        from fastapi.middleware.gzip import GZipMiddleware
        from fastapi.responses import StreamingResponse

        app = FastAPI()
        app.add_middleware(GZipMiddleware, minimum_size=1)

        @app.get('/')
        async def finite_stream():
            async def source():
                for frame in FRAMES:
                    yield frame
            return StreamingResponse(stream.gzip_frames(source()), media_type='text/event-stream',
                                     headers={'Content-Encoding': 'gzip', 'Vary': 'Accept-Encoding'})

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(response.headers['content-encoding'], 'gzip')
        self.assertEqual(response.content, b''.join(FRAMES))
