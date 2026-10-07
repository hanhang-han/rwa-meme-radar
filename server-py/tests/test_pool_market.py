import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.collectors.pool_market import PoolMarketCollector, gecko_pairs, overlay_relation
from app.state import pool_coverage_by_ticker

STOCK = '0x000fe5820d42183fa294454e1c753f4066fcb3b2'
MEME = '0x'+'2'*40
POOL = '0x'+'3'*40


def relation(pool=POOL, chain='56'):
    return {'pool': pool, 'chainId': chain, 'stock': STOCK, 'stockSide': STOCK,
        'token': MEME, 'token0': STOCK, 'token1': MEME, 'status': 'verified', 'ticker': 'EIX'}


def response(pool=POOL, liquidity=1500, volume=30):
    return {'chainId': 'bsc', 'pairAddress': pool, 'baseToken': {'address': STOCK},
        'quoteToken': {'address': MEME}, 'liquidity': {'usd': liquidity}, 'volume': {'h24': volume}}


class DirectPoolTests(unittest.IsolatedAsyncioTestCase):
    def test_gecko_adapter_preserves_null_and_zero_and_rejects_wrong_network(self):
        def row(pool, value):
            return {'id':'x-layer_'+pool,'type':'pool','attributes':{'address':pool,
                'reserve_in_usd':value,'volume_usd':{'h24':None}},
                'relationships':{'base_token':{'data':{'id':'x-layer_'+STOCK}},
                                 'quote_token':{'data':{'id':'x-layer_'+MEME}}}}
        good=row(POOL,'0');missing=row('0x'+'4'*40,None);bad=row('0x'+'5'*40,'5000')
        bad['relationships']['base_token']['data']['id']='bsc_'+STOCK
        result=gecko_pairs('196',{'data':[good,missing,bad]})
        self.assertEqual(len(result),2)
        self.assertEqual(result[0]['liquidity']['usd'],0)
        self.assertIsNone(result[0]['volume']['h24'])
        self.assertIsNone(result[1]['liquidity']['usd'])

    async def test_pool_sides_zero_and_failed_retries_preserve_observation_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1_000_000]
            collector = PoolMarketCollector(path=directory+'/pools.json', clock=lambda: clock[0])
            metadata = {'56:'+POOL: relation()}
            result = await collector.run_once(metadata=metadata, fetch=AsyncMock(return_value={'pairs': [response(liquidity=0, volume=0)]}))
            self.assertEqual(result['accepted'], 1)
            overlay = overlay_relation(relation(), collector.body)
            self.assertEqual(overlay['liquidityUsd'], 0)
            self.assertEqual(overlay['poolMarket']['volume24h'], 0)
            clock[0] += 60_000
            restarted = PoolMarketCollector(path=directory+'/pools.json', clock=lambda: clock[0])
            await restarted.run_once(metadata=metadata, fetch=AsyncMock(return_value={'pairs': []}))
            self.assertEqual(restarted.body['pools']['56:'+POOL]['liquidityAt'], 1_000_000)
            self.assertEqual(restarted.body['attempts']['56:'+POOL]['reason'], 'pool-not-indexed-or-incomplete')
            # Wrong chain, identity revocation and wrong sides cannot adopt an observation.
            for invalid in [relation(chain='196'), {**relation(), 'stockSide': MEME},
                            {**relation(), 'verificationStatus': 'reorged'}, {**relation(), 'token1': POOL}]:
                self.assertIs(overlay_relation(invalid, restarted.body), invalid)
            newer = {**relation(), 'liquidityUsd': 5000, 'liquidityAt': clock[0]}
            self.assertEqual(overlay_relation(newer, restarted.body)['liquidityUsd'], 5000)

    async def test_wrong_response_sides_do_not_create_pool_market(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = PoolMarketCollector(path=directory+'/pools.json', clock=lambda: 1_000_000)
            bad = response(); bad['quoteToken']['address'] = POOL
            result = await collector.run_once(metadata={'56:'+POOL: relation()}, fetch=AsyncMock(return_value={'pairs': [bad]}))
            self.assertEqual(result['accepted'], 0)
            self.assertFalse(collector.body['pools'])

    async def test_bound_and_cold_slots_prevent_hot_pool_starvation(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = PoolMarketCollector(path=directory+'/pools.json', clock=lambda: 1_000_000)
            metadata = {}
            for i in range(300):
                row = relation(pool='0x'+f'{i+1:040x}')
                if i < 240:
                    row['level'] = 'A'
                metadata['56:'+row['pool']] = row
            fetch = AsyncMock(return_value={'pairs': []})
            result = await collector.run_once(metadata=metadata, fetch=fetch)
            self.assertEqual((result['batches'], result['requested']), (8, 240))
            queried = {address for call in fetch.await_args_list for address in call.args[1]}
            self.assertEqual(sum(key.split(':',1)[1] in queried for key,row in metadata.items() if not row.get('level')), 60)

    def test_recorded_coverage_deduplicates_and_keeps_missing_reasons(self):
        rows = [{**relation(), 'evidenceStatus': 'liquidity-unknown'},
                {**relation(), 'evidenceStatus': 'liquidity-unknown'},
                {**relation(pool='0x'+'4'*40), 'evidenceStatus': 'liquidity-stale'},
                {**relation(pool='0x'+'5'*40), 'evidenceStatus': 'issuer-deployment-unverified'},
                {**relation(pool='0x'+'6'*40), 'evidenceStatus': 'qualified', 'level': 'A'}]
        coverage = pool_coverage_by_ticker(rows)['EIX']
        self.assertEqual(coverage['recorded'], 4)
        self.assertEqual(sum(coverage[k] for k in ('qualified','unknown','stale','identityPending','belowMinimum','other')), 4)
