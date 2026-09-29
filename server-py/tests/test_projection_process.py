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
        def register(name, interval, action, delay=0):
            jobs[name] = (interval, delay, action)
        publish = AsyncMock(return_value={'changed': True})
        derive = AsyncMock(return_value={'affected': 2})
        with patch('app.collectors.scheduler.spawn_loop', side_effect=register), \
             patch('app.realtime_projection.projection_tick', publish), \
             patch('app.realtime_projection.refresh_derived', derive):
            start_projection_loops()
            self.assertEqual({name: values[:2] for name, values in jobs.items()}, {
                'realtimeProjection': (1, 0), 'realtimeDerived': (1, 2),
                'comparisons': (30, 22), 'baskets': (60, 55),
            })
            self.assertEqual(asyncio.run(jobs['realtimeProjection'][2]()), {'accepted': 1, 'updated': 1})
            self.assertEqual(asyncio.run(jobs['realtimeDerived'][2]()), {'accepted': 1, 'updated': 2, 'skipped': 0})
        publish.assert_awaited_once()
        derive.assert_awaited_once()

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
        from app.main import health_data
        now = int(time.time()*1000)
        data = SimpleNamespace(assets=[{'updatedAt': now}], relations=[], signals=[],
                               visible_assets=lambda **_: [], stock_views=lambda **_: [])
        database = SimpleNamespace(all=AsyncMock(return_value=[]))
        collector = {'pid': os.getpid(), 'updatedAt': now, 'ageMs': 0, 'ok': True,
                     'tasks': {'liveQuotes': {'status': 'waiting'}}}
        projection = {'pid': os.getpid(), 'updatedAt': now, 'ageMs': 0, 'ok': True,
                      'tasks': {'realtimeProjection': {'status': 'waiting'}}}
        with patch('app.state.reload_if_stale', AsyncMock()), patch('app.state.DATA', data), \
             patch('app.state._source_statuses', return_value=[]), patch('app.db.store', AsyncMock(return_value=database)), \
             patch('shutil.disk_usage', return_value=SimpleNamespace(total=100, free=90)):
            with patch('app.main.process_health', side_effect=[collector, projection]):
                healthy = await health_data()
            self.assertTrue(healthy['worker']['ok'])
            self.assertTrue(healthy['projection']['ok'])
            self.assertEqual(set(healthy['worker']['tasks']), {'liveQuotes', 'realtimeProjection'})
            data.stream_states = [{'provider': 'Chain RPC', 'id': 'chain-stream:4663',
                                   'chainId': '4663', 'status': 'catching-up',
                                   'queueDepth': 12, 'sourceLagMs': 20_000,
                                   'nearTipLagBlocks': 67_000}]
            with patch('app.main.process_health', side_effect=[collector, projection]):
                backlogged = await health_data()
            self.assertEqual(backlogged['status'], 'degraded')
            self.assertIn('chain-stream-degraded', backlogged['issues'])
            data.stream_states = []
            missing = {'ageMs': None, 'ok': False, 'tasks': {}}
            with patch('app.main.process_health', side_effect=[collector, missing]):
                degraded = await health_data()
            self.assertEqual(degraded['status'], 'degraded')
            self.assertIn('projection-unavailable', degraded['issues'])
            self.assertFalse(degraded['worker']['ok'])
            self.assertTrue(degraded['collector']['ok'])

    async def test_optional_entitlement_keeps_core_healthy_but_reports_reference_gap(self):
        from app.main import health_data
        now=int(time.time()*1000)
        stock={'price':12,'stockPrice':100,'referenceScope':'issuer-reference',
               'fieldTimes':{'price':now},'quoteAt':now}
        data=SimpleNamespace(assets=[{'updatedAt':now}],relations=[],signals=[],
                             visible_assets=lambda **_:[],stock_views=lambda **_:[stock])
        database=SimpleNamespace(all=AsyncMock(return_value=[]))
        running={'pid':os.getpid(),'updatedAt':now,'ageMs':0,'ok':True,'tasks':{'maintenance':{'status':'waiting'}}}
        with patch('app.state.reload_if_stale',AsyncMock()),patch('app.state.DATA',data), \
             patch('app.state._source_statuses',return_value=[{'id':'eodhd:exchange','provider':'EODHD','kind':'stock-references','status':'entitlement-required'}]), \
             patch('app.db.store',AsyncMock(return_value=database)), \
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
        from app.main import health_data
        now = int(time.time()*1000)
        priced = {'kind':'candidate','price':1,'fieldTimes':{'price':now},'quoteAt':now}
        unavailable = {'kind':'candidate','price':None,'quoteStatus':'no-verified-market',
                       'quoteReason':'insufficient-liquidity'}
        stock_priced = {'price':2,'fieldTimes':{'price':now},'quoteAt':now}
        stock_unavailable = {'price':None,'quoteStatus':'no-verified-market',
                             'quoteReason':'source-returned-no-price'}
        data = SimpleNamespace(assets=[{'updatedAt':now}],relations=[],signals=[],
                               visible_assets=lambda **_:[priced,unavailable],
                               stock_views=lambda **_:[stock_priced,stock_unavailable])
        database = SimpleNamespace(all=AsyncMock(return_value=[]))
        running = {'pid':os.getpid(),'updatedAt':now,'ageMs':0,'ok':True,
                   'tasks':{'maintenance':{'status':'waiting'}}}
        with patch('app.state.reload_if_stale',AsyncMock()),patch('app.state.DATA',data), \
             patch('app.state._source_statuses',return_value=[]), \
             patch('app.db.store',AsyncMock(return_value=database)), \
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
        from app.main import health_data
        now=int(time.time()*1000)
        data=SimpleNamespace(assets=[{'updatedAt':now}],relations=[],signals=[],
                             visible_assets=lambda **_:[],stock_views=lambda **_:[])
        async def rows(kind):
            return [{'status':'partial','unsupportedPools':3,'failedPools':0}] if kind=='scan' else []
        database=SimpleNamespace(all=AsyncMock(side_effect=rows))
        running={'pid':os.getpid(),'updatedAt':now,'ageMs':0,'ok':True,
                 'tasks':{'discovery':{'status':'waiting'}}}
        with patch('app.state.reload_if_stale',AsyncMock()),patch('app.state.DATA',data), \
             patch('app.state._source_statuses',return_value=[]), \
             patch('app.db.store',AsyncMock(return_value=database)), \
             patch('shutil.disk_usage',return_value=SimpleNamespace(total=100,free=50)), \
             patch('app.main.disk_history',return_value=[]), \
             patch('app.main.process_health',side_effect=[running,running]):
            health=await health_data()
        self.assertTrue(health['ok'])
        self.assertEqual(health['status'],'limited')
        self.assertIn('discovery-unsupported-pools',health['warnings'])
        self.assertEqual(health['discovery']['196']['scanQuality']['unsupportedPools'],3)
