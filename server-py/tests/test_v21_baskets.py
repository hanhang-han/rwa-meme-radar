import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ['NODE_ENV'] = 'test'

from app.collectors.baskets import METHOD_VERSION, MAX_WEIGHT, project_basket, refresh_baskets

NOW = 1_790_341_200_000
STOCK = '0xc845b2894dbddd03858fd2d643b4ef725fe0849d'
WRAPPER = '0xa8ddb5cd96b5222afe198316e9a57caa642850d5'


def token(n):
    return '0x' + format(n, '040x')


def asset(n, chain='196', cap=100, symbol=None):
    return {'chainId': chain, 'token': token(n), 'kind': 'candidate',
            'symbol': symbol or f'MEME{n}', 'price': 1, 'marketCap': cap,
            'fieldTimes': {'price': NOW, 'marketCap': NOW}}


def relation(n, chain='196', liquidity=2000, stock=STOCK, side=WRAPPER):
    return {'id': f'{chain}:{n}', 'chainId': chain, 'token': token(n),
            'stock': stock, 'stockSide': side, 'pool': token(1000 + n),
            'token0': token(n), 'token1': side,
            'ticker': 'NVDA', 'status': 'verified', 'checkedAt': NOW,
            'liquidityUsd': liquidity, 'liquidityAt': NOW}


class BasketQualificationTest(unittest.TestCase):
    def test_current_official_meme_basket_caps_dominant_weight(self):
        assets = {token(i): asset(i, cap=1_000_000 if i == 1 else 100) for i in (1, 2, 3)}
        bnb = '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'
        assets[bnb] = {'chainId': '196', 'token': bnb, 'kind': 'candidate',
                       'symbol': 'WBNB', 'price': 1, 'marketCap': 1_000_000_000,
                       'fieldTimes': {'price': NOW, 'marketCap': NOW}}
        relations = [relation(i) for i in (1, 2, 3)] + [
            {**relation(4), 'token': bnb, 'token0': bnb}]
        base, view = project_basket('芯片', '196', None, assets, relations, NOW)
        self.assertEqual(view['dataStatus'], 'current')
        self.assertAlmostEqual(view['value'], 100)
        self.assertEqual(base['methodVersion'], METHOD_VERSION)
        self.assertEqual({m['token'] for m in base['members']}, {token(1), token(2), token(3)})
        self.assertAlmostEqual(sum(m['weight'] for m in base['members']), 1)
        self.assertTrue(all(m['weight'] <= MAX_WEIGHT + 1e-9 for m in base['members']))
        assets[token(1)]['price'] = 2
        retained, changed = project_basket('芯片', '196', base, assets, relations, NOW)
        self.assertIs(retained, base)
        self.assertAlmostEqual(changed['value'], 135)

    def test_stale_or_unlisted_pool_pauses_without_rewriting_base(self):
        assets = {token(i): asset(i) for i in (1, 2, 3)}
        relations = [relation(i) for i in (1, 2, 3)]
        base, _ = project_basket('芯片', '196', None, assets, relations, NOW)
        stale = [{**r, 'liquidityAt': NOW - 900_001} for r in relations]
        retained, paused = project_basket('芯片', '196', base, assets, stale, NOW)
        self.assertIs(retained, base)
        self.assertIsNone(paused['value'])
        self.assertEqual(paused['dataStatus'], 'paused')
        unknown = [{**r, 'stockSide': token(800 + i), 'token1': token(800 + i)} for i, r in enumerate(relations)]
        retained, paused = project_basket('芯片', '196', base, assets, unknown, NOW)
        self.assertIs(retained, base)
        self.assertEqual(paused['dataStatus'], 'paused')

    def test_insufficient_current_constituents_do_not_keep_old_index_live(self):
        old = {'baseAt': 1, 'version': 1, 'members': [
            {'token': token(i), 'basePrice': 1, 'baseCap': 100} for i in (1, 2, 3)]}
        assets = {token(i): asset(i) for i in (1, 2)}
        retained, view = project_basket('芯片', '196', old, assets,
                                        [relation(i) for i in (1, 2)], NOW)
        self.assertIs(retained, old)
        self.assertEqual(view['dataStatus'], 'paused')
        self.assertIsNone(view['value'])
        self.assertEqual(view['basketVersion'], 1)

    def test_no_qualifying_members_has_explicit_unavailable_projection(self):
        base, view = project_basket('芯片', '56', None, {}, [], NOW)
        self.assertIsNone(base)
        self.assertIsNone(view['value'])
        self.assertEqual(view['dataStatus'], 'unavailable')
        self.assertEqual(view['projectionVersion'], METHOD_VERSION)
        self.assertEqual(view['scopeLabel'], 'BNB Smart Chain')

    def test_unknown_asset_kind_or_other_chain_cannot_become_constituent(self):
        assets = {token(i): asset(i) for i in (1, 2, 3)}
        assets[token(2)].pop('kind')
        assets[token(3)]['chainId'] = '56'
        base, view = project_basket('芯片', '196', None, assets,
                                    [relation(i) for i in (1, 2, 3)], NOW)
        self.assertIsNone(base)
        self.assertEqual(view['dataStatus'], 'unavailable')
        self.assertEqual(view['quoteCoverage']['fresh'], 1)


class BasketHistoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import ResearchStore
        self.temp = tempfile.TemporaryDirectory()
        self.stores = {c: await ResearchStore(self.temp.name + '/research.sqlite', c).connect()
                       for c in ('196', '56', '4663')}

    async def asyncTearDown(self):
        for scoped in self.stores.values():
            await scoped.close()
        self.temp.cleanup()

    async def store(self, chain):
        return self.stores[chain]

    async def test_new_version_archives_old_base_and_uses_own_sample_series(self):
        scoped = self.stores['196']
        old = {'baseAt': NOW - 1000000, 'version': 1,
               'method': 'fixed-base-cap-weight', 'members': [
                   {'token': token(1), 'basePrice': 1, 'baseCap': 100},
                   {'token': token(2), 'basePrice': 1, 'baseCap': 100},
                   {'token': '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c',
                    'basePrice': 1, 'baseCap': 1_000_000_000}]}
        await scoped.put('basket', '芯片', old)
        await scoped.put('basket-last', '芯片', {'value': 50, 'at': NOW - 300000, 'basketVersion': 1})
        await scoped.put('basket-view', '芯片', {'sector': '芯片', 'basketVersion': 1, 'value': 50})
        await scoped.sample('basket:芯片', 50, None, NOW - 300000)
        data = SimpleNamespace(assets=[asset(i) for i in (1, 2, 3)],
                               relations=[relation(i) for i in (1, 2, 3)])
        with patch('app.collectors.baskets.store', AsyncMock(side_effect=self.store)), \
             patch('app.collectors.baskets.now_ms', return_value=NOW), \
             patch('app.collectors.baskets.enrich_asset', side_effect=lambda row: row):
            await refresh_baskets(affected={('196', token(1))}, data=data)
        current = await scoped.get('basket', '芯片')
        self.assertEqual(current['version'], 2)
        self.assertEqual(current['methodVersion'], METHOD_VERSION)
        self.assertNotIn('0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c',
                         {member['token'] for member in current['members']})
        archived = await scoped.get('basket-archive', f'芯片:v1:{old["baseAt"]}')
        self.assertEqual(archived['base'], old)
        self.assertEqual(archived['sampleKey'], 'basket:芯片')
        self.assertEqual(len(await scoped.samples('basket:芯片')), 1)
        self.assertEqual(len(await scoped.samples('basket:芯片:v2')), 1)
        view = await scoped.get('basket-view', '芯片')
        self.assertEqual(view['basketVersion'], 2)
        self.assertAlmostEqual(view['value'], 100)
        self.assertEqual(len(view['history']), 1)

    async def test_identical_theme_names_remain_scoped_to_their_chain(self):
        data = SimpleNamespace(
            assets=[asset(i, chain) for chain in ('196', '56') for i in (1, 2, 3)],
            relations=[relation(i, chain) for chain in ('196', '56') for i in (1, 2, 3)],
        )
        with patch('app.collectors.baskets.store', AsyncMock(side_effect=self.store)), \
             patch('app.collectors.baskets.now_ms', return_value=NOW), \
             patch('app.collectors.baskets.enrich_asset', side_effect=lambda row: row):
            await refresh_baskets(affected={('196', token(1)), ('56', token(1))}, data=data)
        a = await self.stores['196'].get('basket-view', '芯片')
        b = await self.stores['56'].get('basket-view', '芯片')
        self.assertEqual((a['scopeLabel'], b['scopeLabel']), ('X Layer', 'BNB Smart Chain'))
        self.assertEqual((a['basketVersion'], b['basketVersion']), (1, 1))
        self.assertAlmostEqual(a['value'], 100)
        self.assertAlmostEqual(b['value'], 100)

    async def test_unavailable_view_without_base_is_loaded_into_dashboard(self):
        from app.state import DashboardData

        _, view = project_basket('芯片', '56', None, {}, [], NOW)
        await self.stores['56'].put('basket-view', '芯片', view)
        with patch('app.state.store', AsyncMock(side_effect=self.store)):
            data = DashboardData()
            await data.reload()
            sectors = await data._sectors()
        self.assertEqual(len(sectors), 1)
        self.assertEqual((sectors[0]['sector'], sectors[0]['chainId']), ('芯片', '56'))
        self.assertEqual(sectors[0]['dataStatus'], 'unavailable')

    async def test_stale_live_projection_is_paused_at_read_time(self):
        from app.state import DashboardData

        data = DashboardData()
        data.basket_views[('196', '芯片')] = {
            'sector': '芯片', 'chainId': '196', 'projectionVersion': METHOD_VERSION,
            'at': NOW - 1_800_001, 'value': 120, 'lastValue': 120,
            'dataStatus': 'current', 'history': []}
        with patch('app.state.time.time', return_value=NOW / 1000):
            sectors = await data._sectors()
        self.assertIsNone(sectors[0]['value'])
        self.assertEqual(sectors[0]['lastValue'], 120)
        self.assertEqual(sectors[0]['dataStatus'], 'paused')
