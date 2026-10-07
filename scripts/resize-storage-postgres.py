#!/usr/bin/env python3
"""Rebuild only the owned main PostgreSQL container in a stopped-writer window.

Docker's original ``postgres -c max_connections=20`` overrides ALTER SYSTEM.
This script therefore retains the exact image, private environment, volume,
localhost port and WAL settings while replacing the container's resource/CLI
limits. It never drops the volume or starts both containers on the same data.

Default: read-only inspection. Apply, only after stopping migration, read-model
and PostgreSQL clients: ``python3 scripts/resize-storage-postgres.py --apply
--maintenance-window``. Complete container inspection/environment backups stay
inside a mode-0700 server directory, with mode-0600 files. Do not download them.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path('/opt/memedashboard')
CONTAINER = 'cliperx-postgres'
OWNER_LABEL = 'com.cliperx.owner'
OPERATION_LABEL = 'com.cliperx.storage-resize'
OWNER = 'architecture-v1'
SOURCE = ROOT / 'data/architecture/postgres'
DESTINATION = '/var/lib/postgresql/data'
MEMORY = 1024**3
CPU = 2_000_000_000
SETTINGS = {'max_connections': '80', 'shared_buffers': '128MB',
            'work_mem': '2MB', 'maintenance_work_mem': '64MB',
            'max_wal_size': '2GB'}


class ResizeError(RuntimeError):
    """Messages are fixed codes: never include Docker/SQL private output."""


def docker(*arguments, timeout=20, check=True):
    try:
        result = subprocess.run(['sudo', '-n', 'docker', *arguments],
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ResizeError('docker-command-timeout') from None
    except OSError:
        raise ResizeError('docker-command-unavailable') from None
    if check and result.returncode:
        raise ResizeError('docker-command-failed') from None
    return result


def inspect(identity, *, optional=False):
    result = docker('container', 'inspect', identity, check=False)
    if result.returncode:
        if optional and any(text in result.stderr.lower() for text in
                            ('no such object', 'no such container')):
            return None
        raise ResizeError('container-inspection-failed')
    try:
        values = json.loads(result.stdout)
        if len(values) != 1 or not isinstance(values[0], dict):
            raise ValueError()
        return values[0]
    except (TypeError, ValueError):
        raise ResizeError('invalid-container-inspection') from None


def command_settings(command):
    if (not isinstance(command, list) or not command or command[0] != 'postgres'
            or (len(command)-1) % 2):
        raise ResizeError('unexpected-postgres-command')
    settings = {}
    for index in range(1, len(command), 2):
        value = command[index+1]
        if (command[index] != '-c' or not isinstance(value, str) or '=' not in value
                or '\n' in value or '\r' in value or '\x00' in value):
            raise ResizeError('unexpected-postgres-command-option')
        key, content = value.split('=', 1)
        if not re.fullmatch(r'[a-z][a-z0-9_]*', key) or key in settings:
            raise ResizeError('duplicate-or-invalid-postgres-setting')
        settings[key] = content
    return settings


def environment(config):
    values = config.get('Env')
    if not isinstance(values, list):
        raise ResizeError('missing-private-container-environment')
    result = {}
    for value in values:
        if not isinstance(value, str) or '=' not in value or any(char in value for char in ('\n', '\r', '\x00')):
            raise ResizeError('unsupported-private-container-environment')
        key, content = value.split('=', 1)
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or key in result:
            raise ResizeError('duplicate-or-invalid-container-environment')
        result[key] = content
    if (result.get('POSTGRES_USER') != 'cliperx' or result.get('POSTGRES_DB') != 'cliperx'
            or result.get('PGDATA') != DESTINATION or not result.get('POSTGRES_PASSWORD')):
        raise ResizeError('unexpected-project-postgres-environment')
    return result


def validate(value, *, expected_id=None, expected_image=None, operation=None,
             expected_name=CONTAINER):
    config, host = value.get('Config', {}), value.get('HostConfig', {})
    identity = value.get('Id', '')
    if not re.fullmatch(r'[0-9a-f]{64}', identity) or expected_id and identity != expected_id:
        raise ResizeError('container-identity-mismatch')
    if value.get('Name') != '/' + expected_name:
        raise ResizeError('container-name-mismatch')
    labels = config.get('Labels') or {}
    if (labels.get(OWNER_LABEL) != OWNER or set(labels) - {OWNER_LABEL, OPERATION_LABEL}
            or OPERATION_LABEL in labels and not re.fullmatch(r'[0-9a-f]{32}', labels[OPERATION_LABEL])):
        raise ResizeError('container-ownership-mismatch')
    if operation is not None and labels.get(OPERATION_LABEL) != operation:
        raise ResizeError('container-operation-ownership-mismatch')
    image = value.get('Image', '')
    if (not re.fullmatch(r'sha256:[0-9a-f]{64}', image)
            or expected_image and image != expected_image
            or config.get('Image') not in ('postgres:16-alpine', image)
            or config.get('Entrypoint') != ['docker-entrypoint.sh']
            or config.get('User') not in ('', None)):
        raise ResizeError('container-image-or-entrypoint-mismatch')
    environment(config)
    command_settings(config.get('Cmd'))
    mounts = value.get('Mounts') or []
    expected_source = str(SOURCE)
    if SOURCE.resolve() != SOURCE or not SOURCE.is_dir():
        raise ResizeError('project-storage-path-mismatch')
    if (len(mounts) != 1 or mounts[0].get('Type') != 'bind'
            or mounts[0].get('Source') != expected_source
            or mounts[0].get('Destination') != DESTINATION
            or mounts[0].get('RW') is not True
            or mounts[0].get('Propagation') != 'rprivate'):
        raise ResizeError('container-storage-mount-mismatch')
    if host.get('Binds') not in ([expected_source + ':' + DESTINATION],
                                 [expected_source + ':' + DESTINATION + ':rw']):
        raise ResizeError('container-bind-configuration-mismatch')
    if host.get('PortBindings') != {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '5434'}]}:
        raise ResizeError('container-port-ownership-mismatch')
    if (host.get('NetworkMode') not in ('default', 'bridge')
            or set((value.get('NetworkSettings') or {}).get('Networks', {})) - {'bridge'}):
        raise ResizeError('container-network-mismatch')
    # Fail rather than silently discard a customized security, device, mount or
    # networking configuration when constructing the replacement container.
    for key in ('Privileged', 'ReadonlyRootfs', 'AutoRemove', 'PublishAllPorts',
                'OomKillDisable', 'Init', 'PidsLimit'):
        if host.get(key) not in (None, False, 0):
            raise ResizeError('unexpected-container-host-option')
    for key in ('CapAdd', 'CapDrop', 'Devices', 'DeviceRequests', 'VolumesFrom',
                'Tmpfs', 'Links', 'ExtraHosts', 'Dns', 'DnsOptions', 'DnsSearch',
                'SecurityOpt', 'Sysctls', 'Ulimits', 'Mounts'):
        if host.get(key):
            raise ResizeError('unexpected-container-host-option')
    for key in ('CpuShares', 'CpuPeriod', 'CpuQuota', 'CpuRealtimePeriod', 'CpuRealtimeRuntime',
                'MemoryReservation', 'CpusetCpus', 'CpusetMems', 'CgroupParent',
                'Cgroup', 'IpcMode', 'PidMode', 'UTSMode', 'UsernsMode'):
        expected = 'private' if key == 'IpcMode' else '' if key.endswith('Mode') or key in ('CpusetCpus', 'CpusetMems', 'CgroupParent', 'Cgroup') else 0
        if host.get(key) not in (None, expected):
            raise ResizeError('unexpected-container-resource-or-namespace-option')
    if host.get('IpcMode') not in ('private', ''):
        raise ResizeError('unexpected-container-ipc-mode')
    policy = host.get('RestartPolicy') or {}
    if (policy.get('Name') not in ('no', 'always', 'unless-stopped', 'on-failure')
            or not isinstance(policy.get('MaximumRetryCount'), int)
            or policy['MaximumRetryCount'] < 0):
        raise ResizeError('invalid-original-restart-policy')
    log = host.get('LogConfig') or {}
    if log.get('Type') not in ('json-file', 'local') or not isinstance(log.get('Config'), dict):
        raise ResizeError('unexpected-container-log-configuration')
    if any(not re.fullmatch(r'[a-zA-Z0-9_.-]+', key) or '\n' in str(content) or '\x00' in str(content)
           for key, content in log['Config'].items()):
        raise ResizeError('invalid-container-log-option')
    if operation:
        if host.get('Memory') != MEMORY or host.get('NanoCpus') != CPU:
            raise ResizeError('replacement-resource-verification-failed')
        actual = command_settings(config['Cmd'])
        if any(actual.get(key) != content for key, content in SETTINGS.items()):
            raise ResizeError('replacement-postgres-command-verification-failed')
    return identity


def restart_argument(policy):
    return ('on-failure:' + str(policy['MaximumRetryCount'])
            if policy['Name'] == 'on-failure' and policy['MaximumRetryCount']
            else policy['Name'])


def private_file(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def sql_profile(identity):
    query = """BEGIN READ ONLY;
SELECT json_build_object('maxConnections',current_setting('max_connections')::integer,
 'sharedBuffersBytes',pg_size_bytes(current_setting('shared_buffers')),
 'workMemBytes',pg_size_bytes(current_setting('work_mem')),
 'maintenanceWorkMemBytes',pg_size_bytes(current_setting('maintenance_work_mem')),
 'maxWalSizeBytes',pg_size_bytes(current_setting('max_wal_size')),
 'otherClientConnections',(SELECT count(*) FROM pg_stat_activity
  WHERE backend_type='client backend' AND pid<>pg_backend_pid()));
COMMIT;"""
    result = docker('exec', identity, 'psql', '-U', 'cliperx', '-d', 'cliperx',
                    '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-c', query, timeout=5)
    try:
        value = json.loads(result.stdout.strip())
        expected = {'maxConnections', 'sharedBuffersBytes', 'workMemBytes',
                    'maintenanceWorkMemBytes', 'maxWalSizeBytes', 'otherClientConnections'}
        if set(value) != expected or any(not isinstance(number, int) or number < 0 for number in value.values()):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ResizeError('invalid-readonly-postgres-profile') from None


def wait_ready(identity, seconds=30):
    deadline = time.monotonic() + seconds
    while (remaining := deadline-time.monotonic()) > 0:
        try:
            result = docker('exec', identity, 'pg_isready', '-U', 'cliperx', '-d', 'cliperx',
                            '-t', '1', timeout=min(2, remaining), check=False)
            if result.returncode == 0:
                return
        except ResizeError:
            pass
        time.sleep(min(.3, max(0, deadline-time.monotonic())))
    raise ResizeError('postgres-ready-deadline-exceeded')


def create_arguments(original, operation, env_file):
    host, config = original['HostConfig'], original['Config']
    settings = command_settings(config['Cmd'])
    settings.update(SETTINGS)
    arguments = ['create', '--pull', 'never', '--name', CONTAINER,
                 '--restart', restart_argument(host['RestartPolicy']),
                 '--memory', str(MEMORY), '--cpus', '2',
                 '--env-file', str(env_file), '-p', '127.0.0.1:5434:5432',
                 '-v', str(SOURCE)+':'+DESTINATION,
                 '--shm-size', str(host.get('ShmSize', 64*1024**2)),
                 '--network', 'bridge', '--log-driver', host['LogConfig']['Type']]
    for key, value in host['LogConfig']['Config'].items():
        arguments.extend(('--log-opt', key+'='+str(value)))
    labels = {**(config.get('Labels') or {}), OPERATION_LABEL: operation}
    for key, value in labels.items():
        arguments.extend(('--label', key+'='+value))
    if config.get('Hostname'):
        arguments.extend(('--hostname', config['Hostname']))
    arguments.extend((original['Image'], 'postgres'))
    for key, value in settings.items():
        arguments.extend(('-c', key+'='+value))
    return arguments


def rollback(original, backup_name, operation, replacement_id=None):
    identity = original['Id']
    current = inspect(CONTAINER, optional=True)
    if current and current['Id'] != identity:
        # Removal is permitted only for this exact owned operation. Never use
        # rm -v, and never start the old instance until the new one is gone.
        labels = (current.get('Config') or {}).get('Labels') or {}
        if (current.get('Name') != '/'+CONTAINER or labels.get(OWNER_LABEL) != OWNER
                or labels.get(OPERATION_LABEL) != operation
                or current.get('Image') != original['Image']
                or not re.fullmatch(r'[0-9a-f]{64}', current.get('Id', ''))
                or replacement_id is not None and current['Id'] != replacement_id):
            raise ResizeError('rollback-replacement-identity-not-owned')
        replacement = current['Id']
        docker('stop', '--time', '15', replacement, timeout=20, check=False)
        docker('rm', '--force', replacement)
        if inspect(replacement, optional=True) is not None:
            raise ResizeError('replacement-removal-not-confirmed')
    old = inspect(identity)
    name = old.get('Name', '').lstrip('/')
    if name not in (CONTAINER, backup_name):
        raise ResizeError('rollback-original-name-mismatch')
    validate(old, expected_id=identity, expected_image=original['Image'], expected_name=name)
    if old.get('State', {}).get('Running'):
        if name != CONTAINER:
            raise ResizeError('rollback-backup-unexpectedly-running')
    if name == backup_name:
        if inspect(CONTAINER, optional=True) is not None:
            raise ResizeError('rollback-original-name-still-occupied')
        docker('rename', identity, CONTAINER)
    docker('update', '--restart', restart_argument(original['HostConfig']['RestartPolicy']), identity)
    docker('start', identity)
    wait_ready(identity)
    validate(inspect(identity), expected_id=identity, expected_image=original['Image'])


def run(*, apply=False, maintenance_window=False):
    if not ROOT.is_dir() or ROOT.resolve() != ROOT:
        raise ResizeError('deployment-host-project-root-required')
    releases = ROOT / '.releases'
    if not releases.is_dir() or releases.resolve() != releases:
        raise ResizeError('deployment-host-release-directory-required')
    descriptor = os.open(releases/'architecture-services.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a'):
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ResizeError('architecture-services-operation-already-running') from None
        original = inspect(CONTAINER)
        identity = validate(original)
        state = original.get('State') or {}
        if state.get('Running') is not True or state.get('Paused') or state.get('Restarting') or state.get('Dead'):
            raise ResizeError('original-postgres-must-be-running-and-stable')
        before = sql_profile(identity)
        if not apply:
            return {'mode': 'read-only', 'container': CONTAINER, 'postgres': before,
                    'target': {'maxConnections': 80, 'memoryBytes': MEMORY, 'cpu': 2,
                               'sharedBuffersBytes': 128*1024**2, 'workMemBytes': 2*1024**2,
                               'maintenanceWorkMemBytes': 64*1024**2,
                               'maxWalSizeBytes': 2*1024**3}, 'modified': False}
        if not maintenance_window:
            raise ResizeError('explicit-maintenance-window-flag-required')
        if before['otherClientConnections']:
            raise ResizeError('postgres-clients-must-be-stopped-before-rebuild')
        operation = uuid.uuid4().hex
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup_name = CONTAINER+'-before-resize-'+stamp+'-'+operation[:8]
        if inspect(backup_name, optional=True) is not None:
            raise ResizeError('backup-container-name-collision')
        backup = releases / ('postgres-resize-'+stamp+'-'+operation[:8])
        backup.mkdir(mode=0o700)
        backup.chmod(0o700)
        private_file(backup/'original-inspect.json', json.dumps(original, indent=2))
        env_file = backup/'original-environment.env'
        private_file(env_file, ''.join(key+'='+value+'\n' for key, value in environment(original['Config']).items()))
        mutated = False
        created = None
        try:
            # Recheck ownership and clients immediately before the first change.
            validate(inspect(identity), expected_id=identity, expected_image=original['Image'])
            if sql_profile(identity)['otherClientConnections']:
                raise ResizeError('postgres-clients-arrived-before-rebuild')
            mutated = True
            docker('stop', '--time', '30', identity, timeout=35)
            if inspect(identity).get('State', {}).get('Running'):
                raise ResizeError('original-postgres-stop-not-confirmed')
            docker('update', '--restart', 'no', identity)
            docker('rename', identity, backup_name)
            created = docker(*create_arguments(original, operation, env_file)).stdout.strip()
            if not re.fullmatch(r'[0-9a-f]{64}', created):
                raise ResizeError('invalid-replacement-container-id')
            replacement = inspect(created)
            validate(replacement, expected_id=created, expected_image=original['Image'], operation=operation)
            if environment(replacement['Config']) != environment(original['Config']):
                raise ResizeError('replacement-private-environment-mismatch')
            if inspect(identity).get('State', {}).get('Running'):
                raise ResizeError('backup-postgres-started-unexpectedly')
            docker('start', created)
            wait_ready(created)
            after = sql_profile(created)
            expected = {'maxConnections': 80, 'sharedBuffersBytes': 128*1024**2,
                        'workMemBytes': 2*1024**2, 'maintenanceWorkMemBytes': 64*1024**2,
                        'maxWalSizeBytes': 2*1024**3,
                        'otherClientConnections': 0}
            if after != expected:
                raise ResizeError('replacement-readonly-postgres-verification-failed')
            validate(inspect(created), expected_id=created, expected_image=original['Image'], operation=operation)
            old = inspect(identity)
            validate(old, expected_id=identity, expected_image=original['Image'], expected_name=backup_name)
            if old['State'].get('Running') or old['HostConfig']['RestartPolicy']['Name'] != 'no':
                raise ResizeError('backup-container-safety-verification-failed')
            receipt = {'mode': 'applied', 'container': CONTAINER, 'containerId': created,
                       'backupContainer': backup_name, 'backupContainerId': identity,
                       'serverPrivateBackup': str(backup), 'postgres': after,
                       'memoryBytes': MEMORY, 'cpu': 2, 'volumeRetained': True,
                       'backupStopped': True, 'originalRestartPolicy': original['HostConfig']['RestartPolicy']}
            private_file(backup/'receipt.json', json.dumps(receipt, indent=2))
            return receipt
        except BaseException:
            if mutated:
                try:
                    rollback(original, backup_name, operation, created if created and re.fullmatch(r'[0-9a-f]{64}', created) else None)
                except BaseException:
                    raise ResizeError('resize-failed-and-automatic-rollback-needs-operator-recovery') from None
                raise ResizeError('resize-failed-original-container-restored') from None
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--maintenance-window', action='store_true')
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise ResizeError('resize-operation-interrupted')
    # SIGTERM/SIGHUP during a container change follow the same rollback path
    # as readiness or validation failures. SIGKILL cannot be handled; the
    # original backup's restart=no still prevents simultaneous volume use.
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        print(json.dumps(run(apply=args.apply, maintenance_window=args.maintenance_window), indent=2))
    except ResizeError as error:
        print(json.dumps({'error': str(error), 'modifiedByScriptOnly': CONTAINER}), file=sys.stderr)
        return 1
    except Exception as error:
        # OS/Docker diagnostics can contain environment entries; expose only
        # the exception class. Full private state is available on this server.
        print(json.dumps({'error': 'resize-unexpected-error', 'errorClass': type(error).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
