"""Alpha directory refresh keeps stable registry rows out of the write path."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.collectors.binance_alpha import refresh_directory
from app.collectors.market_streams import Market
from app.db import ResearchStore


TOKEN = '0x' + '1' * 40
SECOND = '0x' + '2' * 40
STALE = '0x' + '3' * 40


class Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {'code': '000000', 'data': {}}


class Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get(self, _):
        return Response()


class DirectoryRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = str(Path(self.temp.name) / 'research.sqlite')
        self.stores = {chain: await ResearchStore(path, chain).connect()
                       for chain in ('196', '56', '4663')}

    async def asyncTearDown(self):
        for scoped in self.stores.values():
            await scoped.close()
        self.temp.cleanup()

    async def registry_changes(self, chain):
        row = await self.stores[chain].fetchone(
            'SELECT COUNT(*) FROM change_outbox WHERE kind=? AND entity LIKE ?',
            (self.stores[chain].key('market-registry'), 'binance-alpha:%'))
        return row[0]

    async def test_unchanged_directory_avoids_registry_churn_and_delta_is_atomic(self):
        first = Market('56', TOKEN, 'binance-alpha', 'ALPHA_1USDT', 'USDT', 'ONE')
        second = Market('56', SECOND, 'binance-alpha', 'ALPHA_2USDT', 'USDT', 'TWO')
        stale = Market('56', STALE, 'binance-alpha', 'ALPHA_3USDT', 'USDT', 'OLD')
        robinhood = Market('4663', TOKEN, 'binance-alpha', 'ALPHA_4USDT', 'USDT', 'FOUR')
        unrelated = Market('56', TOKEN, 'binance', 'ONEUSDT', 'USDT', 'ONE')
        await self.stores['56'].put('market-registry', stale.storage, stale.record())
        await self.stores['56'].put('market-registry', unrelated.storage, unrelated.record())
        baseline = await self.registry_changes('56')
        directory = [first, second, robinhood]

        def mapped(*_):
            return list(directory), {'directoryTokens': len(directory),
                                     'mappedMarkets': len(directory),
                                     'ambiguousAlphaIds': 0, 'mappedAssets': len(directory)}

        async def scoped_store(chain):
            return self.stores[chain]

        with patch('app.collectors.binance_alpha.httpx.AsyncClient', return_value=Client()), \
             patch('app.collectors.binance_alpha.map_markets', side_effect=mapped), \
             patch('app.collectors.binance_alpha.store', side_effect=scoped_store):
            self.assertEqual((await refresh_directory())['accepted'], 3)
            self.assertIsNone(await self.stores['56'].get('market-registry', stale.storage))
            self.assertEqual(await self.stores['56'].get('market-registry', first.storage), first.record())
            self.assertEqual(await self.stores['56'].get('market-registry', unrelated.storage), unrelated.record())
            self.assertEqual(await self.stores['4663'].get('market-registry', robinhood.storage), robinhood.record())
            self.assertEqual(await self.registry_changes('56') - baseline, 3)
            stable_56 = await self.registry_changes('56')
            stable_4663 = await self.registry_changes('4663')

            self.assertEqual((await refresh_directory())['accepted'], 3)
            self.assertEqual(await self.registry_changes('56'), stable_56)
            self.assertEqual(await self.registry_changes('4663'), stable_4663)

            renamed = Market('56', TOKEN, 'binance-alpha', 'ALPHA_1USDT', 'USDT', 'RENAMED')
            directory[:] = [renamed, robinhood]
            await self.stores['56'].db.execute('''CREATE TRIGGER reject_alpha_update
                BEFORE UPDATE ON facts WHEN NEW.kind='56:market-registry'
                AND NEW.id LIKE 'binance-alpha:%'
                BEGIN SELECT RAISE(ABORT, 'test failure'); END''')
            await self.stores['56'].db.commit()
            self.assertEqual((await refresh_directory())['failed'], 1)
            self.assertEqual(await self.stores['56'].get('market-registry', first.storage), first.record())
            self.assertEqual(await self.stores['56'].get('market-registry', second.storage), second.record())
            self.assertEqual(await self.registry_changes('56'), stable_56)
            await self.stores['56'].db.execute('DROP TRIGGER reject_alpha_update')
            await self.stores['56'].db.commit()

            self.assertEqual((await refresh_directory())['accepted'], 2)
            self.assertEqual(await self.stores['56'].get('market-registry', renamed.storage), renamed.record())
            self.assertIsNone(await self.stores['56'].get('market-registry', second.storage))
            self.assertEqual(await self.registry_changes('56') - stable_56, 2)
            self.assertEqual(await self.registry_changes('4663'), stable_4663)
