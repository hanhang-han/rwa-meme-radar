"""A busy global tape must not suppress a filtered subscriber's heartbeat."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.api import stream


TOKEN = '0x' + '1' * 40
OTHER = '0x' + '2' * 40


class StreamHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.subscribers = set()
        self.patches = [
            patch.object(stream, 'HEARTBEAT_SECONDS', .1),
            patch.object(stream, 'clients', return_value=self.subscribers),
            patch.object(stream, 'is_postgres_path', return_value=False),
            patch.object(stream, '_journal_path', return_value='not-opened'),
            patch.object(stream, 'cursor', return_value=0),
            patch.object(stream, 'hello', side_effect=lambda queue: queue.put_nowait(
                (None, stream._frame('hello', {'cursor': 0})))),
            patch.object(stream, 'replay_page', side_effect=AssertionError('unexpected database replay')),
            patch.object(stream, 'async_cursor', AsyncMock(side_effect=AssertionError('unexpected database cursor'))),
            patch.object(stream, 'async_replay_page', AsyncMock(side_effect=AssertionError('unexpected database replay'))),
        ]
        for mocked in self.patches:
            mocked.start()
        self.bodies = []
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for body in self.bodies:
            await body.aclose()
        for mocked in reversed(self.patches):
            mocked.stop()
        self.assertFalse(self.subscribers)

    async def subscribe(self, scope='memes'):
        response = await stream.get_stream(last_event_id=None, snapshot=False,
            protocol=2, scope=scope, tokens=f'196:{TOKEN}' if scope == 'quotes' else None,
            trades='none', candles='none', accept_encoding=None)
        body = response.body_iterator
        self.bodies.append(body)
        self.assertIn(b'event: hello\n', await anext(body))
        return body, next(iter(self.subscribers))

    def busy_foreign_tape(self, queue):
        async def publish():
            seq = 0
            while True:
                seq += 1
                queue.put_nowait((seq, stream._frame('price',
                    {'chainId': '196', 'token': OTHER, 'price': seq}, seq)))
                await asyncio.sleep(.004)
        task = asyncio.create_task(publish())
        self.tasks.append(task)
        return task

    async def test_continuous_foreign_traffic_does_not_delay_meme_heartbeat(self):
        body, queue = await self.subscribe()
        producer = self.busy_foreign_tape(queue)
        for _ in range(2):
            started = time.monotonic()
            frame = await asyncio.wait_for(anext(body), .3)
            self.assertIn(b'event: heartbeat\n', frame)
            self.assertNotIn(b'checkpoint', frame)
            self.assertLess(time.monotonic() - started, .25)
            self.assertFalse(producer.done())

    async def test_visible_delta_restarts_heartbeat_deadline(self):
        body, queue = await self.subscribe()
        await asyncio.sleep(.07)
        queue.put_nowait((1, stream._frame('projection.delta', {'revision': 2, 'now': 123}, 1)))
        self.assertIn(b'event: directory.invalidate\n', await anext(body))
        waiting = asyncio.create_task(anext(body))
        self.tasks.append(waiting)
        # The old hello deadline has expired, but this visible delta starts a
        # new full interval. The connection must not emit an early heartbeat.
        await asyncio.sleep(.05)
        self.assertFalse(waiting.done())
        self.assertIn(b'event: heartbeat\n', await asyncio.wait_for(waiting, .2))

    async def test_checkpoint_restarts_deadline_and_preserves_latest_seen_cursor(self):
        body, queue = await self.subscribe('quotes')
        self.busy_foreign_tape(queue)
        checkpoint = await asyncio.wait_for(anext(body), .3)
        self.assertIn(b'event: checkpoint\n', checkpoint)
        # Timeout sends the existing checkpoint + heartbeat pair; neither
        # performs a journal lookup. Subsequent cadence starts after output.
        self.assertIn(b'event: heartbeat\n', await anext(body))
        waiting = asyncio.create_task(anext(body))
        self.tasks.append(waiting)
        await asyncio.sleep(.05)
        self.assertFalse(waiting.done())
        newer = await asyncio.wait_for(waiting, .2)
        self.assertIn(b'event: checkpoint\n', newer)
        old_cursor = int(checkpoint.split(b'id: ', 1)[1].split(b'\n', 1)[0])
        new_cursor = int(newer.split(b'id: ', 1)[1].split(b'\n', 1)[0])
        self.assertGreater(new_cursor, old_cursor)

    async def test_duplicate_cursor_traffic_does_not_delay_heartbeat(self):
        body, queue = await self.subscribe()
        async def duplicates():
            while True:
                queue.put_nowait((0, stream._frame('price', {'token': OTHER}, 0)))
                await asyncio.sleep(.004)
        producer = asyncio.create_task(duplicates())
        self.tasks.append(producer)
        self.assertIn(b'event: heartbeat\n', await asyncio.wait_for(anext(body), .3))
        self.assertFalse(producer.done())

    async def test_disconnect_sentinel_cleans_up_subscriber(self):
        body, queue = await self.subscribe()
        queue.put_nowait(None)
        with self.assertRaises(StopAsyncIteration):
            await anext(body)
        self.assertFalse(self.subscribers)

    async def test_cancellation_while_filtering_cleans_up_subscriber(self):
        body, queue = await self.subscribe()
        self.busy_foreign_tape(queue)
        waiting = asyncio.create_task(anext(body))
        self.tasks.append(waiting)
        await asyncio.sleep(.025)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertFalse(self.subscribers)


if __name__ == '__main__':
    unittest.main()
