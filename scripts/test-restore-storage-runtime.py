#!/usr/bin/env python3
"""No-network restoration checks with synthetic sources, receipts and PM2 state."""
import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


def load(name):
    path = Path(__file__).with_name(name + '.py')
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.module = load('restore-storage-runtime')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.backup = self.root / '.releases/storage-runtime-test'
        self.backup.mkdir(parents=True)
        self.cutover = self.root / '.releases/storage-cutover-test'
        self.cutover.mkdir()
        self.files = frozenset(('package.json', 'server-py/app/old.py', 'server-py/app/new.py'))
        self.manifest = {'format':'cliperx-full-storage-runtime-v1', 'files':{}}
        for name in sorted(self.files):
            current = self.root / name
            current.parent.mkdir(parents=True, exist_ok=True)
            current.write_bytes(b'after:' + name.encode())
            before = None
            if not name.endswith('new.py'):
                saved = self.backup / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(b'before:' + name.encode())
                before = hashlib.sha256(saved.read_bytes()).hexdigest()
            self.manifest['files'][name] = {'before': before,
                'after': hashlib.sha256(current.read_bytes()).hexdigest()}
        (self.backup / 'runtime-release.json').write_text(json.dumps(self.manifest))
        settings = self.root / 'data/architecture-runtime.json'
        settings.parent.mkdir()
        settings.write_text(json.dumps({'managedBy':'cliperx-architecture-v1',
            'dsn':'fake-private-configuration', 'storage':{'enabled':False}}))
        settings_sha = hashlib.sha256(settings.read_bytes()).hexdigest()
        (self.cutover / 'receipt.json').write_text(json.dumps({
            'format':'cliperx-full-storage-cutover-v1', 'backup':str(self.cutover),
            'originalConfigSha256':settings_sha}))
        self.receipt = self.cutover / 'rollback-receipt.json'
        self.rollback = {'format':'cliperx-full-storage-rollback-v1',
            'phase':'restored-awaiting-explicit-service-start', 'settingsRestored':True,
            'processesRestarted':False, 'historyRetentionChanged':False,
            'restoredConfigSha256':settings_sha, 'cutoverReceipt':str(self.cutover / 'receipt.json'),
            'domains':[{'domain':domain, 'forwardReceipt':{'schema':'cliperx_storage_'+domain,
                'runtimeCutover':False, 'tables':[{'table':'fixture', 'match':True}]}}
                for domain in self.module.DOMAINS]}
        self.receipt.write_text(json.dumps(self.rollback))
        self.entries = [{'name':name, 'pid':0,
            'pm2_env':{'status':'stopped', 'pm_cwd':str(self.root)}} for name in self.module.SERVICES]
        self.addCleanup(patch.stopall)
        patch.object(self.module, 'ROOT', self.root).start()
        patch.object(self.module, 'FILES', self.files).start()
        patch.object(self.module.Path, 'cwd', return_value=self.root).start()
        self.runner = patch.object(self.module.subprocess, 'run', side_effect=self.pm2).start()
        self.dependencies = self.root / 'node_modules/pg/retained.json'
        self.dependencies.parent.mkdir(parents=True)
        self.dependencies.write_bytes(b'additive-existing-dependency')

    def pm2(self, command, **kwargs):
        self.assertEqual(command[-1], 'jlist')
        return subprocess.CompletedProcess(command, 0, json.dumps(self.entries), '')

    def restore(self, perform=True):
        return self.module.restore(self.backup, self.receipt, perform)

    def snapshot(self):
        return {name:self.module.digest(self.root/name) for name in self.files}

    def save_manifest(self):
        (self.backup/'runtime-release.json').write_text(json.dumps(self.manifest))

    def test_static_allowlist_matches_publisher_41_exact_files(self):
        actual = load('restore-storage-runtime').FILES
        self.assertEqual(len(actual), 41)
        self.assertEqual(actual, load('publish-storage-runtime').FILES)

    def test_preflight_changes_no_source_and_no_receipt(self):
        before = self.snapshot()
        result = self.restore(False)
        self.assertTrue(result['preflightPassed'])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.backup/'source-restore-receipt.json').exists())

    def test_restore_originals_remove_only_owned_new_sources_retain_dependencies(self):
        result = self.restore()
        self.assertFalse(result['processesRestarted'])
        self.assertTrue(result['dependenciesRetained'])
        self.assertEqual(self.snapshot(), {name:hashes['before'] for name,hashes in self.manifest['files'].items()})
        self.assertEqual(self.dependencies.read_bytes(), b'additive-existing-dependency')
        receipt = json.loads((self.backup/'source-restore-receipt.json').read_text())
        self.assertEqual(receipt['phase'], 'restored-awaiting-explicit-service-start')
        self.assertEqual((self.backup/'source-restore-receipt.json').stat().st_mode & 0o777, 0o600)

    def test_mixed_before_after_and_already_absent_new_source_resume(self):
        (self.root/'package.json').write_bytes((self.backup/'package.json').read_bytes())
        (self.root/'server-py/app/new.py').unlink()
        self.restore()
        self.restore()  # Complete retries are idempotent, too.
        self.assertEqual(self.snapshot(), {name:hashes['before'] for name,hashes in self.manifest['files'].items()})

    def test_unknown_current_hash_refuses_every_source_before_mutation(self):
        (self.root/'server-py/app/old.py').write_bytes(b'unknown-user-change')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'unowned-source'):
            self.restore()
        self.assertEqual(self.snapshot(), before)
        receipt = json.loads((self.backup/'source-restore-receipt.json').read_text())
        self.assertEqual(receipt['phase'], 'restore-failed-writers-remain-stopped')
        self.assertNotIn('fake-private', json.dumps(receipt))

    def test_corrupt_backup_refuses_every_source_before_mutation(self):
        (self.backup/'server-py/app/old.py').write_bytes(b'corrupt')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'unowned-source'):
            self.restore()
        self.assertEqual(self.snapshot(), before)

    def test_missing_or_extra_allowlist_entry_rejected(self):
        for extra in (True, False):
            original = copy.deepcopy(self.manifest)
            if extra:
                self.manifest['files']['unexpected.env'] = {'before':None, 'after':'a'*64}
            else:
                self.manifest['files'].pop('package.json')
            self.save_manifest()
            with self.assertRaisesRegex(ValueError, 'allowlist'):
                self.restore()
            self.manifest = original
        self.save_manifest()

    def test_symlink_source_backup_or_parent_rejected(self):
        before = self.snapshot()
        current = self.root/'server-py/app/old.py'
        contents = current.read_bytes()
        current.unlink()
        current.symlink_to(self.backup/'server-py/app/old.py')
        with self.assertRaisesRegex(ValueError, 'linked'):
            self.restore()
        current.unlink(); current.write_bytes(contents)
        saved = self.backup/'package.json'
        saved.unlink(); saved.symlink_to(self.root/'package.json')
        with self.assertRaisesRegex(ValueError, 'linked'):
            self.restore()
        self.assertEqual(self.snapshot(), before)

    def test_external_backup_path_rejected_without_mutation(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.module.restore(self.root/'other', self.receipt, True)
        self.assertEqual(self.snapshot(), before)

    def test_enabled_authority_or_uncertified_undo_rejected(self):
        self.rollback['settingsRestored'] = False
        self.receipt.write_text(json.dumps(self.rollback))
        with self.assertRaisesRegex(ValueError, 'certified'):
            self.restore()
        self.rollback['settingsRestored'] = True
        self.receipt.write_text(json.dumps(self.rollback))
        settings = self.root/'data/architecture-runtime.json'
        config = json.loads(settings.read_text()); config['storage']['enabled'] = True
        settings.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'certified'):
            self.restore()

    def test_running_required_or_recovery_process_rejected(self):
        before = self.snapshot()
        self.entries[0]['pid'] = 123
        with self.assertRaisesRegex(ValueError, 'barrier'):
            self.restore()
        self.entries[0]['pid'] = 0
        self.entries.append({'name':'pyradar-storage-recovery', 'pid':444,
            'pm2_env':{'status':'online', 'pm_cwd':str(self.root)}})
        with self.assertRaisesRegex(ValueError, 'still-running'):
            self.restore()
        self.assertEqual(self.snapshot(), before)

    def test_partial_failure_has_durable_receipt_and_resumes_mixed_hashes(self):
        original = self.module.restore_file
        calls = []
        def fail_second(*args):
            calls.append(args[0])
            if len(calls) == 2:
                raise OSError('synthetic-no-private-values')
            return original(*args)
        with patch.object(self.module, 'restore_file', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.restore()
        receipt = json.loads((self.backup/'source-restore-receipt.json').read_text())
        self.assertEqual(receipt['errorClass'], 'OSError')
        self.assertFalse(receipt['processesRestarted'])
        self.restore()
        self.assertEqual(self.snapshot(), {name:hashes['before'] for name,hashes in self.manifest['files'].items()})

    def test_cli_holds_all_three_deployment_locks(self):
        original = self.module.fcntl.flock
        arguments = ['restore-storage-runtime.py', '--backup', str(self.backup),
                     '--rollback-receipt', str(self.receipt)]
        with patch('sys.argv', arguments), redirect_stdout(io.StringIO()) as output:
            with patch.object(self.module.fcntl, 'flock', wraps=original) as acquire:
                self.module.main()
        self.assertEqual(acquire.call_count, 3)
        self.assertTrue(json.loads(output.getvalue())['preflightPassed'])

    def test_contended_deployment_lock_refuses_cli_before_sources_change(self):
        before = self.snapshot()
        lock = self.root/'.releases/architecture-deploy.lock'
        arguments = ['restore-storage-runtime.py', '--backup', str(self.backup),
                     '--rollback-receipt', str(self.receipt), '--restore']
        with open(lock, 'a') as owner:
            self.module.fcntl.flock(owner, self.module.fcntl.LOCK_EX | self.module.fcntl.LOCK_NB)
            with patch('sys.argv', arguments):
                with self.assertRaisesRegex(SystemExit, 'BlockingIOError'):
                    self.module.main()
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
