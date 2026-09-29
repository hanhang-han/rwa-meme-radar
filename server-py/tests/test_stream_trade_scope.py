"""A detail subscriber receives only its asset's trades without losing replay progress."""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import stream_hub as hub
from app.api.stream import frame_visible, get_stream, parse_trade_scope


class TradeScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await hub.stop_hub()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, STREAM_LEDGER_PATH=self.tmp.name + '/events.sqlite')
        self.env.start()

    async def asyncTearDown(self):
        await hub.stop_hub()
        self.env.stop()
        self.tmp.cleanup()

    async def test_exact_identity_filters_trade_and_reorg_but_not_other_events(self):
        scope = parse_trade_scope('196:0xAbC')
        self.assertEqual(scope, ('196', '0xabc'))
        cases = [
            ('trade', {'chainId': '196', 'token': '0xABC'}, True),
            ('trade-remove', {'chainId': '196', 'token': '0xabc', 'ids': ['one']}, True),
            ('trade', {'chainId': '56', 'token': '0xabc'}, False),
            ('trade', {'chainId': '196', 'token': '0xdef'}, False),
            ('trade-remove', {'chainId': '56', 'token': '0xabc', 'ids': ['one']}, False),
            ('trade', {'id': 'unknown'}, False),
            ('projection.delta', {'revision': 2}, True),
            ('discovery', {'id': 'new'}, True),
            ('comparison', {'id': 'compare'}, True),
            ('candle', {'chainId': '56', 'token': '0xdef'}, True),
        ]
        for event, body, expected in cases:
            with self.subTest(event=event, body=body):
                frame = hub._frame(event, body)
                self.assertEqual(frame_visible(frame, trade_scope=scope), expected)
                self.assertTrue(frame_visible(frame), 'global stream stays backward compatible')
        for bad in ('196', '196:', ':0xabc', 'x:0xabc'):
            with self.assertRaises(HTTPException):
                parse_trade_scope(bad)

    async def test_filtered_replay_preserves_global_cursor_with_checkpoint(self):
        events = [
            ('trade', {'chainId': '56', 'token': '0xabc', 'id': 'other'}),
            ('trade-remove', {'chainId': '196', 'token': '0xABC', 'ids': ['old']}),
            ('projection.delta', {'revision': 3}),
            ('trade-remove', {'chainId': '56', 'token': '0xabc', 'ids': ['other']}),
            ('trade', {'chainId': '196', 'token': '0xabc', 'id': 'selected'}),
        ]
        ledger = hub._ledger()
        ledger.executemany('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
                           [(event, json.dumps(body), int(time.time() * 1000)) for event, body in events])
        ledger.commit()
        response = await get_stream(last_event_id='0', snapshot=False, protocol=1, trades='196:0xabc')
        frames = [await anext(response.body_iterator) for _ in range(4)]
        await response.body_iterator.aclose()
        self.assertEqual([frame.split(b'\n', 2)[1] for frame in frames],
                         [b'event: trade-remove', b'event: projection.delta', b'event: trade', b'event: checkpoint'])
        self.assertIn(b'id: 5\n', frames[-1])
        self.assertIn(b'"cursor":5', frames[-1])
        self.assertFalse(hub.clients(), 'closed scoped subscribers must be removed')
