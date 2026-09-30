"""Page delivery preserves published values and cursors without global packets."""
import copy
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from app import stream_hub as hub
from app.api.stream import get_stream, scope_frame
from app.realtime_projection import field_operations, view_delta, projection_delta


def apply_ops(value, operations):
    value = copy.deepcopy(value)
    for operation in operations:
        path = operation[0]
        if not path:
            value = copy.deepcopy(operation[1])
            continue
        parent = value
        for key in path[:-1]:
            parent = parent[key]
        if len(operation) == 1:
            del parent[path[-1]]
        else:
            parent[path[-1]] = copy.deepcopy(operation[1])
    return value


def apply_view(before, packet):
    current = copy.deepcopy(before['unified'])
    groups = ('assets', 'stockTokens', 'relations', 'sectors')
    for group in groups:
        index = {row['projectionKey']: row for row in current.get(group, [])}
        for key in packet['removes'].get(group, []):
            del index[key]
        for row in packet['upserts'].get(group, []):
            index[row['projectionKey']] = row
        for key, ops in packet['patches'].get(group, []):
            index[key] = apply_ops(index[key], ops)
        current[group] = [index[key] for key in packet['orders'].get(group, list(index))]
    meta = apply_ops({k: v for k, v in current.items() if k not in groups}, packet['metaOps'])
    return {**{k: current[k] for k in groups}, **meta}


def page(scope='overview', count=2):
    return {'now': 1000, 'unified': {'snapshotScope': scope,
        'assets': [{'projectionKey': f'196:0x{i}', 'chainId': '196', 'token': f'0x{i}',
                    'price': 1, 'fieldTimes': {'price': 900}, 'risk': {'status': 'unknown', 'evidence': 'stable' * 500}}
                   for i in range(count)],
        'stockTokens': [], 'relations': [], 'sectors': [],
        'metrics': {'assets': 1300, 'newAssets24h': 20},
        'hotStocks': [{'ticker': 'NVDA', 'assetCount': 100}, {'ticker': 'TSLA', 'assetCount': 80}],
        'themeMap': {'totalAssets': 1300, 'bubbles': [{'token': '0x1', 'value': 5}]},
        'quality': {'missing': 10}, 'sources': [{'provider': 'OKX', 'status': 'ready'}]}}


class PageDeltaTests(unittest.TestCase):
    def test_null_zero_deletions_array_order_and_new_keys_are_lossless(self):
        before = {'price': 3, 'removed': 'old', 'empty': [], 'rows': [1, 2], 'nested': {'known': 5}}
        after = {'price': None, 'empty': [0], 'rows': [2, 1], 'nested': {'known': 0}, 'new': False}
        self.assertEqual(apply_ops(before, field_operations(before, after)), after)

    def test_keyed_changes_preserve_new_discovery_rank_order_and_global_totals(self):
        before = page()
        after = copy.deepcopy(before)
        after['now'] += 2000
        rows = after['unified']['assets']
        rows[0]['price'] = None
        rows[0]['fieldTimes']['price'] = 2000
        rows.pop()
        rows.insert(0, {'projectionKey': '196:new', 'token': 'new', 'chainId': '196', 'price': 0})
        after['unified']['metrics'] = {'assets': 1301, 'newAssets24h': 21}
        after['unified']['hotStocks'].reverse()
        after['unified']['hotStocks'][0]['assetCount'] = 101
        after['unified']['themeMap']['totalAssets'] += 1
        after['unified']['quality']['missing'] = 11
        after['unified']['sources'][0]['status'] = 'delayed'
        packet = view_delta(before, after, 'overview', 9, 10)
        self.assertEqual(apply_view(before, packet), after['unified'])
        self.assertEqual(packet['baseRevision'], 9)
        self.assertLess(len(json.dumps(packet)), 5000)

    def test_large_catalogue_price_edits_exclude_unchanged_evidence(self):
        before = page('market', 1300)
        after = copy.deepcopy(before)
        for row in after['unified']['assets']:
            row['price'] = 2
            row['fieldTimes']['price'] = 1000
        old = projection_delta(before, after)
        packet = view_delta(before, after, 'market', 1, 2)
        self.assertGreater(len(json.dumps(old)), 4_000_000)
        self.assertLess(len(json.dumps(packet)), 150_000)
        self.assertEqual(apply_view(before, packet), after['unified'])


class PageStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # A preceding isolated-loop collector test may leave its deferred
        # legacy publisher cancelled at loop teardown. It has no live journal
        # work belonging to this test's private ledger.
        if hub._publisher is not None and hub._publisher.cancelled():
            hub._publisher = None
            hub._pending.clear()
        await hub.stop_hub()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, STREAM_LEDGER_PATH=self.tmp.name + '/events.sqlite')
        self.env.start()

    async def asyncTearDown(self):
        await hub.stop_hub()
        self.env.stop()
        self.tmp.cleanup()

    async def test_scoped_replay_filters_large_foreign_data_and_checkpoints_every_id(self):
        before, after = page(), page()
        after['unified']['assets'][0]['price'] = 2
        delta = projection_delta(before, after)
        delta.update(revision=2, views={'overview': view_delta(before, after, 'overview', 1, 2)})
        delta['upserts']['assets'].append({'token': 'other', 'evidence': 'unused' * 200000})
        events = [('comparison', {'token': 'other', 'pairs': []}), ('projection.delta', delta),
                  ('trade', {'chainId': '196', 'token': '0x0', 'venue': 'dex', 'id': 'real'}),
                  ('comparison', {'token': 'other', 'pairs': []})]
        ledger = hub._ledger()
        ledger.executemany('INSERT INTO realtime_events(event,body,at) VALUES (?,?,?)',
                           [(event, json.dumps(body), int(time.time()*1000)) for event, body in events])
        ledger.commit()
        with patch('app.realtime_projection.read_projection_json', AsyncMock(return_value=json.dumps({'trackedAssets': [{'chainId': '196', 'token': '0x0'}]}))):
            response = await get_stream(last_event_id='0', snapshot=False, protocol=2, scope='overview', trades='feed', candles='')
            frames = [await anext(response.body_iterator) for _ in range(3)]
            await response.body_iterator.aclose()
        wire = b''.join(frames)
        self.assertLess(len(wire), 1500)
        self.assertNotIn(b'comparison', wire)
        self.assertNotIn(b'unused', wire)
        self.assertIn(b'"price"', wire)
        self.assertEqual(json.loads(frames[1].split(b'data: ')[1])['id'], 'real')
        self.assertIn(b'"cursor":4', wire)
        self.assertEqual(json.loads(frames[0].split(b'data: ')[1])['baseRevision'], 1)
        self.assertFalse(hub.clients())

    async def test_old_journal_segment_requests_snapshot_instead_of_applying_wrong_page(self):
        frame = hub._frame('projection.delta', {'schema': 1, 'revision': 3, 'meta': {'assets': 1300}}, 7)
        self.assertIn(b'event: reset', scope_frame(frame, 2, 'overview'))
        self.assertIs(scope_frame(frame, 1), frame)

    async def test_legacy_clients_never_receive_internal_page_envelopes(self):
        delta = {'schema': 1, 'revision': 3, 'upserts': {'assets': [{'price': 2}]},
                 'views': {'overview': {'schema': 2, 'metaOps': []}}}
        frame = hub._frame('projection.delta', delta, 5)
        legacy = scope_frame(frame, 1)
        self.assertEqual(json.loads(legacy.split(b'data: ')[1]), {key: value for key, value in delta.items() if key != 'views'})
        self.assertIn(b'id: 5', legacy)

    async def test_asset_scope_keeps_only_its_canonical_rows_comparison_and_invalidations(self):
        delta = {'schema': 1, 'revision': 3, 'upserts': {
            'assets': [{'chainId': '196', 'token': '0xabc', 'price': 2}, {'chainId': '56', 'token': '0xabc', 'price': 500}],
            'relations': [{'chainId': '196', 'token': '0xabc', 'stock': '0xstock', 'id': 'yes'}, {'chainId': '56', 'token': '0xabc', 'id': 'no'}]},
            'invalidations': [{'kind': 'detail', 'chainId': '56', 'token': '0xabc'}, {'kind': 'detail', 'chainId': '196', 'token': '0xabc'}]}
        identity = ('196', '0xabc')
        packet = json.loads(scope_frame(hub._frame('projection.delta', delta, 5), 2, 'asset', identity).split(b'data: ')[1])
        self.assertEqual(len(packet['upserts']['assets']), 1)
        self.assertEqual(packet['upserts']['relations'][0]['id'], 'yes')
        self.assertEqual(len(packet['invalidations']), 1)
        self.assertIn('price', packet['canonicalFields']['assets'])
        self.assertIn('fieldTimes', packet['canonicalFields']['assets'])
        self.assertIn('riskAssessment', packet['canonicalFields']['assets'])
        self.assertNotIn('poolMarkets', packet['canonicalFields']['assets'])
        self.assertIsNone(scope_frame(hub._frame('comparison', {'chainId': '56', 'token': '0xabc'}), 2, 'asset', identity))
        self.assertIsNotNone(scope_frame(hub._frame('comparison', {'chainId': '196', 'token': '0xabc'}), 2, 'asset', identity))
