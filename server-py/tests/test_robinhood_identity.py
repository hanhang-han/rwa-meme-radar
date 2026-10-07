"""Robinhood issuer authority must unlock only exact, chain-verified markets."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.stock_identity import (ROBINHOOD_MANIFEST, _manifest, assess_pool_relation,
                                chain_manifest_version, token_identity)
from app.collectors.discovery_coverage import official_contracts
from app.collectors.pool_market import PoolMarketCollector, gecko_pairs, overlay_relation
from app.dashboard_projection import discovery_rankings

STOCK = '0x117cc2133c37b721f49de2a7a74833232b3b4c0c'  # issuer SPY deployment
MEME = '0x'+'2'*40
POOL = '0x'+'3'*40
NOW = 1_791_006_000_000


def relation(**extras):
    return {'chainId':'4663', 'stock':STOCK, 'stockSide':STOCK, 'ticker':'SPY',
            'token':MEME, 'pool':POOL, 'token0':STOCK, 'token1':MEME,
            'status':'verified', **extras}


def gecko(liquidity='2000'):
    return {'data':[{'id':'robinhood_'+POOL, 'type':'pool',
        'attributes':{'address':POOL,'reserve_in_usd':liquidity,'volume_usd':{'h24':'100'}},
        'relationships':{'base_token':{'data':{'id':'robinhood_'+MEME}},
                         'quote_token':{'data':{'id':'robinhood_'+STOCK}}}}]}


class RobinhoodIdentityTests(unittest.IsolatedAsyncioTestCase):
    def test_release_pinned_issuer_identity_does_not_trust_symbols_or_other_chains(self):
        trust = token_identity('4663', STOCK, 'WRONG')
        self.assertEqual(trust['ticker'], 'SPY')
        self.assertEqual(trust['underlyingIsin'], 'US78462F1030')
        self.assertEqual(trust['issuer'], 'Robinhood Assets (Jersey) Limited')
        self.assertTrue(trust['eligibleForPair'])
        self.assertTrue(trust['underlyingId'].startswith('robinhood:0x'))
        self.assertEqual(trust['sourceUrl'], 'https://api.robinhood.com/rhj/assets')
        for chain, address in [('56', STOCK), ('196', STOCK), ('4663', MEME)]:
            self.assertFalse(token_identity(chain, address, 'SPY')['eligibleForPair'])

    def test_bounded_discovery_includes_rh_and_preserves_other_network_versions(self):
        self.assertEqual(len(official_contracts('4663')), 194)
        self.assertIn(STOCK, official_contracts('4663'))
        self.assertEqual(chain_manifest_version('4663'), 'robinhood-assets-v1-2026-10-03')
        for chain in ('196', '56'):
            self.assertEqual(chain_manifest_version(chain), 'xstocks-assets-v2-2026-09-25')

    def test_pool_grade_still_requires_exact_sides_and_fresh_real_liquidity(self):
        good = relation(liquidityUsd=2000, liquidityAt=NOW-1000)
        self.assertEqual(assess_pool_relation(good, NOW)['level'], 'A')
        cases = [(relation(), 'liquidity-unknown'),
                 ({**good, 'liquidityAt':NOW-900001}, 'liquidity-stale'),
                 ({**good, 'liquidityUsd':0}, 'below-minimum-liquidity'),
                 ({**good, 'stock':MEME}, 'issuer-deployment-unverified'),
                 ({**good, 'token0':POOL}, 'pool-side-mismatch')]
        for row, reason in cases:
            self.assertEqual(assess_pool_relation(row, NOW)['evidenceStatus'], reason)

    def test_corrupt_duplicate_or_wrong_network_manifest_fails_closed(self):
        original = json.loads(ROBINHOOD_MANIFEST.read_text())
        mutations = []
        for field, value in [('sourceUrl','https://untrusted.example/assets'),('issuer','Unknown')]:
            mutations.append({**original, field:value})
        mutations.append({**original, 'assets':original['assets']+[original['assets'][0]]})
        changed = {**original['assets'][0], 'deployments':{'56':original['assets'][0]['deployments']['4663']}}
        mutations.append({**original, 'assets':[changed]})
        try:
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'manifest.json'
                for body in mutations:
                    path.write_text(json.dumps(body))
                    _manifest.cache_clear()
                    with patch('app.stock_identity.ROBINHOOD_MANIFEST',path):
                        self.assertFalse(token_identity('4663',STOCK,'SPY')['eligibleForPair'])
        finally:
            _manifest.cache_clear()

    async def test_market_catalogue_and_home_rankings_restore_only_qualified_rh_pools(self):
        with tempfile.TemporaryDirectory() as folder:
            clock=[NOW]
            collector=PoolMarketCollector(path=folder+'/pools.json',clock=lambda:clock[0])
            scoped=AsyncMock()
            scoped.all.return_value=[relation(),relation(pool='0x'+'4'*40,stock=MEME)]
            with patch('app.collectors.pool_market.catalogue_store',AsyncMock(return_value=scoped)) as stores:
                metadata=await collector.catalogue()
                self.assertIn('4663', [call.args[0] for call in stores.await_args_list])
                rh={k:v for k,v in metadata.items() if v['chainId']=='4663'}
                self.assertEqual(list(rh), ['4663:'+POOL])
            async def fetch(chain, addresses):
                self.assertEqual((chain,addresses), ('4663',[POOL]))
                return {'pairs':gecko_pairs(chain,gecko())}
            result=await collector.run_once(metadata=rh,fetch=fetch)
            self.assertEqual(result['accepted'],1)
            overlaid=overlay_relation(relation(),collector.body)
            self.assertEqual(overlaid['liquidityProvider'],'GeckoTerminal')
            self.assertEqual(overlaid['poolMarket']['volume24h'],100)
            assessed={**overlaid, **assess_pool_relation(overlaid,NOW)}
            asset={'chainId':'4663','token':MEME,'volume24h':100,'volumeCurrency':'USD',
                   'fieldTimes':{'volume24h':NOW},'fieldScopes':{'volume24h':'token'}}
            cards,_=discovery_rankings([asset],[assessed],[{'chainId':'4663',
                'tokenContractAddress':STOCK,'stockCode':'SPY','price':750}],NOW,'4663')
            self.assertEqual(cards[0]['ticker'],'SPY')
            self.assertEqual(cards[0]['assetCount'],1)
            self.assertEqual(discovery_rankings([asset],[assessed],[],NOW,'56')[0],[])
            # Missing responses retain the observation's real clock across restart.
            clock[0]+=60000
            restarted=PoolMarketCollector(path=folder+'/pools.json',clock=lambda:clock[0])
            await restarted.run_once(metadata=rh,fetch=AsyncMock(return_value={'pairs':[]}))
            self.assertEqual(restarted.body['pools']['4663:'+POOL]['liquidityAt'],NOW)
