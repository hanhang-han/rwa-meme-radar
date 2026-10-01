import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, AsyncMock


class CollectionInfrastructureTest(unittest.TestCase):
    def test_usage_display_reads_committed_value_during_quota_charge(self):
        import sqlite3
        from app.request_ledger import shared_usage
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'OKX_LEDGER_PATH': d+'/ledger.sqlite'}):
            self.assertEqual(shared_usage('2099-01-01', 10), 1)
            blocker = sqlite3.connect(d+'/ledger.sqlite')
            try:
                blocker.execute('BEGIN IMMEDIATE')
                blocker.execute('UPDATE budget SET used=2')
                self.assertEqual(shared_usage('2099-01-01'), 1)
                blocker.commit()
                self.assertEqual(shared_usage('2099-01-01'), 2)
            finally:
                blocker.close()

    def test_shared_rate_reservations_and_lane_cap(self):
        from app.request_ledger import reserve_request_slot, shared_usage
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'OKX_LEDGER_PATH':d+'/ledger.sqlite'}):
            reserve_request_slot(500,1000)
            with ThreadPoolExecutor(max_workers=8) as pool:
                slots=list(pool.map(lambda _:reserve_request_slot(500,1000),range(8)))
            self.assertEqual(sorted(slots),[.5,1,1.5,2,2.5,3,3.5,4])
            shared_usage('2099-01-01',10,'candles',1)
            with self.assertRaisesRegex(RuntimeError,'candles daily allowance'):
                shared_usage('2099-01-01',10,'candles',1)
            self.assertEqual(shared_usage('2099-01-01'),1)
            self.assertEqual(shared_usage('2099-01-01',10,'discovery',2),2)

    def test_query_candles_only_registers_demand_and_reuses_canonical_cache(self):
        from app.api import candles
        from app.db import ResearchStore
        async def run():
            with tempfile.TemporaryDirectory() as d:
                db=await ResearchStore(d+'/db.sqlite','56').connect()
                address='0x'+'1'*40
                key=f'dex:56:{address}:5m'
                await db.put('candle-meta',key,{'lastSuccessfulAt':1800000000000,'storage':address,'source':'OKX DEX'})
                await db.put_candles(address,'5m',[{'t':1799999700000,'o':1,'h':2,'l':1,'c':2,'v':3,'vu':4}])
                try:
                    with patch.object(candles,'store',AsyncMock(return_value=db)),patch.object(candles,'candle_series',AsyncMock()) as network:
                        a=await candles.get_candles('56',address,'5m',30,'dex')
                        b=await candles.get_candles('56',address,'5m',500,'dex')
                    self.assertEqual(a['rows'],b['rows'])
                    network.assert_not_called()
                    from app.demand_leases import all_lease_kv, flush_lease_writer
                    await flush_lease_writer()
                    self.assertEqual(len(await all_lease_kv(db,'candle-watch')),1)
                    self.assertEqual(await db.all('candle-watch'),[])
                finally:await db.close()
        asyncio.run(run())

    def test_rejected_quote_preserves_field_provenance(self):
        from app.db import ResearchStore
        async def run():
            with tempfile.TemporaryDirectory() as d:
                db=await ResearchStore(d+'/db.sqlite').connect()
                try:
                    await db.merge_asset_observation('a',{'fieldSources':{'price':{'provider':'new'}}},{'price':(2,2000)})
                    row=await db.merge_asset_observation('a',{'fieldSources':{'price':{'provider':'old'}}},{'price':(1,1000)})
                    self.assertEqual(row['price'],2)
                    self.assertEqual(row['fieldSources']['price']['provider'],'new')
                    await db.comparison_samples_batch([('a',str(t),t,{'at':t}) for t in [1000,3601000,86401000]])
                    self.assertEqual([x['at'] for x in await db.comparison_observations_at('a',3601000,500)], [3601000])
                    self.assertEqual(await db.prune_comparisons(3600000),1)
                finally:await db.close()
        asyncio.run(run())
