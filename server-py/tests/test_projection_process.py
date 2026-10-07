"""Process ownership, health isolation and legacy-release rollback contracts."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def health_storage(*, assets=1, latest=0, candidates=(), stocks=(), relations=(),
                   signals=(), streams=(), scan_quality=None):
    chains = ('196', '56', '4663')
    return {
        'projection': ({'unified': {'assets': list(candidates), 'stockTokens': list(stocks),
                                    'relations': list(relations), 'sources': list(streams)}},
                       {'signals': list(signals)}),
        'assets': assets, 'latestAssetAt': latest,
        'domains': {chain: {} for chain in chains},
        'scanQuality': {chain: (scan_quality if chain == '196' and scan_quality else
                               {'total': 0, 'partial': 0, 'unsupportedPools': 0, 'failedPools': 0})
                        for chain in chains},
    }


class ProjectionProcessTests(unittest.TestCase):
    def test_projection_owner_excludes_an_accidental_second_process(self):
        from app.projection_worker import projection_owner
        probe = '''import fcntl,sys
with open(sys.argv[1], "a+") as lock:
 try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
 except BlockingIOError: sys.exit(42)
'''
        with tempfile.TemporaryDirectory() as root:
            path = root + '/projection.lock'
            with projection_owner(path):
                other = subprocess.run([sys.executable, '-c', probe, path], timeout=5)
                self.assertEqual(other.returncode, 42)
            other = subprocess.run([sys.executable, '-c', probe, path], timeout=5)
            self.assertEqual(other.returncode, 0)

    def test_projection_role_registers_only_local_jobs_and_preserves_cadence(self):
        from app.projection_worker import start_projection_loops
        jobs = {}
        admission_options = {}
        def register(name, interval, action, delay=0, **options):
            jobs[name] = (interval, delay, action)
            admission_options[name] = options
        publish = AsyncMock(return_value={'changed': True})
        derive = AsyncMock(return_value={'affected': 2})
        with patch('app.collectors.scheduler.spawn_loop', side_effect=register), \
             patch('app.realtime_projection.projection_tick', publish), \
             patch('app.realtime_projection.refresh_derived', derive):
            start_projection_loops()
            self.assertEqual({name: values[:2] for name, values in jobs.items()}, {
                'realtimeProjection': (2, 0), 'realtimeDerived': (30, 2),
                'comparisons': (120, 22), 'baskets': (60, 55), 'outboxMaintenance': (2, 1),
            })
            self.assertEqual({name for name, options in admission_options.items() if options.get('priority')},
                             {'realtimeProjection'})
            self.assertEqual({name for name, options in admission_options.items() if options.get('heavy')},
                             {'realtimeProjection', 'realtimeDerived', 'comparisons', 'baskets'})
            self.assertEqual(admission_options['realtimeProjection']['budget_lane'], 'publication')
            self.assertEqual(admission_options['realtimeProjection']['max_run_s'], 90)
            self.assertTrue(all('budget_lane' not in options for name, options in admission_options.items()
                                if name != 'realtimeProjection'))
            self.assertEqual(asyncio.run(jobs['realtimeProjection'][2]()), {'accepted': 1, 'updated': 1})
            self.assertEqual(asyncio.run(jobs['realtimeDerived'][2]()), {'accepted': 1, 'updated': 2, 'skipped': 0})
        publish.assert_awaited_once()
        derive.assert_awaited_once()

    def test_long_derived_turn_leaves_a_gap_without_discarding_dirty_work(self):
        from app.projection_worker import start_projection_loops
        jobs = {}
        def register(name, interval, action, delay=0, **options):
            jobs[name] = action
        clock = [100.0]
        async def slow_derive():
            clock[0] += 40  # The 30s scheduler interval was exceeded.
            return {'affected': 200}
        derive = AsyncMock(side_effect=slow_derive)
        with patch('app.collectors.scheduler.spawn_loop', side_effect=register), \
             patch('app.realtime_projection.refresh_derived', derive), \
             patch('app.projection_worker.time.monotonic', side_effect=lambda: clock[0]):
            start_projection_loops()
            run = jobs['realtimeDerived']
            self.assertEqual(asyncio.run(run())['updated'], 200)
            self.assertEqual(asyncio.run(run()), {'accepted': 0, 'updated': 0, 'skipped': 1})
            derive.assert_awaited_once()
            clock[0] += 10
            self.assertEqual(asyncio.run(run())['updated'], 200)
            self.assertEqual(derive.await_count, 2)

    def test_health_writers_keep_collector_and_projection_files_separate(self):
        from app.collectors import scheduler
        with tempfile.TemporaryDirectory() as root:
            collector, projection = Path(root)/'worker.json', Path(root)/'projection.json'
            with patch.dict(os.environ, {'NODE_ENV': '', 'WORKER_HEALTH_PATH': str(collector)}), \
                 patch.object(scheduler, 'TASKS', {'liveQuotes': {'status': 'waiting'}}):
                scheduler._save_health()
            original = collector.read_bytes()
            with patch.dict(os.environ, {'NODE_ENV': '', 'WORKER_HEALTH_PATH': str(projection)}), \
                 patch.object(scheduler, 'TASKS', {'realtimeProjection': {'status': 'waiting'}}):
                scheduler._save_health()
            self.assertEqual(collector.read_bytes(), original)
            self.assertEqual(set(json.loads(projection.read_text())['tasks']), {'realtimeProjection'})

    def test_missing_old_and_exited_process_health_is_not_healthy(self):
        from app.main import process_health
        now = int(time.time()*1000)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'health.json'
            self.assertFalse(process_health(path, now)['ok'])
            record = {'pid': os.getpid(), 'updatedAt': now-100, 'tasks': {'job': {'status': 'waiting'}}}
            path.write_text(json.dumps(record))
            self.assertTrue(process_health(path, now)['ok'])
            path.write_text(json.dumps({**record, 'updatedAt': now-180001}))
            self.assertFalse(process_health(path, now)['ok'])
            path.write_text(json.dumps(record))
            with patch('os.kill', side_effect=ProcessLookupError):
                self.assertFalse(process_health(path, now)['ok'])
            path.write_text('[]')
            self.assertFalse(process_health(path, now)['ok'])

    @unittest.skipUnless(shutil.which('node'), 'Node is needed to read the release ecosystem')
    def test_rollback_to_old_ecosystem_removes_new_projection_process(self):
        root = Path(__file__).resolve().parents[2]
        deploy = (root/'scripts/deploy.sh').read_text()
        helpers = deploy[deploy.index('release_processes=('):deploy.index('NODE_SWAPPED=0')]
        fake_pm2 = '''
running=" memedashboard pyradar pyradar-worker pyradar-projection "
pm2() {
 case "$1" in
 describe) case "$running" in *" $2 "*) return 0;; *) return 1;; esac;;
 stop) return 0;;
 delete) running=${running//" $2 "/" "};;
 start) running=" ${4//,/ } ";;
 *) return 1;;
 esac
}
'''
        with tempfile.TemporaryDirectory() as directory:
            old_apps = {'apps': [{'name': name} for name in ('memedashboard', 'pyradar', 'pyradar-worker')]}
            Path(directory, 'ecosystem.config.cjs').write_text('module.exports = '+json.dumps(old_apps))
            script = 'set -eu\n'+fake_pm2+helpers+'''\nstop_release_processes
delete_release_processes
start_release_processes
printf '%s' "$running"
'''
            result = subprocess.run(['bash', '-c', script], cwd=directory, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.split(), ['memedashboard', 'pyradar', 'pyradar-worker'])


class ProcessHealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # These health tests provide a complete storage snapshot. Keep the
        # separate coverage reader from opening an unrelated global database.
        self.discovery = patch('app.collectors.discovery_coverage.discovery_coverage_snapshot',
                               AsyncMock(return_value={}))
        self.discovery.start()
        self.addCleanup(self.discovery.stop)

    def test_required_source_failure_and_optional_reference_warning(self):
        from app.main import classify_sources
        sources, issues, warnings = classify_sources([
            {'id':'okx:dex','provider':'OKX','kind':'dex-quotes','status':'budget-exhausted'},
            {'id':'eodhd:exchange','provider':'EODHD','kind':'stock-references','status':'entitlement-required'},
        ])
        self.assertEqual(issues,['OKX-budget-exhausted'])
        self.assertEqual(warnings,['EODHD-entitlement-required'])
        self.assertTrue(sources[0]['required'])
        self.assertFalse(sources[1]['required'])

    def test_no_change_with_skipped_identity_reports_partial_and_preserves_success(self):
        from app.collectors.scheduler import apply_result
        status={}
        apply_result(status,{'requested':2,'processed':1,'accepted':0,'changed':0,
                             'failed':0,'skipped':1,'noChange':True},100)
        self.assertEqual(status['status'],'partial')
        self.assertEqual(status['outcome'],'no-change')
        self.assertEqual(status['lastSuccessAt'],100)
        self.assertIn('missing token identity',status['error'])

    def test_disk_history_is_small_and_independent_of_task_failure(self):
        from app.worker import record_disk_sample
        from app.main import disk_history
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'disk-health.json'
            for at in range(60):
                record_disk_sample(SimpleNamespace(free=40-at,total=100),at,str(path))
            history=disk_history(str(path))
            self.assertEqual(len(history),48)
            self.assertEqual((history[0]['at'],history[-1]['freeBytes']),(12,-19))

    async def test_readiness_does_not_decode_the_asset_catalogue(self):
        from app.main import health_ready
        database = SimpleNamespace(fetchone=AsyncMock(return_value=(1,)), all=AsyncMock(side_effect=AssertionError('catalogue read')))
        with patch('app.db.store', AsyncMock(return_value=database)), \
                patch('app.developer_access.ready', return_value=True):
            self.assertEqual(await health_ready(), {'ok': True, 'storage': 'ready'})
        database.fetchone.assert_awaited_once_with('SELECT 1')
        database.all.assert_not_called()

    async def test_health_aggregates_old_task_contract_but_marks_missing_projection_degraded(self):
        from app.main import health_data_payload as health_data
        now = int(time.time()*1000)
        snapshot = health_storage(latest=now)
        collector = {'pid': os.getpid(), 'updatedAt': now, 'ageMs': 0, 'ok': True,
                     'tasks': {'liveQuotes': {'status': 'waiting'}}}
        projection = {'pid': os.getpid(), 'updatedAt': now, 'ageMs': 0, 'ok': True,
                      'tasks': {'realtimeProjection': {'status': 'waiting'}}}
        with patch('app.main._health_storage_snapshot', AsyncMock(return_value=snapshot)), \
             patch('app.state._source_statuses', return_value=[]), \
             patch('shutil.disk_usage', return_value=SimpleNamespace(total=100, free=90)):
            with patch('app.main.process_health', side_effect=[collector, projection]):
                healthy = await health_data()
            self.assertTrue(healthy['worker']['ok'])
            self.assertTrue(healthy['projection']['ok'])
            self.assertEqual(set(healthy['worker']['tasks']), {'liveQuotes', 'realtimeProjection'})
            snapshot['projection'][0]['unified']['sources'] = [
                {'provider': 'Chain RPC', 'id': 'chain-stream:4663',
                 'chainId': '4663', 'status': 'catching-up',
                 'queueDepth': 12, 'sourceLagMs': 20_000, 'nearTipLagBlocks': 67_000}]
            with patch('app.main.process_health', side_effect=[collector, projection]):
                backlogged = await health_data()
            self.assertEqual(backlogged['status'], 'degraded')
            self.assertIn('chain-stream-degraded', backlogged['issues'])
            snapshot['projection'][0]['unified']['sources'] = []
            missing = {'ageMs': None, 'ok': False, 'tasks': {}}
            with patch('app.main.process_health', side_effect=[collector, missing]):
                degraded = await health_data()
            self.assertEqual(degraded['status'], 'degraded')
            self.assertIn('projection-unavailable', degraded['issues'])
            self.assertFalse(degraded['worker']['ok'])
            self.assertTrue(degraded['collector']['ok'])

    async def test_optional_entitlement_keeps_core_healthy_but_reports_reference_gap(self):
        from app.main import health_data_payload as health_data
        now=int(time.time()*1000)
        stock={'price':12,'stockPrice':100,'referenceScope':'issuer-reference',
               'fieldTimes':{'price':now},'quoteAt':now}
        snapshot = health_storage(latest=now, stocks=[stock])
        running={'pid':os.getpid(),'updatedAt':now,'ageMs':0,'ok':True,'tasks':{'maintenance':{'status':'waiting'}}}
        with patch('app.main._health_storage_snapshot',AsyncMock(return_value=snapshot)), \
             patch('app.state._source_statuses',return_value=[{'id':'eodhd:exchange','provider':'EODHD','kind':'stock-references','status':'entitlement-required'}]), \
             patch('shutil.disk_usage',return_value=SimpleNamespace(total=100,free=50)), \
             patch('app.main.disk_history',return_value=[]), \
             patch('app.main.process_health',side_effect=[running,running]):
            health=await health_data()
        self.assertTrue(health['ok'])
        self.assertEqual(health['status'],'limited')
        self.assertEqual(health['issues'],[])
        self.assertIn('EODHD-entitlement-required',health['warnings'])
        self.assertEqual(health['capabilities']['stockReferences']['independent'],0)
        self.assertEqual(health['capabilities']['stockReferences']['status'],'unavailable')
        self.assertEqual(health['capabilities']['stockReferences']['reason'],'entitlement-required')

    async def test_unquoted_market_remains_in_total_but_not_quoteable_coverage(self):
        from app.main import health_data_payload as health_data
        now = int(time.time()*1000)
        priced = {'kind':'candidate','price':1,'fieldTimes':{'price':now},'quoteAt':now}
        unavailable = {'kind':'candidate','price':None,'quoteStatus':'no-verified-market',
                       'quoteReason':'insufficient-liquidity'}
        stock_priced = {'price':2,'fieldTimes':{'price':now},'quoteAt':now}
        stock_unavailable = {'price':None,'quoteStatus':'no-verified-market',
                             'quoteReason':'source-returned-no-price'}
        snapshot = health_storage(latest=now, candidates=[priced,unavailable],
                                  stocks=[stock_priced,stock_unavailable])
        running = {'pid':os.getpid(),'updatedAt':now,'ageMs':0,'ok':True,
                   'tasks':{'maintenance':{'status':'waiting'}}}
        with patch('app.main._health_storage_snapshot',AsyncMock(return_value=snapshot)), \
             patch('app.state._source_statuses',return_value=[]), \
             patch('shutil.disk_usage',return_value=SimpleNamespace(total=100,free=50)), \
             patch('app.main.disk_history',return_value=[]), \
             patch('app.main.process_health',side_effect=[running,running]):
            health = await health_data()
        coverage = health['coverage']['candidates']
        self.assertEqual({key:coverage[key] for key in ('total','known','missing','quotableTotal','quoteUnavailable','pending')},
                         {'total':2,'known':1,'missing':1,'quotableTotal':1,'quoteUnavailable':1,'pending':0})
        self.assertEqual(coverage['quotableWithinTarget'],1)
        stock_coverage = health['coverage']['stocks']
        self.assertEqual((stock_coverage['total'],stock_coverage['quoteUnavailable'],stock_coverage['quotableTotal']),
                         (2,1,1))
        self.assertNotIn('candidates-coverage-degraded',health['issues'])
        self.assertNotIn('stocks-coverage-degraded',health['issues'])
        self.assertIn('candidate-no-verified-market',health['warnings'])
        self.assertIn('stock-no-verified-market',health['warnings'])

    async def test_known_unsupported_pools_are_visible_without_collector_failure(self):
        from app.main import health_data_payload as health_data
        now=int(time.time()*1000)
        snapshot = health_storage(latest=now, scan_quality={
            'total': 1, 'partial': 1, 'unsupportedPools': 3, 'failedPools': 0})
        running={'pid':os.getpid(),'updatedAt':now,'ageMs':0,'ok':True,
                 'tasks':{'discovery':{'status':'waiting'}}}
        with patch('app.main._health_storage_snapshot',AsyncMock(return_value=snapshot)), \
             patch('app.state._source_statuses',return_value=[]), \
             patch('shutil.disk_usage',return_value=SimpleNamespace(total=100,free=50)), \
             patch('app.main.disk_history',return_value=[]), \
             patch('app.main.process_health',side_effect=[running,running]):
            health=await health_data()
        self.assertTrue(health['ok'])
        self.assertEqual(health['status'],'limited')
        self.assertIn('discovery-unsupported-pools',health['warnings'])
        self.assertEqual(health['discovery']['196']['scanQuality']['unsupportedPools'],3)


class HealthProjectionReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import ResearchStore, WriterLock
        self.discovery = patch('app.collectors.discovery_coverage.discovery_coverage_snapshot',
                               AsyncMock(return_value={}))
        self.discovery.start()
        self.addCleanup(self.discovery.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ResearchStore(self.temp.name + '/research.sqlite', '196', write_lock=WriterLock())
        self.addAsyncCleanup(self.store.close)
        await self.store.connect()
        self.now = int(time.time() * 1000)
        self.market = {'unified': {
            'assets': [{'chainId': '196', 'price': 1, 'fieldTimes': {'price': self.now}}],
            'stockTokens': [{'chainId': '56', 'price': 2, 'stockPrice': 100,
                             'referenceScope': 'equity-exchange', 'fieldTimes': {'price': self.now}}],
            'relations': [{'id': 'pool-1'}],
            'sources': [{'provider': 'Chain RPC', 'id': 'chain-stream:196', 'chainId': '196',
                         'queueDepth': 1, 'nearTipLagBlocks': 2}],
        }}
        self.feed = {'signals': [{'chainId': '196', 't': self.now - 5000}]}
        await self.publish()
        await self.store.db.executemany('INSERT INTO facts(kind,id,body) VALUES (?,?,?)', [
            ('196:asset', 'candidate', json.dumps({'kind': 'candidate', 'updatedAt': self.now - 200})),
            ('56:asset', 'stock', json.dumps({'kind': 'stock', 'updatedAt': self.now - 100})),
            ('4663:asset', 'base', json.dumps({'kind': 'base', 'updatedAt': self.now - 300})),
            ('196:collector-job', 'quote:a', json.dumps({'domain': 'quote', 'failureCount': 2,
                'nextRetryAt': self.now + 1000, 'lastSuccessAt': 40, 'lastAttemptAt': 80})),
            ('196:collector-job', 'quote:b', json.dumps({'domain': 'quote', 'failureCount': 0,
                'nextRetryAt': 0, 'lastSuccessAt': 90, 'lastAttemptAt': 100})),
            ('56:collector-job', 'scan:a', json.dumps({'domain': 'scan', 'failureCount': 1,
                'nextRetryAt': self.now - 1000, 'lastSuccessAt': 60, 'lastAttemptAt': 110})),
            ('196:scan', 'one', json.dumps({'status': 'partial', 'unsupportedPools': 3, 'failedPools': 1})),
            ('196:scan', 'two', json.dumps({'status': 'complete', 'unsupportedPools': 1, 'failedPools': 0})),
        ])
        await self.store.db.commit()

    async def publish(self):
        from app.realtime_projection import _dump, _encode_snapshot
        rows = [('full', 1, 3, 4, '{invalid-full-body', self.now),
                ('market', 1, 3, 4, _encode_snapshot(_dump(self.market)), self.now),
                ('feed', 1, 3, 4, _encode_snapshot(_dump(self.feed)), self.now)]
        await self.store.db.executemany('INSERT INTO dashboard_projection VALUES (?,?,?,?,?,?)', rows)
        await self.store.db.commit()

    async def test_matching_named_views_and_scoped_aggregates_preserve_health_contract(self):
        from app.main import _health_storage_snapshot, health_data_payload as health_data
        running = {'ageMs': 0, 'ok': True, 'tasks': {'maintenance': {'status': 'waiting'}}}
        with patch('app.db.store', AsyncMock(return_value=self.store)), \
             patch('app.state.reload_if_stale', side_effect=AssertionError('full reload forbidden')):
            snapshot = await _health_storage_snapshot(self.now)
            self.assertEqual(snapshot['assets'], 3)
            self.assertEqual(snapshot['latestAssetAt'], self.now - 100)
            self.assertEqual(snapshot['domains']['196']['quote'], {
                'total': 2, 'failed': 1, 'waitingRetry': 1,
                'lastSuccessAt': 90, 'lastAttemptAt': 100})
            self.assertEqual(snapshot['domains']['56']['scan']['failed'], 1)
            self.assertEqual(snapshot['scanQuality']['196'], {
                'total': 2, 'partial': 1, 'unsupportedPools': 4, 'failedPools': 1})
            with patch('app.state._source_statuses', return_value=[]), \
                 patch('app.main.process_health', side_effect=[running, running]), \
                 patch('app.main.disk_history', return_value=[]), \
                 patch('shutil.disk_usage', return_value=SimpleNamespace(total=100, free=90)):
                health = await health_data()
        self.assertEqual(health['status'], 'limited')
        self.assertEqual((health['assets'], health['relations'], health['latestAssetAt']),
                         (3, 1, self.now - 100))
        self.assertEqual(health['coverage']['candidates']['withinTarget'], 1)
        self.assertEqual(health['coverage']['stocks']['withinTarget'], 1)
        self.assertEqual(health['discovery']['196']['latestEventAt'], self.now - 5000)
        self.assertEqual(health['discovery']['196']['scanQuality']['unsupportedPools'], 4)
        self.assertEqual(health['chainStreams'][0]['nearTipLagBlocks'], 2)
        self.assertIn('discovery-unsupported-pools', health['warnings'])

    async def test_missing_or_mismatched_views_degrade_without_building_full_projection(self):
        from app.main import _health_storage_snapshot, health_data_payload as health_data
        running = {'ageMs': 0, 'ok': True, 'tasks': {'maintenance': {'status': 'waiting'}}}
        await self.store.db.execute("UPDATE dashboard_projection SET revision=2 WHERE name='feed'")
        await self.store.db.commit()
        with patch('app.db.store', AsyncMock(return_value=self.store)), \
             patch('app.state.reload_if_stale', side_effect=AssertionError('full reload forbidden')), \
             patch('app.realtime_projection.projection_tick', side_effect=AssertionError('build forbidden')), \
             patch('app.state._source_statuses', side_effect=AssertionError('unavailable projection')), \
             patch('app.main.process_health', side_effect=[running, running]), \
             patch('app.main.disk_history', return_value=[]), \
             patch('shutil.disk_usage', return_value=SimpleNamespace(total=100, free=90)):
            snapshot = await _health_storage_snapshot(self.now)
            health = await health_data()
        self.assertIsNone(snapshot['projection'])
        self.assertEqual(snapshot['assets'], 3)
        self.assertFalse(health['ok'])
        self.assertIn('projection-snapshot-unavailable', health['issues'])
        self.assertNotIn('assets-unavailable', health['issues'])
        self.assertEqual(health['assets'], 3)
        self.assertEqual(health['sources'], [])

    async def test_invalid_named_view_degrades_instead_of_throwing(self):
        from app.main import _health_storage_snapshot
        await self.store.db.execute("UPDATE dashboard_projection SET body=? WHERE name='market'",
                                    (b'CRP1\x00broken',))
        await self.store.db.commit()
        with patch('app.db.store', AsyncMock(return_value=self.store)):
            snapshot = await _health_storage_snapshot(self.now)
        self.assertIsNone(snapshot['projection'])
        self.assertEqual(snapshot['assets'], 3)

    async def test_missing_named_view_keeps_raw_counts_and_degrades(self):
        from app.main import _health_storage_snapshot
        await self.store.db.execute("DELETE FROM dashboard_projection WHERE name='feed'")
        await self.store.db.commit()
        with patch('app.db.store', AsyncMock(return_value=self.store)):
            snapshot = await _health_storage_snapshot(self.now)
        self.assertIsNone(snapshot['projection'])
        self.assertEqual(snapshot['assets'], 3)
        self.assertEqual(snapshot['domains']['196']['quote']['total'], 2)

    async def test_storage_failure_returns_degraded_health(self):
        from app.main import health_data_payload as health_data
        running = {'ageMs': 0, 'ok': True, 'tasks': {}}
        with patch('app.main._health_storage_snapshot', AsyncMock(side_effect=RuntimeError('storage busy'))), \
             patch('app.state.reload_if_stale', side_effect=AssertionError('full reload forbidden')), \
             patch('app.main.process_health', side_effect=[running, running]), \
             patch('app.main.disk_history', return_value=[]), \
             patch('shutil.disk_usage', return_value=SimpleNamespace(total=100, free=90)):
            health = await health_data()
        self.assertEqual(health['status'], 'degraded')
        self.assertIn('health-storage-unavailable', health['issues'])
        self.assertIn('projection-snapshot-unavailable', health['issues'])
