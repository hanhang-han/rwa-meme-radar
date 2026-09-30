import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ['NODE_ENV'] = 'test'

from app.collectors.queue import result


class CollectorRepairTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import ResearchStore
        self.temp = tempfile.TemporaryDirectory()
        self.stores = {c: await ResearchStore(self.temp.name + '/research.sqlite', c).connect() for c in ('196', '56', '4663')}
        self.token = '0x' + '1' * 40

    async def asyncTearDown(self):
        for s in self.stores.values():
            await s.close()
        self.temp.cleanup()

    async def store(self, cid):
        return self.stores[cid]

    async def test_unpaired_candidate_has_baseline_and_failed_asset_backs_off(self):
        from app.collectors import live_quotes as q
        s = self.stores['196']
        await s.put('asset', self.token, {'token': self.token, 'kind': 'candidate', 'price': 1})
        bad = '0x' + '2' * 40
        await s.put('asset', bad, {'token': bad, 'kind': 'candidate'})
        await s.put('collector-job', 'quote:' + bad, {'domain': 'quote', 'key': bad, 'nextRetryAt': 2_000_000})
        fetch = AsyncMock(return_value=result(accepted=1))
        with patch.object(q, 'store', self.store), patch.object(q, 'now_ms', return_value=1_900_000), patch.object(q, '_fetch_entries', fetch):
            await q.refresh_base_candidates()
        entries = fetch.call_args.args[0]
        self.assertIn(('196', self.token), entries)
        self.assertNotIn(('196', bad), entries)

    async def test_wrong_chain_or_metadata_only_rows_do_not_mark_quote_success(self):
        from app.collectors import live_quotes as q
        for cid in ('196', '56'):
            await self.stores[cid].put('asset', self.token, {'token': self.token, 'kind': 'candidate'})
        response = [{'chainIndex': '56', 'tokenContractAddress': self.token, 'price': '9'},
                    {'chainIndex': '196', 'tokenContractAddress': self.token, 'tokenSymbol': 'A'}]
        with patch.object(q, 'store', self.store), patch.object(q, 'okx_post', AsyncMock(return_value=response)), patch.object(q, 'broadcast'):
            outcome = await q._fetch_and_merge('196', [self.token], 'test')
        self.assertEqual(outcome['accepted'], 0)
        self.assertEqual(outcome['unsupported'], 1)
        self.assertIsNone((await self.stores['196'].get('collector-job', 'quote:' + self.token))['lastSuccessAt'])
        self.assertIsNone((await self.stores['56'].get('asset', self.token)).get('price'))

    async def test_zero_price_is_missing_without_erasing_valid_zero_activity(self):
        from app.collectors.assets import save_asset
        s = self.stores['196']
        await save_asset(s, {'chainIndex':'196', 'tokenContractAddress':self.token,
                             'tokenSymbol':'TEST', 'price':'0', 'volume24H':'0', 'txs24H':'0'})
        asset = await s.get('asset', self.token)
        self.assertIsNone(asset.get('price'))
        self.assertNotIn('price', asset.get('fieldTimes') or {})
        self.assertEqual(asset['volume24h'], 0)
        self.assertEqual(asset['txs24h'], 0)

        await save_asset(s, {'chainIndex':'196', 'tokenContractAddress':self.token, 'price':'2'})
        await save_asset(s, {'chainIndex':'196', 'tokenContractAddress':self.token, 'price':'0'})
        self.assertEqual((await s.get('asset', self.token))['price'], 2)

    async def test_missing_provider_time_is_received_and_does_not_rejuvenate_other_fields(self):
        from app.collectors.assets import save_asset
        s = self.stores['196']
        await save_asset(s, {'chainIndex': '196', 'tokenContractAddress': self.token, 'price': '2', 'time': 1000})
        await save_asset(s, {'chainIndex': '196', 'tokenContractAddress': self.token, 'holders': '123'})
        asset = await s.get('asset', self.token)
        self.assertEqual(asset['fieldTimes']['price'], 1000)
        self.assertEqual(asset['fieldTimeKinds']['price'], 'market')
        self.assertEqual(asset['fieldTimeKinds']['holders'], 'received')
        self.assertEqual(asset['fieldSources']['price'], 'OKX')
        self.assertIsNone(asset['fieldObservations']['holders']['marketAt'])

    async def test_unknown_verified_pool_counterpart_becomes_candidate(self):
        from app.collectors import main_round as m
        s = self.stores['196']
        stock, pool = '0x' + '2' * 40, '0x' + '3' * 40
        checked = {'token0': self.token, 'token1': stock, 'relation': {
            'token': self.token, 'stockSide': stock, 'wrapped': False, 'wrapper': False,
            'stock': {'tokenContractAddress': stock, 'stockCode': 'NVDA'}}}
        with patch.object(m, 'okx_get', AsyncMock(return_value=[{'poolAddress': pool}])), \
             patch.object(m, 'verify_pool', AsyncMock(return_value=checked)), \
             patch.object(m, 'rpc_call', AsyncMock(return_value=bytes(32))), patch.object(m, 'broadcast'):
            scan = await m.scan_pools(s, stock, [checked['relation']['stock']], '0x1')
        self.assertEqual(scan['status'], 'ready')
        self.assertEqual((await s.get('asset', self.token))['kind'], 'candidate')
        self.assertEqual(len(await s.all('relation')), 1)
        self.assertEqual(len(await s.events(None, 10)), 2)

    def test_bsc_long_extra_data_block_is_accepted_by_chain_client(self):
        from web3 import Web3
        from web3.providers import BaseProvider
        from web3.exceptions import ExtraDataLengthError
        from app.collectors import main_round as m
        class PoAProvider(BaseProvider):
            def make_request(self, method, params):
                return {'jsonrpc': '2.0', 'id': 1, 'result': {
                    'number': '0x123', 'timestamp': '0x456', 'extraData': '0x' + '11' * 97,
                }}
        with self.assertRaises(ExtraDataLengthError):
            Web3(PoAProvider()).eth.get_block('latest')
        old = m._CLIENTS.pop('56', None)
        handle = m.CHAIN.set('56')
        try:
            client = m.chain_web3()
            client.provider = PoAProvider()
            block = client.eth.get_block('latest')
            self.assertEqual(block['number'], 0x123)
            self.assertEqual(block['timestamp'], 0x456)
            self.assertEqual(len(block['proofOfAuthorityData']), 97)
        finally:
            m.CHAIN.reset(handle)
            m._CLIENTS.pop('56', None)
            if old is not None:
                m._CLIENTS['56'] = old

    async def test_bad_pool_batch_does_not_starve_thirteenth_pool(self):
        from app.collectors import main_round as m
        s = self.stores['196']
        pools = ['0x' + format(n, '040x') for n in range(1, 14)]
        for p in pools:
            await s.put('relation', p, {'id': p, 'pool': p, 'token': self.token, 'stock': self.token, 'status': 'verified'})
        accepted = []
        async def quote(store, rel, number, at):
            if rel['pool'] != pools[-1]:
                raise ValueError('unsupported')
            accepted.append(rel['pool'])
        web3 = SimpleNamespace(eth=SimpleNamespace(chain_id=196, get_block=lambda _: {'number': 1, 'timestamp': 1000}))
        with patch.object(m, 'store', self.store), patch.object(m, '_pool_quote', quote), patch.object(m, 'chain_web3', return_value=web3):
            first = await m.refresh_liquidity()
            second = await m.refresh_liquidity()
        self.assertEqual(first['failed'], 12)
        self.assertEqual(second['accepted'], 1)
        self.assertEqual(accepted, [pools[-1]])

    async def test_wrapper_reserves_require_actual_side_price_or_conversion(self):
        from app.collectors import main_round as m
        s = self.stores['196']
        underlying, wrapper, pool = '0x' + '2' * 40, '0x' + '3' * 40, '0x' + '4' * 40
        now = 2_000_000
        rel = {'id': 'r', 'pool': pool, 'token': self.token, 'stock': underlying, 'stockSide': wrapper,
               'token0': self.token, 'token1': wrapper, 'wrapper': True, 'status': 'verified'}
        await s.put('relation', 'r', rel)
        await s.put('asset', underlying, {'price': 100, 'fieldTimes': {'price': now}})
        conversion = [False]
        async def rpc(address, selector, block='latest'):
            if selector == '0x0902f1ac':
                return (100 * 10**18).to_bytes(32, 'big') + (10 * 10**18).to_bytes(32, 'big')
            if selector.startswith('0x07a2d13a') and conversion[0]:
                return (2 * 10**18).to_bytes(32, 'big')
            raise RuntimeError('revert')
        with patch.object(m, 'rpc_call', rpc), patch.object(m, 'cached_decimals', AsyncMock(return_value=18)), patch.object(m, 'now_ms', return_value=now):
            await m._pool_quote(s, rel, 1, now)
            self.assertIsNone((await s.get('relation', 'r')).get('liquidityUsd'))
            conversion[0] = True
            await m._pool_quote(s, rel, 1, now)
        final = await s.get('relation', 'r')
        self.assertEqual(final['liquidityUsd'], 4000)
        self.assertEqual(final['liquidityMethod'], 'erc4626_reserves_valuation')

    def trade_page(self, start, count=100):
        return [{'chainIndex': '196', 'tokenContractAddress': self.token, 'id': str(t), 'time': t,
                 'type': 'buy', 'price': '1', 'volume': '1'} for t in range(start, start-count, -1)]

    async def test_trade_gap_survives_new_heads_and_resumes_old_cursor(self):
        from app.collectors import trades as t
        s = self.stores['196']
        await s.put('asset', self.token, {'token': self.token, 'symbol': 'A', 'kind': 'candidate', 'tradeAt': 100})
        await s.put_trades(self.token, [{'id': '100', 't': 100}])
        responses = [self.trade_page(1000), self.trade_page(900)]
        with patch.object(t, 'store', self.store), patch.object(t, 'now_ms', return_value=1_000_000), \
             patch('app.collectors.queue.now_ms', return_value=1_000_000), \
             patch.object(t, 'okx_get', AsyncMock(side_effect=responses)), patch.object(t, 'broadcast'):
            await t._run_on_demand('196', self.token)
        gaps = (await s.get('trade-gaps', self.token))['intervals']
        self.assertEqual(gaps[0]['from'], 100)
        self.assertEqual(gaps[0]['cursor'], '801')
        responses = [self.trade_page(1010), self.trade_page(800)]
        fetch = AsyncMock(side_effect=responses)
        with patch.object(t, 'store', self.store), patch.object(t, 'now_ms', return_value=1_200_000), \
             patch('app.collectors.queue.now_ms', return_value=1_200_000), \
             patch.object(t, 'okx_get', fetch), patch.object(t, 'broadcast'):
            await t._run_on_demand('196', self.token)
        fetch.assert_not_called()  # Preserve the five-minute paid-source interval.
        with patch.object(t, 'store', self.store), patch.object(t, 'now_ms', return_value=1_300_000), \
             patch('app.collectors.queue.now_ms', return_value=1_300_000), \
             patch.object(t, 'okx_get', fetch), patch.object(t, 'broadcast'):
            await t._run_on_demand('196', self.token)
        self.assertEqual(fetch.call_args_list[1].args[1]['after'], '801')
        gaps = (await s.get('trade-gaps', self.token))['intervals']
        self.assertEqual(gaps[0]['from'], 100)
        self.assertEqual(gaps[0]['cursor'], '701')
        self.assertEqual((await s.get('asset', self.token))['tradeCoverage'], 'partial-gap')

    def test_zero_rows_are_not_green_and_idle_does_not_advance_watermark(self):
        from app.collectors.scheduler import apply_result
        status = {'lastSuccessAt': 10}
        apply_result(status, result(requested=1, unsupported=1), 20)
        self.assertEqual(status['status'], 'error')
        self.assertEqual(status['lastSuccessAt'], 10)
        apply_result(status, result(requested=2, accepted=1, unsupported=1), 25)
        self.assertEqual((status['status'], status['outcome']), ('waiting', 'partial-data'))
        self.assertEqual(status['lastSuccessAt'], 25)
        apply_result(status, result(), 30)
        self.assertEqual(status['status'], 'idle')
        self.assertEqual(status['lastSuccessAt'], 25)

    def test_oracle_skip_reason_distinguishes_backlog_from_missing_stream(self):
        from app.collectors.scheduler import apply_result
        status = {'lastSuccessAt': 10, 'lastDataAt': 11, 'lastFailureAt': 5}
        apply_result(status, result(skipped=3, deferredReason='live-backlog'), 20)
        self.assertEqual((status['status'], status['outcome']), ('deferred', 'live-backlog'))
        self.assertEqual((status['lastSuccessAt'], status['lastDataAt'], status['lastFailureAt']),
                         (10, 11, 5))
        self.assertIsNone(status['error'])

        apply_result(status, result(skipped=3, unavailableReason='stream-missing'), 30)
        self.assertEqual((status['status'], status['outcome']), ('error', 'source-unavailable'))
        self.assertEqual(status['error'], 'BNB Chain live stream unavailable')
        self.assertEqual((status['lastSuccessAt'], status['lastDataAt'], status['lastFailureAt']),
                         (10, 11, 30))

    def test_completed_unsupported_review_is_opt_in(self):
        from app.collectors.scheduler import apply_result
        generic = {'lastSuccessAt': 10, 'lastDataAt': 11}
        apply_result(generic, result(requested=2, processed=2, unsupported=2), 20)
        self.assertEqual((generic['status'], generic['outcome']), ('error', 'no-data'))
        self.assertEqual((generic['lastSuccessAt'], generic['lastDataAt']), (10, 11))

        reviewed = {'lastSuccessAt': 10, 'lastDataAt': 11}
        apply_result(reviewed, result(requested=2, processed=2, unsupported=2, noChange=True), 20)
        self.assertEqual((reviewed['status'], reviewed['outcome']), ('waiting', 'no-change'))
        self.assertEqual((reviewed['lastSuccessAt'], reviewed['lastDataAt']), (20, 11))

    def test_basket_loses_qualification_without_resetting_baseline(self):
        from app.collectors.baskets import project_basket
        now = 2_000_000
        base = {'baseAt': 1, 'version': 1, 'members': [{'token': str(n), 'basePrice': 1, 'baseCap': 1} for n in range(3)]}
        assets = {str(n): {'token': str(n), 'price': 2, 'marketCap': 2, 'fieldTimes': {'price': now}} for n in range(3)}
        retained, view = project_basket('芯片', '196', base, assets, [], now)
        self.assertIs(retained, base)
        self.assertEqual(view['dataStatus'], 'paused')
        self.assertIsNone(view['value'])
        self.assertEqual(view['members'], 3)


if __name__ == '__main__':
    unittest.main()
