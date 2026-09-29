"""The Node snapshot must seed durable stock facts without inventing quotes."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.collectors.okx_catalogue import sync_okx_catalogues
from app.db import ResearchStore


NOW = int(time.time() * 1000)
TOKEN = '0x' + 'a' * 40
OTHER = '0x' + 'b' * 40


class OkxCatalogueSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.stores = {
            chain: await ResearchStore(str(self.directory / 'research.sqlite'), chain).connect()
            for chain in ('196', '56', '4663')
        }

    async def asyncTearDown(self):
        for scoped in self.stores.values():
            await scoped.close()
        self.temp.cleanup()

    async def scoped(self, chain):
        return self.stores[chain]

    async def sync(self, **options):
        return await sync_okx_catalogues(self.directory, store_factory=self.scoped,
                                         now_ms=NOW, **options)

    def token(self, address=TOKEN, chain='196', at=None, **changes):
        return {'chainIndex': chain, 'tokenContractAddress': address,
                'tokenSymbol': 'ABCx', 'tokenName': 'ABC stock token',
                'stockCode': 'ABC', 'issuer': 'Example', 'price': 12.5,
                'marketCap': 1000, 'volume24h': 20, 'change24h': 0,
                'stockPrice': 13, 'quoteAt': NOW - 30_000 if at is None else at,
                **changes}

    def snapshot(self, tokens, chain='196', **changes):
        name = {'196': 'okx.json', '56': 'okx-56.json', '4663': 'okx-4663.json'}[chain]
        (self.directory / name).write_text(json.dumps({
            'status': 'ready', 'updatedAt': NOW - 20_000, 'tokens': tokens,
            **changes}, separators=(',', ':')))

    async def test_new_token_creates_stock_fact_and_asset_for_quote_baseline(self):
        self.snapshot([self.token()])
        result = await self.sync()
        stock = await self.stores['196'].get('stock', TOKEN)
        asset = await self.stores['196'].get('asset', TOKEN)
        self.assertEqual(result['updated'], 1)
        self.assertEqual(stock['stockCode'], 'ABC')
        self.assertEqual(stock['fieldTimes']['price'], NOW - 30_000)
        self.assertEqual(stock['fieldTimeKinds']['price'], 'received')
        self.assertEqual(asset['kind'], 'stock')
        self.assertEqual(asset['price'], 12.5)
        self.assertEqual(asset['fieldTimes']['price'], NOW - 30_000)
        self.assertEqual(asset['fieldObservations']['price']['marketAt'], None)
        self.assertEqual(asset['fieldObservations']['price']['receivedAt'], NOW - 30_000)
        self.assertEqual(asset['priceProvenance']['timeKind'], 'received')
        self.assertEqual((await self.stores['196'].samples(TOKEN))[0]['price'], 12.5)

    async def test_bsc_and_robinhood_snapshots_use_their_own_scopes(self):
        self.snapshot([self.token(chain='56')], chain='56')
        self.snapshot([self.token(chain='4663')], chain='4663')
        result = await self.sync()
        self.assertEqual(result['updated'], 2)
        self.assertIsNone(await self.stores['196'].get('asset', TOKEN))
        for chain in ('56', '4663'):
            self.assertEqual((await self.stores[chain].get('asset', TOKEN))['kind'], 'stock')
            self.assertEqual((await self.stores[chain].get('stock', TOKEN))['chainIndex'], chain)

    async def test_partial_snapshot_preserves_omitted_stock_and_richer_fields(self):
        scoped = self.stores['196']
        await scoped.put('stock', OTHER, {'chainIndex': '196', 'tokenContractAddress': OTHER,
                                          'stockCode': 'OLD', 'rareDetail': {'verified': True}})
        await scoped.put('asset', OTHER, {'token': OTHER, 'kind': 'stock', 'price': 7,
                                          'fieldTimes': {'price': NOW - 1000}})
        await scoped.put('stock', TOKEN, {'chainIndex': '196', 'tokenContractAddress': TOKEN,
                                          'tokenSymbol': 'ABCx', 'rareDetail': {'verified': True}})
        self.snapshot([self.token()], status='partial')
        await self.sync()
        self.assertEqual((await scoped.get('stock', OTHER))['stockCode'], 'OLD')
        self.assertEqual((await scoped.get('asset', OTHER))['price'], 7)
        self.assertEqual((await scoped.get('stock', TOKEN))['rareDetail'], {'verified': True})

    async def test_malformed_and_wrong_chain_snapshots_make_no_partial_writes(self):
        self.snapshot([self.token(), self.token(OTHER, chain='56')])
        result = await self.sync()
        self.assertEqual(result['failed'], 1)
        self.assertIsNone(await self.stores['196'].get('stock', TOKEN))
        self.assertIsNone(await self.stores['196'].get('asset', TOKEN))
        self.snapshot([self.token(), self.token(OTHER, at=NOW + 120_000)])
        result = await self.sync()
        self.assertEqual(result['failed'], 1)
        self.assertIsNone(await self.stores['196'].get('stock', TOKEN))
        (self.directory / 'okx.json').write_text('{bad JSON')
        self.assertEqual((await self.sync())['failed'], 1)

    async def test_stale_quote_promotes_candidate_without_overwriting_newer_price(self):
        scoped = self.stores['196']
        newer = NOW - 1000
        await scoped.put('stock', TOKEN, {
            'chainIndex': '196', 'tokenContractAddress': TOKEN,
            'stockCode': 'ABC', 'price': 30, 'quoteAt': newer,
            'fieldTimes': {'price': newer}, 'fieldSources': {'price': 'OKX'},
            'richSource': {'verified': True}})
        await scoped.put('asset', TOKEN, {
            'token': TOKEN, 'kind': 'candidate', 'symbol': 'ABCx',
            'price': 31, 'quoteAt': newer, 'fieldTimes': {'price': newer},
            'fieldSources': {'price': 'OKX'}, 'discoveryEventPending': True,
            'firstSeen': NOW - 86_400_000, 'risk': {'scored': True}})
        self.snapshot([self.token(at=NOW - 3_600_000)])
        await self.sync()
        stock = await scoped.get('stock', TOKEN)
        asset = await scoped.get('asset', TOKEN)
        self.assertEqual(stock['price'], 30)
        self.assertEqual(stock['fieldTimes']['price'], newer)
        self.assertEqual(stock['richSource'], {'verified': True})
        self.assertEqual(asset['kind'], 'stock')
        self.assertFalse(asset['discoveryEventPending'])
        self.assertEqual(asset['price'], 31)
        self.assertEqual(asset['fieldTimes']['price'], newer)
        self.assertEqual(asset['risk'], {'scored': True})
        self.assertEqual(asset['firstSeen'], NOW - 86_400_000)

    async def test_clockless_retained_price_never_gets_snapshot_or_worker_time(self):
        # The producer uses null for an inherited row without observation time.
        self.snapshot([self.token(at=None, quoteAt=None)], status='partial')
        await self.sync()
        stock = await self.stores['196'].get('stock', TOKEN)
        asset = await self.stores['196'].get('asset', TOKEN)
        self.assertEqual(stock['price'], 12.5)
        self.assertNotIn('quoteAt', stock)
        self.assertNotIn('fieldTimes', stock)
        self.assertNotIn('price', asset)
        self.assertEqual(asset['kind'], 'stock')

    async def test_timed_catalogue_quote_replaces_clockless_legacy_price(self):
        scoped = self.stores['196']
        await scoped.put('stock', TOKEN, {'chainIndex': '196',
                                          'tokenContractAddress': TOKEN, 'price': 3})
        await scoped.put('asset', TOKEN, {'token': TOKEN, 'kind': 'candidate',
                                          'price': 4, 'risk': {'scored': True}})
        self.snapshot([self.token()])
        await self.sync()
        stock = await scoped.get('stock', TOKEN)
        asset = await scoped.get('asset', TOKEN)
        self.assertEqual(stock['price'], 12.5)
        self.assertEqual(stock['fieldTimes']['price'], NOW - 30_000)
        self.assertEqual(asset['kind'], 'stock')
        self.assertEqual(asset['price'], 12.5)
        self.assertEqual(asset['fieldTimes']['price'], NOW - 30_000)
        self.assertEqual(asset['risk'], {'scored': True})

    async def test_db_restored_snapshot_can_have_no_refresh_time(self):
        self.snapshot([self.token(quoteAt=None)], updatedAt=None)
        await self.sync()
        stock = await self.stores['196'].get('stock', TOKEN)
        asset = await self.stores['196'].get('asset', TOKEN)
        self.assertEqual(stock['tokenSymbol'], 'ABCx')
        self.assertNotIn('fieldTimes', stock)
        self.assertNotIn('price', asset)

    async def test_row_quote_can_be_newer_than_directory_refresh(self):
        self.snapshot([self.token()], updatedAt=NOW - 86_400_000)
        await self.sync()
        asset = await self.stores['196'].get('asset', TOKEN)
        self.assertEqual(asset['fieldTimes']['price'], NOW - 30_000)

    async def test_duplicate_is_write_free_and_limit_drains_incrementally(self):
        self.snapshot([self.token(), self.token(OTHER)])
        first = await self.sync(per_chain_limit=1)
        self.assertEqual(first['updated'], 1)
        self.assertEqual(first['skipped'], 1)
        second = await self.sync(per_chain_limit=1)
        self.assertEqual(second['updated'], 1)
        changes = self.stores['196'].db.total_changes
        third = await self.sync(per_chain_limit=1)
        self.assertEqual(third['updated'], 0)
        self.assertTrue(third['noChange'])
        self.assertEqual(self.stores['196'].db.total_changes, changes)


if __name__ == '__main__':
    unittest.main()
