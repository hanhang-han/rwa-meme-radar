import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from app import stream_hub as hub
from app.api.stream import frame_visible


class ReplayCpuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await hub.stop_hub()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, STREAM_LEDGER_PATH=self.tmp.name + '/events.sqlite')
        self.env.start()
        self.journal = hub._ledger()

    async def asyncTearDown(self):
        await hub.stop_hub()
        self.env.stop()
        self.tmp.cleanup()

    async def test_compact_journal_replay_does_not_decode_and_reencode_json(self):
        body = '{"chainId":"56","token":"0xabc","venue":"binance-alpha","marketId":"ALPHA_1USDT","bar":"5m","name":"测试\\n行情","price":1.2300}'
        self.journal.execute('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
                             ('candle', body, int(time.time() * 1000)))
        self.journal.commit()
        with patch.object(hub.json, 'loads', side_effect=AssertionError('Already serialized')), \
             patch.object(hub.json, 'dumps', side_effect=AssertionError('Already serialized')):
            frames, reached, done = hub.replay_page(0)
        self.assertEqual(frames, [('id: 1\nevent: candle\ndata: ' + body + '\n\n').encode()])
        self.assertEqual((reached, done), (1, True))
        self.assertTrue(frame_visible(frames[0], protocol=1, candle_keys={'56:0xabc:binance-alpha:alpha_1usdt:5m'}))
        self.assertFalse(frame_visible(frames[0], protocol=1, candle_keys={'196:0xabc:dex:pool:5m'}))

    async def test_pretty_legacy_body_keeps_scoped_filter_and_semantics(self):
        data = {'chainId': '196', 'token': '0xabc', 'venue': 'dex', 'poolId': 'pool', 'bar': '1m', 'label': '换行\n文本'}
        frame = hub._serialized_frame('candle', json.dumps(data, indent=2), 42)
        self.assertEqual(frame.count(b'\n'), 4)
        decoded = json.loads(frame.split(b'data: ', 1)[1])
        self.assertEqual(decoded, data)
        self.assertTrue(frame_visible(frame, protocol=1, candle_keys={'196:0xabc:dex:pool:1m'}))
        self.assertFalse(frame_visible(frame, protocol=1, candle_keys={'196:0xabc:dex:other:1m'}))

    async def test_one_event_replay_does_not_scan_ten_thousand_retained_rows(self):
        self.journal.executemany('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
                                [('trade', '{"id":1}', 1)] * 10000)
        self.journal.commit()
        progress = 0

        def budget():
            nonlocal progress
            progress += 1
            return progress > 20  # Abort a full historical scan, not a key seek.

        self.journal.set_progress_handler(budget, 100)
        try:
            frames, reached, done = hub.replay_page(9999, limit=1)
        finally:
            self.journal.set_progress_handler(None, 0)
        self.assertEqual(len(frames), 1)
        self.assertIn(b'id: 10000\n', frames[0])
        self.assertEqual((reached, done), (10000, True))

    async def test_expired_cursor_and_fixed_replay_upper_bound_still_hold(self):
        self.journal.executemany('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
                                [('trade', '{"id":1}', 1)] * 10)
        self.journal.execute('DELETE FROM realtime_events WHERE id<=5')
        self.journal.commit()
        frames, reached, done = hub.replay_page(4, upper=8)
        self.assertIn(b'event: reset\n', frames[0])
        self.assertEqual((reached, done), (8, True))
        frames, reached, done = hub.replay_page(5, upper=8)
        self.assertEqual([int(frame.split(b'\n', 1)[0][4:]) for frame in frames], [6, 7, 8])
        self.assertEqual((reached, done), (8, True))
