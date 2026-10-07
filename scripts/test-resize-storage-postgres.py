#!/usr/bin/env python3
"""No-network Docker state-machine tests for the PostgreSQL resize script.

Run with Python's standard library: python3 scripts/test-resize-storage-postgres.py
Every Docker/SQL call is an in-memory fixture; no real service or credential is
read. Tests exercise the actual validation, apply and rollback functions.
"""
import copy
import importlib.util
import io
import json
import stat
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


OLD = 'a'*64
NEW = 'b'*64
IMAGE = 'sha256:'+'c'*64
SECRET = 'fake-secret-never-print-this'


def load_script():
    path = Path(__file__).with_name('resize-storage-postgres.py')
    spec = importlib.util.spec_from_file_location('resize_storage_postgres_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self):
        self.elapsed = 0

    def monotonic(self):
        return self.elapsed

    def sleep(self, duration):
        self.elapsed += duration


class FakeDocker:
    def __init__(self, module):
        self.module = module
        self.events = []
        self.failure = None
        self.client_samples = []
        self.original = {
            'Id': OLD, 'Name': '/cliperx-postgres', 'Image': IMAGE,
            'Config': {'Labels': {'com.cliperx.owner': 'architecture-v1'},
                'Image': 'postgres:16-alpine', 'Entrypoint': ['docker-entrypoint.sh'],
                'User': '', 'Hostname': 'original-hostname',
                'Env': ['POSTGRES_USER=cliperx', 'POSTGRES_DB=cliperx',
                        'PGDATA=/var/lib/postgresql/data', 'POSTGRES_PASSWORD='+SECRET,
                        'PATH=/usr/local/bin:/usr/bin'],
                'Cmd': ['postgres', '-c', 'max_connections=20', '-c', 'shared_buffers=64MB',
                        '-c', 'work_mem=4MB', '-c', 'max_wal_size=256MB', '-c', 'wal_compression=on']},
            'HostConfig': {'Binds': [str(module.SOURCE)+':'+module.DESTINATION],
                'PortBindings': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '5434'}]},
                'NetworkMode': 'default', 'IpcMode': 'private', 'Memory': 512*1024**2,
                'NanoCpus': 1_000_000_000, 'ShmSize': 64*1024**2,
                'RestartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0},
                'LogConfig': {'Type': 'json-file', 'Config': {'max-size': '10m'}}},
            'Mounts': [{'Type': 'bind', 'Source': str(module.SOURCE),
                        'Destination': module.DESTINATION, 'RW': True, 'Propagation': 'rprivate'}],
            'NetworkSettings': {'Networks': {'bridge': {}}},
            'State': {'Running': True, 'Paused': False, 'Restarting': False, 'Dead': False},
        }
        self.containers = {OLD: self.original}

    def find(self, value):
        return next((container for key, container in self.containers.items()
                     if key == value or container['Name'] == '/'+value), None)

    def profile(self, identity):
        new = identity == NEW
        clients = self.client_samples.pop(0) if self.client_samples else 0
        value = {'maxConnections': 80 if new else 20,
                 'sharedBuffersBytes': (128 if new else 64)*1024**2,
                 'workMemBytes': (2 if new else 4)*1024**2,
                 'maintenanceWorkMemBytes': 64*1024**2,
                 'maxWalSizeBytes': 2*1024**3 if new else 256*1024**2,
                 'otherClientConnections': clients}
        if new and self.failure in ('postcheck', 'wal-postcheck'):
            value['maxWalSizeBytes' if self.failure == 'wal-postcheck' else 'maxConnections'] = 1
        return value

    def __call__(self, *arguments, timeout=20, check=True):
        self.events.append(arguments)
        if arguments[:2] == ('container', 'inspect'):
            container = self.find(arguments[2])
            if container:
                return subprocess.CompletedProcess(arguments, 0, json.dumps([copy.deepcopy(container)]), '')
            self.events.append(('absence-confirmed', arguments[2]))
            return subprocess.CompletedProcess(arguments, 1, '', 'No such container')
        if arguments[0] == 'exec':
            identity = arguments[1]
            if arguments[2] == 'psql':
                query = arguments[-1]
                assert 'BEGIN READ ONLY' in query
                assert "current_setting('max_wal_size')" in query
                return subprocess.CompletedProcess(arguments, 0, json.dumps(self.profile(identity)), '')
            if arguments[2] == 'pg_isready':
                assert timeout <= 2
                return subprocess.CompletedProcess(arguments, 1 if identity == NEW and self.failure == 'ready' else 0, '', '')
        if arguments[0] == 'stop':
            self.find(arguments[-1])['State']['Running'] = False
            return subprocess.CompletedProcess(arguments, 0, '', '')
        if arguments[0] == 'update':
            container = self.find(arguments[-1]); name = arguments[2]
            parts = name.split(':')
            container['HostConfig']['RestartPolicy'] = {'Name': parts[0], 'MaximumRetryCount': int(parts[1]) if len(parts)>1 else 0}
            return subprocess.CompletedProcess(arguments, 0, '', '')
        if arguments[0] == 'rename':
            self.find(arguments[1])['Name'] = '/'+arguments[2]
            return subprocess.CompletedProcess(arguments, 0, '', '')
        if arguments[0] == 'create':
            new = copy.deepcopy(self.original)
            new['Id'] = NEW; new['Name'] = '/cliperx-postgres'
            new['State']['Running'] = False
            new['Config']['Image'] = IMAGE
            new['HostConfig']['Memory'] = int(arguments[arguments.index('--memory')+1])
            new['HostConfig']['NanoCpus'] = int(float(arguments[arguments.index('--cpus')+1])*1e9)
            policy = arguments[arguments.index('--restart')+1].split(':')
            new['HostConfig']['RestartPolicy'] = {'Name': policy[0], 'MaximumRetryCount': int(policy[1]) if len(policy)>1 else 0}
            host_ip, host_port, port = arguments[arguments.index('-p')+1].split(':')
            new['HostConfig']['PortBindings'] = {port+'/tcp': [{'HostIp': host_ip, 'HostPort': host_port}]}
            bind = arguments[arguments.index('-v')+1]
            source, destination = bind.split(':')
            new['HostConfig']['Binds'] = [bind]
            new['Mounts'] = [{'Type': 'bind', 'Source': source, 'Destination': destination,
                              'RW': True, 'Propagation': 'rprivate'}]
            new['HostConfig']['ShmSize'] = int(arguments[arguments.index('--shm-size')+1])
            new['HostConfig']['NetworkMode'] = arguments[arguments.index('--network')+1]
            new['HostConfig']['LogConfig'] = {'Type': arguments[arguments.index('--log-driver')+1], 'Config': {}}
            for index, value in enumerate(arguments):
                if value == '--log-opt':
                    key, content = arguments[index+1].split('=', 1)
                    new['HostConfig']['LogConfig']['Config'][key] = content
            new['Config']['Labels'] = {}
            for index, value in enumerate(arguments):
                if value == '--label':
                    key, content = arguments[index+1].split('=', 1); new['Config']['Labels'][key] = content
            env_file = Path(arguments[arguments.index('--env-file')+1])
            assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
            new['Config']['Env'] = env_file.read_text().splitlines()
            new['Config']['Cmd'] = list(arguments[arguments.index(IMAGE)+1:])
            self.containers[NEW] = new
            if self.failure == 'create':
                raise self.module.ResizeError('fake-create-failed-after-creation')
            return subprocess.CompletedProcess(arguments, 0, NEW+'\n', '')
        if arguments[0] == 'start':
            container = self.find(arguments[1])
            # This assertion proves the algorithm never starts both physical
            # instances against the same data volume, including rollback.
            assert not any(value['State']['Running'] for key, value in self.containers.items() if key != container['Id'])
            container['State']['Running'] = True
            if container['Id'] == NEW and self.failure in ('start', 'foreign-new', 'remove-fails'):
                if self.failure == 'foreign-new':
                    container['Config']['Labels']['com.cliperx.owner'] = 'foreign-project'
                raise self.module.ResizeError('fake-new-start-failed-after-start')
            return subprocess.CompletedProcess(arguments, 0, '', '')
        if arguments[0] == 'rm':
            if self.failure != 'remove-fails':
                del self.containers[arguments[-1]]
            return subprocess.CompletedProcess(arguments, 0, '', '')
        raise AssertionError('Unhandled fake Docker command: '+repr(arguments))


class ResizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cliperx-resize-fake-')
        self.addCleanup(self.temp.cleanup)
        self.module = load_script()
        root = Path(self.temp.name).resolve()/'project'
        (root/'.releases').mkdir(parents=True)
        source = root/'data/architecture/postgres'; source.mkdir(parents=True)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(self.module, 'ROOT', root))
        self.stack.enter_context(patch.object(self.module, 'SOURCE', source))
        self.fake = FakeDocker(self.module)
        self.clock = Clock()
        self.stack.enter_context(patch.object(self.module, 'docker', self.fake))
        self.stack.enter_context(patch.object(self.module.time, 'monotonic', self.clock.monotonic))
        self.stack.enter_context(patch.object(self.module.time, 'sleep', self.clock.sleep))

    def apply(self):
        return self.module.run(apply=True, maintenance_window=True)

    def assert_no_container_change(self):
        self.assertFalse(any(event[0] in ('stop', 'update', 'rename', 'create', 'start', 'rm') for event in self.fake.events))
        self.assertTrue(self.fake.containers[OLD]['State']['Running'])

    def test_readonly_default_reports_wal_target_without_modifying_container(self):
        receipt = self.module.run()
        self.assertFalse(receipt['modified'])
        self.assertEqual(receipt['target']['maxWalSizeBytes'], 2*1024**3)
        self.assert_no_container_change()

    def test_wrong_ownership_rejected_before_sql_or_mutation(self):
        self.fake.original['Config']['Labels']['com.cliperx.owner'] = 'another-project'
        with self.assertRaisesRegex(self.module.ResizeError, 'ownership'):
            self.apply()
        self.assert_no_container_change()

    def test_wrong_localhost_port_rejected_before_mutation(self):
        self.fake.original['HostConfig']['PortBindings']['5432/tcp'][0]['HostPort'] = '5432'
        with self.assertRaisesRegex(self.module.ResizeError, 'port'):
            self.apply()
        self.assert_no_container_change()

    def test_public_bind_rejected_before_mutation(self):
        self.fake.original['HostConfig']['PortBindings']['5432/tcp'][0]['HostIp'] = '0.0.0.0'
        with self.assertRaisesRegex(self.module.ResizeError, 'port'):
            self.apply()
        self.assert_no_container_change()

    def test_wrong_mount_rejected_before_mutation(self):
        self.fake.original['Mounts'][0]['Source'] = '/another-project/database'
        with self.assertRaisesRegex(self.module.ResizeError, 'mount'):
            self.apply()
        self.assert_no_container_change()

    def test_unknown_capability_is_rejected_not_discarded(self):
        self.fake.original['HostConfig']['CapAdd'] = ['SYS_ADMIN']
        with self.assertRaisesRegex(self.module.ResizeError, 'host-option'):
            self.apply()
        self.assert_no_container_change()

    def test_active_clients_and_arriving_clients_are_rejected(self):
        for clients in ([1], [0, 1]):
            with self.subTest(clients=clients):
                self.fake.events.clear(); self.fake.client_samples = list(clients)
                with self.assertRaisesRegex(self.module.ResizeError, 'clients'):
                    self.apply()
                self.assert_no_container_change()

    def test_apply_requires_explicit_maintenance_window(self):
        with self.assertRaisesRegex(self.module.ResizeError, 'maintenance-window'):
            self.module.run(apply=True)
        self.assert_no_container_change()

    def test_success_preserves_stopped_backup_environment_volume_and_wal_durability(self):
        receipt = self.apply()
        self.assertEqual(receipt['postgres']['maxConnections'], 80)
        self.assertEqual(receipt['postgres']['maxWalSizeBytes'], 2*1024**3)
        self.assertTrue(self.fake.containers[NEW]['State']['Running'])
        self.assertFalse(self.fake.containers[OLD]['State']['Running'])
        self.assertEqual(self.fake.containers[OLD]['HostConfig']['RestartPolicy']['Name'], 'no')
        self.assertEqual(self.fake.containers[NEW]['HostConfig']['RestartPolicy']['Name'], 'unless-stopped')
        self.assertEqual(self.fake.containers[NEW]['HostConfig']['LogConfig'], self.fake.containers[OLD]['HostConfig']['LogConfig'])
        self.assertEqual(self.fake.containers[NEW]['Mounts'], self.fake.containers[OLD]['Mounts'])
        self.assertEqual(self.fake.containers[NEW]['Config']['Env'], self.fake.containers[OLD]['Config']['Env'])
        settings = self.module.command_settings(self.fake.containers[NEW]['Config']['Cmd'])
        self.assertEqual(settings['wal_compression'], 'on')
        self.assertNotIn('fsync', settings)
        self.assertNotIn('full_page_writes', settings)
        self.assertEqual(settings['max_wal_size'], '2GB')
        backup = Path(receipt['serverPrivateBackup'])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        for name in ('original-inspect.json', 'original-environment.env', 'receipt.json'):
            self.assertEqual(stat.S_IMODE((backup/name).stat().st_mode), 0o600)
        self.assertIn(SECRET, (backup/'original-inspect.json').read_text())
        self.assertNotIn(SECRET, json.dumps(receipt))
        self.assertFalse(any(event[0] == 'rm' for event in self.fake.events))

    def test_every_new_container_failure_removes_and_confirms_gone_before_restoring_old(self):
        for failure in ('create', 'start', 'ready', 'postcheck', 'wal-postcheck'):
            with self.subTest(failure=failure):
                # Reset only fake state; real apply/rollback paths remain used.
                self.fake = FakeDocker(self.module)
                self.fake.failure = failure
                with patch.object(self.module, 'docker', self.fake):
                    with self.assertRaisesRegex(self.module.ResizeError, 'original-container-restored'):
                        self.apply()
                self.assertNotIn(NEW, self.fake.containers)
                self.assertEqual(self.fake.containers[OLD]['Name'], '/cliperx-postgres')
                self.assertTrue(self.fake.containers[OLD]['State']['Running'])
                self.assertEqual(self.fake.containers[OLD]['HostConfig']['RestartPolicy']['Name'], 'unless-stopped')
                events = self.fake.events
                removed = next(index for index, event in enumerate(events) if event == ('rm', '--force', NEW))
                gone = next(index for index, event in enumerate(events) if index>removed and event == ('absence-confirmed', NEW))
                restored = next(index for index, event in enumerate(events) if event == ('rename', OLD, 'cliperx-postgres'))
                started = next(index for index, event in enumerate(events) if event == ('start', OLD))
                self.assertLess(removed, gone); self.assertLess(gone, restored); self.assertLess(restored, started)
                self.assertFalse(any(event[0]=='rm' and '--volumes' in event for event in events))
                self.assertFalse(any(event[0]=='rm' and '-v' in event for event in events))

    def test_original_on_failure_retry_policy_restored_exactly(self):
        self.fake.original['HostConfig']['RestartPolicy'] = {'Name': 'on-failure', 'MaximumRetryCount': 5}
        self.fake.failure = 'start'
        with self.assertRaisesRegex(self.module.ResizeError, 'original-container-restored'):
            self.apply()
        self.assertEqual(self.fake.containers[OLD]['HostConfig']['RestartPolicy'], {'Name': 'on-failure', 'MaximumRetryCount': 5})

    def test_foreign_new_container_is_never_removed_and_old_is_not_started(self):
        self.fake.failure = 'foreign-new'
        with self.assertRaisesRegex(self.module.ResizeError, 'operator-recovery'):
            self.apply()
        self.assertIn(NEW, self.fake.containers)
        self.assertFalse(self.fake.containers[OLD]['State']['Running'])
        self.assertFalse(any(event[0]=='rm' or event==('start', OLD) for event in self.fake.events))

    def test_failure_to_confirm_new_removal_never_starts_old(self):
        self.fake.failure = 'remove-fails'
        with self.assertRaisesRegex(self.module.ResizeError, 'operator-recovery'):
            self.apply()
        self.assertIn(NEW, self.fake.containers)
        self.assertFalse(self.fake.containers[OLD]['State']['Running'])
        self.assertFalse(any(event==('start', OLD) for event in self.fake.events))

    def test_ready_polling_is_bounded_to_30_seconds(self):
        self.fake.failure = 'ready'
        with self.assertRaisesRegex(self.module.ResizeError, 'original-container-restored'):
            self.apply()
        self.assertLessEqual(self.clock.elapsed, 30)
        self.assertGreaterEqual(self.clock.elapsed, 29.9)

    def test_main_output_does_not_include_private_environment(self):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.argv', ['resize-storage-postgres.py', '--apply', '--maintenance-window']), \
             patch.object(self.module.signal, 'signal'), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(self.module.main(), 0)
        self.assertNotIn(SECRET, out.getvalue()+err.getvalue())
        self.assertNotIn('POSTGRES_PASSWORD', out.getvalue()+err.getvalue())
        self.assertNotIn(SECRET, repr(self.fake.events))


if __name__ == '__main__':
    unittest.main(verbosity=2)
